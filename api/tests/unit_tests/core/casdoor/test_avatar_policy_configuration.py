"""Avatar configuration intent only; synthetic in-memory proof is not live evidence."""

import json
from uuid import UUID

import pytest
import sqlalchemy as sa
import test_configuration_repository_extend as foundation
from core.casdoor.configuration import CasdoorConfiguration
from models.casdoor_extend import CasdoorConfigRevisionExtend, CasdoorValidationExtend
from pydantic import ValidationError
from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationError
from test_configuration_repository_extend import (
    ACTOR,
    NOW,
    config,
    proofs,
    repo,
    save,
)

# Reuse synthetic SQLite/certificate fixtures without changing the original suite.
pin = foundation.pin
storage = foundation.storage

WORKSPACE = UUID("20000000-0000-4000-8000-000000000001")


def test_avatar_schema_defaults_and_explicit_mode():
    configuration = config(WORKSPACE)
    assert configuration.schema_version == 1
    assert configuration.avatar_sync is False
    assert configuration.avatar_mode == "fill_empty"
    schema = CasdoorConfiguration.model_json_schema()["properties"]
    assert schema["avatar_mode"]["enum"] == ["fill_empty", "managed"]
    assert schema["avatar_mode"]["default"] == "fill_empty"
    assert schema["avatar_sync"]["default"] is False


@pytest.mark.parametrize("value", ["off", "MANAGED", " managed", "managed ", "", None, True, 1, [], {}])
def test_avatar_mode_rejects_invalid_values(value):
    with pytest.raises(ValidationError):
        config(WORKSPACE, avatar_mode=value)


@pytest.mark.parametrize("value", ["true", "false", 0, 1, None])
def test_avatar_switch_remains_strict_bool(value):
    with pytest.raises(ValidationError):
        config(WORKSPACE, avatar_sync=value)


@pytest.mark.parametrize("avatar_mode", ["fill_empty", "managed"])
@pytest.mark.parametrize("name_sync", ["off", "fill_empty", "managed"])
@pytest.mark.parametrize("avatar_sync", [False, True])
def test_avatar_mode_and_switch_are_independent_of_name(name_sync, avatar_mode, avatar_sync):
    configuration = config(WORKSPACE, name_sync=name_sync, avatar_mode=avatar_mode, avatar_sync=avatar_sync)
    assert configuration.name_sync == name_sync
    assert configuration.avatar_mode == avatar_mode
    assert configuration.avatar_sync is avatar_sync
    assert json.loads(configuration.canonical_json())["avatar_mode"] == avatar_mode


def test_avatar_mode_changes_policy_digest_even_when_off():
    empty = config(WORKSPACE, avatar_sync=False, avatar_mode="fill_empty")
    managed = config(WORKSPACE, avatar_sync=False, avatar_mode="managed")
    assert empty.config_digest() != managed.config_digest()


def test_mode_roundtrip_keep_secret_creates_immutable_revision(storage):
    session, workspace = storage
    repository = repo(session)
    first = save(session, config(workspace, avatar_sync=True), repository=repository)
    old = session.get(CasdoorConfigRevisionExtend, str(first.draft_revision_id))
    old_policy, old_digest, old_envelope = old.policy_json, old.config_digest, old.encrypted_secret
    session.rollback()
    second = save(
        session,
        config(workspace, avatar_sync=True, avatar_mode="managed"),
        etag=1,
        secret=None,
        repository=repository,
    )
    new = session.get(CasdoorConfigRevisionExtend, str(second.draft_revision_id))
    assert second.draft.configuration.avatar_mode == "managed"
    assert not second.enabled
    assert second.active is None
    assert first.draft_revision_id != second.draft_revision_id
    assert first.draft.namespace_id == second.draft.namespace_id
    assert new.revision_number == 2
    assert json.loads(old.policy_json)["avatar_mode"] == "fill_empty"
    assert json.loads(new.policy_json)["avatar_mode"] == "managed"
    assert (old.policy_json, old.config_digest, old.encrypted_secret) == (old_policy, old_digest, old_envelope)
    assert new.config_digest != old_digest
    assert new.encrypted_secret != old_envelope
    assert (
        repository.crypto.decrypt(new.encrypted_secret, context=repository._secret_context(new.namespace_id, new.id))
        == "synthetic-client-secret"
    )
    assert repository.get().draft.configuration == second.draft.configuration


def test_missing_stored_avatar_mode_is_rejected_without_defaulting(storage):
    session, workspace = storage
    snapshot = save(session, config(workspace))
    revision = session.get(CasdoorConfigRevisionExtend, str(snapshot.draft_revision_id))
    policy = json.loads(revision.policy_json)
    del policy["avatar_mode"]
    revision.policy_json = json.dumps(policy)
    with pytest.raises(CasdoorConfigurationError) as error:
        repo(session).get()
    assert error.value.reason == "revision_policy_invalid"


@pytest.mark.parametrize("avatar_mode", ["fill_empty", "managed"])
def test_enabled_avatar_modes_require_static_capability(storage, pin, avatar_mode):
    session, workspace = storage
    snapshot = save(session, config(workspace, pin, avatar_sync=True, avatar_mode=avatar_mode))
    with session.begin():
        proofs(session, snapshot.draft_revision_id)
        with pytest.raises(CasdoorConfigurationError) as error:
            repo(session).activate(etag=1, revision_id=snapshot.draft_revision_id, actor_account_id=ACTOR, now=NOW)
        assert error.value.reason == "optional_capability_unknown"
        assert not repo(session).get().enabled


@pytest.mark.parametrize("avatar_mode", ["fill_empty", "managed"])
def test_off_does_not_require_avatar_capability_or_infer_from_name(storage, pin, avatar_mode):
    session, workspace = storage
    snapshot = save(session, config(workspace, pin, name_sync="managed", avatar_mode=avatar_mode, avatar_sync=False))
    with session.begin():
        proofs(session, snapshot.draft_revision_id)
        active = repo(session).activate(etag=1, revision_id=snapshot.draft_revision_id, actor_account_id=ACTOR, now=NOW)
        assert active.enabled
        assert active.active.configuration.avatar_sync is False


def test_mode_tampering_rejects_old_full_digest(storage):
    session, workspace = storage
    snapshot = save(session, config(workspace))
    revision = session.get(CasdoorConfigRevisionExtend, str(snapshot.draft_revision_id))
    policy = json.loads(revision.policy_json)
    policy["avatar_mode"] = "managed"
    revision.policy_json = json.dumps(policy)
    with pytest.raises(CasdoorConfigurationError) as error:
        repo(session).get()
    assert error.value.reason == "revision_digest_mismatch"


def test_old_proof_digest_cannot_authorize_changed_mode(storage, pin):
    session, workspace = storage
    first = save(session, config(workspace, pin, avatar_sync=True))
    with session.begin():
        proofs(session, first.draft_revision_id, optional=("avatar_sync",))
        old = session.get(CasdoorConfigRevisionExtend, str(first.draft_revision_id))
        old_digest = old.config_digest
    second = save(session, config(workspace, pin, avatar_sync=True, avatar_mode="managed"), etag=1, secret=None)
    with session.begin():
        proofs(session, second.draft_revision_id, optional=("avatar_sync",))
        rows = session.scalars(
            sa.select(CasdoorValidationExtend).where(
                CasdoorValidationExtend.revision_id == str(second.draft_revision_id)
            )
        ).all()
        for row in rows:
            row.config_digest = old_digest
        session.flush()
        with pytest.raises(CasdoorConfigurationError) as error:
            repo(session).activate(etag=2, revision_id=second.draft_revision_id, actor_account_id=ACTOR, now=NOW)
        assert error.value.reason == "validation_required"
        assert not repo(session).get().enabled
