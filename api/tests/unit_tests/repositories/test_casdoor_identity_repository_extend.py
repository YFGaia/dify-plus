"""Real SQLite constraints/savepoints; not PG/MySQL concurrent acceptance."""

import hashlib
from dataclasses import FrozenInstanceError, replace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from models.account import Account, AccountIntegrate, AccountStatus
from models.casdoor_extend import CasdoorIdentityExtend, CasdoorIntegrationExtend, CasdoorNamespaceExtend
from repositories.casdoor_identity_repository_extend import (
    CasdoorIdentityConflict,
    CasdoorIdentityRepository,
    VerifiedIdentityKey,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@pytest.fixture
def storage():
    engine = sa.create_engine("sqlite://")

    @sa.event.listens_for(engine, "connect")
    def configure_sqlite(connection, _record):
        # Explicit BEGIN keeps released SAVEPOINT writes within the outer UoW.
        connection.isolation_level = None
        connection.execute("PRAGMA foreign_keys = ON")

    @sa.event.listens_for(engine, "begin")
    def begin_sqlite(connection):
        connection.exec_driver_sql("BEGIN")

    for model in (Account, AccountIntegrate, CasdoorIntegrationExtend, CasdoorNamespaceExtend, CasdoorIdentityExtend):
        model.__table__.create(engine)
    with Session(engine) as session:
        account = Account(name="Local", email="local@example.test", status=AccountStatus.PENDING)
        other = Account(name="Other", email="other@example.test")
        integration = CasdoorIntegrationExtend()
        session.add_all([account, other, integration])
        session.flush()
        namespace = CasdoorNamespaceExtend(
            integration_id=integration.id,
            expected_issuer="https://synthetic-idp.example",
            organization="SyntheticOrg",
            application="SyntheticApp",
            client_id="SyntheticClient",
            core_fingerprint="a" * 64,
        )
        session.add(namespace)
        session.flush()
        session.add(AccountIntegrate(account_id=account.id, provider="oauth2", open_id="legacy-id", encrypted_token=""))
        session.commit()
        key = VerifiedIdentityKey(UUID(namespace.id), namespace.expected_issuer, namespace.organization, "SubjectCase")
        yield session, key, UUID(account.id), UUID(other.id)
    engine.dispose()


def repository(storage):
    session, key, account_id, other_id = storage
    return session, CasdoorIdentityRepository(session), key, account_id, other_id


def insert_winner(session, key, account_id, *, subject=None):
    """Inject a winning row via real SQL between precheck and attempted INSERT."""
    session.execute(
        sa.insert(CasdoorIdentityExtend.__table__).values(
            id=str(uuid4()),
            namespace_id=str(key.namespace_id),
            account_id=str(account_id),
            issuer=key.issuer,
            organization=key.organization,
            subject=subject or key.subject,
            subject_digest=hashlib.sha256((subject or key.subject).encode("utf-8")).hexdigest(),
            last_applied_json="{}",
            profile_sync_json="{}",
        )
    )


def test_lookup_bind_and_idempotence_never_modify_local_account_or_legacy_link(storage):
    session, repo, key, account_id, _ = repository(storage)
    assert repo.find(key, expected_fence_epoch=0) is None
    first = repo.bind(key, account_id=account_id, expected_fence_epoch=0, remote_email="remote@example.test")
    second = repo.bind(
        key, account_id=account_id, expected_fence_epoch=0, remote_email="changed@example.test", email_verified=True
    )
    assert first == second == repo.find(key, expected_fence_epoch=0)
    assert second.remote_email == "remote@example.test"
    assert second.email_verified is None
    assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorIdentityExtend)) == 1
    account = session.get(Account, str(account_id))
    assert account is not None
    assert account.email == "local@example.test"
    assert account.status == AccountStatus.PENDING
    assert account.initialized_at is None
    legacy = session.scalar(sa.select(AccountIntegrate))
    assert legacy is not None
    assert legacy.open_id == "legacy-id"
    assert legacy.provider == "oauth2"
    with pytest.raises(FrozenInstanceError):
        second.account_id = uuid4()


def test_case_distinct_subjects_and_separate_namespaces_do_not_merge(storage):
    session, repo, key, account_id, other_id = repository(storage)
    first = repo.bind(key, account_id=account_id, expected_fence_epoch=0)
    second = repo.bind(replace(key, subject="subjectcase"), account_id=other_id, expected_fence_epoch=0)
    assert first.identity_id != second.identity_id
    namespace = session.get(CasdoorNamespaceExtend, str(key.namespace_id))
    assert namespace is not None
    other_namespace = CasdoorNamespaceExtend(
        integration_id=namespace.integration_id,
        expected_issuer=namespace.expected_issuer,
        organization=namespace.organization,
        application=namespace.application,
        client_id=namespace.client_id,
        core_fingerprint=namespace.core_fingerprint,
    )
    session.add(other_namespace)
    session.flush()
    third = repo.bind(
        replace(key, namespace_id=UUID(other_namespace.id)), account_id=account_id, expected_fence_epoch=0
    )
    assert third.identity_id not in {first.identity_id, second.identity_id}


@pytest.mark.parametrize("direction", ["subject", "account"])
def test_conflict_is_safe_and_never_replaces_a_binding(storage, direction):
    session, repo, key, account_id, other_id = repository(storage)
    before = repo.bind(key, account_id=account_id, expected_fence_epoch=0)
    with pytest.raises(CasdoorIdentityConflict, match="^identity_conflict$") as caught:
        repo.bind(
            key if direction == "subject" else replace(key, subject="OtherSubject"),
            account_id=other_id if direction == "subject" else account_id,
            expected_fence_epoch=0,
        )
    assert str(caught.value) == caught.value.code.value == "identity_conflict"
    assert repo.find(key, expected_fence_epoch=0) == before
    assert session.is_active


def test_same_email_does_not_find_or_bind_automatically(storage):
    session, repo, key, account_id, _ = repository(storage)
    assert repo.find(key, expected_fence_epoch=0) is None
    assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorIdentityExtend)) == 0
    with pytest.raises(CasdoorIdentityConflict):
        repo.bind(
            key, account_id=uuid4(), expected_fence_epoch=0, remote_email="local@example.test", email_verified=True
        )
    # Only a caller-authorized, explicit account ID can establish an association.
    bound = repo.bind(key, account_id=account_id, expected_fence_epoch=0, remote_email="local@example.test")
    assert bound.account_id == account_id


@pytest.mark.parametrize(("field", "value"), [("issuer", "https://other.example"), ("organization", "syntheticorg")])
def test_namespace_fields_match_exactly(storage, field, value):
    session, repo, key, account_id, _ = repository(storage)
    mismatched = replace(key, **{field: value})
    with pytest.raises(CasdoorIdentityConflict):
        repo.find(mismatched, expected_fence_epoch=0)
    with pytest.raises(CasdoorIdentityConflict):
        repo.bind(mismatched, account_id=account_id, expected_fence_epoch=0)
    assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorIdentityExtend)) == 0


@pytest.mark.parametrize(("lifecycle", "epoch"), [("fencing", 0), ("archived", 0), ("active", 1)])
def test_namespace_fence_reads_database_instead_of_cached_orm_object(storage, lifecycle, epoch):
    session, repo, key, account_id, _ = repository(storage)
    stale = session.get(CasdoorNamespaceExtend, str(key.namespace_id))
    session.execute(
        sa.update(CasdoorNamespaceExtend.__table__)
        .where(CasdoorNamespaceExtend.id == str(key.namespace_id))
        .values(lifecycle=lifecycle, fence_epoch=epoch)
    )
    assert stale is not None
    assert stale.lifecycle == "active"
    assert stale.fence_epoch == 0
    with pytest.raises(CasdoorIdentityConflict):
        repo.find(key, expected_fence_epoch=0)
    with pytest.raises(CasdoorIdentityConflict):
        repo.bind(key, account_id=account_id, expected_fence_epoch=0)


def test_unknown_namespace_is_conflict(storage):
    _, repo, key, _, _ = repository(storage)
    with pytest.raises(CasdoorIdentityConflict):
        repo.find(replace(key, namespace_id=uuid4()), expected_fence_epoch=0)


@pytest.mark.parametrize(
    ("field", "value"),
    [("subject", "Collision"), ("issuer", "https://other.example"), ("organization", "syntheticorg")],
)
def test_digest_hit_requires_raw_subject_issuer_and_org_equality(storage, field, value):
    session, repo, key, account_id, _ = repository(storage)
    bound = repo.bind(key, account_id=account_id, expected_fence_epoch=0)
    session.execute(
        sa.update(CasdoorIdentityExtend.__table__)
        .where(CasdoorIdentityExtend.id == str(bound.identity_id))
        .values(**{field: value})
    )
    with pytest.raises(CasdoorIdentityConflict):
        repo.find(key, expected_fence_epoch=0)
    with pytest.raises(CasdoorIdentityConflict):
        repo.bind(key, account_id=account_id, expected_fence_epoch=0)


@pytest.mark.parametrize("winner", ["identical", "other_account", "other_subject"])
def test_real_unique_savepoint_race_recovers_only_exact_same_binding(storage, monkeypatch, winner):
    session, repo, key, account_id, other_id = repository(storage)
    original = repo._existing_binding
    calls = 0

    def precheck_then_winner(checked_key, checked_account):
        nonlocal calls
        existing = original(checked_key, checked_account)
        calls += 1
        if calls == 1:
            assert existing is None
            insert_winner(
                session,
                key,
                other_id if winner == "other_account" else account_id,
                subject="WinnerSubject" if winner == "other_subject" else None,
            )
        return existing

    monkeypatch.setattr(repo, "_existing_binding", precheck_then_winner)
    if winner == "identical":
        assert repo.bind(key, account_id=account_id, expected_fence_epoch=0).account_id == account_id
    else:
        with pytest.raises(CasdoorIdentityConflict):
            repo.bind(key, account_id=account_id, expected_fence_epoch=0)
    assert session.is_active
    assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorIdentityExtend)) == 1
    # Savepoint release/recovery must not commit earlier caller changes.
    session.rollback()
    assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorIdentityExtend)) == 0


def test_successful_savepoint_is_rolled_back_with_outer_account_change(storage):
    session, repo, key, account_id, _ = repository(storage)
    account = session.get(Account, str(account_id))
    assert account is not None
    account.name = "CallerPendingChange"
    repo.bind(key, account_id=account_id, expected_fence_epoch=0)
    assert session.is_active
    session.rollback()
    assert session.get(Account, str(account_id)).name == "Local"
    assert repo.find(key, expected_fence_epoch=0) is None


def test_unrelated_integrity_failure_inside_savepoint_is_not_swallowed(storage):
    session, repo, key, account_id, _ = repository(storage)

    def fail_other_constraint(_session, _context, _instances):
        if any(isinstance(row, CasdoorIdentityExtend) for row in session.new):
            session.execute(sa.insert(CasdoorIntegrationExtend.__table__).values(id=str(uuid4()), slot=1))

    sa.event.listen(session, "before_flush", fail_other_constraint)
    try:
        with pytest.raises(IntegrityError):
            repo.bind(key, account_id=account_id, expected_fence_epoch=0)
    finally:
        sa.event.remove(session, "before_flush", fail_other_constraint)
    assert session.is_active
    assert repo.find(key, expected_fence_epoch=0) is None


def test_caller_pending_integrity_failure_before_savepoint_propagates(storage):
    session, repo, key, account_id, _ = repository(storage)
    session.add(CasdoorIntegrationExtend())
    with pytest.raises(IntegrityError):
        repo.bind(key, account_id=account_id, expected_fence_epoch=0)
    assert not session.is_active  # Caller must roll back their failed outer flush.
    session.rollback()
    with session.begin():
        assert repo.find(key, expected_fence_epoch=0) is None


def test_requires_explicit_existing_transaction(storage):
    session, repo, key, account_id, _ = repository(storage)
    session.rollback()
    with pytest.raises(RuntimeError, match="caller-owned transaction"):
        repo.find(key, expected_fence_epoch=0)
    with pytest.raises(RuntimeError, match="caller-owned transaction"):
        repo.bind(key, account_id=account_id, expected_fence_epoch=0)


@pytest.mark.parametrize("subject", ["", "é" * 128, "a" * 256, "\ud800"])
def test_verified_key_rejects_overlong_utf8_without_truncation(storage, subject):
    _, _, key, _, _ = repository(storage)
    with pytest.raises(CasdoorIdentityConflict):
        replace(key, subject=subject)


@pytest.mark.parametrize("change", ["fence", "delete_account"])
def test_pending_caller_fence_or_delete_is_visible_before_bind(storage, change):
    session, repo, key, account_id, _ = repository(storage)
    if change == "fence":
        session.get(CasdoorNamespaceExtend, str(key.namespace_id)).fence_epoch = 1
    else:
        session.delete(session.get(Account, str(account_id)))
    with pytest.raises(CasdoorIdentityConflict):
        repo.bind(key, account_id=account_id, expected_fence_epoch=0)
    assert session.is_active
    assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorIdentityExtend)) == 0
