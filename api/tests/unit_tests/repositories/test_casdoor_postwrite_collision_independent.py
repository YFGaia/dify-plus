"""Independent actual-SQLite checks for the postwrite read seam."""

from datetime import datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.admission import AdmissionContext
from models.account import Account, AccountStatus
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend as Revision,
    CasdoorIdentityExtend as Identity,
    CasdoorIntegrationExtend as Integration,
    CasdoorNamespaceExtend as Namespace,
    CasdoorNamespaceLifecycle,
)
from repositories.casdoor_account_preflight_repository_extend import (
    MAX_COLLISION_ACCOUNTS,
    AccountPreflightConflict,
    CasdoorAccountPreflightRepository,
    CollisionKnowledge,
)
from repositories.casdoor_identity_repository_extend import CasdoorIdentityRepository, VerifiedIdentityKey
from services.account_email import normalize_email
from sqlalchemy.orm import Session


@pytest.fixture
def world(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'postwrite-independent.sqlite'}")
    for model in (Account, Integration, Namespace, Revision, Identity):
        model.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as session:
        chain = dict(
            expected_issuer="https://issuer.example.test", organization="Org", application="App", client_id="Client"
        )
        integration = Integration(enabled=True)
        session.add(integration)
        session.flush()
        namespace = Namespace(integration_id=integration.id, core_fingerprint="b" * 64, **chain)
        session.add(namespace)
        session.flush()
        revision = Revision(
            integration_id=integration.id,
            namespace_id=namespace.id,
            revision_number=1,
            config_digest="a" * 64,
            browser_frontend_url=chain["expected_issuer"],
            backend_api_url=chain["expected_issuer"],
            button_text="SSO",
            default_workspace_id=str(uuid4()),
            certificates_json="[]",
            policy_json="{}",
            mappings_json="[]",
            **chain,
        )
        session.add(revision)
        session.flush()
        integration.active_revision_id = revision.id
        session.commit()
        context = AdmissionContext(
            UUID(integration.id),
            UUID(revision.id),
            UUID(revision.id),
            UUID(namespace.id),
            CasdoorNamespaceLifecycle.ACTIVE,
            0,
            "a" * 64,
            chain["expected_issuer"],
            "Org",
            "App",
            "Client",
            "Subject",
        )
        key = VerifiedIdentityKey(context.namespace_id, context.issuer, context.organization, context.subject)
        yield session, context, key
    engine.dispose()


def create_bound(world, email="local@example.com", own_id=None):
    session, _, key = world
    account = Account(
        name="Created",
        email=email,
        normalized_email=normalize_email(email),
        status=AccountStatus.ACTIVE,
        initialized_at=datetime(2020, 1, 1),
    )
    account.id = str(own_id or uuid4())
    session.add(account)
    session.flush()
    CasdoorIdentityRepository(session).bind(key, account_id=UUID(account.id), expected_fence_epoch=0)
    session.flush()
    return account


def observe(world, account):
    session, context, key = world
    return CasdoorAccountPreflightRepository(session)._observe_postwrite_new_collisions(
        context, key, own_new_account_id=UUID(account.id), collision_email=account.email
    )


@pytest.mark.parametrize(
    ("target", "other_raw"),
    [
        ("üser@example.com", "ÜSER@EXAMPLE.COM"),
        ("firstlast@gmail.com", "First.Last+tag@googlemail.com"),
    ],
)
def test_postwrite_scan_finds_alias_admitted_after_prewrite_and_real_bind(world, target, other_raw):
    session, context, key = world
    with session.begin():
        before = CasdoorAccountPreflightRepository(session).reconstruct(context, key, collision_email=target)
        assert before.collision_knowledge is CollisionKnowledge.COMPLETE
        own = create_bound(world, target)
        # An older writer admitted the alias after the prewrite check. It is
        # independently committed nowhere; this tests same-transaction read composition.
        alias = Account(name="Legacy", email=other_raw)
        session.add(alias)
        session.flush()
        result = observe(world, own)
        assert result.collision_knowledge is CollisionKnowledge.COMPLETE
        assert result.collision_account_ids == (UUID(alias.id),)
        ordinary = CasdoorAccountPreflightRepository(session).reconstruct(context, key, collision_email=own.email)
        assert ordinary.collision_knowledge is CollisionKnowledge.UNCHECKED
        assert ordinary.collision_account_ids == ()


def test_postwrite_sql_exclusion_keeps_2048_other_rows_and_sorted_tail(world):
    session = world[0]
    with session.begin():
        session.execute(
            sa.insert(Account),
            [
                {"id": str(UUID(int=index + 2)), "name": "Legacy", "email": f"old{index}@example.com"}
                for index in range(MAX_COLLISION_ACCOUNTS)
            ],
        )
        tail_id = UUID(int=MAX_COLLISION_ACCOUNTS + 1)
        session.execute(sa.update(Account).where(Account.id == str(tail_id)).values(email="LOCAL@EXAMPLE.COM"))
        own = create_bound(world, own_id=UUID(int=1))
        sql = []
        sa.event.listen(session.bind, "before_cursor_execute", lambda *args: sql.append(args[2]))
        result = observe(world, own)
        assert result.collision_knowledge is CollisionKnowledge.COMPLETE
        assert result.collision_account_ids == (tail_id,)
        pool = next(statement for statement in sql if "accounts.id !=" in statement)
        assert pool.index("accounts.id !=") < pool.index("ORDER BY") < pool.index("LIMIT")
        assert "lower(" not in pool.lower()


def test_historical_bound_row_passes_selector_but_email_drift_fails_closed_without_dml(world):
    session = world[0]
    own = create_bound(world)
    session.commit()
    # This is equivalent old persisted state. The private selector authenticates
    # only current consistency and therefore cannot establish NEW history.
    with session.begin():
        result = observe(world, own)
        assert result.collision_knowledge is CollisionKnowledge.COMPLETE
    session.execute(sa.update(Account).where(Account.id == own.id).values(normalized_email="drift@example.com"))
    session.commit()
    with session.begin():
        sql = []
        sa.event.listen(session.bind, "before_cursor_execute", lambda *args: sql.append(args[2]))
        with pytest.raises(AccountPreflightConflict, match="^identity_conflict$"):
            observe(world, own)
        assert not any("accounts.id !=" in statement for statement in sql)
