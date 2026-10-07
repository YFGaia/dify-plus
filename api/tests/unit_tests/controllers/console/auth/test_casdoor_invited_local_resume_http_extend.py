"""Original producer and owners, interrupted then resumed by new native auth."""

import pytest
from libs.token import _real_cookie_name
from models.casdoor_extend import CasdoorAuditExtend, CasdoorSyncIntentExtend, CasdoorOperationState
from services.account_adapters import RedisInvitationTokenStore
from services.casdoor_invitation_finalization_service_extend import CasdoorInvitationFinalizationService
from sqlalchemy.orm import Session, SessionTransaction
from test_casdoor_invited_local_recovery_http_extend import (
    adapters, durable, finish, invited_mounted as original_invited_mounted, mounted as mounted,
    new_attempt, no_pair, cookie,
)

pytest_plugins = ("test_casdoor_local_http_service_extend",)
invited_mounted = original_invited_mounted


@pytest.fixture
def invited_resume_mounted(invited_mounted, monkeypatch):
    # P3J reviews the real redis-py pool/connection and closes offline sockets.
    # The original mounted fixture's P1 wire has no physical client by itself.
    from test_invitation_publication_transport_extend import Wire, client_for, resp
    case = invited_mounted

    class ReadbackWire(Wire):
        def answer(self, sock, frame):
            if frame[0].upper() != b"EVAL":
                return super().answer(sock, frame)
            self.frames.append(frame)
            assert frame[1] == adapters._INVITATION_READBACK.encode()
            assert frame[2] == b"2" and len(frame) == 7
            result = case.wire.eval(adapters._INVITATION_READBACK, 2, *frame[3:])
            if getattr(self, "fail_reply", False):
                sock.read_failure = TimeoutError("synthetic read-only receipt reply lost")
                return b""
            return resp(result)

    wire = ReadbackWire()
    wrapper, raw, pool = client_for(wire, monkeypatch)
    monkeypatch.setattr(case.m.f.redis, "_require_client", wrapper._require_client, raising=False)
    case.p3j_wire = wire
    yield case
    assert all(wire.releases) and all(sock.closed for sock in wire.sockets)


@pytest.mark.parametrize("fault", ["before-consume", "after-consume", "p3l-ack", "d-ack", "f-ack"])
@pytest.mark.parametrize("bootstrap", [False, True])
@pytest.mark.parametrize("missing_quota", [False, True])
def test_actual_interrupted_original_phase_resumes_new_native_authorization(invited_resume_mounted, monkeypatch, fault, bootstrap, missing_quota):
    case, f = invited_resume_mounted, invited_resume_mounted.m.f
    lost = []
    if fault == "before-consume":
        original = CasdoorInvitationFinalizationService._finalize_invited_login

        def stop(*args, **kwargs):
            raise TimeoutError("synthetic before original consume")

        monkeypatch.setattr(CasdoorInvitationFinalizationService, "_finalize_invited_login", stop)
    elif fault == "after-consume":
        original = RedisInvitationTokenStore.consume_versioned_invitation

        def stop(*args, **kwargs):
            result = original(*args, **kwargs)
            lost.append(result.status)
            raise TimeoutError("synthetic original consume reply lost")

        monkeypatch.setattr(RedisInvitationTokenStore, "consume_versioned_invitation", stop)
    else:
        original_flush, original_commit = Session.flush, SessionTransaction.commit

        def flush(session, *args, **kwargs):
            selected = any(
                (fault == "p3l-ack" and isinstance(row, CasdoorSyncIntentExtend)
                 and row.operation_state is CasdoorOperationState.APPLIED)
                or (fault == "d-ack" and isinstance(row, CasdoorAuditExtend)
                    and row.action == "invited_local_membership_write")
                or (fault == "f-ack" and isinstance(row, CasdoorAuditExtend)
                    and row.action == "invited_local_membership_finalization")
                for row in tuple(session.new) + tuple(session.dirty)
            )
            result = original_flush(session, *args, **kwargs)
            if selected:
                session.info["d18c_actual_phase_write"] = True
            return result

        def commit(transaction, *args, **kwargs):
            selected = transaction._parent is None and not lost and transaction.session.info.get("d18c_actual_phase_write")
            result = original_commit(transaction, *args, **kwargs)
            if selected:
                lost.append(True)
                raise TimeoutError("synthetic actual phase commit reply lost")
            return result

        monkeypatch.setattr(Session, "flush", flush)
        monkeypatch.setattr(SessionTransaction, "commit", commit)
    first = new_attempt(case)
    finish(case, first)
    no_pair(case)
    if missing_quota:
        from models.account_money_extend import AccountMoneyExtend
        import sqlalchemy as sa
        with Session(f.local.engine) as session, session.begin():
            session.execute(sa.delete(AccountMoneyExtend).where(AccountMoneyExtend.account_id == case.account))
    before = durable(case)
    assert len(before[CasdoorSyncIntentExtend.__tablename__]) == 1
    if fault == "before-consume":
        monkeypatch.setattr(CasdoorInvitationFinalizationService, "_finalize_invited_login", original)
    elif fault == "after-consume":
        assert lost == ["consumed"]
        monkeypatch.setattr(RedisInvitationTokenStore, "consume_versioned_invitation", original)
    else:
        assert lost == [True]
    second = new_attempt(case, bootstrap=bootstrap)
    response = finish(case, second)
    assert response.status_code == 302 and response.location.endswith("/apps/invited"), f.control.errors
    after = durable(case)
    assert after["generation"][0][1] == 1 and len(after["proofs"]) == 2
    assert all(after[k] == before[k] for k in ("credentials", "account_integrates"))
    if missing_quota:
        from models.account_money_extend import AccountMoneyExtend
        from configs import dify_config
        import sqlalchemy as sa
        with Session(f.local.engine) as session:
            quota = session.scalar(sa.select(AccountMoneyExtend).where(AccountMoneyExtend.account_id == case.account))
            assert quota.total_quota == dify_config.ACCOUNT_TOTAL_QUOTA and quota.used_quota == 0
    else:
        assert after["quota"] == before["quota"]
    if fault in ("d-ack", "f-ack"):
        assert after["generation"] == before["generation"]
        assert after["joins"] == before["joins"]
        assert all(row in after["proofs"] for row in before["proofs"])
    if fault == "f-ack":
        assert all(after[k] == before[k] for k in before if k != "quota")
    assert len([row for row in case.wire.calls if row[0] == adapters._INVITATION_CONSUME]) == 1
    assert second != first and f.control.created[-1].nonce != f.control.created[-2].nonce
    assert f.control.consumed[-1].code_verifier != f.control.consumed[-2].code_verifier
    assert all(cookie(case.m, _real_cookie_name(name)) for name in ("access_token", "refresh_token", "csrf_token"))
    requests = len(f.control.requests)
    assert finish(case, first).status_code == finish(case, second).status_code == 400
    assert len(f.control.requests) == requests


@pytest.mark.parametrize("changed", ["missing", "wrong", "lost-reply"])
def test_absent_bearer_requires_genuine_read_only_original_receipt(invited_resume_mounted, monkeypatch, changed):
    case, f = invited_resume_mounted, invited_resume_mounted.m.f
    original = RedisInvitationTokenStore.consume_versioned_invitation

    def consumed_without_reply(*args, **kwargs):
        original(*args, **kwargs)
        raise TimeoutError("synthetic consume reply lost")

    monkeypatch.setattr(RedisInvitationTokenStore, "consume_versioned_invitation", consumed_without_reply)
    finish(case, new_attempt(case))
    monkeypatch.setattr(RedisInvitationTokenStore, "consume_versioned_invitation", original)
    no_pair(case)
    assert case.wire.token_key not in case.wire.entries
    before = durable(case)
    receipt_key = next(iter(case.wire.entries))
    if changed == "missing":
        case.wire.entries.clear()
    elif changed == "wrong":
        case.wire.entries[receipt_key] = ("string", b"{}", 60000)
    else:
        case.p3j_wire.fail_reply = True
    finish(case, new_attempt(case, bootstrap=False))
    no_pair(case)
    assert durable(case) == before and not f.control.tokens
    assert len([row for row in case.wire.calls if row[0] == adapters._INVITATION_CONSUME]) == 1
    assert len([frame for frame in case.p3j_wire.frames if frame[0] == b"EVAL"]) == 1


@pytest.mark.parametrize("changed", ["account", "quota", "join", "intent", "lifecycle"])
def test_original_p3l_root_rejects_full_row_trigger_delta_then_new_auth_reconciles(invited_resume_mounted, monkeypatch, changed):
    from models.account import Account, TenantAccountJoin
    from models.account_money_extend import AccountMoneyExtend
    from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend
    import sqlalchemy as sa
    case, f = invited_resume_mounted, invited_resume_mounted.m.f
    original = CasdoorInvitationFinalizationService._finalize_invited_login
    monkeypatch.setattr(CasdoorInvitationFinalizationService, "_finalize_invited_login", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("synthetic before consume")))
    finish(case, new_attempt(case))
    monkeypatch.setattr(CasdoorInvitationFinalizationService, "_finalize_invited_login", original)
    statements = {
        "account": f"UPDATE {Account.__tablename__} SET password='synthetic trigger mutation' WHERE id='{case.account}'",
        "quota": f"UPDATE {AccountMoneyExtend.__tablename__} SET used_quota=used_quota+1 WHERE account_id='{case.account}'",
        "join": f"UPDATE {TenantAccountJoin.__tablename__} SET current=1 WHERE account_id='{case.account}'",
        "intent": f"UPDATE {CasdoorSyncIntentExtend.__tablename__} SET attempt_count=1 WHERE account_id='{case.account}'",
        "lifecycle": f"UPDATE {InvitationAuthorityLifecycleExtend.__tablename__} SET state='withdrawn' WHERE account_id='{case.account}'",
    }
    with f.local.engine.begin() as connection:
        connection.execute(sa.text(f"CREATE TRIGGER d18c_delta AFTER UPDATE OF operation_state ON {CasdoorSyncIntentExtend.__tablename__} WHEN NEW.operation_state='applied' BEGIN {statements[changed]}; END"))
    before = durable(case)
    finish(case, new_attempt(case, bootstrap=False))
    no_pair(case)
    assert durable(case) == before and not f.control.tokens
    with f.local.engine.begin() as connection:
        connection.execute(sa.text("DROP TRIGGER d18c_delta"))
    response = finish(case, new_attempt(case, bootstrap=False))
    assert response.status_code == 302 and response.location.endswith("/apps/invited"), f.control.errors
    assert len([row for row in case.wire.calls if row[0] == adapters._INVITATION_CONSUME]) == 1


@pytest.mark.parametrize("changed", ["missing-bearer", "expired-window"])
def test_pending_original_without_receipt_or_original_window_has_no_consume(invited_resume_mounted, monkeypatch, changed):
    from datetime import timedelta
    from repositories import casdoor_invited_login_scope_repository_extend as selector
    case, f = invited_resume_mounted, invited_resume_mounted.m.f
    original = CasdoorInvitationFinalizationService._finalize_invited_login
    monkeypatch.setattr(CasdoorInvitationFinalizationService, "_finalize_invited_login", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("synthetic before consume")))
    finish(case, new_attempt(case))
    monkeypatch.setattr(CasdoorInvitationFinalizationService, "_finalize_invited_login", original)
    if changed == "missing-bearer":
        case.wire.entries.clear()
    else:
        future = selector._now() + timedelta(days=8)
        monkeypatch.setattr(selector, "_now", lambda: future)
    before = durable(case)
    finish(case, new_attempt(case, bootstrap=False))
    no_pair(case)
    assert durable(case) == before and not f.control.tokens
    assert not [row for row in case.wire.calls if row[0] == adapters._INVITATION_CONSUME]


def test_resume_unknown_actual_consume_uses_one_read_only_reconciliation(invited_resume_mounted, monkeypatch):
    case, f = invited_resume_mounted, invited_resume_mounted.m.f
    original = CasdoorInvitationFinalizationService._finalize_invited_login
    monkeypatch.setattr(CasdoorInvitationFinalizationService, "_finalize_invited_login", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("synthetic before consume")))
    finish(case, new_attempt(case))
    monkeypatch.setattr(CasdoorInvitationFinalizationService, "_finalize_invited_login", original)
    case.wire.fail_after = True
    response = finish(case, new_attempt(case, bootstrap=False))
    assert response.status_code == 302 and response.location.endswith("/apps/invited"), f.control.errors
    assert len([row for row in case.wire.calls if row[0] == adapters._INVITATION_CONSUME]) == 1
    assert len([frame for frame in case.p3j_wire.frames if frame[0] == b"EVAL"]) == 1
    assert len(durable(case)["proofs"]) == 2
