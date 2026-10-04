"""Independent counterexamples for the Casdoor avatar policy patch.

These tests use only the existing synthetic SQLite foundation fixtures. They do
not exercise IdP traffic, profile synchronization, or production evidence.
"""

import json
from uuid import UUID

import pytest
from core.casdoor.configuration import CasdoorConfiguration
from models.casdoor_extend import CasdoorConfigRevisionExtend
from pydantic import ValidationError
from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationError
from test_configuration_repository_extend import config, repo, save, storage


WORKSPACE = UUID("20000000-0000-4000-8000-000000000001")


@pytest.mark.parametrize(
    "updates",
    [
        {"avatar_mode": "off"},
        {"avatar_mode": "MANAGED"},
        {"avatar_mode": " managed"},
        {"avatar_mode": None},
        {"avatar_sync": 1},
        {"avatar_sync": "true"},
    ],
)
def test_invalid_avatar_policy_values_do_not_coerce(updates):
    with pytest.raises(ValidationError):
        config(WORKSPACE, **updates)


@pytest.mark.parametrize("name_sync", ["off", "fill_empty", "managed"])
@pytest.mark.parametrize("avatar_mode", ["fill_empty", "managed"])
def test_mode_and_name_policy_remain_independent(name_sync, avatar_mode):
    value = config(WORKSPACE, name_sync=name_sync, avatar_mode=avatar_mode, avatar_sync=False)
    data = json.loads(value.canonical_json())
    assert (data["name_sync"], data["avatar_mode"], data["avatar_sync"]) == (
        name_sync,
        avatar_mode,
        False,
    )
    assert CasdoorConfiguration.model_validate(data) == value


def test_legacy_policy_cannot_hide_missing_mode_behind_model_default(storage):
    session, workspace = storage
    stored = save(session, config(workspace))
    revision = session.get(CasdoorConfigRevisionExtend, str(stored.draft_revision_id))
    policy = json.loads(revision.policy_json)
    del policy["avatar_mode"]
    revision.policy_json = json.dumps(policy, sort_keys=True, separators=(",", ":"))
    # Make the record digest internally consistent with the reconstructed
    # defaulted model; the repository's exact policy key-set must still fail.
    reconstructed = CasdoorConfiguration.model_validate(
        {
            **{
                field: getattr(revision, field)
                for field in (
                    "schema_version",
                    "browser_frontend_url",
                    "backend_api_url",
                    "expected_issuer",
                    "organization",
                    "application",
                    "client_id",
                    "button_text",
                    "default_workspace_id",
                )
            },
            **policy,
            "workspace_mappings": json.loads(revision.mappings_json),
            "certificates": json.loads(revision.certificates_json),
        }
    )
    revision.config_digest = repo(session)._validation_digest(reconstructed, revision)
    with pytest.raises(CasdoorConfigurationError) as error:
        repo(session).get()
    assert error.value.reason == "revision_policy_invalid"


@pytest.mark.parametrize("avatar_mode", ["fill_empty", "managed"])
def test_get_reads_explicit_mode_and_off_flag_without_inferring_execution(storage, avatar_mode):
    session, workspace = storage
    saved = save(
        session,
        config(workspace, avatar_sync=False, avatar_mode=avatar_mode, name_sync="managed"),
    )
    loaded = repo(session).get()
    assert loaded.draft.configuration.avatar_mode == avatar_mode
    assert loaded.draft.configuration.avatar_sync is False
    assert loaded.draft.configuration.name_sync == "managed"
    assert saved.draft.configuration.avatar_sync is False
