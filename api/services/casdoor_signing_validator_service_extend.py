"""One public-key owner for all Casdoor authentication modes.

Discovery/cache I/O precedes business writes. A complete token bundle is retried
at most once after a signature failure, against one replacement key snapshot.
The persisted diagnostic source is server-owned, never supplied by callbacks.
"""

import json
import time
from uuid import UUID

import sqlalchemy as sa
from core.casdoor.claims import ClaimsError, ClaimsValidator
from core.casdoor.crypto import CertificateTrustStore, TrustedCertificate
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.gateway import GatewayError, GatewayOperation
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend,
    CasdoorValidationExtend,
    CasdoorValidationKind,
    CasdoorValidationStatus,
)
from pydantic import SecretStr
from sqlalchemy.orm import Session


def _source_metadata(namespace_id: UUID, revision_id: UUID) -> dict:
    from extensions.ext_database import db

    with Session(db.engine) as session:
        revision = session.get(CasdoorConfigRevisionExtend, str(revision_id))
        rows = session.scalars(
            sa.select(CasdoorValidationExtend)
            .where(
                CasdoorValidationExtend.revision_id == str(revision_id),
                CasdoorValidationExtend.kind == CasdoorValidationKind.PROTOCOL,
            )
            .order_by(
                sa.func.coalesce(CasdoorValidationExtend.checked_at, CasdoorValidationExtend.created_at).desc(),
                CasdoorValidationExtend.created_at.desc(),
                CasdoorValidationExtend.id.desc(),
            )
            .limit(2)
        ).all()
        if revision is None or revision.namespace_id != str(namespace_id) or not rows:
            raise ClaimsError("signing_key_diagnostic_required", CasdoorErrorCode.CONFIG_CONFLICT)
        row = rows[0]
        if len(rows) > 1 and (row.checked_at or row.created_at, row.created_at) == (
            rows[1].checked_at or rows[1].created_at,
            rows[1].created_at,
        ):
            raise ClaimsError("signing_key_diagnostic_required", CasdoorErrorCode.CONFIG_CONFLICT)
        if row.status != CasdoorValidationStatus.PASSED or row.config_digest != revision.config_digest:
            raise ClaimsError("signing_key_diagnostic_required", CasdoorErrorCode.CONFIG_CONFLICT)
        try:
            summary = json.loads(row.summary_json)
            metadata = summary["signing_keys"]
            if summary.get("configuration_schema_version") != 2 or summary.get("namespace_id") != str(namespace_id):
                raise ValueError
            if not isinstance(metadata, dict) or set(metadata) != {"source", "profile", "fingerprints"}:
                raise ValueError
            return metadata
        except (ValueError, KeyError, TypeError):
            raise ClaimsError("signing_key_diagnostic_required", CasdoorErrorCode.CONFIG_CONFLICT) from None


class ManagedClaimsValidator(ClaimsValidator):
    def __init__(self, *, provider=None, **kwargs):
        self._provider = provider
        self._key_snapshot = provider.snapshot() if provider is not None else None
        if self._key_snapshot is not None:
            kwargs["trust_store"] = self._key_snapshot.trust_store
        super().__init__(**kwargs)

    def verify_token_bundle(self, *args, **kwargs):
        if self._provider is not None:
            self._provider.snapshot()  # Recheck deadline and bounded age before verification.
        try:
            return super().verify_token_bundle(*args, **kwargs)
        except ClaimsError as caught:
            if self._provider is None or caught.reason != "signature_invalid":
                raise
            self._key_snapshot = self._provider.refresh_once()
            self._trust_store = self._key_snapshot.trust_store
            self.verified_key_fingerprints.clear()
            return super().verify_token_bundle(*args, **kwargs)

    def signing_key_metadata(self) -> dict | None:
        if self._key_snapshot is None:
            return None
        return {
            "source": self._key_snapshot.source_url,
            "profile": self._key_snapshot.profile,
            "fingerprints": sorted(self.verified_key_fingerprints),
        }


def create_claims_validator(
    operation: GatewayOperation, *, namespace_id: UUID, revision_id: UUID, diagnostic: bool = False, redis_client=None
) -> ManagedClaimsValidator:
    config = operation.config
    options = dict(
        expected_issuer=config.expected_issuer,
        organization=config.organization,
        application=config.application,
        client_id=config.client_id,
    )
    if config.schema_version == 1:
        return ManagedClaimsValidator(
            trust_store=CertificateTrustStore([TrustedCertificate(**pin.model_dump()) for pin in config.certificates]),
            **options,
        )
    from services.casdoor_signing_key_service_extend import RedisSigningKeyProvider

    metadata = None if diagnostic else _source_metadata(namespace_id, revision_id)
    provider = RedisSigningKeyProvider(
        operation,
        namespace_id=namespace_id,
        revision_id=revision_id,
        source_url=metadata["source"] if metadata else None,
        profile=metadata["profile"] if metadata else None,
        redis_client=redis_client,
    )
    return ManagedClaimsValidator(provider=provider, **options)


def resolve_activation_signing_keys(
    *, configuration, namespace_id: UUID, revision_id: UUID, client_secret: SecretStr, source_metadata: dict
):
    from configs import dify_config

    from services.casdoor_signing_key_service_extend import RedisSigningKeyProvider

    operation = GatewayOperation(
        config=configuration,
        client_secret=client_secret.get_secret_value(),
        registered_redirect_uri=dify_config.CONSOLE_API_URL.rstrip("/") + "/console/api/auth/casdoor/callback",
        allow_loopback_http=dify_config.DEPLOY_ENV == "DEVELOPMENT",
    )
    started = time.time()
    snapshot = RedisSigningKeyProvider(
        operation,
        namespace_id=namespace_id,
        revision_id=revision_id,
        source_url=source_metadata["source"],
        profile=source_metadata["profile"],
    ).snapshot(force_refresh=True)
    # Authentication may tolerate bounded cached keys during a network outage;
    # activation must prove the selected source can currently supply them.
    if snapshot.fetched_at < started:
        raise GatewayError(CasdoorErrorCode.PROVIDER_UNAVAILABLE, "signing_keys_activation_refresh_required")
    return snapshot
