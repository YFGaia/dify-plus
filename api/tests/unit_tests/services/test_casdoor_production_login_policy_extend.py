"""Real production factory and signed loader, with synthetic offline I/O only."""

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from configs import dify_config
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from enums import DeploymentEdition
from models.account import Account
from models.account_money_extend import AccountMoneyExtend
from models.casdoor_extend import (
    CasdoorIdentityExtend,
    CasdoorIntegrationExtend,
    CasdoorManagedMembershipExtend,
)
from services.account_login_adapters import RedisAccountSessionGateway
from services.casdoor_deployment_policy_service_extend import (
    CasdoorDeploymentPolicyService,
)
from services.casdoor_local_http_service_extend import CasdoorLocalHttpService
from services.casdoor_local_login_coordinator_service_extend import (
    CasdoorLocalLoginCoordinatorService,
)
from services.casdoor_local_login_finalization_service_extend import (
    CasdoorLocalLoginFinalizationService,
)
from test_casdoor_local_http_service_extend import begin, complete

pytest_plugins = ("test_casdoor_local_http_service_extend",)


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


@pytest.fixture
def production(http_flow, tmp_path, monkeypatch):
    flow = http_flow
    monkeypatch.setattr(dify_config, "DEPLOYMENT_EDITION", DeploymentEdition.COMMUNITY)
    now = datetime.now(UTC).replace(microsecond=0)
    context = flow.local.env[1]
    config = flow.local.config
    binding = {
        "namespace_id": str(context.namespace_id),
        "revision_id": str(context.revision_id),
        "config_digest": context.config_digest,
        "configuration_digest": config.config_digest(),
        "rbac_mode": "off",
        "deployment_edition": "COMMUNITY",
        **{
            name: getattr(config, name)
            for name in (
                "browser_frontend_url",
                "backend_api_url",
                "expected_issuer",
                "organization",
                "application",
                "client_id",
            )
        },
    }
    manifest = {
        "schema_version": 1,
        "authority_id": "synthetic-review-v1",
        "reviewed_source": "actual_deployment",
        "binding": binding,
        "issued_at": (now - timedelta(seconds=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "casdoor_release": "synthetic-offline-v1",
        "image_digest": "sha256:" + "a" * 64,
        "native_token_schema": "flat_user_v1",
        "directory_schema": "flat_directory_v1",
        "directory_authentication": "basic_header",
        "role_effects_profile": "community_local_v1",
        "evidence": {
            name: {"record_id": "synthetic/" + name, "sha256": "b" * 64}
            for name in (
                "release",
                "image",
                "non_dcr_creation",
                "organization_admin_scope",
                "directory_visibility",
                "directory_schema",
                "native_token_layout",
                "directory_authentication",
                "local_role_effects",
                "pkce_s256_enforcement",
                "id_token_contract",
                "nonce_contract",
                "userinfo_subject_contract",
            )
        },
    }
    payload = json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    key = Ed25519PrivateKey.generate()
    authority = tmp_path / "authority.json"
    evidence = tmp_path / "evidence.json"
    authority.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "authority_id": "synthetic-review-v1",
                "public_key": _b64(key.public_key().public_bytes_raw()),
                "accepted_manifest_sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    )
    evidence.write_text(json.dumps({"manifest": manifest, "signature": _b64(key.sign(payload))}))
    policy = CasdoorDeploymentPolicyService(authority_path=str(authority), evidence_path=str(evidence))
    # No subclass or fabricated policy: real DB preflight, loader and factory.
    flow.service = CasdoorLocalHttpService(
        session_factory=flow.service._session_factory,
        configuration_service=flow.config,
        account_activation=flow.service._account_activation,
        redis_client=flow.redis,
        settings=flow.settings,
        redis_runtime_factory=flow.service._redis_runtime_factory,
        deployment_policy_service=policy,
    )
    return SimpleNamespace(flow=flow, policy=policy, authority=authority, now=now, manifest=manifest)


def _business_counts(flow):
    with flow.local.engine.connect() as connection:
        return tuple(
            connection.scalar(sa.select(sa.func.count()).select_from(model))
            for model in (Account, CasdoorIdentityExtend, AccountMoneyExtend, CasdoorManagedMembershipExtend)
        )


@pytest.mark.parametrize("failure", ["missing", "revoked", "expired", "enterprise", "rbac"])
def test_preflight_denies_before_runtime_provider_or_business(production, monkeypatch, failure):
    env, flow = production, production.flow
    if failure == "missing":
        flow.service._deployment_policy_service = CasdoorDeploymentPolicyService()
    elif failure == "revoked":
        env.authority.unlink()
    elif failure == "expired":
        env.policy._now = lambda: env.now + timedelta(hours=2)
    elif failure == "enterprise":
        monkeypatch.setattr(dify_config, "DEPLOYMENT_EDITION", DeploymentEdition.ENTERPRISE)
    else:
        monkeypatch.setattr(dify_config, "RBAC_ENABLED", True)
    counts = _business_counts(flow)
    result = flow.service.start(browser_scope=None, server_ip="192.0.2.7")
    assert result.error is not None
    assert result.status == 409
    assert not flow.control.runtime_scopes
    assert not flow.control.requests
    assert not flow.control.created
    assert not flow.control.limits
    assert not flow.control.tokens
    assert _business_counts(flow) == counts


def test_production_completes_original_chain_and_issues_only_on_private_redis(production, monkeypatch):
    flow = production.flow
    production.policy.resolve(
        flow.local.config,
        flow.local.env[1].namespace_id,
        flow.local.env[1].revision_id,
        flow.local.env[1].config_digest,
        "off",
    )
    clones = []
    original = CasdoorLocalLoginCoordinatorService._with_request_redis

    def bind(self, client):
        result = original(self, client)
        clones.append(result)
        return result

    monkeypatch.setattr(CasdoorLocalLoginCoordinatorService, "_with_request_redis", bind)
    scope, _ = begin(flow)
    result = complete(flow, scope)
    assert result.tokens
    assert result.status == 302
    clone = clones[-1]
    assert type(clone) is CasdoorLocalLoginCoordinatorService
    assert type(clone._finalization) is CasdoorLocalLoginFinalizationService
    assert type(clone._finalization._session_gateway) is RedisAccountSessionGateway
    assert clone._finalization._session_gateway._redis is flow.control.runtime_scopes[-1].client
    assert clone._finalization._session_gateway._redis is not flow.redis
    assert len(flow.control.consumed) == 1
    assert len(flow.control.tokens) == 2
    assert all(scope.finish_calls == 1 for scope in flow.control.runtime_scopes)


@pytest.mark.parametrize("failure", ["revoked", "active_changed"])
def test_callback_preflight_rechecks_before_consume_or_provider(production, failure):
    env, flow = production, production.flow
    scope, _ = begin(flow)
    before = (len(flow.control.runtime_scopes), len(flow.control.requests))
    if failure == "revoked":
        env.authority.unlink()
    else:
        with flow.local.session.begin():
            integration = flow.local.session.scalar(sa.select(CasdoorIntegrationExtend))
            integration.active_revision_id = None
    result = complete(flow, scope)
    assert result.error
    assert result.tokens is None
    assert (len(flow.control.runtime_scopes), len(flow.control.requests)) == before
    assert not flow.control.consumed
    assert not flow.control.tokens


def test_revocation_during_role_graph_denies_before_local_write(production):
    env, flow = production, production.flow
    scope, _ = begin(flow)
    counts = _business_counts(flow)

    def revoke(path):
        if path == "/api/get-roles":
            env.authority.unlink()

    flow.control.hook = revoke
    result = complete(flow, scope)
    assert result.error
    assert result.tokens is None
    assert not flow.control.tokens
    assert _business_counts(flow) == counts
    assert not flow.lease.data


def test_close_failure_suppresses_production_tokens(production):
    flow = production.flow
    scope, _ = begin(flow)
    flow.control.fail_close = True
    result = complete(flow, scope)
    assert result.status == 503
    assert result.tokens is None
    assert result.phases.token_outcome == "issued"
    assert len(flow.control.tokens) == 2
    assert all(cookie.max_age == 0 for cookie in result.cookies)


@pytest.mark.parametrize("failure", ["revoked", "expired"])
def test_finalizer_fresh_guard_denies_before_token_issue(production, monkeypatch, failure):
    env, flow = production, production.flow
    scope, _ = begin(flow)
    original = CasdoorLocalLoginFinalizationService._login_metadata
    entered = []

    def invalidate(session, account_id, ip_address):
        selected = original(session, account_id, ip_address)
        entered.append(selected)
        if failure == "revoked":
            env.authority.unlink()
        else:
            env.policy._now = lambda: env.now + timedelta(hours=2)
        return selected

    monkeypatch.setattr(CasdoorLocalLoginFinalizationService, "_login_metadata", staticmethod(invalidate))
    result = complete(flow, scope)
    assert entered
    assert result.error
    assert result.tokens is None
    assert not flow.control.tokens
    assert not flow.lease.data
    assert result.phases.local_outcome == "committed"
    assert result.phases.finalization_outcome == "not_committed"
    assert result.phases.token_outcome == "not_started"
