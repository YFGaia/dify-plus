"""Independent D18-C post-commit boundary check; no live provider claim."""

import sqlalchemy as sa
from sqlalchemy.orm import Session

from models.account import AccountIntegrate
from models.account_money_extend import AccountMoneyExtend
from models.casdoor_extend import CasdoorAuditExtend
from services.casdoor_invitation_finalization_service_extend import CasdoorInvitationFinalizationService
from test_casdoor_invited_local_recovery_http_extend import adapters, durable, finish, new_attempt, no_pair

pytest_plugins = ("test_casdoor_invited_local_resume_http_extend",)


def test_committed_b3_trigger_delta_denies_pair_without_same_authorization_retry(
    invited_resume_mounted, monkeypatch
):
    case, f = invited_resume_mounted, invited_resume_mounted.m.f
    original = CasdoorInvitationFinalizationService._finalize_invited_login

    def interrupt_before_consume(*_args, **_kwargs):
        raise TimeoutError("synthetic stop before the original consume")

    monkeypatch.setattr(CasdoorInvitationFinalizationService, "_finalize_invited_login", interrupt_before_consume)
    first = new_attempt(case)
    finish(case, first)
    no_pair(case)
    monkeypatch.setattr(CasdoorInvitationFinalizationService, "_finalize_invited_login", original)

    with f.local.engine.begin() as connection:
        connection.exec_driver_sql(
            f"""CREATE TRIGGER d18c_external_b3_delta AFTER INSERT ON {CasdoorAuditExtend.__tablename__}
            WHEN NEW.action = 'invited_local_membership_write'
            BEGIN
              UPDATE {AccountMoneyExtend.__tablename__}
                SET used_quota = used_quota + 1 WHERE account_id = '{case.account}';
              UPDATE {AccountIntegrate.__tablename__}
                SET encrypted_token = 'external-trigger-delta' WHERE account_id = '{case.account}';
            END"""
        )

    before = durable(case)
    second = new_attempt(case, bootstrap=False)
    rejected = finish(case, second)
    assert rejected.status_code != 302
    no_pair(case)
    assert not f.control.tokens

    after = durable(case)
    assert len(after["proofs"]) == len(before["proofs"]) + 1
    assert after["generation"][0][1] == before["generation"][0][1] + 1
    assert after["joins"] != before["joins"]
    assert after["quota"] != before["quota"]
    assert after["account_integrates"] != before["account_integrates"]
    consume_calls = len([row for row in case.wire.calls if row[0] == adapters._INVITATION_CONSUME])
    assert consume_calls == 1
    with Session(f.local.engine) as session:
        actions = session.scalars(
            sa.select(CasdoorAuditExtend.action).where(
                CasdoorAuditExtend.action.in_(
                    ("invited_local_membership_write", "invited_local_membership_finalization")
                )
            )
        ).all()
        assert actions == ["invited_local_membership_write"]
        assert "invited_local_membership_finalization" not in actions
        assert session.scalar(
            sa.select(AccountMoneyExtend.used_quota).where(AccountMoneyExtend.account_id == case.account)
        ) == 1
        assert session.scalar(
            sa.select(AccountIntegrate.encrypted_token).where(AccountIntegrate.account_id == case.account)
        ) == "external-trigger-delta"

    assert finish(case, second).status_code == 400
    no_pair(case)
    assert len(durable(case)["proofs"]) == len(after["proofs"])
    assert len([row for row in case.wire.calls if row[0] == adapters._INVITATION_CONSUME]) == consume_calls
    with f.local.engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER d18c_external_b3_delta")
