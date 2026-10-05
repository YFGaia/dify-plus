"""NEW actual producer RED cases beyond D33's frozen same-role first domain."""

from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

import pytest
import sqlalchemy as sa
from core.casdoor.configuration import RoleRef, WorkspaceRoleMapping
from core.helper import ssrf_proxy
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin
from models.agent import Agent
from models.casdoor_extend import (
    CasdoorAuditExtend,
    CasdoorIdentityExtend,
    CasdoorManagedMembershipExtend,
    CasdoorSyncIntentExtend,
)
from models.dataset import Dataset
from models.model import App
from services import account_adapters
from services.account_service import TenantService
from sqlalchemy.orm import Session
from test_casdoor_invited_history_http_extend import (
    ordinary_then_invited as original_case,
)
from test_casdoor_invited_history_http_extend import retained
from test_casdoor_invited_local_recovery_http_extend import finish, new_attempt
from test_casdoor_local_http_extend import begin, callback
from test_casdoor_local_http_extend import mounted as original_mounted
from test_gateway import response

pytest_plugins = ("test_casdoor_local_http_service_extend",)
ordinary_then_invited = original_case
mounted = original_mounted


@pytest.mark.parametrize("ordinary_then_invited", ["public-new-workspace"], indirect=True)
def test_actual_current_signed_role_change_after_ordinary_then_public_invitation(ordinary_then_invited):
    case = ordinary_then_invited
    f = case.m.f
    ref = RoleRef(organization="Org", name="operators")
    config = f.local.config.model_copy(
        update={"workspace_mappings": (WorkspaceRoleMapping(workspace_id=UUID(int=200), editor=ref),)}
    )
    with Session(f.local.engine) as session, session.begin():
        owner = f.coordinator._configuration_service._repository(session)
        integration = owner._integration()
        draft = owner.save_draft(config, etag=integration.etag, actor_account_id=UUID(int=700))
        # The same documented offline active-pointer seam as the original HTTP suite.
        integration.active_revision_id = str(draft.draft_revision_id)
        session.flush()
    result = finish(case, new_attempt(case, token=case.token))
    assert result.status_code == 302 and result.location.endswith("/apps/invited"), case.errors
    assert_completed(case, generation=2, mode="role")


@pytest.mark.parametrize("ordinary_then_invited", ["public-new-workspace"], indirect=True)
@pytest.mark.parametrize("regrant", [False, True])
def test_actual_signed_withdraw_or_real_prior_withdraw_regrant_then_public_invitation(
    ordinary_then_invited, monkeypatch, regrant
):
    case = ordinary_then_invited
    f = case.m.f
    for model in (App, Dataset, Agent):
        model.__table__.create(f.local.engine, checkfirst=True)
    with Session(f.local.engine) as session, session.begin():
        owner = Account(
            name="Original resource recipient",
            email="withdraw-owner@example.test",
            status=AccountStatus.ACTIVE,
            initialized_at=datetime(2024, 1, 1),
        )
        session.add(owner)
        session.flush()
        TenantService.persist_tenant_member(session.get(Tenant, str(UUID(int=200))), owner, session, "owner")
    transport = ssrf_proxy.make_request_with_deadline
    hit = [False]

    def current_graph(method, url, **kwargs):
        result = transport(method, url, **kwargs)
        if urlsplit(url).path == "/api/get-roles" and not hit[0]:
            data = result.json()
            data["data"][0]["users"] = []
            return response(data)
        return result

    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", current_graph)
    if regrant:
        # Produce the absent Join and retained FINALIZED history through the real ordinary owner.
        result = callback(case.m, begin(case.m, return_path="/apps/real-withdraw"))
        assert result.status_code == 302 and result.location.endswith("/apps/real-withdraw"), case.errors
        hit[0] = True
    result = finish(case, new_attempt(case, token=case.token))
    assert result.status_code == 302 and result.location.endswith("/apps/invited"), case.errors
    assert_completed(case, generation=3 if regrant else 2, mode="regrant" if regrant else "withdraw")


def assert_completed(case, *, generation, mode):
    import json

    from core.casdoor.ownership import parse_local_withdrawal_json
    from libs.token import _real_cookie_name
    from models.casdoor_extend import CasdoorSyncIntentExtend

    f = case.m.f
    refresh = case.m.client.get_cookie(_real_cookie_name("refresh_token"), domain="console.example.test")
    expected_refresh = f.control.tokens[-1][2]
    if isinstance(expected_refresh, bytes):
        expected_refresh = expected_refresh.decode()
    assert refresh.value == expected_refresh
    with Session(f.local.engine) as session:
        assert session.scalar(sa.select(CasdoorIdentityExtend.sync_generation)) == generation
        operation = session.scalar(sa.select(CasdoorSyncIntentExtend))
        assert operation.operation_state.value == "applied" and operation.termination_state.value == "confirmed"
        writes = tuple(
            session.scalars(
                sa.select(CasdoorAuditExtend).where(
                    CasdoorAuditExtend.correlation_id == operation.id,
                    CasdoorAuditExtend.action == "invited_local_membership_write",
                )
            )
        )
        finals = tuple(
            session.scalars(
                sa.select(CasdoorAuditExtend).where(
                    CasdoorAuditExtend.correlation_id == operation.id,
                    CasdoorAuditExtend.action == "invited_local_membership_finalization",
                )
            )
        )
        assert len(writes) == len(finals) == 1
        d, final = json.loads(writes[0].summary_json), json.loads(finals[0].summary_json)
        histories = tuple(session.execute(sa.select(*CasdoorManagedMembershipExtend.__table__.columns)))
        assert len(histories) == len(case.old) == 2
        assert all(row.finalization.value == "finalized" for row in histories)
        history = next(row for row in histories if row.workspace_id == str(UUID(int=200)))
        original = next(row for row in case.old if row["id"] == history.id)
        for field in (
            "id",
            "namespace_id",
            "identity_id",
            "account_id",
            "workspace_id",
            "source",
            "baseline_json",
            "tombstone",
            "created_at",
        ):
            assert getattr(history, field) == original[field]
        assert history.desired_generation == generation
        if mode == "role":
            assert history.ownership_epoch == 0 and d["schema_version"] == 1
            assert json.loads(history.desired_roles_json)["target_role"] == "editor"
        else:
            assert d["schema_version"] == 2 and history.id in final["finalized_ids"]
            assert history.ownership_epoch == (2 if mode == "regrant" else 1)
            assert session.get(TenantAccountJoin, original["join_id"]) is None
            if mode == "withdraw":
                marker = parse_local_withdrawal_json(history.desired_roles_json)
                assert marker["withdrawal_generation"] == generation and marker["withdrawal_epoch"] == 1
                assert d["withdrawals"][0]["membership_id"] == history.id
            else:
                assert history.join_id != original["join_id"]
                assert (
                    next(item for item in d["results"] if item["membership_id"] == history.id)["membership_regranted"]
                    is True
                )
                assert session.get(TenantAccountJoin, history.join_id).role.value == "admin"


def prepare_domain(case, monkeypatch, mode):
    """Original producer transitions before invitation; no successful rows seeded."""
    f = case.m.f
    if mode == "role":
        ref = RoleRef(organization="Org", name="operators")
        config = f.local.config.model_copy(
            update={"workspace_mappings": (WorkspaceRoleMapping(workspace_id=UUID(int=200), editor=ref),)}
        )
        with Session(f.local.engine) as session, session.begin():
            repo = f.coordinator._configuration_service._repository(session)
            draft = repo.save_draft(config, etag=repo._integration().etag, actor_account_id=UUID(int=700))
            repo._integration().active_revision_id = str(draft.draft_revision_id)
            session.flush()
        return 2
    for model in (App, Dataset, Agent):
        model.__table__.create(f.local.engine, checkfirst=True)
    with Session(f.local.engine) as session, session.begin():
        owner = Account(
            name="Actual withdrawal recipient",
            email="fault-recipient@example.test",
            status=AccountStatus.ACTIVE,
            initialized_at=datetime(2024, 1, 1),
        )
        session.add(owner)
        session.flush()
        TenantService.persist_tenant_member(session.get(Tenant, str(UUID(int=200))), owner, session, "owner")
    transport, hit = ssrf_proxy.make_request_with_deadline, [False]

    def graph(method, url, **kwargs):
        result = transport(method, url, **kwargs)
        if urlsplit(url).path == "/api/get-roles" and not hit[0]:
            data = result.json()
            data["data"][0]["users"] = []
            return response(data)
        return result

    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", graph)
    if mode == "regrant":
        first = callback(case.m, begin(case.m, return_path="/apps/real-withdraw"))
        assert first.location.endswith("/apps/real-withdraw"), case.errors
        hit[0] = True
        return 3
    return 2


@pytest.mark.parametrize("ordinary_then_invited", ["public-new-workspace"], indirect=True)
@pytest.mark.parametrize("manual_role", ["editor", "owner"])
@pytest.mark.parametrize("drift", [None, "history-baseline", "join-current", "forged-preserved"])
def test_actual_changed_signed_target_preserves_manual_override_and_owner(
    ordinary_then_invited, monkeypatch, manual_role, drift
):
    case = ordinary_then_invited
    f = case.m.f
    with Session(f.local.engine) as session, session.begin():
        tenant = session.get(Tenant, str(UUID(int=200)))
        operator = Account(
            name="Original manual owner",
            email="changed-plan-owner@example.test",
            status=AccountStatus.ACTIVE,
            initialized_at=datetime(2024, 1, 1),
        )
        session.add(operator)
        session.flush()
        TenantService.persist_tenant_member(tenant, operator, session, "owner")
        operator_id = operator.id
    with Session(f.local.engine) as session:
        TenantService.update_member_role(
            session.get(Tenant, str(UUID(int=200))),
            session.get(Account, case.account),
            manual_role,
            session.get(Account, operator_id),
            session=session,
        )
    ref = RoleRef(organization="Org", name="operators")
    config = f.local.config.model_copy(
        update={"workspace_mappings": (WorkspaceRoleMapping(workspace_id=UUID(int=200), normal=ref),)}
    )
    with Session(f.local.engine) as session, session.begin():
        repo = f.coordinator._configuration_service._repository(session)
        draft = repo.save_draft(config, etag=repo._integration().etag, actor_account_id=UUID(int=700))
        repo._integration().active_revision_id = str(draft.draft_revision_id)
        session.flush()
    with Session(f.local.engine) as session:
        prior = session.execute(
            sa.select(*CasdoorManagedMembershipExtend.__table__.columns).where(
                CasdoorManagedMembershipExtend.workspace_id == str(UUID(int=200))
            )
        ).one()
        join = session.execute(
            sa.select(*TenantAccountJoin.__table__.columns).where(TenantAccountJoin.id == prior.join_id)
        ).one()
        assert prior.ownership.value == "local_override" and join.role.value == manual_role
    observed = []
    if drift is not None:
        from dataclasses import replace

        from repositories.casdoor_invited_login_guard_repository_extend import (
            CasdoorInvitedLoginGuardRepository,
            _binding,
        )

        original = CasdoorInvitedLoginGuardRepository.verify_after

        def forged(owner, guard):
            binding = _binding(guard, owner.session)
            actual = next(item for item in binding.result.workspaces if item.workspace_id == UUID(int=200))
            assert actual.outcome.value == "preserved" and not actual.role_changed and not actual.metadata_changed
            if drift == "forged-preserved":
                binding.result = replace(
                    binding.result,
                    workspaces=tuple(
                        replace(item, metadata_changed=True) if item is actual else item
                        for item in binding.result.workspaces
                    ),
                )
            elif drift == "history-baseline":
                owner.session.execute(
                    sa.update(CasdoorManagedMembershipExtend)
                    .where(CasdoorManagedMembershipExtend.id == prior.id)
                    .values(baseline_json="{}")
                )
            else:
                owner.session.execute(
                    sa.update(TenantAccountJoin)
                    .where(TenantAccountJoin.id == prior.join_id)
                    .values(current=not join.current)
                )
            observed.append(drift)
            return original(owner, guard)

        monkeypatch.setattr(CasdoorInvitedLoginGuardRepository, "verify_after", forged)
    issued = len(f.control.tokens)
    result = finish(case, new_attempt(case, token=case.token))
    if drift is None:
        assert result.status_code == 302 and result.location.endswith("/apps/invited"), case.errors
    else:
        assert not (result.status_code == 302 and result.location.endswith("/apps/invited"))
        assert observed == [drift] and len(f.control.tokens) == issued
    with Session(f.local.engine) as session:
        if drift is not None:
            assert session.scalar(sa.select(CasdoorIdentityExtend.sync_generation)) == 1
        assert (
            session.execute(
                sa.select(*CasdoorManagedMembershipExtend.__table__.columns).where(
                    CasdoorManagedMembershipExtend.id == prior.id
                )
            ).one()
            == prior
        )
        assert (
            session.execute(
                sa.select(*TenantAccountJoin.__table__.columns).where(TenantAccountJoin.id == prior.join_id)
            ).one()
            == join
        )


@pytest.mark.parametrize("ordinary_then_invited", ["public-new-workspace"], indirect=True)
@pytest.mark.parametrize("mode", ["role", "withdraw", "regrant"])
@pytest.mark.parametrize("drift", ["history-baseline", "duplicate-control", "unrelated-lifecycle"])
def test_actual_recovery_full_delta_rejects_unrelated_or_duplicate_facts(
    ordinary_then_invited, monkeypatch, mode, drift
):
    from types import MappingProxyType
    from uuid import uuid4

    from repositories.casdoor_invited_finalization_receipt_repository_extend import (
        _PendingProjection,
    )
    from services.casdoor_invitation_finalization_service_extend import (
        CasdoorInvitationFinalizationService,
    )
    from services.casdoor_invited_local_login_coordinator_service_extend import (
        CasdoorInvitedLocalLoginCoordinatorService,
    )

    case = ordinary_then_invited
    generation = prepare_domain(case, monkeypatch, mode)
    f, observed = case.m.f, []
    initial = CasdoorInvitationFinalizationService._finalize_invited_login

    def unavailable(*args, **kwargs):
        raise TimeoutError("offline before original consume")

    monkeypatch.setattr(CasdoorInvitationFinalizationService, "_finalize_invited_login", unavailable)
    issued = len(f.control.tokens)
    finish(case, new_attempt(case, token=case.token))
    assert len(f.control.tokens) == issued
    monkeypatch.setattr(CasdoorInvitationFinalizationService, "_finalize_invited_login", initial)
    original = CasdoorInvitedLocalLoginCoordinatorService._resume_membership_delta

    def changed(row, **values):
        return _PendingProjection(MappingProxyType(dict(row._mapping) | values))

    def forged(owner, before, after, *, finalizing):
        if finalizing:
            return original(owner, before, after, finalizing=finalizing)
        original(owner, before, after, finalizing=False)
        current, actual = after
        fresh = dict(actual)
        if drift == "history-baseline":
            fresh["histories"] = tuple(
                changed(row, baseline_json="{}") if row.workspace_id == str(UUID(int=200)) else row
                for row in fresh["histories"]
            )
        elif drift == "duplicate-control":
            # Role has only D; controlled paths also have an original control audit.
            new_audits = tuple(row for row in fresh["audits"] if row not in before[1]["audits"])
            row = next((row for row in new_audits if row.action.startswith("local_member_")), new_audits[0])
            fresh["audits"] = (*fresh["audits"], changed(row, id=str(uuid4())))
        else:
            fresh["lifecycles"] = tuple(
                changed(row, epoch=row.epoch + 1) if row.workspace_id == str(UUID(int=100)) else row
                for row in fresh["lifecycles"]
            )
        observed.append(drift)
        return original(owner, before, (current, fresh), finalizing=False)

    monkeypatch.setattr(CasdoorInvitedLocalLoginCoordinatorService, "_resume_membership_delta", forged)
    result = finish(case, new_attempt(case, token=case.token))
    assert not (result.status_code == 302 and result.location.endswith("/apps/invited"))
    assert observed == [drift] and len(f.control.tokens) == issued
    with Session(f.local.engine) as session:
        assert session.scalar(sa.select(CasdoorIdentityExtend.sync_generation)) == generation
        assert (
            len(
                tuple(
                    session.scalars(
                        sa.select(CasdoorAuditExtend.id).where(
                            CasdoorAuditExtend.action == "invited_local_membership_write"
                        )
                    )
                )
            )
            == 1
        )
        assert not tuple(
            session.scalars(
                sa.select(CasdoorAuditExtend.id).where(
                    CasdoorAuditExtend.action == "invited_local_membership_finalization"
                )
            )
        )


@pytest.mark.parametrize("ordinary_then_invited", ["public-new-workspace"], indirect=True)
@pytest.mark.parametrize("mode", ["withdraw", "regrant"])
@pytest.mark.parametrize("drift", ["before-operation", "after-d", "actor", "equal-termination", "equal-d"])
def test_actual_finalized_control_audit_rejects_time_and_full_row_drift(
    ordinary_then_invited, monkeypatch, mode, drift
):
    from datetime import timedelta

    from models.casdoor_extend import CasdoorSyncIntentExtend
    from repositories.casdoor_invited_finalization_receipt_repository_extend import (
        CasdoorInvitedFinalizationReceiptRepository,
    )

    case = ordinary_then_invited
    prepare_domain(case, monkeypatch, mode)
    f, inputs = case.m.f, []
    original = CasdoorInvitedFinalizationReceiptRepository.observe

    def capture_actual_read(owner, attempt, *, roles):
        actual = original(owner, attempt, roles=roles)
        inputs.append((attempt, roles))
        return actual

    monkeypatch.setattr(CasdoorInvitedFinalizationReceiptRepository, "observe", capture_actual_read)
    result = finish(case, new_attempt(case, token=case.token))
    assert result.status_code == 302 and result.location.endswith("/apps/invited"), case.errors
    assert inputs
    monkeypatch.setattr(CasdoorInvitedFinalizationReceiptRepository, "observe", original)
    attempt, roles = inputs[-1]
    issued = len(f.control.tokens)
    with Session(f.local.engine) as session, session.begin():
        operation = session.scalar(
            sa.select(CasdoorSyncIntentExtend).where(CasdoorSyncIntentExtend.account_id == str(attempt.account_id))
        )
        audit = session.scalar(
            sa.select(CasdoorAuditExtend).where(
                CasdoorAuditExtend.correlation_id == operation.id,
                CasdoorAuditExtend.action
                == ("local_member_withdraw" if mode == "withdraw" else "local_member_regrant"),
            )
        )
        d = session.scalar(
            sa.select(CasdoorAuditExtend).where(
                CasdoorAuditExtend.correlation_id == operation.id,
                CasdoorAuditExtend.action == "invited_local_membership_write",
            )
        )
        assert audit is not None and operation.created_at <= audit.created_at <= d.created_at
        operation_id = UUID(operation.id)
        timestamps = {
            "before-operation": operation.created_at - timedelta(microseconds=1),
            "after-d": d.created_at + timedelta(microseconds=1),
            "equal-termination": operation.terminated_at,
            "equal-d": d.created_at,
        }
        values = {"actor_account_id": case.account} if drift == "actor" else {"created_at": timestamps[drift]}
        session.execute(sa.update(CasdoorAuditExtend).where(CasdoorAuditExtend.id == audit.id).values(**values))
    from repositories.casdoor_login_scope_repository_extend import (
        CasdoorLoginScopeConflict,
    )

    with Session(f.local.engine) as session, session.begin():
        fresh = CasdoorInvitedFinalizationReceiptRepository(session, f.coordinator._configuration_service._repository)
        if drift.startswith("equal-"):
            assert fresh.observe(attempt, roles=roles).operation_id == operation_id
        else:
            with pytest.raises(CasdoorLoginScopeConflict):
                fresh.observe(attempt, roles=roles)
    assert len(f.control.tokens) == issued


@pytest.mark.parametrize("ordinary_then_invited", ["public-new-workspace"], indirect=True)
@pytest.mark.parametrize("mode", ["role", "withdraw", "regrant"])
@pytest.mark.parametrize("fault", ["before-consume", "p3l-ack", "d-ack", "f-ack"])
def test_actual_domain_original_commit_loss_resumes_only_missing_owners(
    ordinary_then_invited, monkeypatch, mode, fault
):
    from models.casdoor_extend import CasdoorSyncIntentExtend
    from services.casdoor_invitation_finalization_service_extend import (
        CasdoorInvitationFinalizationService,
    )
    from sqlalchemy.orm import SessionTransaction

    case, lost = ordinary_then_invited, []
    generation = prepare_domain(case, monkeypatch, mode)
    f, before = case.m.f, retained(case)
    issued = len(f.control.tokens)
    if fault == "before-consume":
        original = CasdoorInvitationFinalizationService._finalize_invited_login

        def before_original(*args, **kwargs):
            raise TimeoutError("offline before actual consume")

        monkeypatch.setattr(CasdoorInvitationFinalizationService, "_finalize_invited_login", before_original)
    else:
        flush, commit = Session.flush, SessionTransaction.commit

        def observe_flush(session, *args, **kwargs):
            selected = any(
                (
                    fault == "p3l-ack"
                    and isinstance(row, CasdoorSyncIntentExtend)
                    and row.operation_state.value == "applied"
                )
                or (
                    isinstance(row, CasdoorAuditExtend)
                    and row.action
                    == (
                        "invited_local_membership_write"
                        if fault == "d-ack"
                        else "invited_local_membership_finalization"
                        if fault == "f-ack"
                        else ""
                    )
                )
                for row in tuple(session.new) + tuple(session.dirty)
            )
            result = flush(session, *args, **kwargs)
            if selected:
                session.info["d33_remaining_actual_commit"] = True
            return result

        def observe_commit(transaction, *args, **kwargs):
            selected = (
                transaction._parent is None and not lost and transaction.session.info.get("d33_remaining_actual_commit")
            )
            result = commit(transaction, *args, **kwargs)
            if selected:
                lost.append(True)
                raise TimeoutError("offline original commit acknowledgement lost")
            return result

        monkeypatch.setattr(Session, "flush", observe_flush)
        monkeypatch.setattr(SessionTransaction, "commit", observe_commit)
    first = new_attempt(case, token=case.token)
    finish(case, first)
    assert len(f.control.tokens) == issued
    if fault == "before-consume":
        monkeypatch.setattr(CasdoorInvitationFinalizationService, "_finalize_invited_login", original)
    else:
        assert lost == [True]
    second = new_attempt(case, token=case.token)
    result = finish(case, second)
    assert result.status_code == 302 and result.location.endswith("/apps/invited"), case.errors
    assert first != second and f.control.created[-1].nonce != f.control.created[-2].nonce
    assert len(f.control.tokens) == issued + 2 and retained(case) == before
    assert len([call for call in case.wire.calls if call[0] == account_adapters._INVITATION_CONSUME]) == 1
    assert_completed(case, generation=generation, mode=mode)


@pytest.mark.parametrize("ordinary_then_invited", ["public-new-workspace"], indirect=True)
@pytest.mark.parametrize("mode", ["withdraw", "regrant"])
@pytest.mark.parametrize("drift", ["audit-epoch", "desired-binding", "removed-id-reappears"])
def test_actual_controlled_readback_rejects_audit_marker_and_global_join_drift(
    ordinary_then_invited, monkeypatch, mode, drift
):
    import json

    from repositories.casdoor_invited_write_receipt_repository_extend import (
        CasdoorInvitedWriteReceiptRepository,
    )

    case = ordinary_then_invited
    generation = prepare_domain(case, monkeypatch, mode)
    f, observed = case.m.f, []
    original = CasdoorInvitedWriteReceiptRepository._rows

    def drift_before_actual_read(owner, attempt, *, lock):
        audit = owner.session.scalar(
            sa.select(CasdoorAuditExtend).where(
                CasdoorAuditExtend.correlation_id == owner.session.scalar(sa.select(CasdoorSyncIntentExtend.id)),
                CasdoorAuditExtend.action
                == ("local_member_withdraw" if mode == "withdraw" else "local_member_regrant"),
            )
        )
        if audit is not None and not observed:
            observed.append(drift)
            data = json.loads(audit.summary_json)
            references = data["references"]
            if drift == "audit-epoch":
                data["epoch_after"] += 1
                owner.session.execute(
                    sa.update(CasdoorAuditExtend)
                    .where(CasdoorAuditExtend.id == audit.id)
                    .values(summary_json=json.dumps(data, sort_keys=True, separators=(",", ":")))
                )
            elif drift == "desired-binding":
                row = owner.session.get(CasdoorManagedMembershipExtend, references["membership_id"])
                desired = json.loads(row.desired_roles_json)
                if mode == "withdraw":
                    desired["fence_epoch"] += 1
                else:
                    desired["target_role"] = "editor"
                owner.session.execute(
                    sa.update(CasdoorManagedMembershipExtend)
                    .where(CasdoorManagedMembershipExtend.id == row.id)
                    .values(desired_roles_json=json.dumps(desired, sort_keys=True, separators=(",", ":")))
                )
            else:
                recipient = owner.session.scalar(
                    sa.select(TenantAccountJoin.account_id).where(
                        TenantAccountJoin.tenant_id == str(UUID(int=200)), TenantAccountJoin.role == "owner"
                    )
                )
                owner.session.execute(
                    sa.insert(TenantAccountJoin).values(
                        id=references["old_join_id"],
                        tenant_id=str(UUID(int=300)),
                        account_id=recipient,
                        role="normal",
                        current=False,
                    )
                )
        return original(owner, attempt, lock=lock)

    monkeypatch.setattr(CasdoorInvitedWriteReceiptRepository, "_rows", drift_before_actual_read)
    issued = len(f.control.tokens)
    result = finish(case, new_attempt(case, token=case.token))
    assert not (result.status_code == 302 and result.location.endswith("/apps/invited"))
    assert observed == [drift] and len(f.control.tokens) == issued
    with Session(f.local.engine) as session:
        assert session.scalar(sa.select(CasdoorIdentityExtend.sync_generation)) == generation
        assert not tuple(
            session.scalars(
                sa.select(CasdoorAuditExtend.id).where(
                    CasdoorAuditExtend.action == "invited_local_membership_finalization"
                )
            )
        )
        assert (
            tuple(
                session.scalars(
                    sa.select(CasdoorManagedMembershipExtend.finalization).where(
                        CasdoorManagedMembershipExtend.workspace_id == str(UUID(int=200))
                    )
                )
            )[0].value
            == "pending"
        )


@pytest.mark.parametrize("ordinary_then_invited", ["public-new-workspace"], indirect=True)
@pytest.mark.parametrize("drift", ["join-current", "history-source", "history-baseline"])
def test_actual_role_change_flag_does_not_forgive_unrelated_full_row_drift(ordinary_then_invited, monkeypatch, drift):
    from models.casdoor_extend import CasdoorMembershipSource
    from repositories.casdoor_invited_login_guard_repository_extend import (
        CasdoorInvitedLoginGuardRepository,
        _binding,
    )

    case = ordinary_then_invited
    f = case.m.f
    ref = RoleRef(organization="Org", name="operators")
    config = f.local.config.model_copy(
        update={"workspace_mappings": (WorkspaceRoleMapping(workspace_id=UUID(int=200), editor=ref),)}
    )
    with Session(f.local.engine) as session, session.begin():
        owner = f.coordinator._configuration_service._repository(session)
        draft = owner.save_draft(config, etag=owner._integration().etag, actor_account_id=UUID(int=700))
        owner._integration().active_revision_id = str(draft.draft_revision_id)
        session.flush()
    original = CasdoorInvitedLoginGuardRepository.verify_after
    observed = []

    def adversarial(owner, guard):
        value = _binding(guard, owner.session)
        actual = next(item for item in value.result.workspaces if item.workspace_id == UUID(int=200))
        assert actual.role_changed is True and actual.current_role.value == "editor"
        if drift == "join-current":
            row = owner.session.execute(
                sa.select(*TenantAccountJoin.__table__.columns).where(TenantAccountJoin.id == str(actual.join_id))
            ).one()
            owner.session.execute(
                sa.update(TenantAccountJoin).where(TenantAccountJoin.id == row.id).values(current=not row.current)
            )
        else:
            values = {"source": CasdoorMembershipSource.ADOPT} if drift == "history-source" else {"baseline_json": "{}"}
            owner.session.execute(
                sa.update(CasdoorManagedMembershipExtend)
                .where(CasdoorManagedMembershipExtend.id == str(actual.membership_id))
                .values(**values)
            )
        observed.append(drift)
        return original(owner, guard)

    monkeypatch.setattr(CasdoorInvitedLoginGuardRepository, "verify_after", adversarial)
    issued = len(f.control.tokens)
    result = finish(case, new_attempt(case, token=case.token))
    assert not (result.status_code == 302 and result.location.endswith("/apps/invited"))
    assert observed == [drift] and len(f.control.tokens) == issued
    with Session(f.local.engine) as session:
        assert session.scalar(sa.select(CasdoorIdentityExtend.sync_generation)) == 1
        join = session.scalar(
            sa.select(TenantAccountJoin).where(
                TenantAccountJoin.account_id == case.account, TenantAccountJoin.tenant_id == str(UUID(int=200))
            )
        )
        assert join.role.value == "admin"
        rows = {
            row.id: dict(row._mapping)
            for row in session.execute(sa.select(*CasdoorManagedMembershipExtend.__table__.columns))
        }
        assert rows == {row["id"]: row for row in case.old}
        assert not tuple(
            session.scalars(
                sa.select(CasdoorAuditExtend.id).where(
                    CasdoorAuditExtend.action.in_(
                        ("invited_local_membership_write", "invited_local_membership_finalization")
                    )
                )
            )
        )


@pytest.mark.parametrize("ordinary_then_invited", ["public-new-workspace"], indirect=True)
@pytest.mark.parametrize("fault", ["producer-role", "producer-outcome", "producer-meta", "fact-outcome", "fact-meta"])
def test_actual_role_changed_misreport_or_forged_receipt_flags_are_rejected(ordinary_then_invited, monkeypatch, fault):
    from copy import deepcopy
    from dataclasses import replace

    from core.casdoor.local_roles import LocalRoleOutcome
    from repositories.casdoor_invited_login_guard_repository_extend import (
        CasdoorInvitedLoginGuardRepository,
        _binding,
    )
    from repositories.casdoor_invited_write_receipt_repository_extend import (
        CasdoorInvitedWriteReceiptRepository,
    )

    case, observed = ordinary_then_invited, []
    f = case.m.f
    ref = RoleRef(organization="Org", name="operators")
    config = f.local.config.model_copy(
        update={"workspace_mappings": (WorkspaceRoleMapping(workspace_id=UUID(int=200), editor=ref),)}
    )
    with Session(f.local.engine) as session, session.begin():
        repo = f.coordinator._configuration_service._repository(session)
        draft = repo.save_draft(config, etag=repo._integration().etag, actor_account_id=UUID(int=700))
        repo._integration().active_revision_id = str(draft.draft_revision_id)
        session.flush()
    if fault.startswith("producer"):
        original = CasdoorInvitedLoginGuardRepository.verify_after

        def misreported(owner, guard):
            value = _binding(guard, owner.session)
            actual = next(item for item in value.result.workspaces if item.workspace_id == UUID(int=200))
            assert actual.role_changed and actual.outcome is LocalRoleOutcome.APPLIED and actual.metadata_changed
            changes = (
                {"role_changed": False}
                if fault == "producer-role"
                else (
                    {"outcome": LocalRoleOutcome.NOOP} if fault == "producer-outcome" else {"metadata_changed": False}
                )
            )
            replacement = replace(actual, **changes)
            value.result = replace(
                value.result,
                workspaces=tuple(replacement if item is actual else item for item in value.result.workspaces),
            )
            observed.append(fault)
            return original(owner, guard)

        monkeypatch.setattr(CasdoorInvitedLoginGuardRepository, "verify_after", misreported)
    else:
        original = CasdoorInvitedWriteReceiptRepository._results

        def forged(owner, scope, rows, value):
            changed = deepcopy(value)
            item = next(item for item in changed["results"] if item["workspace_id"] == str(UUID(int=200)))
            assert item["role_changed"] is True
            item["outcome" if fault == "fact-outcome" else "metadata_changed"] = (
                "noop" if fault == "fact-outcome" else False
            )
            observed.append(fault)
            return original(owner, scope, rows, changed)

        monkeypatch.setattr(CasdoorInvitedWriteReceiptRepository, "_results", forged)
    issued = len(f.control.tokens)
    result = finish(case, new_attempt(case, token=case.token))
    assert not (result.status_code == 302 and result.location.endswith("/apps/invited"))
    assert observed and len(f.control.tokens) == issued
    with Session(f.local.engine) as session:
        generation = session.scalar(sa.select(CasdoorIdentityExtend.sync_generation))
        if fault.startswith("producer"):
            assert generation == 1
            current = {
                row.id: dict(row._mapping)
                for row in session.execute(sa.select(*CasdoorManagedMembershipExtend.__table__.columns))
            }
            assert current == {row["id"]: row for row in case.old}
        else:
            # D was actually committed. A later fact-reader rejection may not
            # roll back that earlier owner; it must prevent session issuance.
            assert generation == 2
