#!/usr/bin/env bash
# Safely add the GitHub Actions app as a pull-request-only ruleset bypass actor
# and bind every required CI status check to that same Integration.
#
# The script is a GitHub API dry run by default: it reads the live ruleset,
# writes a local snapshot, validates the required-signatures invariant, and
# prints the proposed bypass_actors diff. Pass --apply to send the PUT request.
set -euo pipefail

readonly DEFAULT_REPOSITORY="MiladShd/oss-radar"
readonly DEFAULT_RULESET_ID="17938598"
readonly DEFAULT_APP_ID="15368"
readonly REQUIRED_CHECKS_JSON='["analyze","preview","test"]'

repository="${OSS_RADAR_GITHUB_REPOSITORY:-$DEFAULT_REPOSITORY}"
ruleset_id="${OSS_RADAR_RULESET_ID:-$DEFAULT_RULESET_ID}"
app_id="${OSS_RADAR_GITHUB_ACTIONS_APP_ID:-$DEFAULT_APP_ID}"
temp_root="${TMPDIR:-/tmp}"
temp_root="${temp_root%/}"
backup_dir="${OSS_RADAR_RULESET_BACKUP_DIR:-${temp_root}/oss-radar-ruleset-backups}"
apply=false

usage() {
  cat <<'EOF'
Usage: scripts/configure_github_rules.sh [options]

Safely configure the active main-branch ruleset. By default this makes only a
GET request to GitHub, saves the response locally, and displays the proposed
change. It never updates GitHub unless --apply is supplied explicitly.

Options:
  --apply                 Update the GitHub ruleset after all checks pass.
  --repo OWNER/REPO       Repository (default: MiladShd/oss-radar).
  --ruleset-id ID         Repository ruleset ID (default: 17938598).
  --app-id ID             GitHub Actions integration ID used for bypass and checks
                          (default: 15368).
  --backup-dir DIRECTORY  Directory for pre-change JSON snapshots.
  -h, --help              Show this help.

Environment equivalents:
  OSS_RADAR_GITHUB_REPOSITORY, OSS_RADAR_RULESET_ID,
  OSS_RADAR_GITHUB_ACTIONS_APP_ID, OSS_RADAR_RULESET_BACKUP_DIR
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --apply)
      apply=true
      shift
      ;;
    --repo)
      (($# >= 2)) || die "--repo requires OWNER/REPO"
      repository="$2"
      shift 2
      ;;
    --ruleset-id)
      (($# >= 2)) || die "--ruleset-id requires a numeric ID"
      ruleset_id="$2"
      shift 2
      ;;
    --app-id)
      (($# >= 2)) || die "--app-id requires a numeric ID"
      app_id="$2"
      shift 2
      ;;
    --backup-dir)
      (($# >= 2)) || die "--backup-dir requires a directory"
      backup_dir="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1 (try --help)"
      ;;
  esac
done

[[ "$repository" =~ ^[^/[:space:]]+/[^/[:space:]]+$ ]] \
  || die "repository must be in OWNER/REPO form"
[[ "$ruleset_id" =~ ^[0-9]+$ ]] || die "ruleset ID must be numeric"
[[ "$app_id" =~ ^[0-9]+$ ]] || die "app ID must be numeric"

for command_name in gh jq diff mktemp; do
  command -v "$command_name" >/dev/null 2>&1 || die "required command not found: $command_name"
done

umask 077
mkdir -p "$backup_dir"
work_dir="$(mktemp -d "${temp_root}/oss-radar-ruleset.XXXXXX")"
trap 'rm -rf "$work_dir"' EXIT

current_json="$work_dir/current.json"
proposed_json="$work_dir/proposed.json"
response_json="$work_dir/response.json"
latest_json="$work_dir/latest.json"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
safe_repository="${repository//\//-}"
snapshot_path="${backup_dir%/}/${safe_repository}-ruleset-${ruleset_id}-${timestamp}-$$.json"
endpoint="repos/${repository}/rulesets/${ruleset_id}"

printf 'Reading GitHub ruleset %s/%s...\n' "$repository" "$ruleset_id"
gh api --method GET "$endpoint" >"$current_json"
jq -e . "$current_json" >/dev/null || die "GitHub returned invalid JSON"
cp "$current_json" "$snapshot_path"
printf 'Saved pre-change snapshot: %s\n' "$snapshot_path"

jq -e --argjson expected_id "$ruleset_id" '.id == $expected_id' "$current_json" >/dev/null \
  || die "response ruleset ID does not match the requested ID"
jq -e '.enforcement == "active"' "$current_json" >/dev/null \
  || die "refusing to update a ruleset that is not active"
jq -e '
  .target == "branch"
  and (.conditions.ref_name.include | index("~DEFAULT_BRANCH") != null)
' "$current_json" >/dev/null \
  || die "refusing to update a ruleset that does not target the default branch"
jq -e '.rules | any(.type == "required_signatures")' "$current_json" >/dev/null \
  || die "required_signatures is absent; refusing to update a weakened ruleset"

# PUT accepts only the ruleset update fields. Preserve each one except for the two intentional
# changes: narrow this Integration actor to pull-request bypass and require the complete CI set,
# with every required check bound to that verified GitHub Actions Integration.
jq --argjson app_id "$app_id" --argjson required_checks "$REQUIRED_CHECKS_JSON" '
  {
    name,
    target,
    enforcement,
    bypass_actors: (
      [.bypass_actors[]?
        | select((.actor_type == "Integration" and .actor_id == $app_id) | not)]
      + [{
          actor_id: $app_id,
          actor_type: "Integration",
          bypass_mode: "pull_request"
        }]
    ),
    conditions,
    rules: [
      .rules[]
      | if .type == "required_status_checks" then
          .parameters.required_status_checks = (
            $required_checks | map({
              context: .,
              integration_id: $app_id
            })
          )
          | .parameters.strict_required_status_checks_policy = true
        else
          .
        end
    ]
  }
' "$current_json" >"$proposed_json"

# These assertions make accidental rule weakening a hard failure before PUT.
jq -e '.rules | any(.type == "required_signatures")' "$proposed_json" >/dev/null \
  || die "proposed payload removed required_signatures"
jq -e --argjson app_id "$app_id" --argjson required_checks "$REQUIRED_CHECKS_JSON" '
  ($required_checks
    | map({context: ., integration_id: $app_id})
    | sort_by(.context)) as $expected
  | ([.rules[] | select(.type == "required_status_checks")] | length) == 1
  and (
    [
      .rules[]
      | select(.type == "required_status_checks")
      | .parameters.required_status_checks[]
      | {context, integration_id}
    ] | sort_by(.context)
  ) == $expected
' "$proposed_json" >/dev/null \
  || die "proposed payload does not require the exact app-bound CI check set"
jq -e --slurp '
  .[0] as $before | .[1] as $after
  | ($before.name == $after.name)
    and ($before.target == $after.target)
    and ($before.enforcement == $after.enforcement)
    and ($before.conditions == $after.conditions)
    and (
      [$before.rules[] | select(.type != "required_status_checks")]
      ==
      [$after.rules[] | select(.type != "required_status_checks")]
    )
' "$current_json" "$proposed_json" >/dev/null \
  || die "proposed payload changes fields outside bypass/status-check policy"

printf '\nProposed bypass_actors change:\n'
diff -u \
  <(jq --sort-keys '.bypass_actors' "$current_json") \
  <(jq --sort-keys '.bypass_actors' "$proposed_json") || true
printf '\nProposed required-status-check change:\n'
diff -u \
  <(jq --sort-keys '.rules[] | select(.type == "required_status_checks")' "$current_json") \
  <(jq --sort-keys '.rules[] | select(.type == "required_status_checks")' "$proposed_json") || true

if jq -e --argjson app_id "$app_id" --argjson required_checks "$REQUIRED_CHECKS_JSON" '
  ($required_checks
    | map({context: ., integration_id: $app_id})
    | sort_by(.context)) as $expected
  | (
    ([.bypass_actors[]?
        | select(.actor_type == "Integration" and .actor_id == $app_id)]
      | length == 1)
    and
    ([.bypass_actors[]?
        | select(
            .actor_type == "Integration"
            and .actor_id == $app_id
            and .bypass_mode == "pull_request"
          )]
      | length == 1)
    and
    ([.rules[] | select(.type == "required_status_checks")] | length == 1)
    and
    (
      [
        .rules[]
        | select(.type == "required_status_checks")
        | .parameters.required_status_checks[]
        | {context, integration_id}
      ] | sort_by(.context)
    ) == $expected
    and
    (
      .rules[]
      | select(.type == "required_status_checks")
      | .parameters.strict_required_status_checks_policy == true
    )
  )
' "$current_json" >/dev/null; then
  printf '\nNo update needed: bypass and app-bound required checks already match policy.\n'
  exit 0
fi

if [[ "$apply" != true ]]; then
  printf '\nDRY RUN: GitHub was not changed. Re-run with --apply to send this exact update.\n'
  exit 0
fi

printf '\nApplying pull-request-only bypass for GitHub Actions app %s...\n' "$app_id"
# Re-read immediately before the full PUT. Abort rather than overwriting a concurrent admin edit
# made after the reviewed snapshot/proposed diff was produced.
gh api --method GET "$endpoint" >"$latest_json"
jq -e --slurp '.[0] == .[1]' "$current_json" "$latest_json" >/dev/null \
  || die "ruleset changed after review; no update sent (rerun to inspect the new state)"
gh api --method PUT "$endpoint" --input "$proposed_json" >"$response_json"

jq -e --argjson app_id "$app_id" --argjson required_checks "$REQUIRED_CHECKS_JSON" '
  ($required_checks
    | map({context: ., integration_id: $app_id})
    | sort_by(.context)) as $expected
  | (
    (.rules | any(.type == "required_signatures"))
    and (
      [.bypass_actors[]?
        | select(.actor_type == "Integration" and .actor_id == $app_id)]
      | length == 1
    )
    and (
      [.bypass_actors[]?
        | select(
            .actor_type == "Integration"
            and .actor_id == $app_id
            and .bypass_mode == "pull_request"
          )]
      | length == 1
    )
    and
    ([.rules[] | select(.type == "required_status_checks")] | length == 1)
    and
    (
      [
        .rules[]
        | select(.type == "required_status_checks")
        | .parameters.required_status_checks[]
        | {context, integration_id}
      ] | sort_by(.context)
    ) == $expected
    and
    (
      .rules[]
      | select(.type == "required_status_checks")
      | .parameters.strict_required_status_checks_policy == true
    )
  )
' "$response_json" >/dev/null \
  || die "GitHub response did not preserve the required invariant; inspect $snapshot_path"
jq -e --slurp '
  .[0] as $before | .[1] as $after
  | ($before.name == $after.name)
    and ($before.target == $after.target)
    and ($before.enforcement == $after.enforcement)
    and ($before.conditions == $after.conditions)
    and (
      [$before.rules[] | select(.type != "required_status_checks")]
      ==
      [$after.rules[] | select(.type != "required_status_checks")]
    )
' "$current_json" "$response_json" >/dev/null \
  || die "GitHub response changed fields outside bypass/status-check policy; inspect $snapshot_path"

printf 'Ruleset updated and verified. Signed commits and all three app-bound CI checks remain required.\n'
printf 'Rollback source: %s\n' "$snapshot_path"
