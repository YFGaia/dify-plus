"""Actual C1 root + readonly C2 signed fixture; synthetic offline evidence only."""

import json
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest
import sqlalchemy as sa
from core.casdoor.admission import AdmissionAction as Action
from models.account import Account, AccountStatus
from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision
from models.casdoor_extend import CasdoorFinalizationState
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_avatar_repository_extend import CasdoorAvatarRepository
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict, CasdoorLoginScopeRepository
from services.casdoor_local_login_coordinator_service_extend import _CoordinationConflict
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_casdoor_local_login_coordinator_service_extend import chain as original_chain
from test_casdoor_local_login_coordinator_service_extend import rows as chain_rows
from test_casdoor_local_login_coordinator_service_extend import signing as original_signing
from test_casdoor_local_login_profile_extend import (
    NOW,
    all_business,
    arguments,
    execute,
    ready,
    rows,
    seed,
)
from test_casdoor_local_login_profile_extend import profiled as original_profiled
from test_casdoor_local_login_service_extend import config_factory
from test_casdoor_local_login_service_extend import local as original_local
from test_casdoor_local_login_service_extend import login_env as original_env

chain = original_chain
signing = original_signing
profiled = original_profiled
local_fixture = original_local
login_env = original_env

URL = "https://images.example.test/avatar.png?signature=synthetic-secret"


def policy(local, mode):
    config = local.config.model_copy(
        update={"avatar_sync": mode != "off", "avatar_mode": mode if mode != "off" else "fill_empty"}
    )
    with local.session.begin():
        revision = local.session.get(Revision, str(local.env[1].revision_id))
        data = json.loads(revision.policy_json)
        data.update(avatar_sync=config.avatar_sync, avatar_mode=config.avatar_mode)
        local.session.execute(sa.update(Revision).values(policy_json=json.dumps(data)))
        local.session.expire(revision)
        digest = config_factory(local.session)._validation_digest(config, revision)
        local.session.execute(sa.update(Revision).values(config_digest=digest))
    local.env = (local.session, replace(local.env[1], config_digest=digest), *local.env[2:])
    local.config = config


def args(local, **updates):
    result = arguments(local, **updates)
    result["profile"] = replace(result["profile"], picture=URL)
    return result


@pytest.mark.parametrize("mode", ["off", "fill_empty", "managed"])
@pytest.mark.parametrize("bound", [False, True])
def test_actual_create_bound_pending_before_barriers_same_root_and_b3_generation(profiled, monkeypatch, mode, bound):
    local = profiled
    policy(local, mode)
    if bound:
        seed(local.env, AccountStatus.ACTIVE, NOW.replace(tzinfo=None))
    observations = []
    original = CasdoorAvatarRepository.persist_pending

    def observe(owner, *values, **kwargs):
        session = owner._session
        assert session.scalar(sa.select(Identity.sync_generation)) == 1
        assert not session.new and not session.dirty and not session.deleted
        result = original(owner, *values, **kwargs)
        observations.append((session, session.get_transaction(), result))
        return result

    monkeypatch.setattr(CasdoorAvatarRepository, "persist_pending", observe)
    barrier = CasdoorLoginScopeRepository.recheck_before_commit

    def checked(owner, *values, **kwargs):
        assert len(observations) == 1
        assert owner.session is observations[0][0]
        assert owner.session.get_transaction() is observations[0][1]
        assert owner.session.scalar(sa.select(sa.func.count()).select_from(Intent)) == (mode != "off")
        return barrier(owner, *values, **kwargs)

    monkeypatch.setattr(CasdoorLoginScopeRepository, "recheck_before_commit", checked)
    result = execute(local, ready(local, Action.USE_BOUND if bound else Action.CREATE_INITIALIZED), **args(local))
    assert result.generation == 1
    assert all(item.finalization is CasdoorFinalizationState.PENDING for item in result.workspaces)
    assert observations[0][2].reason == ("disabled" if mode == "off" else "pending")
    with Session(local.engine) as reader:
        assert reader.scalar(sa.select(Account.avatar)) is None
        assert reader.scalar(sa.select(Identity.sync_generation)) == 1
        for raw in reader.scalars(sa.select(Identity.profile_sync_json)):
            assert "picture" not in raw and "avatar" not in raw and "signature" not in raw
        for raw in reader.scalars(sa.select(Intent.desired_json)):
            assert URL not in raw and "synthetic-secret" not in raw


@pytest.mark.parametrize(
    "baseline", [None, "", " ", "https://manual.example.test/private?token=sensitive", str(UUID(int=444))]
)
@pytest.mark.parametrize("mode", ["fill_empty", "managed"])
def test_first_bound_import_never_adopts_nonempty_local_avatar(profiled, baseline, mode):
    local = profiled
    policy(local, mode)
    seed(local.env, AccountStatus.ACTIVE, NOW.replace(tzinfo=None))
    with local.session.begin():
        local.session.execute(sa.update(Account).values(avatar=baseline))
    execute(local, ready(local, Action.USE_BOUND), **args(local))
    with Session(local.engine) as reader:
        assert reader.scalar(sa.select(Account.avatar)) == baseline
        outbox = list(reader.scalars(sa.select(Intent)))
        assert len(outbox) == (baseline in (None, ""))
        if outbox:
            assert json.loads(outbox[0].desired_json)["baseline"] == baseline


@pytest.mark.parametrize("late", [True, False])
def test_late_or_equal_foreign_profile_skips_avatar_even_after_b3_advance(profiled, late):
    local = profiled
    policy(local, "managed")
    execute(local, ready(local), **args(local))
    before = rows(local, Intent)
    execute(
        local,
        ready(local, Action.USE_BOUND),
        **args(
            local,
            auth_started_at=NOW - timedelta(seconds=2 if late else 1),
            correlation_id=UUID(int=991),
            now=NOW + timedelta(seconds=1),
        ),
    )
    assert rows(local, Intent) == before
    with Session(local.engine) as reader:
        assert reader.scalar(sa.select(Identity.sync_generation)) == 2


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("failure", ["outbox", "avatar_audit", "avatar_guard"])
def test_avatar_write_audit_or_guard_failure_rolls_back_original_root(profiled, monkeypatch, bound, failure):
    local = profiled
    policy(local, "managed")
    if bound:
        seed(local.env, AccountStatus.PENDING)
    before, outbox_before = all_business(local), rows(local, Intent)
    bundle = ready(local, Action.INITIALIZE_BOUND if bound else Action.CREATE_INITIALIZED)
    if failure == "avatar_guard":
        original = CasdoorAvatarRepository.persist_pending

        def tamper(owner, *args, **kwargs):
            result = original(owner, *args, **kwargs)
            owner._session.execute(sa.update(Account).values(avatar="unauthorized-file"))
            return result

        monkeypatch.setattr(CasdoorAvatarRepository, "persist_pending", tamper)
        error = CasdoorLoginScopeConflict
    else:
        with local.session.begin():
            trigger = (
                "CREATE TRIGGER fail_avatar BEFORE INSERT ON casdoor_sync_intent_extend "
                "WHEN NEW.kind='profile_avatar' "
                if failure == "outbox"
                else "CREATE TRIGGER fail_avatar BEFORE INSERT ON casdoor_audit_extend "
                "WHEN NEW.action='avatar_pending' "
            )
            local.session.execute(sa.text(trigger + "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"))
        error = IntegrityError
    with pytest.raises(error):
        execute(local, bundle, **args(local))
    assert all_business(local) == before
    assert rows(local, Intent) == outbox_before


def test_c2_only_second_fresh_picture_changes_each_pass_no_local_write_or_session(chain, monkeypatch):
    from services.casdoor_local_login_service_extend import CasdoorLocalLoginService

    calls = []
    original = CasdoorLocalLoginService._persist_local_login

    def observe(owner, **kwargs):
        calls.append(True)
        return original(owner, **kwargs)

    monkeypatch.setattr(CasdoorLocalLoginService, "_persist_local_login", observe)
    chain.profile["picture"] = URL

    def hook(path, count):
        if path == "/api/userinfo" and count in (2, 4):
            chain.profile["picture"] = URL + str(count)

    chain.control.hook = hook
    before = chain_rows(chain)
    with pytest.raises(_CoordinationConflict, match="drift_limit"):
        chain.invoke()
    assert not calls and not chain.sessions
    assert chain_rows(chain) == before
    assert chain.calls.count("/api/userinfo") == 4 and len(chain.prepared) == 2
    assert all(not p._consumed for p in chain.prepared)
    assert not chain.redis.data and not chain.opened
