"""Registered LOCAL recovery with real SQL/source/release owners and offline wires."""

import pytest
import sqlalchemy as sa
from test_casdoor_local_lifecycle_flow_extend import managed, review, send

from configs import dify_config
from core.casdoor.crypto import CryptoError
from models.account import Account, TenantAccountJoin, TenantAccountRole
from models.casdoor_extend import (
    CasdoorAuditExtend as Audit,
)
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
    CasdoorManagedMembershipExtend as History,
)
from models.casdoor_extend import (
    CasdoorNamespaceExtend as Namespace,
)
from models.casdoor_extend import (
    CasdoorNamespaceLifecycle,
)
from models.casdoor_extend import (
    CasdoorValidationExtend as Validation,
)
from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationRepository
from services.account_password_hasher import DefaultAccountPasswordHasher

pytest_plugins = ("test_casdoor_local_lifecycle_flow_extend",)
ROOT = "/console/api/system-manage-extend/integration/casdoor"
RESET = ROOT + "/reset-namespace"
MODELS = (Account, TenantAccountJoin, Identity, History, Revision, Validation, Audit, Namespace, Integration)


def rows(d, models=MODELS):
    with d.f.service._session_factory() as session:
        return {
            model: tuple(tuple(row) for row in session.execute(sa.select(*model.__table__.columns).order_by(model.id)))
            for model in models
        }


def prepare(d, monkeypatch, *, history=True, late_action=False):
    credential = DefaultAccountPasswordHasher().hash("SyntheticPassword12")
    with d.f.service._session_factory() as session, session.begin():
        actor = session.get(Account, d.actor.id)
        actor.password, actor.password_salt = credential.password_hash, credential.password_salt
    monkeypatch.setattr(dify_config, "ENABLE_EMAIL_PASSWORD_LOGIN", True)
    if history:
        target = managed(d)
        proof = review(d, target, "release")
        assert send(d, ROOT + "/local-membership/release", method="POST", json=proof).status_code == 200
        proof = review(d, target, "adopt")
        assert send(d, ROOT + "/local-membership/adopt", method="POST", json=proof).status_code == 200
    if late_action:
        from test_casdoor_identity_action_flow_extend import begin

        d.late_state = begin(d)[0]
    snapshot = d.services.casdoor_configuration.get(d.actor)
    disabled = send(d, ROOT + "/disable", method="POST", json={"etag": snapshot.etag})
    assert disabled.status_code == 200, disabled.json
    if history:
        proof = review(d, target, "release")
        assert send(d, ROOT + "/local-membership/release", method="POST", json=proof).status_code == 200
    d.reset_input = {
        "namespace_id": str(snapshot.active.namespace_id),
        "etag": disabled.json["configuration"]["etag"],
        "confirm_management_review": True,
    }
    return d


@pytest.fixture
def resettable(lifecycle, monkeypatch):
    return prepare(lifecycle, monkeypatch)


def reset_review(d):
    response = send(d, RESET + "/review", method="POST", json=d.reset_input)
    assert response.status_code == 200, response.json
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json["credential_check"] == "format_only"
    assert response.json["expires_in"] == 60
    return {key: response.json[key] for key in ("review_id", "etag")}


def test_registered_release_reset_fresh_get_save_secret_aad_and_preserved_rows(resettable):
    d = resettable
    before = rows(d)
    with d.f.service._session_factory() as session, session.begin():
        owner = d.services.casdoor_configuration._repository(session)
        old = owner._revision(owner._integration().id, owner._integration().active_revision_id)
        old_id, old_envelope = old.id, old.encrypted_secret
        plaintext = owner.crypto.decrypt(old_envelope, context=owner._secret_context(old.namespace_id, old.id))
    proof = reset_review(d)
    response = send(d, RESET, method="POST", json=proof)
    assert response.status_code == 200, response.json
    assert response.json["enabled"] is False
    assert response.json["active"] is None
    assert response.json["draft"]["namespace_id"] != d.reset_input["namespace_id"]
    assert response.json["draft"]["validation"] == []
    actual = send(d, ROOT)
    assert actual.status_code == 200
    assert actual.json == response.json
    after = rows(d)
    for model in (Account, TenantAccountJoin, Identity, History, Validation):
        assert after[model] == before[model]
    assert set(before[Revision]) < set(after[Revision])
    assert set(before[Audit]) < set(after[Audit])
    with d.f.service._session_factory() as session, session.begin():
        owner = d.services.casdoor_configuration._repository(session)
        old_ns = session.get(Namespace, d.reset_input["namespace_id"])
        assert old_ns.lifecycle is CasdoorNamespaceLifecycle.ARCHIVED
        assert old_ns.archived_at is not None
        old = owner._revision(owner._integration().id, old_id)
        new = owner._revision(owner._integration().id, owner._integration().draft_revision_id)
        assert old.encrypted_secret == old_envelope
        assert new.encrypted_secret != old_envelope
        context = owner._secret_context(new.namespace_id, new.id)
        assert owner.crypto.decrypt(new.encrypted_secret, context=context) == plaintext
        with pytest.raises(CryptoError):
            owner.crypto.decrypt(old_envelope, context=context)
    saved = send(
        d,
        ROOT,
        method="PUT",
        json={"etag": actual.json["etag"], "configuration": actual.json["draft"]["configuration"]},
    )
    assert saved.status_code == 200, saved.json
    assert saved.json["draft"]["namespace_id"] == response.json["draft"]["namespace_id"]
    assert saved.json["etag"] == response.json["etag"] + 1
    assert send(d, RESET, method="POST", json=proof).status_code == 400
    assert (
        send(d, ROOT + "/activate", method="POST", json={"etag": saved.json["etag"], "revision_id": old_id}).status_code
        == 409
    )


def test_empty_namespace_actual_reset_save_without_remote_dependency(lifecycle, monkeypatch):
    d = prepare(lifecycle, monkeypatch, history=False)
    response = send(d, RESET, method="POST", json=reset_review(d))
    assert response.status_code == 200, response.json
    assert response.json["draft"]["namespace_id"] != d.reset_input["namespace_id"]
    assert not rows(d, (Identity, History))[Identity]
    assert not rows(d, (Identity, History))[History]
    actual = send(d, ROOT).json
    assert (
        send(
            d, ROOT, method="PUT", json={"etag": actual["etag"], "configuration": actual["draft"]["configuration"]}
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "failure",
    ["extra", "confirm_false", "bool_etag", "expired", "revoked", "fence", "join", "audit", "credential", "flag"],
)
def test_closed_review_and_drift_deny_real_mutation(resettable, monkeypatch, failure):
    d = resettable
    if failure in ("extra", "confirm_false", "bool_etag"):
        changes = {
            "extra": {"quiescent": True},
            "confirm_false": {"confirm_management_review": False},
            "bool_etag": {"etag": True},
        }[failure]
        before = rows(d)
        assert send(d, RESET + "/review", method="POST", json=d.reset_input | changes).status_code == 400
        assert rows(d) == before
        return
    proof = reset_review(d)
    if failure == "expired":
        key = "casdoor:namespace-reset-review:v1:" + proof["review_id"]
        raw, _ = d.review_records[key]
        d.review_records[key] = (raw, 0)
    elif failure == "revoked":
        d.source.clear()
    elif failure == "flag":
        monkeypatch.setattr(dify_config, "ENABLE_EMAIL_PASSWORD_LOGIN", False)
    else:
        with d.f.service._session_factory() as session, session.begin():
            if failure == "fence":
                session.execute(sa.update(Namespace).values(fence_epoch=Namespace.fence_epoch + 1))
            elif failure == "join":
                session.execute(
                    sa.update(TenantAccountJoin)
                    .where(TenantAccountJoin.account_id == d.actor.id)
                    .values(role=TenantAccountRole.EDITOR)
                )
            elif failure == "credential":
                session.execute(sa.update(Account).where(Account.id == d.actor.id).values(password="invalid"))
            else:
                session.execute(
                    sa.update(Audit).where(Audit.action == "local_membership_release_v1").values(summary_json="{}")
                )
    before = rows(d)
    assert send(d, RESET, method="POST", json=proof).status_code == 400
    assert rows(d) == before
    assert send(d, RESET, method="POST", json=proof).status_code == 400


@pytest.mark.parametrize(
    "failure",
    ["flush", "crypto", "source_after_write", "manager_after_write", "late_join", "late_intent", "lease_lost"],
)
def test_whole_root_rollback_after_archive_and_draft(resettable, monkeypatch, failure):
    d = resettable
    proof, before = reset_review(d), rows(d)
    original = CasdoorConfigurationRepository.reset_namespace
    original_policy = d.services.casdoor_configuration._management_policy
    if failure == "crypto":
        from core.casdoor.crypto import CasdoorCrypto

        def broken(*args, **kwargs):
            raise CryptoError("synthetic_failure")

        monkeypatch.setattr(CasdoorCrypto, "encrypt", broken)
    else:

        def after_write(owner, *args, **kwargs):
            result = original(owner, *args, **kwargs)
            if failure == "flush":
                raise RuntimeError("synthetic after flush")
            if failure == "source_after_write":
                d.source.clear()
            elif failure == "manager_after_write":
                owner.session.execute(
                    sa.update(TenantAccountJoin)
                    .where(TenantAccountJoin.account_id == d.actor.id, TenantAccountJoin.current.is_(True))
                    .values(role=TenantAccountRole.NORMAL)
                )
                owner.session.flush()
            elif failure == "late_join":
                owner.session.execute(
                    sa.update(TenantAccountJoin)
                    .where(TenantAccountJoin.account_id == d.actor.id)
                    .values(role=TenantAccountRole.EDITOR)
                )
            elif failure == "lease_lost":
                d.f.lease.data.clear()
            elif failure == "late_intent":
                from test_casdoor_namespace_reset_repository_extend import insert_intent

                insert_intent(owner.session, d)
            return result

        monkeypatch.setattr(CasdoorConfigurationRepository, "reset_namespace", after_write)
    response = send(d, RESET, method="POST", json=proof)
    assert response.status_code in (400, 409, 503), response.json
    assert rows(d) == before
    if failure == "manager_after_write":
        monkeypatch.setattr(d.services.casdoor_configuration, "_management_policy", original_policy)
    assert send(d, RESET, method="POST", json=proof).status_code == 400


def test_old_actual_source_callback_after_archive_cannot_write(lifecycle, monkeypatch):
    from test_casdoor_identity_action_flow_extend import complete

    d = prepare(lifecycle, monkeypatch, late_action=True)
    assert send(d, RESET, method="POST", json=reset_review(d)).status_code == 200
    before = rows(d)
    result = complete(d, d.late_state)
    assert result.status_code in (400, 302)
    assert not result.location or not result.location.endswith("=linked")
    assert rows(d) == before
