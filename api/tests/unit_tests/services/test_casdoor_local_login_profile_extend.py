"""Actual C1/B2/B3/I22/audit/F12 roots with SQLite and fake Redis transport.

Synthetic profile/configuration observations do not attest provider authentication.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from time import monotonic
from uuid import UUID

import pytest
import sqlalchemy as sa
from core.casdoor.admission import AdmissionAction as Action
from core.casdoor.claims import VerifiedProfile
from core.casdoor.leases import CasdoorLeaseError
from core.casdoor.request_safety import ProfileNameReason
from models.account import Account, AccountStatus
from models.account import TenantAccountJoin as Join
from models.account_money_extend import AccountMoneyExtend as Quota
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision
from models.casdoor_extend import CasdoorFinalizationState
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from repositories.casdoor_account_preflight_repository_extend import CasdoorAccountPreflightRepository
from repositories.casdoor_audit_repository_extend import CasdoorAuditRepository
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict, CasdoorLoginScopeRepository
from repositories.casdoor_profile_repository_extend import CasdoorProfileConflict, CasdoorProfileRepository
from services.casdoor_local_membership_service_extend import CasdoorLocalMembershipService
from services.casdoor_login_account_service_extend import CasdoorLoginAccountService
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_casdoor_local_login_service_extend import config_factory, ready, run, seed
from test_casdoor_local_login_service_extend import local as original_local
from test_casdoor_local_login_service_extend import login_env as login_env

local_fixture = original_local

NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)
CORRELATION = UUID(int=801)


def mode(local, value):
    configuration = local.config.model_copy(update={"name_sync": value})
    with local.session.begin():
        revision = local.session.get(Revision, str(local.env[1].revision_id))
        policy = json.loads(revision.policy_json)
        policy["name_sync"] = value
        local.session.execute(sa.update(Revision).values(policy_json=json.dumps(policy)))
        local.session.expire(revision)
        digest = config_factory(local.session)._validation_digest(configuration, revision)
        local.session.execute(sa.update(Revision).values(config_digest=digest))
    local.env = (local.session, replace(local.env[1], config_digest=digest), *local.env[2:])
    local.config = configuration


@pytest.fixture
def profiled(local_fixture):
    local = local_fixture
    Audit.__table__.create(local.engine)
    mode(local, "managed")
    return local


def arguments(local, name="Ready", **updates):
    result = dict(
        profile=VerifiedProfile(local.env[1].subject, "remote@example.test", True, name, "zh-Hans", "Asia/Shanghai"),
        auth_started_at=NOW - timedelta(seconds=1),
        correlation_id=CORRELATION,
        now=NOW,
    )
    result.update(updates)
    return result


def rows(local, model):
    with Session(local.engine) as reader:
        return tuple(tuple(row) for row in reader.execute(sa.select(*model.__table__.columns).order_by(model.id)))


def all_business(local):
    return {model: rows(local, model) for model in (Account, Quota, Identity, Join, History, Audit)}


def execute(local, bundle, **kwargs):
    try:
        return run(local, bundle, **kwargs)
    finally:
        bundle[3].release()


@pytest.mark.parametrize("sync_mode", ["managed", "fill_empty", "off"])
def test_create_baseline_same_session_root_one_generation_before_f12(profiled, monkeypatch, sync_mode):
    local = profiled
    mode(local, sync_mode)
    bundle = ready(local)
    calls, sessions, roots, sql = [], [], [], []

    def observe(label, cls, method, session_of):
        original = getattr(cls, method)

        def wrapped(owner, *args, **kwargs):
            session = session_of(owner, kwargs)
            sessions.append(session)
            roots.append(session.get_transaction())
            calls.append(label)
            if label == "profile":
                assert session.scalar(sa.select(Identity.sync_generation)) == 1
                assert session.scalar(sa.select(sa.func.count()).select_from(Join)) == 2
                assert not session.new and not session.dirty
            if label == "f12":
                assert session.scalar(sa.select(Identity.remote_profile_version)).startswith("sha256:")
                assert session.scalar(sa.select(sa.func.count()).select_from(Audit)) == 1
            return original(owner, *args, **kwargs)

        monkeypatch.setattr(cls, method, wrapped)

    observe("b2", CasdoorLoginAccountService, "persist_login_account", lambda owner, kwargs: kwargs["session"])
    observe("b3", CasdoorLocalMembershipService, "persist_local_memberships", lambda owner, kwargs: owner._session)
    observe("profile", CasdoorProfileRepository, "persist", lambda owner, kwargs: owner._session)
    observe("audit", CasdoorAuditRepository, "append_profile", lambda owner, kwargs: owner._session)
    observe(
        "f12",
        CasdoorAccountPreflightRepository,
        "_observe_postwrite_new_collisions",
        lambda owner, kwargs: owner._session,
    )

    def record(_connection, _cursor, statement, _params, _context, _many):
        sql.append(statement)

    sa.event.listen(local.engine, "before_cursor_execute", record)
    try:
        result = execute(local, bundle, **arguments(local))
    finally:
        sa.event.remove(local.engine, "before_cursor_execute", record)
    assert calls == ["b2", "b3", "profile", "audit", "f12"]
    assert all(session is sessions[0] for session in sessions)
    assert all(root is roots[0] for root in roots) and roots[0] is not None
    assert result.generation == 1
    assert all(item.finalization is CasdoorFinalizationState.PENDING for item in result.workspaces)
    account_updates = [statement for statement in sql if statement.startswith("UPDATE accounts SET")]
    assert len(account_updates) == 1  # Original B2 initialization, no second name UPDATE from I22.
    assert sum(statement.startswith("UPDATE casdoor_identity_extend SET sync_generation=") for statement in sql) == 1
    with Session(local.engine) as reader:
        identity = reader.scalar(sa.select(Identity))
        assert json.loads(identity.last_applied_json) == (
            {} if sync_mode == "off" else {"schema_version": 1, "name": "Ready", "name_generation": 1}
        )
        sync = json.loads(identity.profile_sync_json)
        assert sync["name_reason"] == (
            ProfileNameReason.DISABLED.value if sync_mode == "off" else ProfileNameReason.CREATED_BASELINE.value
        )
        assert sync["correlation_id"] == str(CORRELATION)
        assert reader.scalar(sa.select(Account.email)) == "new@example.test"
        assert reader.scalar(sa.select(Quota.total_quota)) == 15


@pytest.mark.parametrize("sync_mode", ["managed", "fill_empty", "off"])
@pytest.mark.parametrize(
    "action,status,marker",
    [
        (Action.INITIALIZE_BOUND, AccountStatus.PENDING, None),
        (Action.ACTIVATE_BOUND, AccountStatus.PENDING, datetime(2025, 1, 1)),
        (Action.USE_BOUND, AccountStatus.ACTIVE, datetime(2025, 1, 1)),
    ],
)
def test_bound_modes_preserve_local_protected_values(profiled, sync_mode, action, status, marker):
    local = profiled
    mode(local, sync_mode)
    seed(local.env, status, marker)
    previous_name = "Ready" if action is Action.INITIALIZE_BOUND else "Preserved"
    with local.session.begin():
        local.session.execute(
            sa.update(Identity).values(
                last_applied_json=json.dumps({"schema_version": 1, "name": previous_name, "name_generation": 0})
            )
        )
        local.session.execute(
            sa.update(Account).values(
                avatar="local-avatar",
                password="synthetic-hash",
                password_salt="synthetic-salt",
                updated_at=datetime(2020, 1, 1),
            )
        )
    quota_before = rows(local, Quota)
    with Session(local.engine) as reader:
        protected = reader.execute(
            sa.select(Account.email, Account.normalized_email, Account.avatar, Account.password, Account.password_salt)
        ).one()
    result = execute(local, ready(local, action), **arguments(local, "Remote Name"))
    assert result.generation == 1
    assert rows(local, Quota) == quota_before
    with Session(local.engine) as reader:
        account = reader.scalar(sa.select(Account))
        assert account.name == ("Remote Name" if sync_mode == "managed" else previous_name)
        assert (
            reader.execute(
                sa.select(
                    Account.email, Account.normalized_email, Account.avatar, Account.password, Account.password_salt
                )
            ).one()
            == protected
        )
        assert account.status is AccountStatus.ACTIVE
        assert account.initialized_at is not None
        if action is Action.INITIALIZE_BOUND:
            assert (account.interface_language, account.timezone, account.interface_theme) == ("en-US", "UTC", "light")
        else:
            assert account.initialized_at == marker
            assert (account.interface_language, account.timezone, account.interface_theme) == (
                "ja-JP",
                "Asia/Tokyo",
                "dark",
            )
        if sync_mode == "managed":
            assert account.updated_at != datetime(2020, 1, 1)


@pytest.mark.parametrize(
    "sync_mode,name,baseline,remote,expected,reason",
    [
        ("managed", "", None, "Filled", "Filled", ProfileNameReason.FILLED_EMPTY),
        ("fill_empty", "  ", None, "Filled", "Filled", ProfileNameReason.FILLED_EMPTY),
        ("off", "", None, "Filled", "", ProfileNameReason.DISABLED),
        ("managed", "Manual", "Old managed", "Remote", "Manual", ProfileNameReason.LOCAL_OVERRIDE),
        ("managed", "Unowned", None, "Remote", "Unowned", ProfileNameReason.UNOWNED_LOCAL_NAME),
        ("managed", "Preserved", "Preserved", None, "Preserved", ProfileNameReason.EMPTY_REMOTE_NAME),
        ("managed", "Preserved", "Preserved", "Preserved", "Preserved", ProfileNameReason.SAME_NAME),
    ],
)
def test_fill_override_unowned_and_empty_profile(profiled, sync_mode, name, baseline, remote, expected, reason):
    local = profiled
    mode(local, sync_mode)
    seed(local.env, AccountStatus.ACTIVE, datetime(2025, 1, 1))
    with local.session.begin():
        local.session.execute(sa.update(Account).values(name=name))
        if baseline is not None:
            local.session.execute(
                sa.update(Identity).values(
                    last_applied_json=json.dumps({"schema_version": 1, "name": baseline, "name_generation": 0})
                )
            )
    execute(local, ready(local, Action.USE_BOUND), **arguments(local, remote))
    with Session(local.engine) as reader:
        assert reader.scalar(sa.select(Account.name)) == expected
        assert json.loads(reader.scalar(sa.select(Identity.profile_sync_json)))["name_reason"] == reason.value


@pytest.mark.parametrize("late", [True, False])
def test_late_or_ambiguous_attempt_keeps_snapshot_with_new_generation(profiled, late):
    local = profiled
    execute(local, ready(local), **arguments(local))
    with Session(local.engine) as reader:
        prior = reader.execute(
            sa.select(
                Identity.last_applied_json,
                Identity.profile_sync_json,
                Identity.remote_profile_version,
                Identity.remote_email,
                Identity.email_verified,
                Identity.last_seen_at,
            )
        ).one()
    audit = rows(local, Audit)
    execute(
        local,
        ready(local, Action.USE_BOUND),
        **arguments(
            local,
            "Late overwrite",
            auth_started_at=NOW - timedelta(seconds=2 if late else 1),
            correlation_id=UUID(int=802),
            now=NOW + timedelta(seconds=1),
        ),
    )
    with Session(local.engine) as reader:
        assert reader.scalar(sa.select(Account.name)) == "Ready"
        assert reader.scalar(sa.select(Identity.sync_generation)) == 2
        assert (
            reader.execute(
                sa.select(
                    Identity.last_applied_json,
                    Identity.profile_sync_json,
                    Identity.remote_profile_version,
                    Identity.remote_email,
                    Identity.email_verified,
                    Identity.last_seen_at,
                )
            ).one()
            == prior
        )
    assert rows(local, Audit) == audit


def test_real_created_baseline_managed_repeat_and_manual_override(profiled):
    local = profiled
    execute(local, ready(local), **arguments(local))
    for generation, expected_reason in ((2, ProfileNameReason.MANAGED_UPDATE), (3, ProfileNameReason.SAME_NAME)):
        result = execute(
            local,
            ready(local, Action.USE_BOUND),
            **arguments(
                local,
                "Updated remote",
                auth_started_at=NOW + timedelta(seconds=generation),
                now=NOW + timedelta(seconds=generation + 1),
                correlation_id=UUID(int=800 + generation),
            ),
        )
        assert result.generation == generation
        with Session(local.engine) as reader:
            assert reader.scalar(sa.select(Account.name)) == "Updated remote"
            assert (
                json.loads(reader.scalar(sa.select(Identity.profile_sync_json)))["name_reason"] == expected_reason.value
            )
            assert json.loads(reader.scalar(sa.select(Identity.last_applied_json)))["name_generation"] == 2
    with local.session.begin():
        local.session.execute(sa.update(Account).values(name="Local manual"))
    execute(
        local,
        ready(local, Action.USE_BOUND),
        **arguments(
            local, "Updated remote", auth_started_at=NOW + timedelta(seconds=5), now=NOW + timedelta(seconds=6)
        ),
    )
    with Session(local.engine) as reader:
        assert reader.scalar(sa.select(Account.name)) == "Local manual"
        assert json.loads(reader.scalar(sa.select(Identity.last_applied_json)))["name"] == "Updated remote"
        assert (
            json.loads(reader.scalar(sa.select(Identity.profile_sync_json)))["name_reason"]
            == ProfileNameReason.LOCAL_OVERRIDE.value
        )


@pytest.mark.parametrize("field", ["email", "timezone", "interface_language", "interface_theme", "initialized_at"])
def test_profile_name_exception_does_not_relax_other_account_guards(profiled, monkeypatch, field):
    local = profiled
    before = all_business(local)
    bundle = ready(local)
    scanner = CasdoorAccountPreflightRepository._observe_postwrite_new_collisions

    def tamper(repository, *args, **kwargs):
        result = scanner(repository, *args, **kwargs)
        assert repository._session.scalar(sa.select(Identity.remote_profile_version)) is not None
        value = None if field == "initialized_at" else "changed@example.test" if field == "email" else "changed"
        repository._session.execute(sa.update(Account).values({field: value}))
        return result

    monkeypatch.setattr(CasdoorAccountPreflightRepository, "_observe_postwrite_new_collisions", tamper)
    with pytest.raises(CasdoorLoginScopeConflict):
        execute(local, bundle, **arguments(local))
    assert all_business(local) == before


def test_new_profile_root_rejects_avatar_delta_against_original_default(profiled, monkeypatch):
    local = profiled
    before = all_business(local)
    bundle = ready(local)
    scanner = CasdoorAccountPreflightRepository._observe_postwrite_new_collisions

    def tamper(repository, *args, **kwargs):
        session = repository._session
        assert session.scalar(sa.select(Identity.remote_profile_version)) is not None
        assert session.scalar(sa.select(Account.avatar)) is None
        changed = session.execute(
            sa.update(Account).where(Account.id == str(bundle[0].account_id)).values(avatar="unauthorized-avatar")
        )
        assert changed.rowcount == 1
        assert session.scalar(sa.select(Account.avatar)) == "unauthorized-avatar"
        return scanner(repository, *args, **kwargs)

    monkeypatch.setattr(CasdoorAccountPreflightRepository, "_observe_postwrite_new_collisions", tamper)
    with pytest.raises(CasdoorLoginScopeConflict):
        execute(local, bundle, **arguments(local))
    assert all_business(local) == before
    assert bundle[0]._consumed


@pytest.mark.parametrize(
    "field",
    [
        "profile",
        "auth_started_at",
        "correlation_id",
        "now",
        "subject",
        "name",
        "verified",
        "naive",
        "non_utc",
        "future",
        "partial",
    ],
)
def test_bad_or_partial_profile_inputs_reject_before_sql(profiled, field):
    local = profiled
    bundle = ready(local)
    kwargs = arguments(local)
    if field in ("profile", "auth_started_at", "correlation_id", "now"):
        kwargs[field] = None
    elif field == "subject":
        kwargs["profile"] = replace(kwargs["profile"], subject="Other")
    elif field == "name":
        kwargs["profile"] = replace(kwargs["profile"], name="bad\nname")
    elif field == "verified":
        kwargs["profile"] = replace(kwargs["profile"], email_verified=1)
    elif field == "naive":
        kwargs["now"] = NOW.replace(tzinfo=None)
    elif field == "non_utc":
        kwargs["now"] = NOW.astimezone(timezone(timedelta(hours=8)))
    elif field == "future":
        kwargs["auth_started_at"] = NOW + timedelta(seconds=1)
    else:
        kwargs["profile"] = object.__new__(VerifiedProfile)
    statements = []

    def record(_connection, _cursor, sql, _params, _context, _many):
        statements.append(sql)

    sa.event.listen(local.engine, "before_cursor_execute", record)
    try:
        with pytest.raises((CasdoorLoginScopeConflict, CasdoorProfileConflict)):
            execute(local, bundle, **kwargs)
        assert not statements
        assert not bundle[0]._consumed
    finally:
        sa.event.remove(local.engine, "before_cursor_execute", record)


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("failure", ["profile", "audit"])
def test_sql_failure_rolls_back_all_prior_owners(profiled, bound, failure):
    local = profiled
    if bound:
        seed(local.env, AccountStatus.PENDING)
        with local.session.begin():
            local.session.execute(
                sa.update(Identity).values(
                    last_applied_json=json.dumps({"schema_version": 1, "name": "Ready", "name_generation": 0})
                )
            )
    before = all_business(local)
    bundle = ready(local, Action.INITIALIZE_BOUND if bound else Action.CREATE_INITIALIZED)
    with local.session.begin():
        statement = (
            "CREATE TRIGGER fail_profile BEFORE UPDATE ON casdoor_identity_extend "
            "WHEN NEW.remote_profile_version IS NOT OLD.remote_profile_version "
            if failure == "profile"
            else "CREATE TRIGGER fail_profile BEFORE INSERT ON casdoor_audit_extend "
        )
        local.session.execute(sa.text(statement + "BEGIN SELECT RAISE(ABORT, 'offline profile failure'); END"))
    with pytest.raises(IntegrityError):
        execute(local, bundle, **arguments(local, "Changed" if bound else "Ready"))
    assert all_business(local) == before
    assert bundle[0]._consumed


@pytest.mark.parametrize(
    "target", ["name", "baseline", "watermark", "version", "email", "verified", "last_seen", "collision", "lease"]
)
def test_after_profile_tamper_f12_or_final_lease_rolls_back_every_owner(profiled, monkeypatch, target):
    local = profiled
    before = all_business(local)
    bundle = ready(local)
    original = CasdoorLoginScopeRepository.recheck_before_commit

    def sabotage(repository, *args, **kwargs):
        original(repository, *args, **kwargs)
        session = repository.session
        if target == "name":
            session.execute(sa.update(Account).values(name="Unauthorized"))
        elif target == "baseline":
            session.execute(
                sa.update(Identity).values(last_applied_json='{"schema_version":1,"name":"Other","name_generation":1}')
            )
        elif target == "watermark":
            sync = json.loads(session.scalar(sa.select(Identity.profile_sync_json)))
            sync["correlation_id"] = str(UUID(int=999))
            session.execute(sa.update(Identity).values(profile_sync_json=json.dumps(sync)))
        elif target == "version":
            session.execute(sa.update(Identity).values(remote_profile_version="sha256:" + "f" * 64))
        elif target == "email":
            session.execute(sa.update(Identity).values(remote_email="tamper@example.test"))
        elif target == "verified":
            session.execute(sa.update(Identity).values(email_verified=False))
        elif target == "last_seen":
            session.execute(sa.update(Identity).values(last_seen_at=NOW.replace(tzinfo=None) + timedelta(seconds=1)))
        elif target == "lease":
            bundle[2].data[bundle[3].canonical_keys[-1]] = (b"winner", monotonic() + 40)

    if target == "collision":
        scanner = CasdoorAccountPreflightRepository._observe_postwrite_new_collisions

        def add_collision(repository, *args, **kwargs):
            assert repository._session.scalar(sa.select(Identity.remote_profile_version)) is not None
            repository._session.add(Account(name="Racer", email="NEW@example.test", status=AccountStatus.ACTIVE))
            repository._session.flush()
            return scanner(repository, *args, **kwargs)

        monkeypatch.setattr(CasdoorAccountPreflightRepository, "_observe_postwrite_new_collisions", add_collision)
    else:
        monkeypatch.setattr(CasdoorLoginScopeRepository, "recheck_before_commit", sabotage)
    with pytest.raises((CasdoorLoginScopeConflict, CasdoorLeaseError)):
        execute(local, bundle, **arguments(local))
    assert all_business(local) == before
