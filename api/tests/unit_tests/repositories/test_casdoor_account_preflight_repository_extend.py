"""Actual SQLite observations and offline lock ordering, not concurrency proof."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.admission import AdmissionContext
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
from models.casdoor_extend import (
    CasdoorNamespaceLifecycle,
)
from repositories.casdoor_account_preflight_repository_extend import (
    MAX_COLLISION_ACCOUNTS,
    AccountCollisionUnknown,
    AccountPreflightConflict,
    CasdoorAccountPreflightRepository,
    CollisionKnowledge,
)
from repositories.casdoor_identity_repository_extend import VerifiedIdentityKey
from sqlalchemy.orm import Session


@pytest.fixture
def db(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'preflight.sqlite'}")
    for model in (Account, Integration, Namespace, Revision, Identity):
        model.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as session:
        integration = Integration(enabled=True)
        account = Account(name="Local", email="local@example.com", initialized_at=datetime(2020, 1, 1))
        session.add_all([integration, account])
        session.flush()
        chain = dict(
            expected_issuer="https://issuer.example.test", organization="Org", application="App", client_id="Client"
        )
        namespace = Namespace(integration_id=integration.id, core_fingerprint="b" * 64, **chain)
        session.add(namespace)
        session.flush()
        revision = Revision(
            integration_id=integration.id,
            namespace_id=namespace.id,
            revision_number=1,
            config_digest="a" * 64,
            browser_frontend_url="https://issuer.example.test",
            backend_api_url="https://issuer.example.test",
            button_text="SSO",
            default_workspace_id=str(uuid4()),
            certificates_json="[]",
            policy_json="{}",
            mappings_json="[]",
            **chain,
        )
        identity = Identity(
            namespace_id=namespace.id,
            account_id=account.id,
            issuer=chain["expected_issuer"],
            organization="Org",
            subject="Subject",
            last_applied_json="{}",
            profile_sync_json="{}",
        )
        session.add_all([revision, identity])
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
        yield session, context, key, account, identity
    engine.dispose()


def run(db, **kwargs):
    session, context, key, *_ = db
    return CasdoorAccountPreflightRepository(session).reconstruct(context, key, **kwargs)


def unbind(db):
    session, _, _, _, identity = db
    session.execute(sa.delete(Identity).where(Identity.id == identity.id))
    session.commit()


def test_exact_binding_changed_email_skips_scan_and_has_no_writes(db, monkeypatch):
    session, context, _, account, _ = db
    statements = []
    sa.event.listen(session.bind, "before_cursor_execute", lambda c, u, s, p, x, e: statements.append(s))

    def forbidden(*args, **kwargs):
        pytest.fail("repository attempted write or shared preparation")

    with session.begin():
        with monkeypatch.context() as patch:
            for name in ("flush", "commit", "rollback", "begin_nested"):
                patch.setattr(session, name, forbidden)
            patch.setattr(CasdoorAccountPreflightRepository, "_collisions", forbidden)
            result = run(db, collision_email="someone.else+alias@googlemail.com")
        assert result.context == context
        assert result.account.email == account.email
        assert result.exact_binding.account_id == UUID(account.id)
        assert result.collision_knowledge is CollisionKnowledge.UNCHECKED
        assert result.collision_account_ids == ()
        assert not session.new and not session.dirty and not session.deleted
        with pytest.raises(FrozenInstanceError):
            result.candidate_absent = True
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)


@pytest.mark.parametrize(
    "model,field,value",
    [
        (Integration, "enabled", False),
        (Integration, "active_revision_id", None),
        (Integration, "active_revision_id", str(uuid4())),
        (Namespace, "integration_id", str(uuid4())),
        (Namespace, "expected_issuer", "https://other.test"),
        (Namespace, "organization", "Other"),
        (Namespace, "application", "Other"),
        (Namespace, "client_id", "Other"),
        (Namespace, "lifecycle", "fencing"),
        (Namespace, "fence_epoch", 1),
        (Revision, "namespace_id", str(uuid4())),
        (Revision, "integration_id", str(uuid4())),
        (Revision, "config_digest", "b" * 64),
        (Revision, "expected_issuer", "https://other.test"),
        (Revision, "organization", "Other"),
        (Revision, "application", "Other"),
        (Revision, "client_id", "Other"),
    ],
)
def test_current_owner_chain_mismatch_denies(db, model, field, value):
    session = db[0]
    session.execute(sa.update(model).values({field: value}))
    session.commit()
    with session.begin(), pytest.raises(AccountPreflightConflict):
        run(db)


def test_draft_changes_do_not_invalidate_active_revision(db):
    session = db[0]
    session.execute(sa.update(Integration).values(etag=100, draft_revision_id=str(uuid4())))
    session.commit()
    with session.begin():
        assert run(db).exact_binding is not None


@pytest.mark.parametrize("status", list(AccountStatus))
@pytest.mark.parametrize("marker", [None, datetime(2020, 1, 1)])
def test_status_and_marker_are_independent_fresh_observations(db, status, marker):
    session, _, _, account, _ = db
    # A separately committed writer leaves this Session's identity map stale.
    with Session(session.bind) as writer:
        writer.execute(sa.update(Account).values(status=status, initialized_at=marker, email="fresh@example.com"))
        writer.commit()
    assert account.email == "local@example.com"
    with session.begin():
        if status in (AccountStatus.BANNED, AccountStatus.CLOSED):
            with pytest.raises(AccountPreflightConflict):
                run(db)
        else:
            result = run(db)
            assert (result.account.status, result.account.initialized_at, result.account.email) == (
                status,
                marker,
                "fresh@example.com",
            )
            assert account.name == "Local"
            assert account.email == "local@example.com"  # scalar projections never assign ORM fields


@pytest.mark.parametrize(
    "raw,stored,target",
    [
        ("CASE@EXAMPLE.COM", None, "case@example.com"),
        ("ÜSER@EXAMPLE.COM", None, "üser@example.com"),
        ("ÜSER@EXAMPLE.COM", "wrong@example.com", "üser@example.com"),
        ("USER@ÜDOMAIN.COM", None, "user@üdomain.com"),
        ("First.Last+tag@googlemail.com", None, "firstlast@gmail.com"),
        ("First.Last+tag@gmail.com", "old@example.com", "firstlast@gmail.com"),
        ("other@example.com", "target@example.com", "target@example.com"),
    ],
)
def test_full_python_owner_collision_scan(db, raw, stored, target):
    session, _, _, account, _ = db
    unbind(db)
    session.execute(sa.update(Account).values(email=raw, normalized_email=stored))
    session.commit()
    with session.begin():
        result = run(db, collision_email=target)
        assert result.account is None  # collisions never select an account
        assert result.collision_knowledge is CollisionKnowledge.COMPLETE
        assert result.collision_account_ids == (UUID(account.id),)


def test_sqlite_lower_counterexample_and_duplicate_preservation(db):
    session, _, _, account, _ = db
    unbind(db)
    session.execute(sa.update(Account).values(email="ÜSER@EXAMPLE.COM"))
    duplicate = Account(name="Duplicate", email="üser@example.com")
    session.add(duplicate)
    session.commit()
    with session.begin():
        assert session.scalar(sa.select(sa.func.lower("ÜSER@EXAMPLE.COM"))) != "üser@example.com"
        assert set(run(db, collision_email="üser@example.com").collision_account_ids) == {
            UUID(account.id),
            UUID(duplicate.id),
        }


def test_unchecked_complete_empty_and_selected_absence_are_distinct(db):
    session = db[0]
    unbind(db)
    candidate = uuid4()
    with session.begin():
        unchecked = run(db, candidate_account_id=candidate)
        assert unchecked.candidate_absent and unchecked.account is None
        assert unchecked.collision_knowledge is CollisionKnowledge.UNCHECKED
        complete = run(db, collision_email="absent@example.com")
        assert complete.collision_knowledge is CollisionKnowledge.COMPLETE
        assert not complete.candidate_absent and complete.collision_account_ids == ()


def test_whole_table_sentinel_is_unknown_even_with_no_matching_domain(db):
    session = db[0]
    unbind(db)
    session.add_all(
        Account(name="Legacy", email=f"legacy{i}@unrelated.test") for i in range(MAX_COLLISION_ACCOUNTS - 1)
    )
    session.commit()
    with session.begin():
        assert run(db, collision_email="absent@example.com").collision_knowledge is CollisionKnowledge.COMPLETE
    session.add(Account(name="Sentinel", email="sentinel@unrelated.test"))
    session.commit()
    with session.begin(), pytest.raises(AccountCollisionUnknown, match="^authorization_pending$"):
        run(db, collision_email="absent@example.com")


@pytest.mark.parametrize(
    "defect", ["raw_subject", "issuer", "organization", "missing_account", "candidate_conflict", "reverse"]
)
def test_identity_inconsistency_denies(db, defect):
    session, context, key, account, identity = db
    kwargs = {}
    if defect == "candidate_conflict":
        kwargs["candidate_account_id"] = uuid4()
    elif defect == "missing_account":
        session.execute(sa.delete(Account))
    elif defect == "reverse":
        session.execute(sa.update(Identity).values(subject="Other", subject_digest="b" * 64))
        kwargs["candidate_account_id"] = UUID(account.id)
    else:
        field = "subject" if defect == "raw_subject" else defect
        session.execute(sa.update(Identity).values({field: "Other"}))
    session.commit()
    with session.begin(), pytest.raises(AccountPreflightConflict):
        run(db, **kwargs)


def test_account_locks_precede_final_identity_and_discovery_race_denies(db, monkeypatch):
    session, _, _, account, _ = db
    observed = []
    original = session.execute

    def execute(statement, *args, **kwargs):
        observed.append((str(statement), statement._for_update_arg is not None))
        return original(statement, *args, **kwargs)

    monkeypatch.setattr(session, "execute", execute)
    with session.begin():
        run(db)
    tables = [(sql.split("FROM ")[1].split()[0], locked) for sql, locked in observed]
    assert tables == [
        ("casdoor_integration_extend", True),
        ("casdoor_namespace_extend", True),
        ("casdoor_config_revision_extend", False),
        ("casdoor_identity_extend", False),
        ("accounts", True),
        ("casdoor_identity_extend", True),
        ("casdoor_identity_extend", True),
    ]
    original_subject = CasdoorAccountPreflightRepository._subject

    def changed(self, key, *, lock):
        result = original_subject(self, key, lock=lock)
        return None if lock else result

    monkeypatch.setattr(CasdoorAccountPreflightRepository, "_subject", changed)
    with session.begin(), pytest.raises(AccountPreflightConflict):
        run(db)


@pytest.mark.parametrize("defect", ["missing_root", "nested", "new", "dirty", "deleted", "failed"])
def test_session_gates_emit_zero_sql(db, defect):
    session, _, _, account, _ = db
    if defect != "missing_root":
        session.begin()
    if defect == "nested":
        session.begin_nested()
    elif defect == "new":
        session.add(Account(name="New", email="new@example.com"))
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
    with pytest.raises(AccountPreflightConflict):
        run(db)
    assert statements == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("integration_id", "uuid"),
        ("revision_id", "uuid"),
        ("active_revision_id", uuid4()),
        ("namespace_id", "uuid"),
        ("fence_epoch", True),
        ("fence_epoch", -1),
        ("fence_epoch", 2**63),
        ("namespace_lifecycle", "active"),
        ("config_digest", "invalid"),
        ("issuer", "x" * 2049),
        ("subject", "\ud800"),
        ("organization", "\nOrg"),
        ("application", ""),
        ("client_id", " "),
    ],
)
def test_malformed_context_rejected_before_sql(db, field, value):
    session, context, key, *_ = db
    statements = []
    sa.event.listen(session.bind, "before_cursor_execute", lambda *args: statements.append(args[2]))
    with session.begin(), pytest.raises(AccountPreflightConflict):
        CasdoorAccountPreflightRepository(session).reconstruct(replace(context, **{field: value}), key)
    assert statements == []


@pytest.mark.parametrize(
    "kind", ["context", "key", "candidate", "email", "email_surrogate", "email_long", "email_invalid"]
)
def test_other_invalid_shapes_rejected_before_sql(db, kind):
    session, context, key, *_ = db
    kwargs = {}
    if kind == "context":
        context = object.__new__(AdmissionContext)
    elif kind == "key":
        key = object.__new__(VerifiedIdentityKey)
    elif kind == "candidate":
        kwargs["candidate_account_id"] = "uuid"
    else:
        kwargs["collision_email"] = {
            "email": 42,
            "email_surrogate": "\ud800",
            "email_long": "a" * 255,
            "email_invalid": "bad",
        }[kind]
    statements = []
    sa.event.listen(session.bind, "before_cursor_execute", lambda *args: statements.append(args[2]))
    with session.begin(), pytest.raises(AccountPreflightConflict):
        CasdoorAccountPreflightRepository(session).reconstruct(context, key, **kwargs)
    assert statements == []


def test_unknown_database_status_has_stable_denial(db):
    session = db[0]
    session.execute(sa.text("UPDATE accounts SET status = 'unknown'"))
    session.commit()
    with session.begin(), pytest.raises(AccountPreflightConflict, match="^identity_conflict$"):
        run(db)


def test_corrupt_duplicate_reverse_binding_denies(db):
    session, context, _, account, _ = db
    # Deliberately remove identity uniqueness only in this corruption fixture.
    session.execute(sa.text("CREATE TABLE identity_copy AS SELECT * FROM casdoor_identity_extend"))
    session.execute(sa.text("DROP TABLE casdoor_identity_extend"))
    session.execute(sa.text("ALTER TABLE identity_copy RENAME TO casdoor_identity_extend"))
    session.add(
        Identity(
            namespace_id=str(context.namespace_id),
            account_id=account.id,
            issuer=context.issuer,
            organization=context.organization,
            subject="Duplicate",
            last_applied_json="{}",
            profile_sync_json="{}",
        )
    )
    session.commit()
    with session.begin(), pytest.raises(AccountPreflightConflict):
        run(db)


def test_existing_unbound_candidate_remains_selector_only(db):
    session, _, _, account, _ = db
    unbind(db)
    with session.begin():
        result = run(db, candidate_account_id=UUID(account.id))
        assert result.account.account_id == UUID(account.id)
        assert result.account.namespace_subject is None
        assert result.exact_binding is None
        assert result.collision_knowledge is CollisionKnowledge.UNCHECKED


@pytest.mark.parametrize(
    "value",
    [
        None,
        42,
        b"private-malformed-account",
        "private-malformed-account",
        "a" * 10000,
        "ffffffffffffffffffffffffffffffff",
        "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF",
        "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        "\ud800" * 36,
    ],
)
def test_persisted_uuid_helper_has_bounded_stable_denial(value):
    from repositories.casdoor_account_preflight_repository_extend import _persisted_uuid

    with pytest.raises(AccountPreflightConflict, match="^identity_conflict$") as caught:
        _persisted_uuid(value)
    assert caught.value.__suppress_context__ or caught.value.__context__ is None


def test_persisted_uuid_helper_preserves_canonical_id():
    from repositories.casdoor_account_preflight_repository_extend import _persisted_uuid

    value = uuid4()
    assert _persisted_uuid(str(value)) == value


@pytest.mark.parametrize("value", ["private-malformed-account", "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF"])
@pytest.mark.parametrize("site", ["discovery", "collision"])
def test_corrupt_stored_account_ids_reject_without_echo(db, value, site):
    session = db[0]
    kwargs = {}
    if site == "discovery":
        session.execute(sa.update(Identity).values(account_id=value))
    else:
        unbind(db)
        session.execute(sa.update(Account).values(id=value))
        kwargs["collision_email"] = "local@example.com"
    session.commit()
    with session.begin(), pytest.raises(AccountPreflightConflict, match="^identity_conflict$") as caught:
        run(db, **kwargs)
    assert value not in str(caught.value)
