# Repository governance

OSS Radar is maintained by one person but governed like a team repo — every change to `main` goes
through a pull request, CI, and an automated review trail. This doc explains the setup so it can be
copied as a boilerplate.

## Branch protection (GitHub ruleset: "main branch protection")

The ruleset targets **`~DEFAULT_BRANCH` (`main`) only** — feature branches are unrestricted so the PR
workflow actually works, while everything that lands on `main` is gated. Nothing is "bypassed"; the
rules are scoped to the branch that matters.

| Rule | Why |
|---|---|
| **Require a pull request** (0 required approvals) | Every change to `main` is a reviewable, CI-gated PR. Zero approvals because a solo maintainer can't approve their own PR — requiring ≥1 would deadlock merges. Add reviewers and raise this the moment a second maintainer joins. |
| **Require status checks — `test`** | The CI job (ruff + pytest) must pass before merge. The quality gate, enforced. |
| **Require linear history** | No merge commits on `main`; PRs land via **squash or rebase** only (`merge` method is disabled, which would otherwise contradict this rule). History stays bisectable. |
| **Require signed commits** | Every commit on `main` has a verified signature (SSH signing). Provenance, not vibes. |
| **Block force-pushes** (non-fast-forward) | `main` history is append-only — no silent rewrites. |
| **Block deletion** | `main` can't be deleted. |

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

## The workflow, end to end

1. Branch off `main`, commit (auto-signed).
2. Open a PR → CI (`test`) + CodeQL run.
3. CI green → squash-merge → `main` stays linear, signed, and green.

## What I'd add for a team

Raise required approvals to ≥1, add a `CODEOWNERS` file and enable code-owner review, turn on
`require_last_push_approval`, and promote CodeQL to a required status check.
