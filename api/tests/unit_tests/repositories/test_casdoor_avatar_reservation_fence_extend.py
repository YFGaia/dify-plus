"""Real initial/count2 claim and profile owners, finite SQL-only reservation fence."""

import json
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker
from test_casdoor_avatar_attempt_extend import avatar_fixture as original_avatar_fixture
from test_casdoor_avatar_attempt_extend import claim
from test_casdoor_avatar_attempt_extend import storage_fixture as original_storage_fixture
from test_casdoor_avatar_attempt_extend import worker as original_worker

from machinery.context import RequestContext
from models.account import Account
from models.casdoor_avatar_file_guard_extend import CasdoorAvatarFileGuardExtend as Guard
from models.casdoor_extend import CasdoorAuditExtend as Audit
from repositories.account_repository import SQLAlchemyAccountRepository
from services.account_profile_service import AccountProfileService
from services.entities.account_entities import AccountProfileChanges

worker = original_worker
avatar_fixture = original_avatar_fixture
storage_fixture = original_storage_fixture


@pytest.fixture
def fenced_worker(worker):
    Guard.__table__.create(worker.session.get_bind(), checkfirst=True)
    return worker


def test_actual_initial_claim_creates_indexed_fence_in_same_root(fenced_worker):
    s = fenced_worker
    result = claim(s)
    with Session(s.session.get_bind()) as reader:
        guard = reader.execute(sa.select(*Guard.__table__.columns)).mappings().one()
        audit = reader.scalar(sa.select(Audit).where(Audit.action == "avatar_reservation_fence"))
        assert guard["stage"] == "reserved"
        assert guard["file_id"] == result.reservation.file_id
        assert guard["intent_id"] == str(s.intent_id)
        assert guard["attempt_id"] == str(result.attempt.attempt_id)
        assert audit.correlation_id == result.reservation.file_id
        summary = json.loads(audit.summary_json)
        assert summary["references"]["tenant_id"] == result.reservation.tenant_id
        assert summary["count"] == 1
        assert result.source.ciphertext not in audit.summary_json
        assert result.reservation.storage_key not in audit.summary_json


def test_actual_profile_write_before_first_integration_creates_parent(tmp_path):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "profile.sqlite"}')
    Account.__table__.create(engine)
    Guard.__table__.create(engine)
    Audit.__table__.create(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    account_id, target = str(uuid4()), str(uuid4())
    with maker.begin() as session:
        account = Account(name="Original", email="profile@example.test")
        account.id = account_id
        session.add(account)
    service = AccountProfileService(accounts=SQLAlchemyAccountRepository(maker))
    # The production service uses only account_id from its admitted context.
    result = service.update(
        RequestContext("test", None, account_id, str(uuid4())), AccountProfileChanges(name="Updated", avatar=target)
    )
    assert result.avatar == target and result.name == "Updated"
    with maker() as reader:
        parent = reader.execute(sa.select(*Guard.__table__.columns)).mappings().one()
        assert parent["file_id"] == target and parent["stage"] == "unbound"
    engine.dispose()


def all_rows(s):
    from models.casdoor_extend import CasdoorSyncIntentExtend as Intent

    with Session(s.session.get_bind()) as reader:
        return {
            model.__tablename__: [
                tuple(x)
                for x in reader.execute(sa.select(*model.__table__.columns).order_by(list(model.__table__.columns)[0]))
            ]
            for model in (Guard, Audit, Intent, Account)
        }


def profile_service(s):
    return AccountProfileService(accounts=SQLAlchemyAccountRepository(sessionmaker(s.session.get_bind())))


def context(account_id):
    return RequestContext("finite", None, account_id, str(uuid4()))


def foreign_account(s):
    with Session(s.session.get_bind()) as session, session.begin():
        account = Account(name="Other", email="other@example.test")
        account.id = str(uuid4())
        session.add(account)
        return account.id


@pytest.mark.parametrize("form", ["canonical", "upper", "hex", "brace", "urn", "urn_only", "uuid_only", "hyphens"])
def test_real_manual_variant_reference_vetoes_forced_candidate(fenced_worker, monkeypatch, form):
    import repositories.casdoor_avatar_repository_extend as module

    s = fenced_worker
    target = uuid4()
    raw = {
        "canonical": str(target),
        "upper": str(target).upper(),
        "hex": target.hex,
        "brace": "{" + str(target) + "}",
        "urn": target.urn,
        "urn_only": "urn:" + str(target),
        "uuid_only": "uuid:" + str(target),
        "hyphens": "-".join(target.hex),
    }[form]
    assert UUID(raw) == target  # Actual Python parser domain, including nonstandard accepted prefixes.
    other = foreign_account(s)
    result = profile_service(s).update(context(other), AccountProfileChanges(name="Manual", avatar=raw))
    assert result.avatar == raw
    before = all_rows(s)
    monkeypatch.setattr(module, "uuid4", lambda: target)
    from repositories.casdoor_avatar_repository_extend import CasdoorAvatarConflict

    with pytest.raises(CasdoorAvatarConflict):
        claim(s)
    assert all_rows(s) == before


@pytest.mark.parametrize(
    "damage",
    ["missing", "duplicate", "oversize", "bad_summary", "binding", "version", "cleanup_pending", "cleanup_complete"],
)
def test_actual_reservation_malformed_or_terminal_guard_denies_whole_profile(fenced_worker, damage):
    from services.account_errors import AvatarFileNotFoundError

    s = fenced_worker
    result = claim(s)
    target = result.reservation.file_id
    with Session(s.session.get_bind()) as session, session.begin():
        fence = dict(
            session.execute(sa.select(*Audit.__table__.columns).where(Audit.action == "avatar_reservation_fence"))
            .mappings()
            .one()
        )
        if damage == "missing":
            session.execute(sa.delete(Audit).where(Audit.id == fence["id"]))
        elif damage == "duplicate":
            fence["id"] = str(uuid4())
            session.execute(sa.insert(Audit.__table__).values(**fence))
        elif damage in ("oversize", "bad_summary"):
            session.execute(
                sa.update(Audit)
                .where(Audit.id == fence["id"])
                .values(summary_json="x" * 4097 if damage == "oversize" else "{}")
            )
        elif damage in ("binding", "version"):
            session.execute(
                sa.update(Guard).values(**({"attempt_id": str(uuid4())} if damage == "binding" else {"version": 3}))
            )
        else:
            # Refusal-only future/corrupt stage fixture. This is not a cleanup producer or permit.
            session.execute(sa.update(Guard).values(stage=damage, version=3))
    before = all_rows(s)
    with pytest.raises(AvatarFileNotFoundError):
        profile_service(s).update(context(s.account.id), AccountProfileChanges(name="Must Roll Back", avatar=target))
    assert all_rows(s) == before


def test_valid_reserved_target_and_external_profile_preserve_existing_behavior(fenced_worker):
    s = fenced_worker
    result = claim(s)
    service = profile_service(s)
    assert (
        service.update(context(s.account.id), AccountProfileChanges(avatar=result.reservation.file_id)).avatar
        == result.reservation.file_id
    )
    before = all_rows(s)[Guard.__tablename__]
    assert (
        service.update(
            context(s.account.id), AccountProfileChanges(name="External", avatar="https://example.test/a")
        ).avatar
        == "https://example.test/a"
    )
    assert service.update(context(s.account.id), AccountProfileChanges(avatar="not-a-uuid")).avatar == "not-a-uuid"
    assert all_rows(s)[Guard.__tablename__] == before
    assert (
        SQLAlchemyAccountRepository(sessionmaker(s.session.get_bind())).update_profile(
            str(uuid4()), AccountProfileChanges(avatar=str(uuid4()))
        )
        is None
    )


@pytest.mark.parametrize("collision", ["id", "id_variant", "key"])
def test_actual_upload_metadata_collision_rolls_back_candidate(fenced_worker, monkeypatch, collision):
    import repositories.casdoor_avatar_repository_extend as module
    from extensions.storage.storage_type import StorageType
    from models.enums import CreatorUserRole
    from models.model import UploadFile
    from repositories.casdoor_avatar_repository_extend import CasdoorAvatarConflict

    s = fenced_worker
    target, attempt = uuid4(), uuid4()
    key = f"casdoor-avatar/{s.intent_id}/{attempt}/{target}.png"
    with Session(s.session.get_bind()) as session, session.begin():
        upload = UploadFile(
            tenant_id=str(uuid4()),
            storage_type=StorageType.LOCAL,
            key=key if collision == "key" else "other/key",
            name="existing",
            size=1,
            created_at=s.account.created_at,
            used=False,
            extension="png",
            mime_type="image/png",
            created_by=str(uuid4()),
            created_by_role=CreatorUserRole.ACCOUNT,
        )
        upload.id = target.hex if collision == "id_variant" else str(target if collision == "id" else uuid4())
        session.add(upload)
    before = all_rows(s)
    generated = iter([target, attempt, uuid4()])
    monkeypatch.setattr(module, "uuid4", lambda: next(generated))
    with pytest.raises(CasdoorAvatarConflict):
        claim(s)
    assert all_rows(s) == before
    with Session(s.session.get_bind()) as reader:
        assert reader.scalar(sa.select(sa.func.count()).select_from(UploadFile)) == 1


def test_actual_fence_audit_sql_fault_rolls_back_original_claim_and_guard(fenced_worker):
    from repositories.casdoor_avatar_repository_extend import CasdoorAvatarConflict

    s = fenced_worker
    with s.session.get_bind().begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER b2_fence_fail BEFORE INSERT ON casdoor_audit_extend "
            "WHEN NEW.action = 'avatar_reservation_fence' "
            "BEGIN SELECT RAISE(ABORT, 'finite fence fault'); END"
        )
    before = all_rows(s)
    with pytest.raises(CasdoorAvatarConflict):
        claim(s)
    assert all_rows(s) == before


def test_actual_caller_rollback_and_busy_do_not_reissue_candidate(fenced_worker, monkeypatch):
    s = fenced_worker
    before = all_rows(s)
    from test_casdoor_profile_repository_extend import NOW

    with pytest.raises(RuntimeError), s.session.begin():
        assert s.avatar_owner.claim_and_reserve(s.intent_id, now=NOW).code == "reserved"
        raise RuntimeError("caller rollback")
    assert all_rows(s) == before
    claim(s)
    before = all_rows(s)
    import repositories.casdoor_avatar_repository_extend as module

    def denied():
        raise AssertionError("busy delivery minted candidate")

    monkeypatch.setattr(module, "uuid4", denied)
    assert claim(s).code == "busy"
    assert all_rows(s) == before


def test_profile_and_original_claim_sql_order(fenced_worker):
    s = fenced_worker
    calls = []

    def observe(connection, cursor, statement, parameters, context, many):
        calls.append(statement.lower())

    sa.event.listen(s.session.get_bind(), "before_cursor_execute", observe)
    try:
        result = claim(s)
        first_guard = next(i for i, x in enumerate(calls) if "casdoor_avatar_file_guard_extend" in x)
        first_parent = next(i for i, x in enumerate(calls) if "from casdoor_integration_extend" in x)
        assert first_guard < first_parent
        calls.clear()
        profile_service(s).update(context(s.account.id), AccountProfileChanges(avatar=result.reservation.file_id))
        first_guard = next(i for i, x in enumerate(calls) if "casdoor_avatar_file_guard_extend" in x)
        first_account = next(i for i, x in enumerate(calls) if "from accounts" in x)
        assert first_guard < first_account
    finally:
        sa.event.remove(s.session.get_bind(), "before_cursor_execute", observe)


def test_manual_and_real_claim_contend_on_same_file_parent(fenced_worker, tmp_path, monkeypatch):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from test_casdoor_profile_repository_extend import NOW

    import repositories.casdoor_avatar_repository_extend as module
    from repositories.casdoor_avatar_repository_extend import CasdoorAvatarRepository
    from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationRepository
    from services.account_errors import AvatarFileNotFoundError

    s = fenced_worker
    other = foreign_account(s)
    path = tmp_path / "race.sqlite"
    with s.session.get_bind().connect() as source, sqlite3.connect(path) as destination:
        source.connection.driver_connection.backup(destination)
    engine = sa.create_engine(f"sqlite:///{path}", connect_args={"timeout": 0.1})
    target = uuid4()
    original_candidate = CasdoorAvatarRepository._reservation_candidate
    acquired, released = Event(), Event()

    def hold(self, *args):
        candidate = original_candidate(self, *args)
        acquired.set()
        assert released.wait(5)
        return candidate

    monkeypatch.setattr(CasdoorAvatarRepository, "_reservation_candidate", hold)
    monkeypatch.setattr(module, "uuid4", lambda: target)

    def actual_claim():
        with Session(engine) as session, session.begin():
            config = CasdoorConfigurationRepository(session, crypto=s.config_owner.crypto, rbac_enabled=False)
            return CasdoorAvatarRepository(session, configuration_repository=config).claim_and_reserve(
                s.intent_id, now=NOW
            )

    service = AccountProfileService(accounts=SQLAlchemyAccountRepository(sessionmaker(engine)))
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(actual_claim)
            assert acquired.wait(5)
            try:
                with pytest.raises(AvatarFileNotFoundError):
                    service.update(context(other), AccountProfileChanges(name="No Partial Write", avatar=str(target)))
                with Session(engine) as reader:
                    assert reader.get(Account, other).name == "Other"
            finally:
                released.set()
            result = future.result(timeout=5)
            assert result.code == "reserved" and result.reservation.file_id == str(target)
        assert service.update(context(other), AccountProfileChanges(avatar=str(target))).avatar == str(target)
    finally:
        released.set()
        engine.dispose()


def test_uuid_reference_sql_has_actual_portable_bound_predicate():
    from sqlalchemy.dialects import mysql, postgresql, sqlite

    from repositories.casdoor_avatar_repository_extend import _avatar_uuid_reference

    target = uuid4()
    query = sa.select(Account.id).where(_avatar_uuid_reference(Account.avatar, target)).limit(1)
    for dialect in (postgresql.dialect(), mysql.dialect(), sqlite.dialect()):
        compiled = query.compile(dialect=dialect)
        assert "lower(accounts.avatar)" in str(compiled)
        assert set(("urn:", "uuid:", "{", "}", "-", target.hex)) <= set(compiled.params.values())


def test_controller_maps_actual_profile_guard_rejection_to_existing_notfound(fenced_worker, monkeypatch):
    import importlib
    from types import SimpleNamespace

    from werkzeug.exceptions import NotFound

    controller = importlib.import_module("controllers.console.workspace.account")
    s = fenced_worker
    result = claim(s)
    with Session(s.session.get_bind()) as session, session.begin():
        session.execute(sa.update(Guard).values(stage="cleanup_pending", version=3))
    service = profile_service(s)
    monkeypatch.setattr(
        controller, "application_services", lambda: SimpleNamespace(accounts=SimpleNamespace(profile=service))
    )
    before = all_rows(s)
    with pytest.raises(NotFound, match="Avatar file not found"):
        controller._update_account_profile(
            context(s.account.id), AccountProfileChanges(name="Atomic", avatar=result.reservation.file_id)
        )
    assert all_rows(s) == before


def test_final_fresh_reference_probe_rolls_back_entire_original_claim(fenced_worker):
    from repositories.casdoor_avatar_repository_extend import CasdoorAvatarConflict

    s = fenced_worker
    other = foreign_account(s)
    with s.session.get_bind().begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER b2_fence_ref AFTER INSERT ON casdoor_audit_extend "
            "WHEN NEW.action = 'avatar_reservation_fence' BEGIN "
            f"UPDATE accounts SET avatar = NEW.correlation_id WHERE id = '{other}'; END"
        )
    before = all_rows(s)
    with pytest.raises(CasdoorAvatarConflict):
        claim(s)
    assert all_rows(s) == before


def test_navigation_race_loser_only_retains_empty_guard(fenced_worker, tmp_path, monkeypatch):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from test_casdoor_profile_repository_extend import NOW

    from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
    from repositories.casdoor_avatar_repository_extend import CasdoorAvatarRepository
    from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationRepository

    s = fenced_worker
    path = tmp_path / "navigation.sqlite"
    with s.session.get_bind().connect() as source, sqlite3.connect(path) as destination:
        source.connection.driver_connection.backup(destination)
    engine = sa.create_engine(f"sqlite:///{path}", connect_args={"timeout": 0.1})
    observed, released = Event(), Event()
    original = CasdoorAvatarRepository._worker_read

    def pause(self, model, *args, **kwargs):
        row = original(self, model, *args, **kwargs)
        if model is Intent and self._session.info.pop("b2-pause", False):
            observed.set()
            assert released.wait(5)
        return row

    monkeypatch.setattr(CasdoorAvatarRepository, "_worker_read", pause)

    def actual_claim(wait):
        with Session(engine) as session, session.begin():
            session.info["b2-pause"] = wait
            config = CasdoorConfigurationRepository(session, crypto=s.config_owner.crypto, rbac_enabled=False)
            return CasdoorAvatarRepository(session, configuration_repository=config).claim_and_reserve(
                s.intent_id, now=NOW
            )

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            loser = executor.submit(actual_claim, True)
            assert observed.wait(5)
            try:
                winner = actual_claim(False)
                assert winner.code == "reserved"
            finally:
                released.set()
            assert loser.result(timeout=5).code == "busy"
        with Session(engine) as reader:
            guards = reader.scalars(sa.select(Guard)).all()
            assert sorted(x.stage for x in guards) == ["reserved", "unbound"]
            empty = next(x for x in guards if x.stage == "unbound")
            assert empty.intent_id is None and empty.attempt_id is None and empty.version == 1
            assert (
                reader.scalar(
                    sa.select(sa.func.count()).select_from(Audit).where(Audit.action == "avatar_reservation_fence")
                )
                == 1
            )
            assert reader.scalar(sa.select(Intent.attempt_count)) == 1
    finally:
        released.set()
        engine.dispose()


@pytest.mark.parametrize("malformed", ["references", "namespace"])
def test_malformed_original_pending_lineage_is_controlled_whole_root_refusal(fenced_worker, malformed):
    from repositories.casdoor_avatar_repository_extend import CasdoorAvatarConflict

    s = fenced_worker
    with Session(s.session.get_bind()) as session, session.begin():
        audit = session.scalar(sa.select(Audit).where(Audit.action == "avatar_pending"))
        data = json.loads(audit.summary_json)
        if malformed == "references":
            del data["references"]
        else:
            del data["references"]["namespace_id"]
        audit.summary_json = json.dumps(data, sort_keys=True, separators=(",", ":"))
    before = all_rows(s)
    with pytest.raises(CasdoorAvatarConflict):
        claim(s)
    assert all_rows(s) == before


@pytest.mark.parametrize(
    "field", ["id", "namespace_id", "revision_id", "identity_id", "account_id", "actor_account_id"]
)
def test_uuid_audit_headers_reject_oversize_before_row_materialization(fenced_worker, field):
    from repositories.casdoor_audit_repository_extend import CasdoorAuditRepository

    s = fenced_worker
    result = claim(s)
    with Session(s.session.get_bind()) as session, session.begin():
        session.execute(
            sa.update(Audit).where(Audit.action == "avatar_reservation_fence").values(**{field: "x" * 65537})
        )
    full_projection = []

    def observe(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("SELECT") and "casdoor_audit_extend" in statement:
            names = {x[0] for x in cursor.description}
            if "summary_json" in names:
                full_projection.append(True)

    sa.event.listen(s.session.get_bind(), "after_cursor_execute", observe)
    try:
        with Session(s.session.get_bind()) as session, session.begin(), pytest.raises(ValueError):
            CasdoorAuditRepository(session)._read_avatar_reservation_fence(UUID(result.reservation.file_id))
        assert not full_projection, "Oversized UUID audit header reached full scalar-row projection"
    finally:
        sa.event.remove(s.session.get_bind(), "after_cursor_execute", observe)
