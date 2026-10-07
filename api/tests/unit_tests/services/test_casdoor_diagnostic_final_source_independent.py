"""Independent final-writer source-session regression tests."""

import pytest
from models.casdoor_extend import CasdoorValidationKind, CasdoorValidationStatus
from repositories.casdoor_validation_repository_extend import CasdoorValidationRepository
from test_casdoor_diagnostic_flow_extend import assert_source_unchanged, send
from test_casdoor_diagnostic_flow_extend import begin, complete, counts, rows

pytest_plugins = ("test_casdoor_diagnostic_flow_extend",)


@pytest.mark.parametrize("failure", ["revoked", "redis_read_failed"])
def test_final_writer_rechecks_original_refresh_after_initial_source_guard(diagnostic, monkeypatch, failure):
    """A refresh change after the pre-write guard must not create passed rows."""
    d = diagnostic
    _, state = begin(d)
    before = counts(d.f)
    service = d.services.casdoor_diagnostic
    original_write = service._write
    original_get = d.f.redis.get
    armed = False
    refresh_key = "refresh_token:" + d.token
    refresh_reads = 0

    def source_get(name):
        nonlocal refresh_reads
        if armed and name == refresh_key:
            refresh_reads += 1
            if refresh_reads >= 2 and failure == "redis_read_failed":
                raise OSError("synthetic private Redis read failure")
            value = original_get(name)
            if refresh_reads == 1 and failure == "revoked":
                d.source.clear()
            return value
        return original_get(name)

    def write(*args, **kwargs):
        nonlocal armed
        # Arm only after the entire provider/claims/directory chain succeeded;
        # the first read here belongs to _write's original source guard.
        armed = True
        return original_write(*args, **kwargs)

    monkeypatch.setattr(d.f.redis, "get", source_get)
    monkeypatch.setattr(service, "_write", write)
    response = complete(d, state)

    assert refresh_reads >= 2
    assert response.status_code != 302
    assert counts(d.f) == before
    assert all(
        row.status is not CasdoorValidationStatus.PASSED
        for row in rows(d)
        if row.kind in (CasdoorValidationKind.PROTOCOL, CasdoorValidationKind.DIAGNOSTIC)
    )
    assert_source_unchanged(d)


@pytest.mark.parametrize("failure", ["revoked", "redis_read_failed"])
def test_missing_policy_writer_rechecks_source_before_unknown_rows_commit(diagnostic, monkeypatch, failure):
    """A blocked policy result may record UNKNOWN only while its request source remains valid."""
    d = diagnostic
    d.production.authority.unlink()
    original_record = CasdoorValidationRepository.record
    original_get = d.f.redis.get
    triggered = False
    before = counts(d.f)

    def record(writer, binding, **kwargs):
        nonlocal triggered
        result = original_record(writer, binding, **kwargs)
        if kwargs["status"] is CasdoorValidationStatus.UNKNOWN:
            triggered = True
            if failure == "revoked":
                d.source.clear()
        return result

    def source_get(name):
        if triggered and d.f.opened and failure == "redis_read_failed":
            raise OSError("synthetic private Redis read failure")
        return original_get(name)

    monkeypatch.setattr(CasdoorValidationRepository, "record", record)
    monkeypatch.setattr(d.f.redis, "get", source_get)
    snapshot = d.services.casdoor_configuration.get(d.actor)
    response = send(
        d,
        "/console/api/system-manage-extend/integration/casdoor/test-login",
        method="POST",
        json={"etag": snapshot.etag, "revision_id": str(snapshot.draft_revision_id)},
    )

    assert response.status_code in (400, 500)
    assert triggered
    assert counts(d.f) == before
    assert not rows(d)
    assert_source_unchanged(d)
