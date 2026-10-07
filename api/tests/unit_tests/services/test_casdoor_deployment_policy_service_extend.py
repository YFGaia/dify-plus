"""Read-only temporary synthetic files; no actual accepted deployment artifact."""

import json
import os
from datetime import timedelta

import pytest
from core.casdoor.deployment_evidence import MAX_EVIDENCE_BYTES, DeploymentEvidenceError
from services.casdoor_deployment_policy_service_extend import (
    CasdoorDeploymentPolicyService,
)

from tests.unit_tests.core.casdoor.test_deployment_evidence import (
    NAMESPACE,
    NOW,
    REVISION,
    documents,
    synthetic_artifact,
)


def service_files(tmp_path, *, edition="COMMUNITY"):
    config, key, manifest = synthetic_artifact()
    authority, envelope = documents(key, manifest)
    authority_path = tmp_path / "synthetic-authority.json"
    evidence_path = tmp_path / "synthetic-evidence.json"
    authority_path.write_bytes(authority)
    evidence_path.write_bytes(envelope)
    service = CasdoorDeploymentPolicyService(
        authority_path=str(authority_path),
        evidence_path=str(evidence_path),
        deployment_edition=edition,
        now=lambda: NOW,
    )
    return config, service, authority_path, evidence_path


def resolve(service, config):
    return service.resolve(config, NAMESPACE, REVISION, config.config_digest(), "off")


def test_empty_settings_are_closed_without_filesystem_access(monkeypatch):
    def unexpected(*args):
        pytest.fail("default policy must not read files")

    monkeypatch.setattr(os, "open", unexpected)
    service = CasdoorDeploymentPolicyService()
    with pytest.raises(DeploymentEvidenceError, match="^deployment_proof_missing$"):
        service.resolve(None, NAMESPACE, REVISION, "a" * 64, "off")


def test_every_resolution_rechecks_current_operator_pin_key_and_expiry(tmp_path):
    config, service, authority_path, _ = service_files(tmp_path)
    previous = resolve(service, config)
    authority = json.loads(authority_path.read_bytes())
    authority["accepted_manifest_sha256"] = "b" * 64
    authority_path.write_text(json.dumps(authority))
    with pytest.raises(DeploymentEvidenceError, match="^deployment_proof_invalid$"):
        resolve(service, config)
    assert previous.expires_at > NOW  # An immutable object is no license to cache authority.
    authority_path.unlink()
    with pytest.raises(DeploymentEvidenceError, match="^deployment_proof_invalid$"):
        resolve(service, config)
    config, service, _, _ = service_files(tmp_path)
    service._now = lambda: NOW + timedelta(minutes=10)
    with pytest.raises(DeploymentEvidenceError, match="^deployment_proof_invalid$"):
        resolve(service, config)


@pytest.mark.parametrize("edition", ["ENTERPRISE", "CLOUD"])
def test_rbac_off_cannot_open_other_deployment_editions(tmp_path, edition):
    config, service, _, _ = service_files(tmp_path, edition=edition)
    with pytest.raises(DeploymentEvidenceError, match="^deployment_proof_invalid$"):
        resolve(service, config)


def test_bounds_apply_to_actual_read_even_if_file_stat_is_small(tmp_path, monkeypatch):
    config, service, _, evidence_path = service_files(tmp_path)
    evidence_path.write_bytes(b"x" * (MAX_EVIDENCE_BYTES + 1))
    original = os.fstat

    def small_stat(descriptor):
        current = original(descriptor)
        # Only st_mode is consumed, not the attacker-controlled stale st_size.
        return os.stat_result((current.st_mode, 0, 0, 0, 0, 0, 1, 0, 0, 0))

    monkeypatch.setattr(os, "fstat", small_stat)
    with pytest.raises(DeploymentEvidenceError, match="^deployment_proof_invalid$"):
        resolve(service, config)


def test_nonregular_files_and_relative_paths_fail_safely(tmp_path):
    fifo = tmp_path / "synthetic-evidence-fifo"
    os.mkfifo(fifo)
    with pytest.raises(DeploymentEvidenceError, match="^deployment_proof_invalid$"):
        CasdoorDeploymentPolicyService._read(str(fifo))
    with pytest.raises(DeploymentEvidenceError, match="^deployment_proof_invalid$"):
        CasdoorDeploymentPolicyService._read(str(tmp_path))
    with pytest.raises(DeploymentEvidenceError, match="^deployment_proof_invalid$"):
        CasdoorDeploymentPolicyService._read("relative.json")


def test_safe_exception_hides_io_paths_and_input_canary(tmp_path, monkeypatch):
    config, service, _, _ = service_files(tmp_path)

    def unavailable(path):
        raise OSError("synthetic-private-canary:" + path)

    monkeypatch.setattr(service, "_read", unavailable)
    with pytest.raises(DeploymentEvidenceError, match="^deployment_proof_invalid$") as error:
        resolve(service, config)
    assert "synthetic-private-canary" not in str(error.value)
    assert str(tmp_path) not in repr(error.value)
    assert error.value.__cause__ is None
