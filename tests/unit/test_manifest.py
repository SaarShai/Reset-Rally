"""Unit tests for rally manifest loading, validation, and secrecy filtering.

Covers specifications M-01, M-02, and M-03.
"""

from pathlib import Path
import pytest

from rally.checks import ManifestError, load_manifest, public_manifest

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "rally"


def test_m01_load_three_fixture_manifests():
    """Verify loading the three fixture manifests and checking tracks and modes (M-01)."""
    manifest_a = load_manifest(FIXTURES_DIR / "a_math_physics.json")
    manifest_b = load_manifest(FIXTURES_DIR / "b_code_opt.json")
    manifest_c = load_manifest(FIXTURES_DIR / "c_corpus.json")

    assert manifest_a["track"] == "relay"
    assert manifest_a["verification"]["mode"] == "deterministic"

    assert manifest_b["track"] == "relay"
    assert manifest_b["verification"]["mode"] == "holdout"

    assert manifest_c["track"] == "brain"
    assert manifest_c["verification"]["mode"] == "quorum"


def test_m02_manifest_validation_errors():
    """Verify ManifestError is raised with offending field name on invalid manifest (M-02)."""
    # Missing or empty id
    with pytest.raises(ManifestError) as exc_info:
        load_manifest({"id": "", "track": "relay", "verification": {"mode": "deterministic"}})
    assert "id" in str(exc_info.value).lower()

    # Unknown track
    with pytest.raises(ManifestError) as exc_info:
        load_manifest({"id": "p1", "track": "invalid_track", "verification": {"mode": "deterministic"}})
    assert "track" in str(exc_info.value).lower()

    # Missing verification block
    with pytest.raises(ManifestError) as exc_info:
        load_manifest({"id": "p2", "track": "brain"})
    assert "verification" in str(exc_info.value).lower()

    # Unknown verification mode
    with pytest.raises(ManifestError) as exc_info:
        load_manifest({"id": "p3", "track": "brain", "verification": {"mode": "unknown_mode"}})
    assert "mode" in str(exc_info.value).lower()

    # Quorum with k < 2
    with pytest.raises(ManifestError) as exc_info:
        load_manifest({"id": "p4", "track": "brain", "verification": {"mode": "quorum", "quorum_k": 1}})
    assert "quorum" in str(exc_info.value).lower()

    # Canary on with no Canary items
    with pytest.raises(ManifestError) as exc_info:
        load_manifest({
            "id": "p5",
            "track": "brain",
            "verification": {"mode": "quorum", "quorum_k": 2},
            "canary": {"rate": 0.05, "enabled": True},
            "canaries": [],
        })
    assert "canar" in str(exc_info.value).lower()


def test_m03_public_manifest_strips_secrets():
    """Verify public_manifest strips holdout and canary secrets (M-03)."""
    manifest_b = load_manifest(FIXTURES_DIR / "b_code_opt.json")
    assert "holdout" in manifest_b

    public_b = public_manifest(manifest_b)
    assert "holdout" not in public_b
    assert "canaries" not in public_b
    # Original manifest remains intact
    assert "holdout" in manifest_b

    manifest_c = load_manifest(FIXTURES_DIR / "c_corpus.json")
    assert "canaries" in manifest_c
    public_c = public_manifest(manifest_c)
    assert "canaries" not in public_c
    assert "holdout" not in public_c
