"""Casdoor immutable revisions in a caller-owned Session/unit of work.

All writes require an explicit transaction owned by the caller. Methods may
add/flush and issue compare-and-swap SQL, but never commit or rollback. The caller
must roll back its complete unit of work after any exception (including a losing
first-insert race). No network I/O occurs here. CasdoorManagementPolicy and CSRF
belong at the authenticated service/controller boundary, before these methods.

Validation rows are read-only here: dedicated trusted server validators own writing
real proof summaries. An HTTP payload must never supply deployment proof, RBAC
mode, evidence_source, status or capabilities. Offline fixtures are not proof.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa
from core.casdoor.configuration import CasdoorConfiguration
from core.casdoor.crypto import (
    CasdoorCrypto,
    CertificateTrustStore,
    CryptoError,
    EncryptionContext,
    EncryptionPurpose,
    TrustedCertificate,
)
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.rp_logout_activation import RPLogoutActivationAdmission
from models.account import Tenant, TenantStatus
from models.casdoor_extend import (
    CasdoorAuditExtend,
    CasdoorConfigRevisionExtend,
    CasdoorIdentityExtend,
    CasdoorIntegrationExtend,
    CasdoorManagedMembershipExtend,
    CasdoorNamespaceExtend,
    CasdoorNamespaceLifecycle,
    CasdoorOperationState,
    CasdoorSyncIntentExtend,
    CasdoorTerminationState,
    CasdoorValidationExtend,
    CasdoorValidationKind,
    CasdoorValidationStatus,
)
from pydantic import SecretStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

_POLICY_FIELDS = (
    "schema_version",
    "scope",
    "default_normal_fallback",
    "name_sync",
    "avatar_sync",
    "avatar_mode",
    "rp_logout",
    "self_unlink",
)
_CORE_FIELDS = ("expected_issuer", "organization", "application", "client_id")
# Server-owned summary fields, not selectable by administrators.
REQUIRED_CAPABILITIES = {
    CasdoorValidationKind.STATIC: frozenset({"config_shape", "workspace_targets", "certificate_trust", "secret_ready"}),
    CasdoorValidationKind.DEPLOYMENT: frozenset(
        {"release_identity", "non_dcr_org_admin_application", "directory_credential_transport", "role_effects"}
    ),
    CasdoorValidationKind.PROTOCOL: frozenset(
        {"standard_id_token", "pkce_s256", "nonce", "issuer_audience_azp", "userinfo_subject"}
    ),
    CasdoorValidationKind.DIAGNOSTIC: frozenset(
        {"online_status", "role_graph_complete", "account_items_visibility", "workspace_plan"}
    ),
}


def required_capabilities(kind: CasdoorValidationKind, schema_version: int) -> frozenset[str] | None:
    """Proof requirements follow immutable configuration policy, not caller input."""
    required = REQUIRED_CAPABILITIES.get(kind)
    if required is not None and kind is CasdoorValidationKind.STATIC and schema_version == 2:
        return (required - {"certificate_trust"}) | {"signing_key_policy"}
    return required


_OPTIONAL_CAPABILITIES = {
    "avatar_sync": (CasdoorValidationKind.STATIC, "avatar_sync"),
    "rp_logout": (CasdoorValidationKind.PROTOCOL, "rp_logout"),
    "self_unlink": (CasdoorValidationKind.PROTOCOL, "reauth_unlink"),
}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fingerprint(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def signing_key_fingerprint(value: object) -> bool:
    """RFC 7638 RSA thumbprints; historical X.509 fingerprints remain readable."""
    return isinstance(value, str) and (_fingerprint(value) or re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is not None)


class CasdoorConfigurationError(ValueError):
    """Safe domain code/reason; raw config, credentials and provider errors omitted."""

    def __init__(self, code: CasdoorErrorCode, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(code.value)


def _conflict(reason: str) -> CasdoorConfigurationError:
    return CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, reason)


@dataclass(frozen=True, repr=False)
class _DisplayMetadata:
    """Public configuration display only; not authentication readiness."""

    enabled: bool = False
    button_text: str = "Casdoor"


@dataclass(frozen=True)
class ValidationSnapshot:
    kind: CasdoorValidationKind
    status: str
    checked_at: datetime | None
    expires_at: datetime | None
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class RevisionSnapshot:
    revision_id: UUID
    namespace_id: UUID
    configuration: CasdoorConfiguration
    secret_configured: bool
    validation: tuple[ValidationSnapshot, ...]


@dataclass(frozen=True)
class ConfigurationSnapshot:
    enabled: bool = False
    etag: int = 0
    active_revision_id: UUID | None = None
    draft_revision_id: UUID | None = None
    active: RevisionSnapshot | None = None
    draft: RevisionSnapshot | None = None


@dataclass(frozen=True)
class DisableResult:
    configuration: ConfigurationSnapshot
    reconciliation_required: bool


@dataclass(frozen=True)
class StaticValidationSnapshot:
    """Local checks only; never an activation or deployment capability proof."""

    revision_id: UUID
    etag: int
    checked_at: datetime
    certificates: tuple[TrustedCertificate, ...]


@dataclass(frozen=True)
class WorkspaceSelection:
    workspace_id: UUID
    name: str
    available: bool
    created_at: datetime


@dataclass(frozen=True)
class WorkspaceSelectionPage:
    workspaces: tuple[WorkspaceSelection, ...]
    page: int
    limit: int
    total: int
    has_more: bool
    earliest_created_workspace: WorkspaceSelection | None
    earliest_created_ambiguous: bool


class CasdoorConfigurationRepository:
    def __init__(
        self,
        session: Session,
        *,
        crypto: CasdoorCrypto,
        rbac_enabled: bool,
        deployment_proof_fingerprint: str | None = None,
        allow_development_loopback_http: bool = False,
    ) -> None:
        if type(rbac_enabled) is not bool or type(allow_development_loopback_http) is not bool:
            raise ValueError("deployment flags must be booleans")
        if deployment_proof_fingerprint is not None and not _fingerprint(deployment_proof_fingerprint):
            raise ValueError("invalid deployment proof fingerprint")
        self.session = session
        self.crypto = crypto
        self.rbac_mode = "on" if rbac_enabled else "off"
        self.deployment_proof_fingerprint = deployment_proof_fingerprint
        self._context = {"allow_development_loopback_http": allow_development_loopback_http}

    def _integration(self) -> CasdoorIntegrationExtend | None:
        # Even reads must not implicitly flush unrelated pending caller work.
        with self.session.no_autoflush:
            return self.session.scalar(sa.select(CasdoorIntegrationExtend).where(CasdoorIntegrationExtend.slot == 1))

    def _revision(self, integration_id: str, revision_id: str) -> CasdoorConfigRevisionExtend:
        revision = self.session.get(CasdoorConfigRevisionExtend, revision_id)
        if revision is None or revision.integration_id != integration_id:
            raise _conflict("revision_owner_mismatch")
        namespace = self.session.get(CasdoorNamespaceExtend, revision.namespace_id)
        if namespace is None or namespace.integration_id != integration_id:
            raise _conflict("namespace_owner_mismatch")
        configuration = self._configuration(revision)
        if any(getattr(namespace, field) != getattr(configuration, field) for field in _CORE_FIELDS):
            raise _conflict("namespace_core_mismatch")
        if namespace.core_fingerprint != self._core_fingerprint(configuration):
            raise _conflict("namespace_core_mismatch")
        if revision.config_digest != self._validation_digest(configuration, revision):
            raise _conflict("revision_digest_mismatch")
        return revision

    def _configuration(self, revision: CasdoorConfigRevisionExtend) -> CasdoorConfiguration:
        data = {
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
        }
        policy = json.loads(revision.policy_json)
        policy_fields = set(_POLICY_FIELDS)
        if revision.schema_version == 2:
            policy_fields.add("signing_key_mode")
        if (
            not isinstance(policy, dict)
            or set(policy) != policy_fields
            or policy["schema_version"] != revision.schema_version
        ):
            raise _conflict("revision_policy_invalid")
        data.update(policy)
        data["workspace_mappings"] = json.loads(revision.mappings_json)
        data["certificates"] = json.loads(revision.certificates_json)
        return CasdoorConfiguration.model_validate(data, context=self._context)

    @staticmethod
    def _core_fingerprint(configuration: CasdoorConfiguration) -> str:
        return _sha(_json({field: getattr(configuration, field) for field in _CORE_FIELDS}))

    @staticmethod
    def _validation_digest(configuration: CasdoorConfiguration, revision: CasdoorConfigRevisionExtend) -> str:
        # Opaque randomized envelope prevents a guessable Secret hash. Revision
        # and namespace identity also invalidate proof even for identical policy.
        return _sha(
            _json(
                {
                    "configuration": json.loads(configuration.canonical_json()),
                    "namespace_id": revision.namespace_id,
                    "revision_id": revision.id,
                    "encrypted_secret_envelope": revision.encrypted_secret,
                }
            )
        )

    def display(self) -> _DisplayMetadata:
        """Read only validated active metadata, independently of draft/proof state."""
        with self.session.no_autoflush:
            integration = self._integration()
            if integration is None or not integration.enabled:
                return _DisplayMetadata()
            if integration.active_revision_id is None:
                raise _conflict("active_revision_missing")
            revision = self._revision(integration.id, integration.active_revision_id)
            namespace = self.session.get(CasdoorNamespaceExtend, revision.namespace_id)
            if namespace is None or namespace.lifecycle != CasdoorNamespaceLifecycle.ACTIVE:
                raise _conflict("namespace_fenced")
            return _DisplayMetadata(enabled=True, button_text=self._configuration(revision).button_text)

    def get(self, *, now: datetime | None = None) -> ConfigurationSnapshot:
        """Read-only including unconfigured GET; never inserts a singleton."""
        with self.session.no_autoflush:
            integration = self._integration()
            if integration is None:
                return ConfigurationSnapshot()
            checked_at = self._now(now)
            active = self._snapshot(integration, integration.active_revision_id, checked_at)
            draft = self._snapshot(integration, integration.draft_revision_id, checked_at)
            return ConfigurationSnapshot(
                enabled=integration.enabled,
                etag=integration.etag,
                active_revision_id=UUID(integration.active_revision_id) if integration.active_revision_id else None,
                draft_revision_id=UUID(integration.draft_revision_id) if integration.draft_revision_id else None,
                active=active,
                draft=draft,
            )

    def list_workspaces(self, *, page: int, limit: int) -> WorkspaceSelectionPage:
        """Global administrator selection, including unavailable rows; no automatic choice.

        Creation order is evidence about DB rows, not proof of which workspace an
        administrator originally used. Equal earliest timestamps are ambiguous.
        Metadata always describes the earliest row, independent of page/filter.
        """
        if type(page) is not int or page < 1 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid workspace pagination")
        with self.session.no_autoflush:
            ordered = sa.select(Tenant).order_by(Tenant.created_at.asc(), Tenant.id.asc())
            total = self.session.scalar(sa.select(sa.func.count()).select_from(Tenant)) or 0
            earliest = self.session.scalar(ordered.limit(1))
            earliest_count = (
                self.session.scalar(
                    sa.select(sa.func.count()).select_from(Tenant).where(Tenant.created_at == earliest.created_at)
                )
                if earliest is not None
                else 0
            )
            rows = self.session.scalars(ordered.offset((page - 1) * limit).limit(limit)).all()

            def selection(row: Tenant) -> WorkspaceSelection:
                return WorkspaceSelection(UUID(row.id), row.name, row.status == TenantStatus.NORMAL, row.created_at)

            return WorkspaceSelectionPage(
                tuple(selection(row) for row in rows),
                page,
                limit,
                total,
                page * limit < total,
                selection(earliest) if earliest is not None else None,
                bool(earliest_count and earliest_count > 1),
            )

    def validate_static(self, *, etag: int, revision_id: UUID, now: datetime | None = None) -> StaticValidationSnapshot:
        """Check the exact current draft without writing proof or changing ETag.

        Workspaces, public certificate policies and the encrypted Secret are
        checked by their existing owners. No network request, authentication,
        diagnostic, deployment proof or optional runtime capability is asserted.
        The result becomes obsolete as soon as this ETag/draft changes.
        """
        self._etag(etag)
        with self.session.no_autoflush:
            integration = self._integration()
            if integration is None:
                raise CasdoorConfigurationError(CasdoorErrorCode.NOT_CONFIGURED, "not_configured")
            if integration.etag != etag or integration.draft_revision_id != str(revision_id):
                raise _conflict("draft_pointer_mismatch")
            revision = self._revision(integration.id, str(revision_id))
            configuration = self._configuration(revision)
            self._workspaces(configuration)
            if revision.encrypted_secret is None:
                raise _conflict("required_secret_missing")
            self.crypto.decrypt(
                revision.encrypted_secret, context=self._secret_context(revision.namespace_id, revision.id)
            )
            checked_at = self._now(now)
            pins = self._legacy_pins(configuration, checked_at)
            return StaticValidationSnapshot(UUID(revision.id), integration.etag, checked_at, pins)

    @staticmethod
    def _legacy_pins(configuration: CasdoorConfiguration, checked_at: datetime) -> tuple[TrustedCertificate, ...]:
        """Automatic mode validates policy locally; discovery is never a static check."""
        if configuration.schema_version == 2:
            return ()
        pins = tuple(TrustedCertificate(**pin.model_dump()) for pin in configuration.certificates)
        CertificateTrustStore(pins)
        if len(pins) == 2 and max(pin.not_before for pin in pins) >= min(pin.accept_until for pin in pins):
            raise CryptoError("casdoor_certificate_invalid")
        if not any(
            pin.not_before.replace(tzinfo=None) <= checked_at < pin.accept_until.replace(tzinfo=None) for pin in pins
        ):
            raise CryptoError("casdoor_certificate_invalid")
        return pins

    def _snapshot(
        self, integration: CasdoorIntegrationExtend, revision_id: str | None, now: datetime
    ) -> RevisionSnapshot | None:
        if revision_id is None:
            return None
        revision = self._revision(integration.id, revision_id)
        rows, ambiguous = self._latest_validations(revision.id)
        latest: dict[CasdoorValidationKind, ValidationSnapshot] = {}
        for row in rows.values():
            if row.kind not in latest:
                status = row.status.value
                if status == "passed" and not self._fresh(row, now):
                    status = "expired"
                elif status == "passed" and not self._record_capabilities(row, revision):
                    status = "unknown"
                if row.kind in ambiguous:
                    status = "unknown"
                try:
                    correlation = UUID(row.correlation_id)
                except (ValueError, TypeError, AttributeError):
                    correlation = None
                latest[row.kind] = ValidationSnapshot(row.kind, status, row.checked_at, row.expires_at, correlation)
        return RevisionSnapshot(
            UUID(revision.id),
            UUID(revision.namespace_id),
            self._configuration(revision),
            revision.encrypted_secret is not None,
            tuple(latest.values()),
        )

    @staticmethod
    def _now(value: datetime | None) -> datetime:
        if value is None:
            return datetime.now(UTC).replace(tzinfo=None)
        if value.tzinfo is not None:
            if value.utcoffset() != timedelta(0):
                raise ValueError("now must be UTC")
            return value.replace(tzinfo=None)
        return value

    def _workspaces(self, configuration: CasdoorConfiguration) -> None:
        workspace_ids = {str(configuration.default_workspace_id)} | {
            str(mapping.workspace_id) for mapping in configuration.workspace_mappings
        }
        found = set(
            self.session.scalars(
                sa.select(Tenant.id)
                .where(Tenant.id.in_(workspace_ids), Tenant.status == TenantStatus.NORMAL)
                .with_for_update()
            ).all()
        )
        if found != workspace_ids:
            raise CasdoorConfigurationError(CasdoorErrorCode.WORKSPACE_UNAVAILABLE, "workspace_unavailable")

    def _cas(self, integration: CasdoorIntegrationExtend, etag: int, actor_account_id: UUID) -> None:
        result = self.session.execute(
            sa.update(CasdoorIntegrationExtend)
            .where(
                CasdoorIntegrationExtend.id == integration.id,
                CasdoorIntegrationExtend.etag == etag,
            )
            .values(etag=etag + 1, updated_by=str(actor_account_id))
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise _conflict("stale_etag")
        self.session.refresh(integration)

    @staticmethod
    def _etag(etag: int) -> None:
        if type(etag) is not int or not 0 <= etag < 2**63 - 1:
            raise _conflict("invalid_etag")

    def _write_integration(self, etag: int, actor_account_id: UUID) -> CasdoorIntegrationExtend:
        self._etag(etag)
        integration = self._integration()
        if integration is None:
            if etag != 0:
                raise _conflict("stale_etag")
            integration = CasdoorIntegrationExtend(id=str(uuid4()), etag=1, updated_by=str(actor_account_id))
            self.session.add(integration)
            try:
                self.session.flush()
            except IntegrityError:
                raise _conflict("singleton_insert_race") from None
        else:
            self._cas(integration, etag, actor_account_id)
        return integration

    def _require_transaction(self) -> None:
        if not self.session.in_transaction():
            raise RuntimeError("Casdoor writes require a caller-owned transaction")

    def _namespace(
        self,
        integration: CasdoorIntegrationExtend,
        configuration: CasdoorConfiguration,
        source: CasdoorConfigRevisionExtend | None,
    ) -> CasdoorNamespaceExtend:
        fingerprint = self._core_fingerprint(configuration)
        for pointer in {integration.active_revision_id, integration.draft_revision_id} - {None}:
            current = self._revision(integration.id, pointer)
            if self._core_fingerprint(self._configuration(current)) != fingerprint and self._has_bindings(
                current.namespace_id
            ):
                raise _conflict("bound_namespace_core_change")
        if source is not None and self._core_fingerprint(self._configuration(source)) == fingerprint:
            namespace = self.session.get(CasdoorNamespaceExtend, source.namespace_id)
            if namespace is None or namespace.lifecycle != CasdoorNamespaceLifecycle.ACTIVE:
                raise _conflict("namespace_fenced")
            return namespace
        # No automatic lookup/reuse by digest, and no archive/reset of old scope.
        namespace = CasdoorNamespaceExtend(
            id=str(uuid4()),
            integration_id=integration.id,
            **{field: getattr(configuration, field) for field in _CORE_FIELDS},
            core_fingerprint=fingerprint,
        )
        self.session.add(namespace)
        self.session.flush()
        return namespace

    def _has_bindings(self, namespace_id: str) -> bool:
        # Membership tombstones and historical intents prevent a disguised reset
        # even if an identity was removed by a future properly fenced unlink.
        return any(
            self.session.scalar(sa.select(sa.exists().where(model.namespace_id == namespace_id)))
            for model in (CasdoorIdentityExtend, CasdoorManagedMembershipExtend, CasdoorSyncIntentExtend)
        )

    @staticmethod
    def _secret_context(namespace_id: str, revision_id: str) -> EncryptionContext:
        return EncryptionContext(EncryptionPurpose.CONFIG_SECRET, UUID(namespace_id), UUID(revision_id))

    def save_draft(
        self, configuration: CasdoorConfiguration, *, etag: int, actor_account_id: UUID, secret: SecretStr | None = None
    ) -> ConfigurationSnapshot:
        """Whole replacement. Missing/empty Secret keeps it via decrypt/re-encrypt."""
        return self._save(configuration, etag=etag, actor_account_id=actor_account_id, secret=secret, clear=False)

    def _save(
        self,
        configuration: CasdoorConfiguration,
        *,
        etag: int,
        actor_account_id: UUID,
        secret: SecretStr | None,
        clear: bool,
    ) -> ConfigurationSnapshot:
        self._require_transaction()
        self._etag(etag)
        configuration = CasdoorConfiguration.model_validate(
            configuration.model_dump(mode="json"), context=self._context
        )
        if secret is not None and not isinstance(secret, SecretStr):
            raise ValueError("replacement must be SecretStr")
        replacement = secret.get_secret_value() if secret is not None else ""
        if len(replacement.encode("utf-8")) > 4096 or replacement == "********":
            raise ValueError("invalid replacement secret")
        self._workspaces(configuration)
        integration = self._write_integration(etag, actor_account_id)
        source_id = integration.draft_revision_id or integration.active_revision_id
        source = self._revision(integration.id, source_id) if source_id else None
        namespace = self._namespace(integration, configuration, source)
        revision_id = str(uuid4())
        plaintext = None
        if not clear:
            if replacement:
                plaintext = replacement
            elif source is not None and source.encrypted_secret is not None:
                plaintext = self.crypto.decrypt(
                    source.encrypted_secret, context=self._secret_context(source.namespace_id, source.id)
                )
        envelope = (
            self.crypto.encrypt(plaintext, context=self._secret_context(namespace.id, revision_id))
            if plaintext is not None
            else None
        )
        data = json.loads(configuration.canonical_json())
        revision_number = (
            self.session.scalar(
                sa.select(sa.func.max(CasdoorConfigRevisionExtend.revision_number)).where(
                    CasdoorConfigRevisionExtend.integration_id == integration.id
                )
            )
            or 0
        ) + 1
        revision = CasdoorConfigRevisionExtend(
            id=revision_id,
            integration_id=integration.id,
            namespace_id=namespace.id,
            revision_number=revision_number,
            schema_version=configuration.schema_version,
            **{
                field: getattr(configuration, field)
                for field in (
                    "browser_frontend_url",
                    "backend_api_url",
                    "expected_issuer",
                    "organization",
                    "application",
                    "client_id",
                    "button_text",
                )
            },
            default_workspace_id=str(configuration.default_workspace_id),
            encrypted_secret=envelope,
            certificates_json=_json(data["certificates"]),
            mappings_json=_json(data["workspace_mappings"]),
            policy_json=_json(
                {field: data[field] for field in _POLICY_FIELDS}
                | ({"signing_key_mode": "automatic"} if configuration.schema_version == 2 else {})
            ),
            config_digest="",
            created_by=str(actor_account_id),
        )
        revision.config_digest = self._validation_digest(configuration, revision)
        self.session.add(revision)
        self.session.flush()
        integration.draft_revision_id = revision.id
        self._audit("draft_clear_secret" if clear else "draft_save", revision, actor_account_id)
        self.session.flush()
        return self.get()

    def clear_draft_secret(self, *, etag: int, revision_id: UUID, actor_account_id: UUID) -> ConfigurationSnapshot:
        self._require_transaction()
        integration = self._integration()
        if integration is None or integration.draft_revision_id != str(revision_id):
            raise _conflict("draft_pointer_mismatch")
        draft = self._revision(integration.id, str(revision_id))
        return self._save(
            self._configuration(draft), etag=etag, actor_account_id=actor_account_id, secret=None, clear=True
        )

    @staticmethod
    def _fresh(row: CasdoorValidationExtend, now: datetime) -> bool:
        return (
            row.checked_at is not None
            and row.expires_at is not None
            and row.checked_at <= now < row.expires_at <= row.checked_at + timedelta(minutes=15)
        )

    def _activation_proof(
        self,
        revision: CasdoorConfigRevisionExtend,
        configuration: CasdoorConfiguration,
        now: datetime,
        rp_logout_admission=None,
    ) -> None:
        # This deployment currently supports the local Community path. Its
        # activation authority is exact-revision static validation plus the
        # real browser diagnostic; an external reviewer manifest is not a
        # runtime prerequisite. Enterprise/RBAC activation remains unsupported
        # until its remote role/resource owners have their own live contract.
        if self.rbac_mode != "off":
            raise _conflict("local_mode_required")
        latest, ambiguous = self._latest_validations(revision.id)
        if ambiguous:
            raise _conflict("validation_ambiguous")
        capabilities_by_kind: dict[CasdoorValidationKind, dict[str, str]] = {}
        for kind in (
            CasdoorValidationKind.STATIC,
            CasdoorValidationKind.PROTOCOL,
            CasdoorValidationKind.DIAGNOSTIC,
        ):
            row = latest.get(kind)
            if (
                row is None
                or row.status != CasdoorValidationStatus.PASSED
                or not self._fresh(row, now)
                or row.config_digest != revision.config_digest
                or row.rbac_mode != self.rbac_mode
                or (
                    self.deployment_proof_fingerprint is not None
                    and row.proof_fingerprint != self.deployment_proof_fingerprint
                )
            ):
                raise _conflict("validation_required")
            capabilities = self._record_capabilities(row, revision)
            if capabilities is None:
                raise _conflict("validation_required")
            capabilities_by_kind[kind] = capabilities
        for option, (kind, capability) in _OPTIONAL_CAPABILITIES.items():
            if not getattr(configuration, option):
                continue
            if option == "rp_logout":
                if type(rp_logout_admission) is not RPLogoutActivationAdmission or not rp_logout_admission.matches(
                    revision=revision,
                    configuration=configuration,
                    fingerprint=self.deployment_proof_fingerprint,
                    rbac_mode=self.rbac_mode,
                    now=now,
                ):
                    raise _conflict("optional_capability_unknown")
            elif capabilities_by_kind[kind].get(capability) != "passed":
                raise _conflict("optional_capability_unknown")

    def _latest_validations(
        self, revision_id: str
    ) -> tuple[dict[CasdoorValidationKind, CasdoorValidationExtend], set[CasdoorValidationKind]]:
        rows = self.session.scalars(
            sa.select(CasdoorValidationExtend)
            .where(CasdoorValidationExtend.revision_id == revision_id)
            .order_by(
                sa.func.coalesce(CasdoorValidationExtend.checked_at, CasdoorValidationExtend.created_at).desc(),
                CasdoorValidationExtend.created_at.desc(),
                CasdoorValidationExtend.id.desc(),
            )
        ).all()
        latest: dict[CasdoorValidationKind, CasdoorValidationExtend] = {}
        ambiguous: set[CasdoorValidationKind] = set()
        for row in rows:
            current = latest.setdefault(row.kind, row)
            if row.id != current.id and (row.checked_at or row.created_at, row.created_at) == (
                current.checked_at or current.created_at,
                current.created_at,
            ):
                ambiguous.add(row.kind)
        return latest, ambiguous

    def _record_capabilities(
        self, row: CasdoorValidationExtend, revision: CasdoorConfigRevisionExtend
    ) -> dict[str, str] | None:
        required = required_capabilities(row.kind, revision.schema_version)
        if required is None:
            return None
        if row.kind == CasdoorValidationKind.DEPLOYMENT and self.rbac_mode == "on":
            required = required | {"role_and_resource_termination"}
        if (
            row.config_digest != revision.config_digest
            or row.rbac_mode != self.rbac_mode
            or (
                self.deployment_proof_fingerprint is not None
                and row.proof_fingerprint != self.deployment_proof_fingerprint
            )
            or (row.kind is CasdoorValidationKind.DEPLOYMENT and row.proof_fingerprint is None)
        ):
            return None
        try:
            summary = json.loads(row.summary_json)
        except (ValueError, TypeError):
            return None
        if (
            not isinstance(summary, dict)
            or type(summary.get("schema_version")) is not int
            or summary.get("schema_version") != 1
            or (
                revision.schema_version == 2
                and (
                    type(summary.get("configuration_schema_version")) is not int
                    or summary.get("configuration_schema_version") != 2
                )
            )
            or summary.get("namespace_id") != revision.namespace_id
            or summary.get("evidence_source") != "real"
            or not isinstance(summary.get("capabilities"), dict)
            or any(summary["capabilities"].get(item) != "passed" for item in required)
        ):
            return None
        return summary["capabilities"]

    def _automatic_activation_keys(self, revision, configuration, snapshot, now: datetime) -> None:
        """Accept only the service's bounded snapshot of the diagnosed source.

        Tokens are never retained to reverify at activation. Both actual signing
        fingerprints recorded by the protocol owner must remain in this source.
        """
        from core.casdoor.signing_keys import SigningKeySnapshot

        latest, ambiguous = self._latest_validations(revision.id)
        protocol = latest.get(CasdoorValidationKind.PROTOCOL)
        if protocol is None or CasdoorValidationKind.PROTOCOL in ambiguous:
            raise _conflict("signing_key_diagnostic_required")
        try:
            metadata = json.loads(protocol.summary_json)["signing_keys"]
            used = metadata["fingerprints"]
            timestamp = now.replace(tzinfo=UTC).timestamp()
            valid = (
                type(snapshot) is SigningKeySnapshot
                and snapshot.namespace_id == UUID(revision.namespace_id)
                and snapshot.revision_id == UUID(revision.id)
                and snapshot.config_digest == configuration.config_digest()
                and type(snapshot.fetched_at) in (int, float)
                and 0 <= timestamp - snapshot.fetched_at <= 600
                and snapshot.fingerprints == snapshot.trust_store.fingerprints
                and metadata["profile"] in ("application", "global")
                and metadata["profile"] == snapshot.profile
                and metadata["source"] == snapshot.source_url
                and isinstance(used, list)
                and 1 <= len(used) <= 16
                and all(signing_key_fingerprint(item) for item in used)
                and set(used) <= set(snapshot.fingerprints)
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            valid = False
        if not valid:
            raise _conflict("signing_key_diagnostic_required")

    def activate(
        self,
        *,
        etag: int,
        revision_id: UUID,
        actor_account_id: UUID,
        now: datetime | None = None,
        rp_logout_admission=None,
        signing_key_snapshot=None,
    ) -> ConfigurationSnapshot:
        self._require_transaction()
        self._etag(etag)
        integration = self._integration()
        if integration is None:
            raise CasdoorConfigurationError(CasdoorErrorCode.NOT_CONFIGURED, "not_configured")
        revision = self._revision(integration.id, str(revision_id))
        configuration = self._configuration(revision)
        namespace = self.session.get(CasdoorNamespaceExtend, revision.namespace_id)
        if namespace is None or namespace.lifecycle != CasdoorNamespaceLifecycle.ACTIVE:
            raise _conflict("namespace_fenced")
        if integration.active_revision_id and integration.active_revision_id != revision.id:
            current = self._revision(integration.id, integration.active_revision_id)
            if current.namespace_id != revision.namespace_id and self._has_bindings(current.namespace_id):
                raise _conflict("bound_namespace_core_change")
        self._workspaces(configuration)
        if revision.encrypted_secret is None:
            raise _conflict("required_secret_missing")
        self.crypto.decrypt(revision.encrypted_secret, context=self._secret_context(revision.namespace_id, revision.id))
        checked_at = self._now(now)
        self._legacy_pins(configuration, checked_at)
        self._activation_proof(revision, configuration, checked_at, rp_logout_admission)
        if configuration.schema_version == 2:
            self._automatic_activation_keys(revision, configuration, signing_key_snapshot, checked_at)
        self._cas(integration, etag, actor_account_id)
        integration.active_revision_id = revision.id
        integration.enabled = True
        self._audit("activate", revision, actor_account_id)
        self.session.flush()
        return self.get(now=now)

    def disable(self, *, etag: int, actor_account_id: UUID) -> DisableResult:
        self._require_transaction()
        self._etag(etag)
        integration = self._integration()
        if integration is None:
            if etag != 0:
                raise _conflict("stale_etag")
            return DisableResult(ConfigurationSnapshot(), False)
        self._cas(integration, etag, actor_account_id)
        integration.enabled = False
        self.session.execute(
            sa.update(CasdoorNamespaceExtend)
            .where(
                CasdoorNamespaceExtend.integration_id == integration.id,
                CasdoorNamespaceExtend.lifecycle != CasdoorNamespaceLifecycle.ARCHIVED,
            )
            .values(lifecycle=CasdoorNamespaceLifecycle.FENCING, fence_epoch=CasdoorNamespaceExtend.fence_epoch + 1)
            .execution_options(synchronize_session="fetch")
        )
        unresolved = bool(
            self.session.scalar(
                sa.select(
                    sa.exists().where(
                        CasdoorSyncIntentExtend.namespace_id.in_(
                            sa.select(CasdoorNamespaceExtend.id).where(
                                CasdoorNamespaceExtend.integration_id == integration.id
                            )
                        ),
                        sa.or_(
                            CasdoorSyncIntentExtend.operation_state.in_(
                                [CasdoorOperationState.IN_FLIGHT, CasdoorOperationState.UNKNOWN]
                            ),
                            sa.and_(
                                CasdoorSyncIntentExtend.sent_at.is_not(None),
                                CasdoorSyncIntentExtend.termination_state != CasdoorTerminationState.CONFIRMED,
                            ),
                        ),
                    )
                )
            )
        )
        if integration.active_revision_id:
            self._audit("disable", self._revision(integration.id, integration.active_revision_id), actor_account_id)
        self.session.flush()
        return DisableResult(self.get(), unresolved)

    def _audit(self, action: str, revision: CasdoorConfigRevisionExtend, actor_account_id: UUID) -> None:
        self.session.add(
            CasdoorAuditExtend(
                namespace_id=revision.namespace_id,
                revision_id=revision.id,
                actor_account_id=str(actor_account_id),
                action=action,
                result_code="ok",
                correlation_id=str(uuid4()),
                summary_json=_json({"schema_version": 1}),
            )
        )

    def reset_namespace(self, namespace_id, *, etag, actor_account_id, scope_fingerprint):
        """Archive only a freshly locked, released zero-intent LOCAL scope."""
        from core.casdoor.auth_transactions import AuthTransactionError

        from repositories.casdoor_local_lifecycle_repository_extend import CasdoorLocalLifecycleRepository

        self._require_transaction()
        self._etag(etag)
        if self.session.new or self.session.dirty or self.session.deleted or self.session.in_nested_transaction():
            raise _conflict("namespace_reset_dirty_root")
        owner = CasdoorLocalLifecycleRepository(self.session)
        scope = owner.inspect_namespace_reset(namespace_id, source_account_id=actor_account_id, lock=True)
        if scope["fingerprint"] != scope_fingerprint or scope["etag"] != etag or self.rbac_mode != "off":
            raise AuthTransactionError("context_changed")
        revision = self._revision(scope["integration_id"], scope["pointer"])
        configuration = self._configuration(revision)
        secret = (
            SecretStr(
                self.crypto.decrypt(
                    revision.encrypted_secret, context=self._secret_context(revision.namespace_id, revision.id)
                )
            )
            if revision.encrypted_secret is not None
            else None
        )
        archived = self.session.execute(
            sa.update(CasdoorNamespaceExtend)
            .where(
                CasdoorNamespaceExtend.id == str(namespace_id),
                CasdoorNamespaceExtend.integration_id == scope["integration_id"],
                CasdoorNamespaceExtend.lifecycle == CasdoorNamespaceLifecycle.FENCING,
                CasdoorNamespaceExtend.fence_epoch == scope["fence_epoch"],
            )
            .values(
                lifecycle=CasdoorNamespaceLifecycle.ARCHIVED,
                fence_epoch=scope["fence_epoch"] + 1,
                archived_at=datetime.now(UTC),
            )
            .execution_options(synchronize_session=False)
        )
        if archived.rowcount != 1:
            raise _conflict("namespace_reset_fence_changed")
        integration = self._integration()
        cleared = self.session.execute(
            sa.update(CasdoorIntegrationExtend)
            .where(
                CasdoorIntegrationExtend.id == scope["integration_id"],
                CasdoorIntegrationExtend.enabled.is_(False),
                CasdoorIntegrationExtend.etag == etag,
                CasdoorIntegrationExtend.active_revision_id.is_(None)
                if integration.active_revision_id is None
                else CasdoorIntegrationExtend.active_revision_id == integration.active_revision_id,
                CasdoorIntegrationExtend.draft_revision_id.is_(None)
                if integration.draft_revision_id is None
                else CasdoorIntegrationExtend.draft_revision_id == integration.draft_revision_id,
            )
            .values(active_revision_id=None, draft_revision_id=None)
            .execution_options(synchronize_session=False)
        )
        if cleared.rowcount != 1:
            raise _conflict("namespace_reset_pointer_changed")
        self.session.refresh(integration)
        snapshot = self._save(configuration, etag=etag, actor_account_id=actor_account_id, secret=secret, clear=False)
        secret = None
        self.session.add(
            CasdoorAuditExtend(
                namespace_id=str(namespace_id),
                revision_id=revision.id,
                actor_account_id=str(actor_account_id),
                action="local_namespace_reset_v1",
                result_code="success",
                correlation_id=str(uuid4()),
                summary_json=_json(
                    {
                        "schema_version": 1,
                        "old_namespace_id": str(namespace_id),
                        "new_namespace_id": str(snapshot.draft.namespace_id),
                        "new_revision_id": str(snapshot.draft.revision_id),
                        "review_sha256": scope_fingerprint,
                        "fence_epoch": scope["fence_epoch"] + 1,
                        "etag": etag + 1,
                    }
                ),
            )
        )
        self.session.flush()
        self._namespace_reset_last_readback(scope, snapshot)
        return snapshot

    def _namespace_reset_last_readback(self, scope, snapshot):
        from repositories.casdoor_local_lifecycle_repository_extend import CasdoorLocalLifecycleRepository

        CasdoorLocalLifecycleRepository(self.session)._namespace_reset_readback(scope)
        old = self.session.get(CasdoorNamespaceExtend, scope["namespace_id"], populate_existing=True)
        integration = self._integration()
        self.session.refresh(integration)
        draft = self._revision(integration.id, integration.draft_revision_id)
        new = self.session.get(CasdoorNamespaceExtend, draft.namespace_id, populate_existing=True)
        if (
            old.lifecycle != CasdoorNamespaceLifecycle.ARCHIVED
            or old.fence_epoch != scope["fence_epoch"] + 1
            or integration.enabled is not False
            or integration.etag != scope["etag"] + 1
            or integration.active_revision_id is not None
            or new.id == old.id
            or new.lifecycle != CasdoorNamespaceLifecycle.ACTIVE
            or new.fence_epoch != 0
            or integration.draft_revision_id != str(snapshot.draft.revision_id)
            or UUID(new.id) != snapshot.draft.namespace_id
        ):
            raise _conflict("namespace_reset_readback_changed")
