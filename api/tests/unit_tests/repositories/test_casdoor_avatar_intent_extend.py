"""Offline avatar outbox checks with original SQLite profile/config/crypto owners."""

import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.claims import VerifiedProfile
from core.casdoor.crypto import CryptoError, EncryptionContext, EncryptionPurpose
from models.account import Account, AccountStatus, TenantAccountJoin
from models.casdoor_avatar_file_guard_extend import CasdoorAvatarFileGuardExtend
from models.casdoor_extend import (
    CasdoorAuditExtend as Audit,
)
from models.casdoor_extend import (
    CasdoorIdentityExtend as Identity,
)
from models.casdoor_extend import (
    CasdoorIntegrationExtend as Integration,
)
from models.casdoor_extend import (
    CasdoorManagedMembershipExtend,
    CasdoorOperationState,
    CasdoorTerminationState,
)
from models.casdoor_extend import (
    CasdoorNamespaceExtend as Namespace,
)
from models.casdoor_extend import (
    CasdoorSyncIntentExtend as Intent,
)
from models.enums import CreatorUserRole
from models.model import UploadFile
from repositories.casdoor_avatar_repository_extend import (
    CasdoorAvatarConflict,
    CasdoorAvatarRepository,
    _desired,
)
from repositories.casdoor_profile_repository_extend import _dump
from sqlalchemy.orm import Session
from test_casdoor_profile_repository_extend import NOW, persist, profile
from test_casdoor_profile_repository_extend import storage as original_storage
from test_claims import NONCE, START, SUB, id_claims, sign
from test_claims import NOW as CLAIM_NOW
from test_claims import key as original_key
from test_claims import validator as original_validator

storage_fixture = original_storage
key = original_key
validator = original_validator
URL = "https://images.example.test/picture.png?signature=synthetic-sensitive-value"


@pytest.fixture
def avatar(storage_fixture):
    s = storage_fixture
    for model in (TenantAccountJoin, CasdoorManagedMembershipExtend, Intent, UploadFile, CasdoorAvatarFileGuardExtend):
        model.__table__.create(s.session.get_bind())
    with s.session.begin():
        config = s.config_owner._configuration(s.revision).model_copy(
            update={"avatar_sync": True, "avatar_mode": "managed"}
        )
        policy = json.loads(s.revision.policy_json)
        policy.update(avatar_sync=True, avatar_mode="managed")
        revision_type = type(s.revision)
        s.session.execute(sa.update(revision_type).values(policy_json=json.dumps(policy)))
        s.session.expire(s.revision)
        digest = s.config_owner._validation_digest(config, s.revision)
        s.session.execute(sa.update(revision_type).values(config_digest=digest))
        s.account.avatar = None
    s.context = replace(s.context, config_digest=s.revision.config_digest)
    s.admission = replace(s.admission, context=replace(s.admission.context, config_digest=s.context.config_digest))
    s.avatar_owner = CasdoorAvatarRepository(s.session, configuration_repository=s.config_owner)
    with s.session.begin():
        s.outcome = persist(s, profile=profile(s, picture=URL))
    yield s


def pending(s, **updates):
    args = dict(
        expected_generation=1, expected_fence_epoch=0, auth_started_at=NOW, correlation_id=s.correlation, now=NOW
    )
    context = updates.pop("context", s.context)
    outcome = updates.pop("profile_outcome", s.outcome)
    p = updates.pop("profile", profile(s, picture=URL))
    args.update(updates)
    return s.avatar_owner.persist_pending(context, p, outcome, **args)


def intents(s):
    with Session(s.session.get_bind()) as reader:
        return list(reader.scalars(sa.select(Intent)))


@pytest.mark.parametrize("candidate", [None, "", " ", 1, {}, [], "x" * 4097, "\ud800", "x\n", "x\x7f"])
def test_userinfo_optional_bad_picture_skips_without_affecting_profile(validator, key, candidate):
    identity = validator.verify_id_token(
        sign(key, id_claims()), expected_nonce=NONCE, auth_started_at=START, now=CLAIM_NOW
    )
    result = validator.verify_userinfo({"sub": SUB, "picture": candidate, "name": "Person"}, identity=identity)
    assert result.picture is None and result.name == "Person"
    assert repr(result) == "VerifiedProfile(<redacted>)"


def test_userinfo_picture_is_subject_bound_equality_and_six_positional_compatible(validator, key):
    from core.casdoor.claims import ClaimsError

    identity = validator.verify_id_token(
        sign(key, id_claims()), expected_nonce=NONCE, auth_started_at=START, now=CLAIM_NOW
    )
    result = validator.verify_userinfo({"sub": SUB, "picture": URL, "avatar": "ignored"}, identity=identity)
    assert result.picture == URL and URL not in repr(result)
    assert result != replace(result, picture=URL + "2")
    assert VerifiedProfile(SUB, None, None, None, None, None).picture is None
    assert validator.verify_userinfo({"sub": SUB, "avatar": URL}, identity=identity).picture is None
    with pytest.raises(ClaimsError, match="userinfo_subject_invalid"):
        validator.verify_userinfo({"sub": "foreign", "picture": URL}, identity=identity)


def test_pending_uses_exact_generation_encryption_aad_closed_schema_and_replay(avatar):
    s = avatar
    with s.session.begin():
        result = pending(s)
        replay = pending(s)
        assert result.intent_id == replay.intent_id and replay.reason == "replayed"
    [row] = intents(s)
    data = _desired(row)
    assert row.generation == s.outcome.generation == 1
    assert data["baseline"] is None and not data["reservations"] and data["result_file_id"] is None
    assert URL not in row.desired_json and "synthetic-sensitive-value" not in row.desired_json
    assert data["url_sha256"] == hashlib.sha256(URL.encode()).hexdigest()
    assert (
        s.config_owner.crypto.decrypt(
            data["url_ciphertext"],
            context=EncryptionContext(
                EncryptionPurpose.AVATAR_URL, s.context.namespace_id, s.context.revision_id, row.id
            ),
        )
        == URL
    )
    with pytest.raises(CryptoError):
        s.config_owner.crypto.decrypt(
            data["url_ciphertext"],
            context=EncryptionContext(
                EncryptionPurpose.AVATAR_URL, s.context.namespace_id, s.context.revision_id, str(uuid4())
            ),
        )
    with s.session.begin():
        assert s.session.scalar(sa.select(Account.avatar)) is None
        assert s.session.scalar(sa.select(Identity.sync_generation)) == 1
        avatar_audits = list(s.session.scalars(sa.select(Audit).where(Audit.action == "avatar_pending")))
        assert len(avatar_audits) == 1
        assert URL not in avatar_audits[0].summary_json and data["url_sha256"] not in avatar_audits[0].summary_json


@pytest.mark.parametrize(
    "candidate",
    [
        None,
        "",
        " ",
        "http://example.test/a",
        "https://u:p@example.test/a",
        "https://example.test/a#fragment",
        "https://127.0.0.1/a",
        "https://224.0.0.1/a",
        "https://[ff00::1]/a",
        "https://[2001:4860:4860::8888%25eth0]/a",
        "https://example.test/%zz",
        "https://[::1]/a",
        "https://10.0.0.1/a",
        "https://localhost/a",
        "https://metadata.internal/a",
        "https://2130706433/a",
        "https://example.test:80/a",
        "https://example.test/%0a",
        "https://example.test/\\x",
        "https://example.test/" + "x" * 2049,
        "\ud800",
    ],
)
def test_invalid_optional_url_never_writes(avatar, candidate):
    with avatar.session.begin():
        result = pending(avatar, profile=profile(avatar, picture=candidate))
        assert result.reason == "candidate_unavailable"
    assert not intents(avatar)


@pytest.mark.parametrize("tamper", ["generation", "fence", "disabled", "account", "revision"])
def test_current_database_parents_are_rechecked(avatar, tamper):
    s = avatar
    with s.session.begin():
        if tamper == "generation":
            s.session.execute(sa.update(Identity).values(sync_generation=2))
        elif tamper == "fence":
            s.session.execute(sa.update(Namespace).values(fence_epoch=1))
        elif tamper == "disabled":
            s.session.execute(sa.update(Integration).values(enabled=False))
        elif tamper == "account":
            s.session.execute(sa.update(Account).values(status=AccountStatus.BANNED))
        else:
            s.session.execute(sa.update(Integration).values(active_revision_id=None))
    with pytest.raises(CasdoorAvatarConflict), s.session.begin():
        pending(s)
    assert not intents(s)


@pytest.mark.parametrize("tamper", ["candidate", "ciphertext", "extra_key", "correlation", "baseline", "scope"])
def test_replay_collision_never_replaces_intent(avatar, tamper):
    s = avatar
    with s.session.begin():
        pending(s)
    with s.session.begin():
        row = s.session.scalar(sa.select(Intent))
        data = json.loads(row.desired_json)
        if tamper == "ciphertext":
            data["url_ciphertext"] = "invalid"
        elif tamper == "extra_key":
            data["extra"] = "forbidden"
        elif tamper == "correlation":
            # Keep watermark accepted but tamper the durable intent's timestamp.
            data["auth_started_at"] = (NOW - timedelta(seconds=1)).isoformat(timespec="microseconds")
        elif tamper == "baseline":
            data["baseline"] = ""
        elif tamper == "scope":
            row.scope_digest = "f" * 64
        row.desired_json = _dump(data)
    before = intents(s)[0].desired_json
    with pytest.raises(CasdoorAvatarConflict), s.session.begin():
        pending(s, profile=profile(s, picture=URL + "different" if tamper == "candidate" else URL))
    assert intents(s)[0].desired_json == before


def test_expiry_is_half_open_and_does_not_refresh_ciphertext(avatar):
    s = avatar
    with s.session.begin():
        pending(s)
    before = intents(s)[0].desired_json
    with s.session.begin():
        assert pending(s, now=NOW + timedelta(seconds=299)).reason == "replayed"
        assert pending(s, now=NOW + timedelta(seconds=300)).reason == "expired_url"
    assert intents(s)[0].desired_json == before


def applied_baseline(s):
    """Synthetic future worker receipt, never production ownership evidence."""
    with s.session.begin():
        pending(s)
    with s.session.begin():
        row = s.session.scalar(sa.select(Intent))
        file_id, attempt = str(uuid4()), str(uuid4())
        s.session.execute(
            sa.insert(UploadFile).values(
                id=file_id,
                tenant_id=str(s.config_owner._configuration(s.revision).default_workspace_id),
                storage_type="local",
                key=f"casdoor-avatar/{row.id}/{attempt}/{file_id}.png",
                name="avatar.png",
                size=9,
                extension="png",
                mime_type="image/png",
                created_by=str(s.context.account_id),
                created_by_role=CreatorUserRole.ACCOUNT,
            )
        )
        data = json.loads(row.desired_json)
        data["url_ciphertext"] = ""
        data["result_file_id"] = file_id
        data["reservations"] = [
            dict(
                attempt_id=attempt,
                file_id=file_id,
                storage_key=f"casdoor-avatar/{row.id}/{attempt}/{file_id}.png",
                cleanup_state="none",
            )
        ]
        row.desired_json = _dump(data)
        row.operation_state = CasdoorOperationState.APPLIED
        row.termination_state = CasdoorTerminationState.CONFIRMED
        row.attempt_count = 1
        row.resource_id = file_id
        s.session.execute(sa.update(Account).values(avatar=file_id))
        s.session.execute(sa.update(Identity).values(sync_generation=2))
    with s.session.begin():
        s.outcome = persist(
            s,
            expected_generation=2,
            auth_started_at=NOW + timedelta(seconds=1),
            now=NOW + timedelta(seconds=1),
            profile=profile(s, picture=URL),
        )
    return file_id


@pytest.mark.parametrize("state", ["owned", "manual", "foreign_creator", "wrong_role", "unowned_uuid"])
def test_managed_only_latest_applied_account_owned_file_can_be_baseline(avatar, state):
    s = avatar
    file_id = applied_baseline(s)
    with s.session.begin():
        if state == "manual":
            s.session.execute(sa.update(Account).values(avatar="https://manual.example.test/private"))
        elif state == "foreign_creator":
            s.session.execute(sa.update(UploadFile).values(created_by=str(uuid4())))
        elif state == "wrong_role":
            s.session.execute(sa.update(UploadFile).values(created_by_role=CreatorUserRole.END_USER))
        elif state == "unowned_uuid":
            s.session.execute(sa.update(Account).values(avatar=str(uuid4())))
    with s.session.begin():
        outcome = pending(
            s, expected_generation=2, auth_started_at=NOW + timedelta(seconds=1), now=NOW + timedelta(seconds=1)
        )
        assert outcome.reason == (
            "pending" if state == "owned" else "local_override" if state == "unowned_uuid" else "unowned_local_avatar"
        )
    rows = intents(s)
    assert len(rows) == (2 if state == "owned" else 1)
    if state == "owned":
        assert _desired(next(r for r in rows if r.generation == 2))["baseline"] == file_id
    assert any(r.operation_state == CasdoorOperationState.APPLIED for r in rows)


def test_repository_rejects_other_session_and_pending_unflushed_rows(avatar):
    s = avatar
    with Session(s.session.get_bind()) as other, pytest.raises(CasdoorAvatarConflict):
        CasdoorAvatarRepository(other, configuration_repository=s.config_owner)
    with pytest.raises(CasdoorAvatarConflict):
        pending(s)
    with s.session.begin():
        s.account.name = "Pending unrelated mutation"
        with pytest.raises(CasdoorAvatarConflict):
            pending(s)
        s.session.expire(s.account)
