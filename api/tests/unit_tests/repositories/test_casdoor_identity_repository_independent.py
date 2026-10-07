"""Independent storage checks for the task 4.1 identity repository."""

import hashlib
import sqlite3
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from models.account import Account, AccountIntegrate, AccountStatus
from models.casdoor_extend import CasdoorIdentityExtend, CasdoorIntegrationExtend, CasdoorNamespaceExtend
from repositories.casdoor_identity_repository_extend import (
    CasdoorIdentityConflict,
    CasdoorIdentityRepository,
    VerifiedIdentityKey,
    _is_identity_unique_violation,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@pytest.fixture
def identity_store():
    engine = sa.create_engine("sqlite://")

    @sa.event.listens_for(engine, "connect")
    def configure_sqlite(connection, _record):
        connection.isolation_level = None
        connection.execute("PRAGMA foreign_keys = ON")

    @sa.event.listens_for(engine, "begin")
    def begin_sqlite(connection):
        connection.exec_driver_sql("BEGIN")

    for model in (Account, AccountIntegrate, CasdoorIntegrationExtend, CasdoorNamespaceExtend, CasdoorIdentityExtend):
        model.__table__.create(engine)

    with Session(engine) as session:
        account = Account(name="Pending local", email="same@example.test", status=AccountStatus.PENDING)
        other = Account(name="Other", email="same@example.test")
        integration = CasdoorIntegrationExtend()
        session.add_all((account, other, integration))
        session.flush()
        namespace = CasdoorNamespaceExtend(
            integration_id=integration.id,
            expected_issuer="https://issuer.example.test",
            organization="OrgCase",
            application="App",
            client_id="client",
            core_fingerprint="b" * 64,
        )
        session.add(namespace)
        session.flush()
        session.add(
            AccountIntegrate(account_id=account.id, provider="oauth2", open_id="preserved", encrypted_token="")
        )
        session.commit()
        key = VerifiedIdentityKey(UUID(namespace.id), namespace.expected_issuer, namespace.organization, "subject-1")
        yield session, key, UUID(account.id), UUID(other.id)
    engine.dispose()


def _bind_parts(identity_store):
    session, key, account_id, other_id = identity_store
    return session, CasdoorIdentityRepository(session), key, account_id, other_id


def test_full_namespace_and_raw_identity_tuple_is_exact(identity_store):
    session, repository, key, account_id, other_id = _bind_parts(identity_store)
    first = repository.bind(key, account_id=account_id, expected_fence_epoch=0)
    second = repository.bind(replace(key, subject="Subject-1"), account_id=other_id, expected_fence_epoch=0)
    assert first.identity_id != second.identity_id
    assert hashlib.sha256(first.subject.encode("utf-8")).hexdigest() == key.subject_digest

    for altered in (
        replace(key, issuer="https://other.example.test"),
        replace(key, organization="orgcase"),
    ):
        with pytest.raises(CasdoorIdentityConflict):
            repository.find(altered, expected_fence_epoch=0)
        with pytest.raises(CasdoorIdentityConflict):
            repository.bind(altered, account_id=account_id, expected_fence_epoch=0)

    another_namespace = CasdoorNamespaceExtend(
        integration_id=session.get(CasdoorNamespaceExtend, str(key.namespace_id)).integration_id,
        expected_issuer=key.issuer,
        organization=key.organization,
        application="other app",
        client_id="other client",
        core_fingerprint="c" * 64,
    )
    session.add(another_namespace)
    session.flush()
    isolated_key = replace(key, namespace_id=UUID(another_namespace.id))
    assert repository.find(isolated_key, expected_fence_epoch=0) is None
    assert repository.bind(isolated_key, account_id=account_id, expected_fence_epoch=0).namespace_id == UUID(
        another_namespace.id
    )


def test_digest_collision_requires_raw_subject_equality(identity_store):
    session, repository, key, account_id, _ = _bind_parts(identity_store)
    bound = repository.bind(key, account_id=account_id, expected_fence_epoch=0)
    # Simulate a SHA-256 collision/corrupt row by retaining the sought digest while
    # changing the raw subject. The digest index must never become identity proof.
    session.execute(
        sa.update(CasdoorIdentityExtend.__table__)
        .where(CasdoorIdentityExtend.id == str(bound.identity_id))
        .values(subject="different-raw-subject")
    )
    with pytest.raises(CasdoorIdentityConflict):
        repository.find(key, expected_fence_epoch=0)
    with pytest.raises(CasdoorIdentityConflict):
        repository.bind(key, account_id=account_id, expected_fence_epoch=0)


@pytest.mark.parametrize(("field", "value"), [("lifecycle", "fencing"), ("lifecycle", "archived"), ("fence_epoch", 1)])
def test_database_namespace_state_and_expected_epoch_are_checked(identity_store, field, value):
    session, repository, key, account_id, _ = _bind_parts(identity_store)
    session.execute(
        sa.update(CasdoorNamespaceExtend.__table__)
        .where(CasdoorNamespaceExtend.id == str(key.namespace_id))
        .values(**{field: value})
    )
    with pytest.raises(CasdoorIdentityConflict):
        repository.find(key, expected_fence_epoch=0)
    with pytest.raises(CasdoorIdentityConflict):
        repository.bind(key, account_id=account_id, expected_fence_epoch=0)


def test_existing_account_required_and_email_does_not_create_or_activate(identity_store):
    session, repository, key, account_id, _ = _bind_parts(identity_store)
    with pytest.raises(CasdoorIdentityConflict):
        repository.bind(key, account_id=uuid4(), expected_fence_epoch=0, remote_email="same@example.test")
    assert repository.find(key, expected_fence_epoch=0) is None
    row = repository.bind(
        key,
        account_id=account_id,
        expected_fence_epoch=0,
        remote_email="same@example.test",
        email_verified=True,
    )
    local = session.get(Account, str(account_id))
    legacy = session.scalar(sa.select(AccountIntegrate))
    assert row.account_id == account_id
    assert local is not None
    assert local.status == AccountStatus.PENDING
    assert local.email == "same@example.test"
    assert local.initialized_at is None
    assert legacy is not None
    assert legacy.open_id == "preserved"
    assert legacy.provider == "oauth2"


def test_same_binding_is_idempotent_and_preserves_initial_minimal_snapshot(identity_store):
    _, repository, key, account_id, _ = _bind_parts(identity_store)
    initial = repository.bind(
        key, account_id=account_id, expected_fence_epoch=0, remote_email="first@example.test", email_verified=False
    )
    repeated = repository.bind(
        key, account_id=account_id, expected_fence_epoch=0, remote_email="later@example.test", email_verified=True
    )
    assert repeated == initial
    assert repeated.remote_email == "first@example.test"
    assert repeated.email_verified is False


@pytest.mark.parametrize("direction", ["subject-to-other-account", "account-to-other-subject"])
def test_both_unique_directions_conflict_without_rebinding(identity_store, direction):
    _, repository, key, account_id, other_id = _bind_parts(identity_store)
    original = repository.bind(key, account_id=account_id, expected_fence_epoch=0)
    conflicting_key = key if direction == "subject-to-other-account" else replace(key, subject="subject-2")
    conflicting_account = other_id if direction == "subject-to-other-account" else account_id
    with pytest.raises(CasdoorIdentityConflict, match="^identity_conflict$"):
        repository.bind(conflicting_key, account_id=conflicting_account, expected_fence_epoch=0)
    assert repository.find(key, expected_fence_epoch=0) == original


@pytest.mark.parametrize("subject", ["é" * 128, "é" * 127 + "a", "a" * 255, "a" * 256, "", "\ud800"])
def test_subject_limit_counts_utf8_bytes_and_never_truncates(identity_store, subject):
    _, _, key, _, _ = _bind_parts(identity_store)
    if subject in {"é" * 127 + "a", "a" * 255}:
        accepted = replace(key, subject=subject)
        assert len(accepted.subject.encode("utf-8")) == 255
        assert accepted.subject_digest == hashlib.sha256(subject.encode("utf-8")).hexdigest()
    else:
        with pytest.raises(CasdoorIdentityConflict):
            replace(key, subject=subject)


def test_savepoint_failure_keeps_caller_work_until_outer_rollback(identity_store, monkeypatch):
    session, repository, key, account_id, _ = _bind_parts(identity_store)
    account = session.get(Account, str(account_id))
    assert account is not None
    account.name = "outer pending"
    original = repository._existing_binding
    calls = 0

    def winning_other_account(checked_key, checked_account):
        nonlocal calls
        result = original(checked_key, checked_account)
        calls += 1
        if calls == 1:
            session.execute(
                sa.insert(CasdoorIdentityExtend.__table__).values(
                    id=str(uuid4()),
                    namespace_id=str(key.namespace_id),
                    account_id=str(uuid4()),
                    issuer=key.issuer,
                    organization=key.organization,
                    subject=key.subject,
                    subject_digest=key.subject_digest,
                    last_applied_json="{}",
                    profile_sync_json="{}",
                )
            )
        return result

    monkeypatch.setattr(repository, "_existing_binding", winning_other_account)
    with pytest.raises(CasdoorIdentityConflict):
        repository.bind(key, account_id=account_id, expected_fence_epoch=0)
    assert session.is_active
    assert session.scalar(sa.select(Account.name).where(Account.id == str(account_id))) == "outer pending"
    assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorIdentityExtend)) == 1
    session.rollback()
    assert session.get(Account, str(account_id)).name == "Pending local"
    assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorIdentityExtend)) == 0


def test_non_identity_integrity_error_is_not_misclassified_or_swallowed(identity_store, monkeypatch):
    session, repository, key, account_id, _ = _bind_parts(identity_store)

    def conflicting_slot(_session, _context, _instances):
        if any(isinstance(instance, CasdoorIdentityExtend) for instance in session.new):
            session.execute(sa.insert(CasdoorIntegrationExtend.__table__).values(id=str(uuid4()), slot=1))

    sa.event.listen(session, "before_flush", conflicting_slot)
    try:
        with pytest.raises(IntegrityError):
            repository.bind(key, account_id=account_id, expected_fence_epoch=0)
    finally:
        sa.event.remove(session, "before_flush", conflicting_slot)
    assert session.is_active
    assert repository.find(key, expected_fence_epoch=0) is None


def test_unique_error_classification_is_narrow_for_pg_mysql_and_sqlite():
    class PgUnique(Exception):
        sqlstate = "23505"
        diag = type("Diag", (), {"constraint_name": "casdoor_identity_account_key"})()

    class PgOtherUnique(Exception):
        sqlstate = "23505"
        diag = type("Diag", (), {"constraint_name": "casdoor_integration_slot_key"})()

    class MysqlDuplicate(Exception):
        args = (1062, "Duplicate entry 'x' for key 'casdoor_identity_subject_key'")

    class MysqlOtherDuplicate(Exception):
        args = (1062, "Duplicate entry 'x' for key 'casdoor_integration_slot_key'")

    def integrity(original):
        return IntegrityError("INSERT", {}, original)

    assert _is_identity_unique_violation(integrity(PgUnique()))
    assert not _is_identity_unique_violation(integrity(PgOtherUnique()))
    assert _is_identity_unique_violation(integrity(MysqlDuplicate()))
    assert not _is_identity_unique_violation(integrity(MysqlOtherDuplicate()))
    class SqliteUnique(sqlite3.IntegrityError):
        sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT_UNIQUE

    sqlite_error = SqliteUnique(
        "UNIQUE constraint failed: casdoor_identity_extend.namespace_id, casdoor_identity_extend.subject_digest"
    )
    assert _is_identity_unique_violation(integrity(sqlite_error))
