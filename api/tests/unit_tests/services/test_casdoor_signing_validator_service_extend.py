"""Managed claims-validator retry/source tests with synthetic signing keys."""

import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from core.casdoor.claims import ClaimsError
from core.casdoor.crypto import CryptoError
from core.casdoor.gateway import RawTokens
from pydantic import SecretStr
from services import casdoor_signing_validator_service_extend as managed

from tests.unit_tests.core.casdoor import test_claims as claims
from tests.unit_tests.core.casdoor.test_gateway import configuration


class _TrustStore:
    def __init__(self, keys, attempts):
        self.keys = keys
        self.attempts = attempts

    def verify_rs256(self, signing_input, signature, *, kid, now, algorithm="RS256"):
        self.attempts.append((kid, tuple(self.keys)))
        if algorithm != "RS256":
            raise CryptoError("algorithm")
        selected = self.keys.get(kid)
        if selected is None:
            raise CryptoError("kid")
        try:
            selected.public_key().verify(
                signature, signing_input, padding.PKCS1v15(), hashes.SHA256()
            )
        except InvalidSignature:
            raise CryptoError("signature") from None
        return SimpleNamespace(kid=kid, fingerprint=(kid or "") + "f" * 63)


class _Provider:
    def __init__(self, initial, refreshed):
        self.initial = initial
        self.refreshed = refreshed
        self.refreshes = 0

    def snapshot(self):
        return self.initial

    def refresh_once(self):
        self.refreshes += 1
        return self.refreshed


def _snapshot(store, *, source, profile="application"):
    return SimpleNamespace(trust_store=store, source_url=source, profile=profile)


def test_signature_failure_restarts_entire_bundle_on_one_refreshed_snapshot():
    identity_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    access_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    attempts = []
    initial = _snapshot(
        _TrustStore({"identity": identity_key}, attempts),
        source="https://casdoor.invalid/app/jwks",
    )
    refreshed = _snapshot(
        _TrustStore({"identity": identity_key, "access": access_key}, attempts),
        source="https://casdoor.invalid/app/jwks",
    )
    provider = _Provider(initial, refreshed)
    validator = managed.ManagedClaimsValidator(
        provider=provider,
        expected_issuer=claims.ISSUER,
        organization=claims.ORG,
        application=claims.APP,
        client_id=claims.CLIENT,
    )
    tokens = RawTokens(
        {
            "id_token": claims.sign(
                identity_key,
                claims.id_claims(),
                header={"alg": "RS256", "typ": "JWT", "kid": "identity"},
            ),
            "access_token": claims.sign(
                access_key,
                claims.native_claims(),
                header={"alg": "RS256", "typ": "JWT", "kid": "access"},
            ),
        }
    )

    result = validator.verify_token_bundle(
        tokens,
        expected_nonce=claims.NONCE,
        auth_started_at=claims.START,
        now=claims.NOW,
        contract=claims.SYNTHETIC_CONTRACT,
    )

    assert result.identity.subject == result.native_access.subject == claims.SUB
    assert provider.refreshes == 1
    assert [keys for _, keys in attempts] == [
        ("identity",),
        ("identity",),
        ("identity", "access"),
        ("identity", "access"),
    ]
    assert validator.signing_key_metadata() == {
        "source": "https://casdoor.invalid/app/jwks",
        "profile": "application",
        "fingerprints": sorted(["identity" + "f" * 63, "access" + "f" * 63]),
    }


def test_bundle_does_not_refresh_for_non_signature_claim_failure():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    attempts = []
    store = _TrustStore({"identity": key}, attempts)
    provider = _Provider(
        _snapshot(store, source="https://casdoor.invalid/app/jwks"), None
    )
    validator = managed.ManagedClaimsValidator(
        provider=provider,
        expected_issuer=claims.ISSUER,
        organization=claims.ORG,
        application=claims.APP,
        client_id=claims.CLIENT,
    )
    tokens = RawTokens(
        {
            "id_token": claims.sign(
                key,
                claims.id_claims(iss=claims.ISSUER + "/wrong"),
                header={"alg": "RS256", "typ": "JWT", "kid": "identity"},
            ),
            "access_token": "unused",
        }
    )

    with pytest.raises(ClaimsError, match="issuer_invalid"):
        validator.verify_token_bundle(
            tokens,
            expected_nonce=claims.NONCE,
            auth_started_at=claims.START,
            now=claims.NOW,
            contract=claims.SYNTHETIC_CONTRACT,
        )
    assert provider.refreshes == 0


def test_diagnostic_source_metadata_is_revision_bound_and_profile_specific(monkeypatch):
    namespace_id, revision_id = uuid4(), uuid4()
    revision = SimpleNamespace(namespace_id=str(namespace_id), config_digest="d" * 64)
    row = SimpleNamespace(
        status=managed.CasdoorValidationStatus.PASSED,
        config_digest="d" * 64,
        summary_json=json.dumps(
            {
                "configuration_schema_version": 2,
                "namespace_id": str(namespace_id),
                "signing_keys": {
                    "source": "https://casdoor.invalid/.well-known/app/jwks",
                    "profile": "application",
                    "fingerprints": ["f" * 64],
                },
            }
        ),
        checked_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        id="synthetic-validation",
    )

    class Session:
        def __init__(self, _engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, model, _id):
            return revision

        def scalars(self, _query):
            return SimpleNamespace(all=lambda: [row])

    from extensions import ext_database

    monkeypatch.setattr(ext_database, "db", SimpleNamespace(engine=object()))
    monkeypatch.setattr(managed, "Session", Session)

    metadata = managed._source_metadata(namespace_id, revision_id)

    assert metadata == {
        "source": "https://casdoor.invalid/.well-known/app/jwks",
        "profile": "application",
        "fingerprints": ["f" * 64],
    }


def test_activation_resolver_forces_refresh_from_diagnostic_profile(monkeypatch):
    from configs import dify_config
    from services import casdoor_signing_key_service_extend as key_service

    captured = {}
    expected = SimpleNamespace(fetched_at=time.time())

    class Provider:
        def __init__(
            self, operation, namespace_id, revision_id, *, source_url, profile
        ):
            captured.update(
                operation=operation,
                namespace_id=namespace_id,
                revision_id=revision_id,
                source_url=source_url,
                profile=profile,
            )

        def snapshot(self, *, force_refresh=False):
            captured["force_refresh"] = force_refresh
            expected.fetched_at = time.time()
            return expected

    monkeypatch.setattr(key_service, "RedisSigningKeyProvider", Provider)
    monkeypatch.setattr(dify_config, "CONSOLE_API_URL", "https://console.example.test/")
    monkeypatch.setattr(dify_config, "DEPLOY_ENV", "DEVELOPMENT")
    config = configuration().model_copy(
        update={
            "schema_version": 2,
            "signing_key_mode": "automatic",
            "certificates": (),
        }
    )
    namespace_id, revision_id = uuid4(), uuid4()
    secret = SecretStr("synthetic-client-secret")
    source_metadata = {
        "source": "https://casdoor.invalid/.well-known/app/jwks",
        "profile": "global",
    }

    resolved = managed.resolve_activation_signing_keys(
        configuration=config,
        namespace_id=namespace_id,
        revision_id=revision_id,
        client_secret=secret,
        source_metadata=source_metadata,
    )

    assert resolved is expected
    assert captured["namespace_id"] == namespace_id
    assert captured["revision_id"] == revision_id
    assert captured["source_url"] == source_metadata["source"]
    assert captured["profile"] == "global"
    assert captured["force_refresh"] is True
    assert captured["operation"].client_secret == "synthetic-client-secret"
    assert (
        captured["operation"].registered_redirect_uri
        == "https://console.example.test/console/api/auth/casdoor/callback"
    )
