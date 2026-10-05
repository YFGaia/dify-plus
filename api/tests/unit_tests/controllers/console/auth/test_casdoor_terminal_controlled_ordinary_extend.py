"""Real controlled invitation producers followed by two ordinary authorizations."""

from urllib.parse import urlsplit

import pytest
from test_casdoor_invited_history_http_extend import ordinary_then_invited as original_case
from test_casdoor_invited_history_remaining_http_extend import prepare_domain
from test_casdoor_invited_local_recovery_http_extend import finish, new_attempt
from test_casdoor_local_http_extend import begin, callback, mounted as original_mounted

pytest_plugins = ("test_casdoor_local_http_service_extend",)
ordinary_then_invited = original_case
mounted = original_mounted


@pytest.mark.parametrize("ordinary_then_invited", ["public-new-workspace"], indirect=True)
@pytest.mark.parametrize("mode", ["role", "withdraw", "regrant"])
def test_real_controlled_invitation_then_two_ordinary_callbacks(ordinary_then_invited, monkeypatch, mode):
    case = ordinary_then_invited
    prepare_domain(case, monkeypatch, mode)
    invited = finish(case, new_attempt(case, token=case.token))
    assert invited.status_code == 302 and invited.location.endswith("/apps/invited"), case.errors
    for index in range(2):
        target = f"/apps/ordinary-followup-{index}"
        result = callback(case.m, begin(case.m, return_path=target))
        assert result.status_code == 302 and urlsplit(result.location or "").path == target, case.errors


@pytest.mark.parametrize("ordinary_then_invited", ["public-new-workspace"], indirect=True)
@pytest.mark.parametrize("mode", ["role", "withdraw", "regrant"])
def test_actual_d_commit_unknown_then_recovery_and_two_ordinary(ordinary_then_invited, monkeypatch, mode):
    from sqlalchemy.orm import SessionTransaction
    from repositories import casdoor_terminal_retained_invitation_facts_extend as facts

    case = ordinary_then_invited
    prepare_domain(case, monkeypatch, mode)
    append, commit, lost = facts.append_actual, SessionTransaction.commit, []

    def mark(owner, guard):
        result = append(owner, guard)
        if result is not None:
            owner.session.info["terminal_actual_retained_d"] = True
        return result

    def unknown(transaction, *args, **kwargs):
        selected = (
            transaction._parent is None and not lost and transaction.session.info.get("terminal_actual_retained_d")
        )
        result = commit(transaction, *args, **kwargs)
        if selected:
            lost.append(True)
            raise TimeoutError("Actual D commit acknowledged, synthetic reply lost")
        return result

    monkeypatch.setattr(facts, "append_actual", mark)
    monkeypatch.setattr(SessionTransaction, "commit", unknown)
    issued = len(case.m.f.control.tokens)
    first = finish(case, new_attempt(case, token=case.token))
    assert lost == [True] and len(case.m.f.control.tokens) == issued
    assert not (first.location or "").endswith("/apps/invited")
    recovered = finish(case, new_attempt(case, token=case.token))
    assert recovered.location.endswith("/apps/invited"), case.errors
    for index in range(2):
        target = f"/apps/recovered-ordinary-{index}"
        assert callback(case.m, begin(case.m, return_path=target)).location.endswith(target), case.errors


@pytest.mark.parametrize("ordinary_then_invited", ["public-new-workspace"], indirect=True)
@pytest.mark.parametrize("manual_role", ["editor", "owner"])
def test_actual_manual_override_and_owner_remain_through_two_ordinary(ordinary_then_invited, monkeypatch, manual_role):
    from test_casdoor_invited_history_remaining_http_extend import (
        test_actual_changed_signed_target_preserves_manual_override_and_owner,
    )
    from sqlalchemy.orm import Session
    import sqlalchemy as sa
    from models.casdoor_extend import CasdoorManagedMembershipExtend as History
    from models.account import TenantAccountJoin as Join
    from uuid import UUID

    case = ordinary_then_invited
    test_actual_changed_signed_target_preserves_manual_override_and_owner(case, monkeypatch, manual_role, None)
    with Session(case.m.f.local.engine) as session:
        prior = session.execute(
            sa.select(*History.__table__.columns).where(History.workspace_id == str(UUID(int=200)))
        ).one()
    for index in range(2):
        target = f"/apps/manual-ordinary-{index}"
        assert callback(case.m, begin(case.m, return_path=target)).location.endswith(target), case.errors
    with Session(case.m.f.local.engine) as session:
        assert session.execute(sa.select(*History.__table__.columns).where(History.id == prior.id)).one() == prior
        assert session.get(Join, prior.join_id).role.value == manual_role


@pytest.mark.parametrize("ordinary_then_invited", ["public-new-workspace"], indirect=True)
@pytest.mark.parametrize(
    "drift",
    [
        "missing",
        "duplicate",
        "actor",
        "header-account",
        "body-account",
        "body-operation",
        "kind",
        "action",
        "d-hash",
        "d-id",
        "generation",
        "plan-reason",
        "postwrite",
        "before-baseline",
        "after-role",
        "membership-id",
        "before-d",
        "after-f",
        "extra",
        "noncanonical",
        "oversize",
    ],
)
def test_retained_fact_drift_denies_real_next_ordinary(ordinary_then_invited, monkeypatch, drift):
    import json
    from datetime import timedelta
    from uuid import uuid4, UUID
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from models.casdoor_extend import CasdoorAuditExtend as Audit
    from repositories.casdoor_terminal_retained_invitation_facts_extend import ACTION

    case = ordinary_then_invited
    prepare_domain(case, monkeypatch, "role")
    invited = finish(case, new_attempt(case, token=case.token))
    assert invited.location.endswith("/apps/invited"), case.errors
    issued = len(case.m.f.control.tokens)
    with Session(case.m.f.local.engine) as session, session.begin():
        audit = session.scalar(sa.select(Audit).where(Audit.action == ACTION))
        d = json.loads(audit.summary_json)
        if drift == "missing":
            session.delete(audit)
        elif drift == "duplicate":
            data = {c.name: getattr(audit, c.name) for c in Audit.__table__.columns}
            data["id"] = str(uuid4())
            session.add(Audit(**data))
        elif drift == "actor":
            audit.actor_account_id = str(UUID(int=700))
        elif drift == "action":
            audit.action = "unknown_retained_kind"
        elif drift == "header-account":
            audit.account_id = str(UUID(int=999))
        elif drift == "before-d":
            original = session.get(Audit, d["write_audit_id"])
            audit.created_at = original.created_at - timedelta(microseconds=1)
        elif drift == "after-f":
            final = session.scalar(sa.select(Audit).where(Audit.action == "invited_local_membership_finalization"))
            audit.created_at = final.created_at + timedelta(microseconds=1)
        else:
            if drift == "body-account":
                d["references"]["account_id"] = str(UUID(int=999))
            elif drift == "body-operation":
                d["references"]["operation_id"] = str(uuid4())
            elif drift == "kind":
                d["receipt_kind"] = "unknown_retained_kind"
            elif drift == "d-hash":
                d["write_receipt_sha256"] = "0" * 64
            elif drift == "d-id":
                d["write_audit_id"] = str(uuid4())
            elif drift == "generation":
                d["generation"] += 1
            elif drift == "plan-reason":
                d["plan"][1]["reason"] = "default_normal_fallback"
            elif drift == "postwrite":
                d["postwrite"]["identity"]["sync_generation"] += 1
            elif drift == "before-baseline":
                d["histories"][0]["before"]["baseline_json"] = "{}"
            elif drift == "after-role":
                d["histories"][0]["join_after"]["role"] = "owner"
            elif drift == "membership-id":
                d["histories"][0]["before"]["id"] = str(uuid4())
            elif drift == "extra":
                d["authorized"] = True
            audit.summary_json = json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            if drift == "noncanonical":
                audit.summary_json += " "
            elif drift == "oversize":
                audit.summary_json += " " * 32769
    result = callback(case.m, begin(case.m, return_path="/apps/must-deny"))
    assert not result.location.endswith("/apps/must-deny")
    assert len(case.m.f.control.tokens) == issued


@pytest.mark.parametrize("ordinary_then_invited", ["public-new-workspace"], indirect=True)
@pytest.mark.parametrize(
    "drift",
    ["foreign-correlation-duplicate", "foreign-header", "count", "old-join", "epoch", "time", "unrelated-budget"],
)
def test_control_audit_related_domain_navigation(ordinary_then_invited, monkeypatch, drift):
    import json
    from datetime import timedelta
    from uuid import uuid4, UUID
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from models.casdoor_extend import CasdoorAuditExtend as Audit

    case = ordinary_then_invited
    prepare_domain(case, monkeypatch, "regrant")
    assert finish(case, new_attempt(case, token=case.token)).location.endswith("/apps/invited"), case.errors
    issued = len(case.m.f.control.tokens)
    with Session(case.m.f.local.engine) as session, session.begin():
        audit = session.scalar(sa.select(Audit).where(Audit.action == "local_member_regrant"))
        if drift in ("foreign-correlation-duplicate", "unrelated-budget"):
            data = {c.name: getattr(audit, c.name) for c in Audit.__table__.columns}
            data.update(id=str(uuid4()), correlation_id=str(uuid4()), account_id=str(UUID(int=999)))
            if drift == "unrelated-budget":
                data["summary_json"] = "unrelated malformed oversized record " + "x" * 33000
            session.add(Audit(**data))
        elif drift == "foreign-header":
            audit.account_id = str(UUID(int=999))
        elif drift == "time":
            audit.created_at = audit.created_at - timedelta(days=1)
        else:
            data = json.loads(audit.summary_json)
            if drift == "count":
                data["count"] = 2
            elif drift == "old-join":
                data["references"]["old_join_id"] = str(uuid4())
            else:
                data["epoch_after"] += 1
            audit.summary_json = json.dumps(data, sort_keys=True, separators=(",", ":"))
    result = callback(case.m, begin(case.m, return_path="/apps/control-facts"))
    if drift == "unrelated-budget":
        assert result.location.endswith("/apps/control-facts"), case.errors
    else:
        assert not result.location.endswith("/apps/control-facts")
        assert len(case.m.f.control.tokens) == issued


@pytest.mark.parametrize("ordinary_then_invited", ["public-new-workspace"], indirect=True)
def test_historical_role_is_not_compared_to_later_signed_role(ordinary_then_invited, monkeypatch):
    from uuid import UUID
    from sqlalchemy.orm import Session
    from core.casdoor.configuration import RoleRef, WorkspaceRoleMapping

    case = ordinary_then_invited
    prepare_domain(case, monkeypatch, "role")
    assert finish(case, new_attempt(case, token=case.token)).location.endswith("/apps/invited"), case.errors
    f = case.m.f
    config = f.local.config.model_copy(
        update={
            "workspace_mappings": (
                WorkspaceRoleMapping(workspace_id=UUID(int=200), normal=RoleRef(organization="Org", name="operators")),
            )
        }
    )
    with Session(f.local.engine) as session, session.begin():
        repo = f.coordinator._configuration_service._repository(session)
        draft = repo.save_draft(config, etag=repo._integration().etag, actor_account_id=UUID(int=700))
        repo._integration().active_revision_id = str(draft.draft_revision_id)
        session.flush()
    for index in range(2):
        target = f"/apps/later-signed-role-{index}"
        assert callback(case.m, begin(case.m, return_path=target)).location.endswith(target), case.errors


@pytest.mark.parametrize("ordinary_then_invited", ["public-new-workspace"], indirect=True)
@pytest.mark.parametrize("drift", ["actor", "header-account", "raw-extra", "foreign-duplicate"])
def test_actual_retained_append_drift_rolls_back_d_before_session(ordinary_then_invited, monkeypatch, drift):
    from uuid import UUID, uuid4
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from models.casdoor_extend import CasdoorAuditExtend as Audit
    from repositories import casdoor_terminal_retained_invitation_facts_extend as facts

    case = ordinary_then_invited
    prepare_domain(case, monkeypatch, "role")
    append, seen = facts.append_actual, []

    def tamper(owner, guard):
        expected = append(owner, guard)
        if expected is not None:
            seen.append(drift)
            if drift == "foreign-duplicate":
                duplicate = dict(expected, id=str(uuid4()), correlation_id=str(uuid4()), account_id=str(UUID(int=999)))
                owner.session.add(Audit(**duplicate))
                owner.session.flush()
            else:
                values = (
                    {"actor_account_id": str(UUID(int=700))}
                    if drift == "actor"
                    else {"account_id": str(UUID(int=999))}
                    if drift == "header-account"
                    else {"summary_json": expected["summary_json"] + " "}
                )
                owner.session.execute(sa.update(Audit).where(Audit.id == expected["id"]).values(**values))
        return expected

    issued = len(case.m.f.control.tokens)
    monkeypatch.setattr(facts, "append_actual", tamper)
    result = finish(case, new_attempt(case, token=case.token))
    assert not (result.location or "").endswith("/apps/invited")
    assert seen == [drift] and len(case.m.f.control.tokens) == issued
    with Session(case.m.f.local.engine) as session:
        assert not tuple(
            session.scalars(
                sa.select(Audit.id).where(Audit.action.in_((facts.ACTION, "invited_local_membership_write")))
            )
        )


@pytest.mark.parametrize("ordinary_then_invited", ["public-new-workspace"], indirect=True)
def test_unregistered_dto_or_guard_cannot_append_retained_facts(ordinary_then_invited):
    from sqlalchemy.orm import Session
    from repositories.casdoor_invited_login_guard_repository_extend import CasdoorInvitedLoginGuardRepository
    from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict
    from repositories.casdoor_terminal_retained_invitation_facts_extend import append_actual

    case = ordinary_then_invited
    with Session(case.m.f.local.engine) as session, session.begin():
        owner = CasdoorInvitedLoginGuardRepository(session, case.m.f.coordinator._configuration_service._repository)
        for forged in (True, {"verified": True}, object()):
            with pytest.raises(CasdoorLoginScopeConflict):
                append_actual(owner, forged)
