"""Actual original P3K/P3L/B3/D/F1, then bounded historical observations.

SQLite is sequential source evidence only. Current-state changes below exercise
the reader's historical boundary; they are not ordinary caller acceptance.
"""

import json
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.invited_finalization_receipt import RECEIPT_KIND as F_ACTION
from core.casdoor.invited_finalization_receipt import canonical
from core.casdoor.invited_write_receipt import RECEIPT_KIND as D_ACTION
from models.account import TenantAccountJoin as Join
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend as Issuance
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict, CasdoorLoginScopeRepository
from repositories.casdoor_terminal_local_invitation_repository_extend import (
    CasdoorTerminalLocalInvitationRepository,
    TerminalLocalInvitationObservation,
)

from tests.unit_tests.repositories.test_casdoor_invited_login_scope_repository_extend import configuration_factory
from tests.unit_tests.services.test_casdoor_invited_local_finalization_service_extend import all_state, finalize
from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import (
    invited_scope_case as original_case,
)
from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import persist
from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import (
    writer_case as original_writer,
)

invited_scope_case = original_case
writer_case = original_writer


def prepare(case):
    persist(case)
    finalize(case)  # Real original D/F owners, no synthetic terminal receipt.


def observe(case, *, context=None, key=None, account_id=None):
    context, key, account_id = context or case.context, key or case.key, account_id or case.attempt.account_id
    with case.db() as session, session.begin():
        scope = CasdoorLoginScopeRepository(session, configuration_factory)._project_scope(
            context, key, account_id, creation_email=None, extra_workspace_ids=()
        )
        return CasdoorTerminalLocalInvitationRepository(session, configuration_factory).observe(
            context, key, account_id, scope=scope
        )


def denied(case, **kwargs):
    before = all_state(case)
    with pytest.raises(CasdoorLoginScopeConflict, match="^authorization_pending$") as error:
        observe(case, **kwargs)
    assert error.value.__cause__ is None
    assert all_state(case) == before


@pytest.mark.parametrize("absent", [False, True])
@pytest.mark.parametrize("invite_target", [False, True])
def test_real_terminal_facts_frozen_select_only_no_authority(writer_case, absent, invite_target, monkeypatch):
    case = writer_case(absent=absent, invite_target=invite_target)
    prepare(case)
    before, statements = all_state(case), []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    sa.event.listen(case.engine, "before_cursor_execute", record)
    try:
        result = observe(case)
    finally:
        sa.event.remove(case.engine, "before_cursor_execute", record)
    assert type(result) is TerminalLocalInvitationObservation
    assert result.operation_id == case.pending.snapshot.operation_id
    assert result.current_generation == 1
    assert result.result_epoch == case.completed.facts.result_epoch
    assert all(s.lstrip().upper().startswith("SELECT") or s == "BEGIN" for s in statements)
    assert all_state(case) == before
    assert str(result.operation_id) not in repr(result)
    assert result.write_summary_sha256 not in repr(result)
    assert not any(hasattr(result, name) for name in ("guard", "session", "leases", "skip_ids", "excluded_ids"))
    assert "encrypted_secret" not in dict(result._historical_rows[2])
    with pytest.raises(FrozenInstanceError):
        result.current_generation = 9
    with pytest.raises((AttributeError, TypeError)):
        result.guard = object()


@pytest.mark.parametrize("absent", [False, True])
@pytest.mark.parametrize("changed", ["generation", "role", "remove", "withdrawn"])
def test_historical_closure_after_current_changes_uses_original_join_role(writer_case, absent, changed):
    case = writer_case(absent=absent)
    prepare(case)
    original = observe(case)
    with case.db() as session, session.begin():
        session.execute(sa.update(Identity).values(sync_generation=3))
        if changed == "role":
            session.execute(sa.update(Join).where(Join.id == case.completed.facts.join_id).values(role="editor"))
        elif changed == "remove":
            session.execute(sa.delete(Join).where(Join.id == case.completed.facts.join_id))
        if changed != "generation":
            session.execute(
                sa.update(Lifecycle)
                .where(Lifecycle.lifecycle_id == case.ids["lifecycle"])
                .values(
                    epoch=original.result_epoch + 2,
                    state="withdrawn" if changed in ("remove", "withdrawn") else "active",
                )
            )
    result = observe(case)
    assert result.current_generation == 3
    assert result.result_epoch == original.result_epoch
    assert result.write_summary_sha256 == original.write_summary_sha256
    assert result.finalization_summary_sha256 == original.finalization_summary_sha256


def test_terminal_after_seven_days_does_not_reopen_original_reconcile_window(writer_case, monkeypatch):
    import repositories.casdoor_terminal_local_invitation_repository_extend as owner

    case = writer_case(absent=True)
    prepare(case)
    now = owner._now()
    monkeypatch.setattr(owner, "_now", lambda: now + timedelta(days=8))
    assert observe(case).current_generation == 1


def advance_current_revision(case):
    with case.db() as session, session.begin():
        integration = session.get(Integration, case.ids["integration"])
        updated_configuration = type(case.configuration).model_validate(
            dict(case.configuration.model_dump(), button_text="New current login")
        )
        saved = configuration_factory(session).save_draft(
            updated_configuration, etag=integration.etag, actor_account_id=case.attempt.account_id
        )
        revision = session.get(Revision, str(saved.draft_revision_id))
        integration.active_revision_id = revision.id
        namespace = session.get(Namespace, case.ids["namespace"])
        namespace.fence_epoch += 2
        case.context = replace(
            case.context,
            revision_id=UUID(revision.id),
            active_revision_id=UUID(revision.id),
            config_digest=revision.config_digest,
            fence_epoch=namespace.fence_epoch,
        )


def test_old_revision_and_fence_with_current_same_namespace_revision(writer_case):
    case = writer_case(absent=True)
    prepare(case)
    old = observe(case)
    advance_current_revision(case)
    result = observe(case)
    assert result.write_summary_sha256 == old.write_summary_sha256
    assert result.finalization_summary_sha256 == old.finalization_summary_sha256


@pytest.mark.parametrize("action", [D_ACTION, F_ACTION])
@pytest.mark.parametrize(
    "kind", ["missing", "duplicate", "actor", "result", "reference", "before", "oversized", "noncanonical", "crosshash"]
)
def test_corrupt_missing_duplicate_noncanonical_or_unlinked_audit_pending(writer_case, action, kind):
    case = writer_case()
    prepare(case)
    with case.db() as session, session.begin():
        row = session.execute(sa.select(*Audit.__table__.columns).where(Audit.action == action)).one()
        values = {}
        if kind == "missing":
            session.execute(sa.delete(Audit).where(Audit.id == row.id))
        elif kind == "duplicate":
            session.execute(sa.insert(Audit).values(dict(row._mapping, id=str(uuid4()))))
        elif kind == "actor":
            values["actor_account_id"] = case.ids["account"]
        elif kind == "result":
            values["result_code"] = "unknown"
        elif kind == "reference":
            values["revision_id"] = str(uuid4())
        elif kind == "before":
            values["created_at"] = row.created_at - timedelta(days=1)
        elif kind == "oversized":
            values["summary_json"] = " " * 32769
        elif kind == "noncanonical":
            values["summary_json"] = row.summary_json + " "
        elif kind == "crosshash":
            data = json.loads(row.summary_json)
            data["scope_digest" if action == D_ACTION else "before_postwrite_sha256"] = "0" * 64
            values["summary_json"] = canonical(data)
        if values:
            session.execute(sa.update(Audit).where(Audit.id == row.id).values(**values))
    denied(case)


@pytest.mark.parametrize(
    "kind",
    [
        "generation",
        "epoch",
        "identity",
        "proof",
        "desired",
        "receipt",
        "payload",
        "pending",
        "inflight",
        "extra",
        "mixed",
        "old-revision",
        "old-fence",
        "desired-duplicate",
    ],
)
def test_nonterminal_extra_or_corrupt_owner_chain_pending(writer_case, kind):
    case = writer_case(absent=True)
    prepare(case)
    if kind == "old-revision":
        advance_current_revision(case)
    with case.db() as session, session.begin():
        operation = session.execute(
            sa.select(*Intent.__table__.columns).where(Intent.kind == "invitation_finalize")
        ).one()
        if kind == "generation":
            session.execute(sa.update(Identity).values(sync_generation=0))
        elif kind == "epoch":
            session.execute(sa.update(Lifecycle).where(Lifecycle.lifecycle_id == case.ids["lifecycle"]).values(epoch=1))
        elif kind == "identity":
            session.execute(sa.update(Identity).values(subject="different", subject_digest="0" * 64))
        elif kind == "proof":
            session.execute(
                sa.update(Intent)
                .where(Intent.id == operation.id)
                .values(proof_ref=operation.proof_ref[:-1] + ("0" if operation.proof_ref[-1] != "0" else "1"))
            )
        elif kind == "desired":
            session.execute(sa.update(Intent).where(Intent.id == operation.id).values(desired_json=" " * 16385))
        elif kind == "receipt":
            session.execute(sa.update(Issuance).values(consumption_receipt_json="{}"))
        elif kind == "payload":
            session.execute(sa.update(Issuance).values(payload_digest="0" * 64))
        elif kind == "pending":
            session.execute(sa.update(Intent).where(Intent.id == operation.id).values(operation_state="pending"))
        elif kind == "inflight":
            session.execute(
                sa.update(Intent).where(Intent.id == operation.id).values(attempt_id=str(uuid4()), attempt_count=1)
            )
        elif kind in ("extra", "mixed"):
            session.execute(
                sa.insert(Intent).values(
                    dict(
                        operation._mapping,
                        id=str(uuid4()),
                        idempotency_key="f" * 64,
                        kind="role_replace" if kind == "mixed" else operation.kind,
                    )
                )
            )
        elif kind == "old-revision":
            session.execute(
                sa.update(Revision).where(Revision.id == case.ids["revision"]).values(config_digest="0" * 64)
            )
        elif kind == "old-fence":
            session.execute(sa.update(Intent).where(Intent.id == operation.id).values(fence_epoch=1))
        elif kind == "desired-duplicate":
            raw = operation.desired_json[:-1] + ',"generation":0}'
            session.execute(sa.update(Intent).where(Intent.id == operation.id).values(desired_json=raw))
    denied(case)


def test_zero_intents_no_terminal_or_audit_read(writer_case, monkeypatch):
    case = writer_case()
    prepare(case)
    with case.db() as session, session.begin():
        session.execute(sa.delete(Intent))
        session.execute(sa.delete(Audit))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("zero intents reached historical row reader")

    monkeypatch.setattr(CasdoorTerminalLocalInvitationRepository, "_rows", forbidden)
    assert observe(case) is None


@pytest.mark.parametrize("column", ["mappings_json", "encrypted_secret"])
def test_historical_revision_growth_between_bound_and_original_owner_is_denied_before_get(
    writer_case, monkeypatch, column
):
    case = writer_case()
    prepare(case)
    advance_current_revision(case)
    before, calls = all_state(case), []
    original = CasdoorTerminalLocalInvitationRepository._bounded_orm

    def grow(owner, model, row):
        if model is Revision:
            owner.session.execute(sa.update(Revision).where(Revision.id == row.id).values({column: "x" * 16385}))
            calls.append(True)
        return original(owner, model, row)

    monkeypatch.setattr(CasdoorTerminalLocalInvitationRepository, "_bounded_orm", grow)
    denied(case)
    assert calls == [True]
    assert all_state(case) == before


def test_current_evidence_drift_during_first_last_read_fails_with_whole_rollback(writer_case, monkeypatch):
    case = writer_case()
    prepare(case)
    before, reads = all_state(case), []
    original = CasdoorTerminalLocalInvitationRepository._rows

    def drift(owner, intent):
        rows = original(owner, intent)
        reads.append(True)
        if len(reads) == 1:
            owner.session.execute(sa.update(Audit).where(Audit.action == F_ACTION).values(result_code="drift"))
        return rows

    monkeypatch.setattr(CasdoorTerminalLocalInvitationRepository, "_rows", drift)
    denied(case)
    assert reads == [True, True]
    assert all_state(case) == before


@pytest.mark.parametrize("wrong", ["subject", "namespace", "account", "fence", "scope"])
def test_current_exact_context_or_complete_scope_required(writer_case, wrong):
    case = writer_case()
    prepare(case)
    if wrong == "scope":
        with case.db() as session, session.begin():
            scope = CasdoorLoginScopeRepository(session, configuration_factory)._project_scope(
                case.context, case.key, case.attempt.account_id, creation_email=None, extra_workspace_ids=()
            )
            with pytest.raises(CasdoorLoginScopeConflict):
                CasdoorTerminalLocalInvitationRepository(session, configuration_factory).observe(
                    case.context, case.key, case.attempt.account_id, scope=replace(scope, intents=())
                )
    else:
        changes = {
            "subject": {"context": replace(case.context, subject="different")},
            "namespace": {"key": replace(case.key, namespace_id=uuid4())},
            "account": {"account_id": uuid4()},
            "fence": {"context": replace(case.context, fence_epoch=case.context.fence_epoch + 1)},
        }
        denied(case, **changes[wrong])
