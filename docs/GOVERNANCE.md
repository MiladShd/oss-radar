# Repository governance

OSS Radar is maintained by one person but governed like a team repo — every change to `main` goes
through a pull request, CI, and an automated review trail. This doc explains the setup so it can be
copied as a boilerplate.

## Branch protection (GitHub ruleset: "main branch protection")

The ruleset targets **`~DEFAULT_BRANCH` (`main`) only** — feature branches are unrestricted so the PR
workflow actually works, while everything that lands on `main` is gated. Once configured with the helper below,
the only bypass actor is the repository's GitHub Actions app, limited to **pull-request bypass mode**. That exception lets the
allowlisted auto-triage workflow complete an already-green bot PR; it does not grant direct-push bypass.

| Rule | Why |
|---|---|
| **Require a pull request** (0 required approvals) | Every change to `main` is a reviewable, CI-gated PR. Zero approvals because a solo maintainer can't approve their own PR — requiring ≥1 would deadlock merges. Add reviewers and raise this the moment a second maintainer joins. |
| **Require status checks — `test`** | The CI job (ruff + pytest) must pass before merge. The quality gate, enforced. |
| **Require linear history** | No merge commits on `main`; PRs land via **squash or rebase** only (`merge` method is disabled, which would otherwise contradict this rule). History stays bisectable. |
| **Require signed commits** | Every commit on `main` has a verified signature (SSH signing). Provenance, not vibes. |
| **Block force-pushes** (non-fast-forward) | `main` history is append-only — no silent rewrites. |
| **Block deletion** | `main` can't be deleted. |

### GitHub Actions pull-request bypass

Daily model-improvement commits are created by automation and are not signed with the maintainer's SSH
key. GitHub's squash merge creates the final commit on `main`, but a required-signatures rule can still
prevent the app from completing the PR. Ruleset `17938598` should therefore grant GitHub Actions (integration
ID `15368`) a narrowly scoped `pull_request` bypass. Required signed commits, required `test` status,
linear history, deletion protection, and force-push protection all remain present in the ruleset.

Use the checked-in helper to audit or configure that exception:

```bash
# Safe default: GET the live ruleset, save a local JSON snapshot, and print a diff.
./scripts/configure_github_rules.sh

# Explicitly apply the displayed bypass_actors-only update.
./scripts/configure_github_rules.sh --apply
```

The helper refuses to proceed if the ruleset is inactive, if `required_signatures` is absent, or if the
proposed payload changes conditions or rules. Before an apply it stores the full pre-change response under
`${TMPDIR:-/tmp}/oss-radar-ruleset-backups`; pass `--backup-dir PATH` to keep the audit copy elsewhere.
`--apply` is an administrative repository change: review the diff and snapshot path before using it.

The bypass is necessary but not sufficient by itself. `scripts/auto_triage.py` independently requires an exact
title/branch/date match, repository-owner authorship, the expected labels, a single allowlisted file, a
non-fork branch, and successful `test`, `preview`, and `analyze` checks. It scans up to 100 open PRs and lands
daily reports oldest-first, so a backlog drains deterministically instead of remaining one blocked PR per day.
Feature proposals have an additional exact-JSON-diff and minimum-lift check.

The same workflow consolidates repeated drift incidents only when they have the exact automation title,
`oss-radar` + `model-drift` labels, and repository-owner authorship. It preserves the oldest issue as the
canonical audit thread, links every duplicate, and leaves all human-authored or non-matching issues untouched.
See [OPERATIONS.md](OPERATIONS.md) for the one-time activation and cleanup sequence.

## Commit signing

Commits are signed with SSH (`gpg.format=ssh`), and the public key is registered on GitHub as a
**Signing Key**, so commits show as **Verified**. Setup:

```bash
git config gpg.format ssh
git config user.signingkey ~/.ssh/id_ed25519.pub
git config commit.gpgsign true
# then add ~/.ssh/id_ed25519.pub to GitHub → Settings → SSH and GPG keys → New → "Signing Key"
```

## Security scanning

- **CodeQL** (`.github/workflows/codeql.yml`) runs on every PR, on pushes to `main`, and weekly,
  with the `security-and-quality` query suite. Alerts surface in the Security tab. It runs but does
  **not** hard-gate merges yet — promote it into the ruleset's required checks once there's a clean
  baseline, so a pre-existing finding can't block unrelated work.
- **Dependency self-audit** — the daily pipeline audits OSS Radar's *own* pinned dependencies
  (version-aware OSV) and stores the result; see `oss-radar audit` and `docs/ARCHITECTURE_GUIDE.md`.

## Workflow supply-chain and deployment identity

Every third-party `uses:` reference is pinned to an exact commit SHA; release behavior does not float with an
upstream action tag. GCP Workload Identity Federation checks repository name, immutable repository/owner ids,
`refs/heads/main`, and the exact
`MiladShd/oss-radar/.github/workflows/deploy.yml@refs/heads/main` `workflow_ref`. A different workflow in the same
repository therefore cannot reuse the deploy identity merely because it runs on `main`.

## The workflow, end to end

1. Branch off `main`, commit (auto-signed).
2. Open a PR → CI (`test`) + CodeQL run.
3. CI green → squash-merge → `main` stays linear and green; maintainer commits remain signed, while exact bot PRs
   use the documented pull-request-only integration bypass.

## What I'd add for a team

Raise required approvals to ≥1, add a `CODEOWNERS` file and enable code-owner review, turn on
`require_last_push_approval`, and promote CodeQL to a required status check.
