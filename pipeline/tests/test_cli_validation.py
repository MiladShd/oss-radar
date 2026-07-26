"""Validation-upload durability contracts."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from oss_radar import cli


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        artifact_bucket="managed-artifacts",
        gcp_project="example-project",
    )


def test_validation_upload_uses_existing_managed_bucket(monkeypatch, tmp_path):
    from google.cloud import storage

    artifact = tmp_path / "validation_results.json"
    artifact.write_text("{}")
    bucket = MagicMock()
    client = MagicMock()
    client.bucket.return_value = bucket
    monkeypatch.setattr(storage, "Client", MagicMock(return_value=client))

    cli._upload_validation(_settings(), tmp_path, [artifact.name])

    client.bucket.assert_called_once_with("managed-artifacts")
    client.create_bucket.assert_not_called()
    bucket.exists.assert_not_called()
    bucket.blob.assert_called_once_with("validation/validation_results.json")
    bucket.blob.return_value.upload_from_filename.assert_called_once_with(str(artifact))


def test_validation_upload_failure_is_not_reported_as_success(monkeypatch, tmp_path):
    from google.cloud import storage

    artifact = tmp_path / "validation_results.json"
    artifact.write_text("{}")
    bucket = MagicMock()
    bucket.blob.return_value.upload_from_filename.side_effect = RuntimeError("upload denied")
    client = MagicMock()
    client.bucket.return_value = bucket
    monkeypatch.setattr(storage, "Client", MagicMock(return_value=client))

    with pytest.raises(RuntimeError, match="upload denied"):
        cli._upload_validation(_settings(), tmp_path, [artifact.name])


def test_validation_run_requires_every_reproducibility_artifact(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.subprocess, "run", MagicMock())
    monkeypatch.setattr(cli, "_check_wolfram_staleness", MagicMock())

    with pytest.raises(RuntimeError, match="did not produce required artifacts"):
        cli._run_validation(_settings(), str(tmp_path), upload=False, staleness_hours=36)
