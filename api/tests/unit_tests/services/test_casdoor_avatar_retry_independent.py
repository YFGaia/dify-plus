"""Independent adversarial proof for cross-account avatar retry scope."""

import hashlib
from uuid import uuid4

import pytest
import sqlalchemy as sa
from models.account import Account
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from repositories.casdoor_avatar_repository_extend import (
    CasdoorAvatarConflict,
    CasdoorAvatarRepository,
)
from sqlalchemy.orm import Session
from test_casdoor_avatar_consumer_extend import row
from test_casdoor_avatar_pre_storage_recovery_extend import produce
from test_casdoor_avatar_retry_flow_extend import (
    avatar_fixture as original_avatar_fixture,
)
from test_casdoor_avatar_retry_flow_extend import consumer as original_consumer
from test_casdoor_avatar_retry_flow_extend import (
    storage_fixture as original_storage_fixture,
)
from test_casdoor_avatar_retry_mounted_extend import mounted as original_mounted
from test_casdoor_avatar_retry_mounted_extend import registered as original_registered

avatar_fixture = original_avatar_fixture
consumer = original_consumer
storage_fixture = original_storage_fixture
mounted = original_mounted
registered = original_registered


def test_registered_post_rejects_fk_valid_cross_account_history(mounted):
    s = mounted
    produce(s)
    foreign_account_id = str(uuid4())
    foreign_identity_id = str(uuid4())
    with s.maker() as session, session.begin():
        source_account = session.get(Account, s.account.id)
        account_values = {
            column.name: getattr(source_account, column.name)
            for column in Account.__table__.columns
        }
        account_values.update(id=foreign_account_id, email=f"{foreign_account_id}@example.test")
        foreign_account = Account(name=account_values["name"], email=account_values["email"])
        for name, value in account_values.items():
            setattr(foreign_account, name, value)
        session.add(foreign_account)
        source_identity = session.scalar(sa.select(Identity).where(Identity.account_id == s.account.id))
        identity_values = {
            column.name: getattr(source_identity, column.name)
            for column in Identity.__table__.columns
        }
        identity_values.update(
            id=foreign_identity_id,
            account_id=foreign_account_id,
            subject=f"independent-{foreign_identity_id}",
            subject_digest=hashlib.sha256(
                f"independent-{foreign_identity_id}".encode()
            ).hexdigest(),
        )
        session.add(Identity(**identity_values))
        session.flush()
        session.add(
            History(
                namespace_id=source_identity.namespace_id,
                identity_id=foreign_identity_id,
                account_id=s.account.id,
                workspace_id=s.revision.default_workspace_id,
                revision_id=s.revision.id,
                ownership="managed",
                source="mapping",
                ownership_epoch=0,
                desired_generation=1,
                finalization="finalized",
                tombstone=False,
                last_applied_roles_json="[]",
                desired_roles_json="[]",
                baseline_json="{}",
            )
        )

    with Session(s.session.get_bind()) as session:
        assert session.scalar(sa.text("PRAGMA foreign_keys")) == 1
        assert session.get(Account, foreign_account_id) is not None
        assert session.get(Identity, foreign_identity_id) is not None
        assert session.scalar(
            sa.select(sa.func.count())
            .select_from(History)
            .where(
                History.account_id == s.account.id,
                History.identity_id == foreign_identity_id,
            )
        ) == 1

    before = row(s)
    with s.maker() as session, session.begin():
        repository = CasdoorAvatarRepository(
            session,
            configuration_repository=s.consumer._configuration_service._repository(session),
        )
        with pytest.raises(CasdoorAvatarConflict) as conflict:
            repository.inspect_retry_target(s.intent_id, now=s.utc)
        assert conflict.traceback[-1].name == "_retry_scope_values"
    response = s.send(
        "/console/api/system-manage-extend/integration/casdoor/sync/retry",
        method="POST",
        json={"intent_id": str(s.intent_id)},
    )
    assert row(s) == before
    assert not s.calls and not s.published
    with Session(s.session.get_bind()) as session:
        assert session.scalar(
            sa.select(sa.func.count())
            .select_from(Audit)
            .where(Audit.action.in_(("avatar_retry", "avatar_retry_claim")))
        ) == 0
    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.json["code"] == "invalid_transaction"
    assert response.headers["Cache-Control"] == "no-store"
