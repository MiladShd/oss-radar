"""Shared operator guidance for source-health surfaces."""

LOCAL_GITHUB_RECOVERY_GUIDANCE = (
    'Run `export OSS_RADAR_GITHUB_TOKEN="$(gh auth token)"` before rerunning '
    "to improve GitHub API rate limits."
)
CLOUD_GITHUB_RECOVERY_GUIDANCE = (
    "Validate the enabled `oss-radar-github-token` Secret Manager version, then rerun "
    "the pipeline using the recovery procedure in `docs/OPERATIONS.md`."
)


def github_recovery_guidance(*, is_cloud: bool) -> str:
    return (
        CLOUD_GITHUB_RECOVERY_GUIDANCE
        if is_cloud
        else LOCAL_GITHUB_RECOVERY_GUIDANCE
    )
