"""OSV recency and severity normalization."""

from datetime import UTC

from oss_radar.ingest.osv import _published, _severity_label


def test_published_normalizes_naive_timestamp_to_utc():
    published = _published({"published": "2026-07-01T12:00:00"})

    assert published is not None
    assert published.tzinfo == UTC


def test_severity_label_parses_cvss3_vector():
    vulnerability = {
        "database_specific": {"severity": None},
        "severity": [{
            "type": "CVSS_V3",
            "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
        }],
    }

    assert _severity_label(vulnerability) == "CRITICAL"


def test_severity_label_uses_the_worst_available_signal():
    vulnerability = {
        "database_specific": {"severity": "LOW"},
        "severity": [{"type": "CVSS_V3", "score": "8.1"}],
    }

    assert _severity_label(vulnerability) == "HIGH"


def test_severity_label_ignores_a_malformed_upstream_vector():
    vulnerability = {
        "database_specific": {"severity": None},
        "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/not-a-vector"}],
    }

    assert _severity_label(vulnerability) is None
