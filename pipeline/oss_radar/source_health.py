"""Shared operator guidance for source-health surfaces."""

LOCAL_GITHUB_RECOVERY_GUIDANCE = (
    "Inspect connector warnings for a renamed repository, provider error, or rate limit. "
    'If logs show authentication/rate-limit errors, run `export '
    'OSS_RADAR_GITHUB_TOKEN="$(gh auth token)"`; then rerun.'
)
CLOUD_GITHUB_RECOVERY_GUIDANCE = (
    "Inspect connector warnings for a renamed repository, provider error, or rate limit. "
    "Validate the enabled `oss-radar-github-token` Secret Manager "
    "version only when logs show authentication/rate-limit errors, then rerun using "
    "`docs/OPERATIONS.md`."
)


def github_recovery_guidance(*, is_cloud: bool) -> str:
    return (
        CLOUD_GITHUB_RECOVERY_GUIDANCE
        if is_cloud
        else LOCAL_GITHUB_RECOVERY_GUIDANCE
    )
