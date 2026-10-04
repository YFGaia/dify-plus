"""Postwrite observation composition; SQLite is not lease/isolation proof."""

from dataclasses import replace
from datetime import datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from models.account import Account, AccountStatus
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend as Revision,
)
from models.casdoor_extend import (
    CasdoorIdentityExtend as Identity,
)
from models.casdoor_extend import (
    CasdoorIntegrationExtend as Integration,
)
from models.casdoor_extend import (
    CasdoorNamespaceExtend as Namespace,
)
from repositories.casdoor_account_preflight_repository_extend import (
    MAX_COLLISION_ACCOUNTS,
    AccountCollisionUnknown,
    AccountPreflightConflict,
    CasdoorAccountPreflightRepository,
    CollisionKnowledge,
)
from repositories.casdoor_identity_repository_extend import CasdoorIdentityRepository
from services.account_email import normalize_email
from sqlalchemy.orm import Session
from test_casdoor_account_preflight_repository_extend import db as _author_db


@pytest.fixture
def db(tmp_path):
    for session, context, key, *_ in _author_db.__wrapped__(tmp_path):
        session.execute(sa.delete(Identity))
        session.execute(sa.delete(Account))
        session.commit()
        yield session, context, key


def create_bound(db, email="local@example.com", account_id=None):
    session, _, key = db
    account = Account(
        name="New",
        email=email,
        normalized_email=normalize_email(email),
        status=AccountStatus.ACTIVE,
        initialized_at=datetime(2020, 1, 1),
    )
    account.id = str(account_id or uuid4())
    session.add(account)
    session.flush()
    CasdoorIdentityRepository(session).bind(key, account_id=UUID(account.id), expected_fence_epoch=0)
    session.flush()
    return account


def observe(db, account, **kwargs):
    session, context, key = db
    arguments = {"own_new_account_id": UUID(account.id), "collision_email": account.email}
    arguments.update(kwargs)
    return CasdoorAccountPreflightRepository(session)._observe_postwrite_new_collisions(context, key, **arguments)


def test_actual_bind_complete_and_ordinary_bound_skip_without_writes(db, monkeypatch):
    session, context, key = db
    with session.begin():
        account = create_bound(db)
        statements = []
        sa.event.listen(session.bind, "before_cursor_execute", lambda *args: statements.append(args[2]))

        def forbidden(*args, **kwargs):
            pytest.fail("observation attempted a write or transaction lifetime change")

        with monkeypatch.context() as patch:
            patch.setattr(Session, "__init__", forbidden)
            patch.setattr(Account, "__setattr__", forbidden)
            patch.setattr(Identity, "__setattr__", forbidden)
            for name in ("add", "add_all", "flush", "commit", "rollback", "begin", "begin_nested"):
                patch.setattr(session, name, forbidden)
            result = observe(db, account)
            assert result.collision_knowledge is CollisionKnowledge.COMPLETE
            assert result.collision_account_ids == ()
            assert result.account.account_id == UUID(account.id)
            assert result.exact_binding.account_id == UUID(account.id)
            assert result.context == context
            assert not session.new
            assert not session.dirty
            assert not session.deleted
            private_statements = statements.copy()
            patch.setattr(CasdoorAccountPreflightRepository, "_collisions", forbidden)
            ordinary = CasdoorAccountPreflightRepository(session).reconstruct(
                context, key, collision_email=account.email
            )
            assert ordinary.collision_knowledge is CollisionKnowledge.UNCHECKED
        assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
        account_queries = [sql for sql in private_statements if "FROM accounts" in sql]
        assert len(account_queries) == 2  # exactly one selected-row read plus one thin pool
        assert "accounts.normalized_email" in account_queries[0]
        assert "accounts.id =" in account_queries[0]
        pool = account_queries[1]
        assert pool.index("accounts.id !=") < pool.index("ORDER BY") < pool.index("LIMIT")
        assert "lower(" not in pool.lower()


@pytest.mark.parametrize(
    ("target", "raw", "stored"),
    [
        ("üser@example.com", "ÜSER@EXAMPLE.COM", None),
        ("firstlast@gmail.com", "First.Last+tag@googlemail.com", None),
        ("firstlast@gmail.com", "First.Last+tag@gmail.com", "obsolete@example.com"),
        ("local@example.com", "other@example.com", "local@example.com"),
        ("local@example.com", "local@example.com", "local@example.com"),
    ],
)
def test_legacy_collision_inserted_after_prewrite_is_seen_after_real_bind(db, target, raw, stored):
    session, context, key = db
    with session.begin():
        before = CasdoorAccountPreflightRepository(session).reconstruct(context, key, collision_email=target)
        assert before.collision_knowledge is CollisionKnowledge.COMPLETE
        assert before.collision_account_ids == ()
        account = create_bound(db, target)
        legacy = Account(name="Legacy older path", email=raw, normalized_email=stored)
        session.add(legacy)
        session.flush()
        result = observe(db, account)
        assert result.collision_account_ids == (UUID(legacy.id),)
        assert result.account.account_id == UUID(account.id)
        assert result.collision_knowledge is CollisionKnowledge.COMPLETE


@pytest.mark.parametrize("own_id", [UUID(int=1), UUID(int=2**128 - 1)])
@pytest.mark.parametrize("others", [MAX_COLLISION_ACCOUNTS, MAX_COLLISION_ACCOUNTS + 1])
def test_exclusion_before_limit_preserves_tail_and_other_account_capacity(db, own_id, others):
    session = db[0]
    with session.begin():
        session.execute(
            sa.insert(Account),
            [{"id": str(UUID(int=i + 2)), "name": "Legacy", "email": f"legacy{i}@example.com"} for i in range(others)],
        )
        tail = UUID(int=others + 1)
        session.execute(sa.update(Account).where(Account.id == str(tail)).values(email="LOCAL@EXAMPLE.COM"))
        account = create_bound(db, account_id=own_id)
        if others > MAX_COLLISION_ACCOUNTS:
            with pytest.raises(AccountCollisionUnknown, match="^authorization_pending$"):
                observe(db, account)
        else:
            result = observe(db, account)
            assert result.collision_knowledge is CollisionKnowledge.COMPLETE
            assert result.collision_account_ids == (tail,)


@pytest.mark.parametrize(
    "defect",
    [
        "wrong_uuid",
        "missing_account",
        "unbound",
        "other_subject",
        "bad_id",
        "raw_email",
        "normalized_email",
        "missing_normalized",
        "pending",
        "uninitialized",
        "banned",
        "closed",
        "missing_marker",
    ],
)
def test_own_row_and_binding_inconsistencies_have_stable_denial(db, defect):
    session = db[0]
    with session.begin():
        account = create_bound(db)
        kwargs = {}
        if defect == "wrong_uuid":
            kwargs["own_new_account_id"] = uuid4()
        elif defect == "missing_account":
            session.execute(sa.delete(Account))
        elif defect == "unbound":
            session.execute(sa.delete(Identity))
        elif defect == "other_subject":
            session.execute(sa.update(Identity).values(subject="Other", subject_digest="b" * 64))
        elif defect == "bad_id":
            session.execute(sa.update(Identity).values(account_id="private-malformed-id"))
        else:
            values = {
                "raw_email": {"email": "LOCAL@example.com"},
                "normalized_email": {"normalized_email": "wrong@example.com"},
                "missing_normalized": {"normalized_email": None},
                "missing_marker": {"initialized_at": None},
            }.get(defect, {"status": defect})
            session.execute(sa.update(Account).values(**values).execution_options(synchronize_session=False))
        with pytest.raises(AccountPreflightConflict, match="^identity_conflict$"):
            observe(db, account, **kwargs)


@pytest.mark.parametrize(
    ("model", "values"),
    [
        (Integration, {"enabled": False}),
        (Integration, {"active_revision_id": str(uuid4())}),
        (Namespace, {"fence_epoch": 1}),
        (Namespace, {"lifecycle": "fencing"}),
        (Namespace, {"application": "Other"}),
        (Revision, {"config_digest": "b" * 64}),
        (Revision, {"client_id": "Other"}),
    ],
)
def test_fresh_chain_guards_still_apply_postwrite(db, model, values):
    session = db[0]
    with session.begin():
        account = create_bound(db)
        session.execute(sa.update(model).values(**values))
        with pytest.raises(AccountPreflightConflict, match="^identity_conflict$"):
            observe(db, account)


@pytest.mark.parametrize("defect", ["missing_root", "nested", "new", "dirty", "deleted", "failed"])
def test_postwrite_session_gates_reject_before_sql(db, defect):
    session, context, key = db
    account = create_bound(db)
    own_id, email = UUID(account.id), account.email
    session.commit()
    if defect != "missing_root":
        session.begin()
    if defect == "nested":
        session.begin_nested()
    elif defect == "new":
        session.add(Account(name="Pending", email="pending@example.com"))
    elif defect == "dirty":
        account.name = "Dirty"
    elif defect == "deleted":
        session.delete(account)
    elif defect == "failed":
        session.add(Integration())
        with pytest.raises(sa.exc.IntegrityError):
            session.flush()
    statements = []
    sa.event.listen(session.bind, "before_cursor_execute", lambda *args: statements.append(args[2]))
    with pytest.raises(AccountPreflightConflict, match="^identity_conflict$"):
        CasdoorAccountPreflightRepository(session)._observe_postwrite_new_collisions(
            context, key, own_new_account_id=own_id, collision_email=email
        )
    assert statements == []


@pytest.mark.parametrize("value", [None, "uuid", 42])
def test_missing_or_malformed_own_selector_is_not_default_scan(db, value):
    session = db[0]
    with session.begin():
        account = create_bound(db)
        statements = []
        sa.event.listen(session.bind, "before_cursor_execute", lambda *args: statements.append(args[2]))
        with pytest.raises(AccountPreflightConflict, match="^identity_conflict$"):
            observe(db, account, own_new_account_id=value)
        assert statements == []


@pytest.mark.parametrize("value", [None, "bad", "\ud800", 42])
def test_missing_or_malformed_expected_email_is_zero_sql(db, value):
    session = db[0]
    with session.begin():
        account = create_bound(db)
        statements = []
        sa.event.listen(session.bind, "before_cursor_execute", lambda *args: statements.append(args[2]))
        with pytest.raises(AccountPreflightConflict, match="^identity_conflict$"):
            observe(db, account, collision_email=value)
        assert statements == []


def test_invalid_current_context_zero_sql(db):
    session, context, key = db
    with session.begin():
        account = create_bound(db)
        statements = []
        sa.event.listen(session.bind, "before_cursor_execute", lambda *args: statements.append(args[2]))
        with pytest.raises(AccountPreflightConflict, match="^identity_conflict$"):
            CasdoorAccountPreflightRepository(session)._observe_postwrite_new_collisions(
                replace(context, fence_epoch=True),
                key,
                own_new_account_id=UUID(account.id),
                collision_email=account.email,
            )
        assert statements == []


def test_equivalent_old_bound_state_passes_but_does_not_prove_new_provenance(db):
    session = db[0]
    account = create_bound(db)
    session.commit()
    with session.begin():
        result = observe(db, account)
        assert result.collision_knowledge is CollisionKnowledge.COMPLETE
        # The same observation is possible in a later transaction: callers must
        # authenticate actual same-attempt NEW provenance separately in F12-B.
        assert result.collision_account_ids == ()


def test_caller_rollback_removes_account_and_real_binding(db):
    session = db[0]
    account = create_bound(db)
    own_id = account.id
    assert observe(db, account).collision_knowledge is CollisionKnowledge.COMPLETE
    session.rollback()
    assert session.scalar(sa.select(Account.id).where(Account.id == own_id)) is None
    assert session.scalar(sa.select(Identity.id).where(Identity.account_id == own_id)) is None


@pytest.mark.parametrize("site", ["own", "pool"])
def test_normalizer_scalar_errors_have_fixed_boundary(db, monkeypatch, site):
    import repositories.casdoor_account_preflight_repository_extend as module

    session = db[0]
    with session.begin():
        account = create_bound(db)
        session.add(Account(name="Legacy", email="legacy@example.com"))
        session.flush()
        original = module.normalize_email

        def broken(value):
            if site == "own" or value == "legacy@example.com":
                raise ValueError("private scalar diagnostic")
            return original(value)

        monkeypatch.setattr(module, "normalize_email", broken)
        with pytest.raises(AccountPreflightConflict, match="^identity_conflict$"):
            observe(db, account)


def test_database_failure_never_becomes_complete_empty(db, monkeypatch):
    session = db[0]
    with session.begin():
        account = create_bound(db)
        original = session.execute

        def broken(statement, *args, **kwargs):
            if "accounts.id !=" in str(statement):
                raise sa.exc.OperationalError("SELECT", {}, RuntimeError("offline failure"))
            return original(statement, *args, **kwargs)

        monkeypatch.setattr(session, "execute", broken)
        with pytest.raises(sa.exc.OperationalError):
            observe(db, account)
