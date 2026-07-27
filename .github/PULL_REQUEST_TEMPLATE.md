## What changed

Describe the user-visible or operational outcome.

## Evidence

List the tests, screenshots, benchmark cohort, or deployment checks used to validate it. Model
claims must identify the evaluation lineage and must not use the governance test cohort for feature
selection.

## Checklist

- [ ] Tests and lint checks pass locally.
- [ ] Documentation and sample configuration match the behavior.
- [ ] No credentials, local state, generated databases, or model artifacts are included.
- [ ] Warehouse changes are additive or have an explicit migration plan.
- [ ] Workflow and infrastructure changes preserve least privilege and rollback behavior.
- [ ] Any model promotion claim uses comparable, leak-resistant evidence.
