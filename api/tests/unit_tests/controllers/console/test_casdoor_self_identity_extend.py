"""Synthetic SQLite + mounted native Console admission, no production proof."""

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
import sqlalchemy as sa
from flask import Flask, Response, g
from sqlalchemy.orm import Session, sessionmaker

from configs import dify_config
from constants import COOKIE_NAME_CSRF_TOKEN, HEADER_NAME_CSRF_TOKEN
from controllers.console import bp, console_ns, wraps
from controllers.console.workspace import casdoor_identity_extend as controller
from core.casdoor.ownership import (
    MembershipBackend,
    MembershipObservation,
    role_baseline_json,
)
from extensions import ext_request_logging
from libs.token import generate_csrf_token
from machinery.context import RequestContext
from models.account import (
    Account,
    AccountStatus,
    Tenant,
    TenantAccountJoin,
    TenantAccountRole,
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
    CasdoorManagedMembershipExtend as Membership,
)
from models.casdoor_extend import (
    CasdoorNamespaceExtend as Namespace,
)
from repositories.casdoor_self_identity_repository_extend import (
    CasdoorSelfIdentityRepository,
    CasdoorSelfReadConflict,
)
from services.account_service import AccountService, TenantService
from services.casdoor_self_identity_service_extend import CasdoorSelfIdentityService
from services.entities.feature_entities import LicenseStatus
from services.system_feature_service import SystemFeatureService

NOW = datetime(2026, 10, 1, tzinfo=UTC)
PATH = controller.PATH


def uid(n):
    return str(UUID(int=n))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def role_snapshot(role="normal"):
    return role_baseline_json(
        MembershipObservation(
            UUID(uid(2)),
            UUID(uid(1)),
            UUID(uid(7)) if role else None,
            TenantAccountRole(role) if role else None,
            MembershipBackend.LOCAL,
        )
    )


def sync_snapshot(**changes):
    value = dict(
        schema_version=1,
        generation=2,
        auth_started_at=NOW.isoformat(timespec="microseconds"),
        correlation_id=uid(9),
        last_sync_at=NOW.isoformat(timespec="microseconds"),
        name_status="applied",
        name_reason="managed_update",
        remote_email_status="same",
        remote_email_differs=False,
    )
    value.update(changes)
    return canonical(value)


@pytest.fixture
def storage():
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    for model in (Account, Tenant, TenantAccountJoin, Integration, Namespace, Revision, Identity, Membership):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if column.server_default is not None and str(column.server_default.arg) == "CURRENT_TIMESTAMP(0)":
                column.server_default = sa.DefaultClause(sa.text("CURRENT_TIMESTAMP"))
    metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        account = Account(
            name="Private Name",
            email="Local@Example.test",
            normalized_email="local@example.test",
            status=AccountStatus.ACTIVE,
            initialized_at=NOW.replace(tzinfo=None),
        )
        account.id = uid(1)
        account.last_active_at = NOW.replace(tzinfo=None)
        tenant = Tenant(name="Private Workspace")
        tenant.id = uid(2)
        integration = Integration(id=uid(3), enabled=True, active_revision_id=uid(5))
        namespace = Namespace(
            id=uid(4),
            integration_id=uid(3),
            expected_issuer="https://synthetic.invalid",
            organization="synthetic-organization",
            application="synthetic-app",
            client_id="synthetic-client",
            core_fingerprint="a" * 64,
            lifecycle="active",
            fence_epoch=0,
        )
        revision = Revision(
            id=uid(5),
            integration_id=uid(3),
            namespace_id=uid(4),
            revision_number=1,
            config_digest="b" * 64,
            browser_frontend_url="https://synthetic.invalid",
            backend_api_url="https://synthetic.invalid",
            expected_issuer=namespace.expected_issuer,
            organization=namespace.organization,
            application=namespace.application,
            client_id=namespace.client_id,
            button_text="Synthetic",
            default_workspace_id=uid(2),
            certificates_json="[]",
            policy_json="{}",
            mappings_json="[]",
        )
        identity = Identity(
            id=uid(6),
            namespace_id=uid(4),
            account_id=uid(1),
            issuer=namespace.expected_issuer,
            organization=namespace.organization,
            subject="private-subject",
            remote_email="local@example.test",
            email_verified=True,
            sync_generation=2,
            last_applied_json=canonical(dict(schema_version=1, name="Private Name", name_generation=2)),
            profile_sync_json=sync_snapshot(),
        )
        join = TenantAccountJoin(account_id=uid(1), tenant_id=uid(2), role=TenantAccountRole.NORMAL, current=True)
        join.id = uid(7)
        applied = role_snapshot()
        member = Membership(
            id=uid(8),
            namespace_id=uid(4),
            identity_id=uid(6),
            account_id=uid(1),
            workspace_id=uid(2),
            join_id=uid(7),
            revision_id=uid(5),
            ownership="managed",
            ownership_epoch=0,
            source="mapping",
            desired_generation=2,
            last_applied_roles_json=applied,
            last_applied_fingerprint=hashlib.sha256(applied.encode()).hexdigest(),
            desired_roles_json=canonical(
                dict(
                    schema_version=1,
                    backend="local",
                    target_role="normal",
                    builtin_id="normal",
                    role_ids=["normal"],
                    fence_epoch=0,
                )
            ),
            baseline_json=applied,
            finalization="pending",
            tombstone=False,
        )
        session.add_all([account, tenant, integration, namespace, revision, identity, join, member])
    account._current_tenant = tenant
    account.role = TenantAccountRole.NORMAL
    context = RequestContext("synthetic-request", None, uid(1), uid(2))
    yield SimpleNamespace(
        engine=engine,
        factory=factory,
        account=account,
        tenant=tenant,
        context=context,
        service=CasdoorSelfIdentityService(session_factory=factory, rbac_enabled=False),
    )
    engine.dispose()


def change(storage, model, **values):
    with storage.factory.begin() as session:
        session.execute(sa.update(model).values(**values))


def read(storage, **kwargs):
    result = storage.service.get(storage.context, **kwargs)
    controller.CasdoorSelfIdentityStatusResponse.model_validate(result)
    return result


def test_actual_reader_select_only_and_private_projection(storage, monkeypatch):
    statements = []

    def sql(_conn, _cursor, statement, _parameters, _context, _many):
        assert statement.lstrip().upper().startswith("SELECT")
        assert "FOR UPDATE" not in statement.upper()
        statements.append(statement)

    def forbidden(*_args, **_kwargs):
        pytest.fail("new reader attempted mutation or external owner")

    sa.event.listen(storage.engine, "before_cursor_execute", sql)
    for method in ("flush", "commit", "add", "add_all", "delete"):
        monkeypatch.setattr(Session, method, forbidden)
    from repositories.casdoor_membership_repository_extend import (
        CasdoorMembershipRepository,
    )
    from repositories.casdoor_profile_repository_extend import CasdoorProfileRepository

    monkeypatch.setattr(CasdoorProfileRepository, "persist", forbidden)
    monkeypatch.setattr(CasdoorMembershipRepository, "inspect", forbidden)
    from core.casdoor.gateway import CasdoorDirectoryGateway, GatewayOperation
    from extensions.ext_redis import RedisClientWrapper
    from extensions.ext_storage import Storage
    from services import account_quota_service_extend

    monkeypatch.setattr(GatewayOperation, "_request", forbidden)
    monkeypatch.setattr(CasdoorDirectoryGateway, "_read", forbidden)
    monkeypatch.setattr(RedisClientWrapper, "__getattr__", forbidden)
    for method in ("load", "save", "download", "delete"):
        monkeypatch.setattr(Storage, method, forbidden)
    monkeypatch.setattr(account_quota_service_extend, "ensure_account_quota_extend", forbidden)
    monkeypatch.setattr(AccountService, "load_user", forbidden)
    for method in ("persist_tenant_member", "_persist_member_removal_effect", "update_member_role"):
        monkeypatch.setattr(TenantService, method, forbidden)
    sessions = []

    def transaction(session, _transaction):
        assert session.autoflush is False
        assert not session.new and not session.dirty and not session.deleted
        sessions.append(session)

    sa.event.listen(Session, "after_transaction_create", transaction)
    try:
        result = read(storage)
    finally:
        sa.event.remove(Session, "after_transaction_create", transaction)
    assert len(sessions) == 1
    assert result["binding"] == "linked"
    assert result["identities"][0]["name"]["current_local_differs_from_last_applied"] is False
    assert result["identities"][0]["email"]["current_differs"] is False
    assert result["memberships"][0]["state"] == "recorded_managed"
    assert result["current_memberships"][0]["state"] == "history_present"
    assert not any(result["actions"].values())
    serialized = json.dumps(result)
    for private in (
        "private-subject",
        "Private Name",
        "Private Workspace",
        "local@example.test",
        "synthetic.invalid",
        "certificates_json",
        "desired_roles_json",
    ):
        assert private not in serialized
    assert 20 < len(statements) < 120
    assert all("encrypted_secret" not in sql and "subject," not in sql for sql in statements)


@pytest.mark.parametrize(
    "field,raw",
    [
        ("last_applied_json", '{"schema_version":1,"schema_version":1}'),
        ("profile_sync_json", '{"schema_version":2}'),
        ("last_applied_json", "x" * 4097),
        ("profile_sync_json", "界" * 1400),
        ("profile_sync_json", sync_snapshot(generation=3)),
        (
            "profile_sync_json",
            sync_snapshot(auth_started_at=(NOW + timedelta(days=1)).isoformat(timespec="microseconds")),
        ),
    ],
    ids=["duplicate", "schema", "ascii-oversize", "utf8-oversize", "future-generation", "time-order"],
)
def test_bad_snapshots_are_scoped_unknown(storage, field, raw):
    change(storage, Identity, **{field: raw})
    identity = read(storage)["identities"][0]
    assert identity["profile_consistency"] == "unknown"
    assert identity["name"]["last_status"] is None
    assert identity["email"]["current_differs"] is None


def test_old_applied_and_later_local_changes_remain_visible(storage):
    change(storage, Account, name="Later private edit", email="later@example.test")
    change(storage, Identity, sync_generation=3)
    item = read(storage)["identities"][0]
    assert item["profile_consistency"] == "historical"
    assert item["name"]["last_status"] == "applied"
    assert item["name"]["baseline_generation"] == 2
    assert item["name"]["current_local_differs_from_last_applied"] is True
    assert item["email"]["current_differs"] is True
    assert item["email"]["last_differs"] is False


@pytest.mark.parametrize("status", ["invalid", "unavailable"])
def test_invalid_retained_email_never_becomes_fresh(storage, status):
    change(storage, Identity, profile_sync_json=sync_snapshot(remote_email_status=status))
    email = read(storage)["identities"][0]["email"]
    assert email == dict(current_differs=None, verified=None, last_status=status, last_differs=False)


@pytest.mark.parametrize("value", ["not-an-email", "界" * 100, "x" * 256, "a@example.test\n"])
def test_invalid_stored_email_unknown(storage, value):
    change(storage, Identity, remote_email=value)
    assert read(storage)["identities"][0]["email"]["current_differs"] is None


@pytest.mark.parametrize(
    "model,changes,expected",
    [
        (Integration, {"enabled": False}, "inactive"),
        (Integration, {"active_revision_id": uid(999)}, "unknown"),
        (Namespace, {"lifecycle": "archived"}, "inactive"),
        (Namespace, {"lifecycle": "fencing"}, "inactive"),
    ],
)
def test_disabled_archived_and_missing_pointer(storage, model, changes, expected):
    change(storage, model, **changes)
    assert read(storage)["identities"][0]["activity"] == expected


def test_three_pages_independent_and_linked_beyond_final_page(storage):
    result = read(storage, identity_after=UUID(uid(999)), membership_after=UUID(uid(999)))
    assert result["binding"] == "linked"
    assert result["identities"] == result["memberships"] == []
    assert len(result["current_memberships"]) == 1
    assert result["current_memberships"][0]["state"] == "history_present"
    result = read(storage, current_membership_after=UUID(uid(999)))
    assert result["current_memberships"] == [] and len(result["memberships"]) == 1


def test_unlinked_history_missing_identity_is_historical(storage):
    with storage.factory.begin() as session:
        session.execute(sa.delete(Identity))
    result = read(storage)
    assert result["binding"] == "unlinked" and result["identities"] == []
    assert result["memberships"][0]["state"] == "historical"
    assert result["memberships"][0]["identity_id"] is None


@pytest.mark.parametrize(
    "changes,state",
    [
        ({"ownership": "local_override"}, "local_override"),
        ({"ownership": "released"}, "unmanaged"),
        ({"tombstone": True}, "tombstone"),
        ({"join_id": uid(99)}, "unknown"),
        ({"desired_generation": 1}, "unknown"),
        ({"identity_id": uid(99)}, "historical"),
        ({"revision_id": uid(99)}, "unknown"),
    ],
)
def test_recorded_states_never_regrant(storage, changes, state):
    change(storage, Membership, **changes)
    assert read(storage)["memberships"][0]["state"] == state


@pytest.mark.parametrize("role", ["owner", "dataset_operator"])
def test_native_non_target_roles_are_only_observed(storage, role):
    change(storage, TenantAccountJoin, role=role)
    result = read(storage)
    assert result["current_memberships"][0]["local_role"] == role
    assert result["memberships"][0]["state"] == ("unknown" if role == "owner" else "local_override")


def test_rbac_actual_is_always_unknown(storage):
    storage.service._rbac_enabled = True
    change(storage, TenantAccountJoin, role="editor")
    result = read(storage)
    assert result["memberships"][0]["remote_actual_state"] == "unknown"
    assert result["memberships"][0]["state"] == "recorded_managed"


def test_unmanaged_requires_complete_scope_probe(storage):
    with storage.factory.begin() as session:
        session.execute(sa.delete(Membership))
    assert read(storage)["current_memberships"][0]["state"] == "unmanaged"


def withdrawal(storage):
    with storage.factory.begin() as session:
        session.execute(sa.delete(TenantAccountJoin))
    empty = role_snapshot(None)
    change(
        storage,
        Membership,
        ownership_epoch=1,
        last_applied_roles_json=empty,
        last_applied_fingerprint=hashlib.sha256(empty.encode()).hexdigest(),
        desired_roles_json=canonical(
            dict(
                schema_version=2,
                backend="local",
                operation="controlled_withdrawal",
                removed_join_id=uid(7),
                withdrawal_generation=2,
                withdrawal_epoch=1,
                fence_epoch=0,
            )
        ),
    )


def test_controlled_withdrawal_distinguished_from_manual_deletion(storage):
    with storage.factory.begin() as session:
        session.execute(sa.delete(TenantAccountJoin))
    assert read(storage)["memberships"][0]["state"] == "absent_unknown"
    withdrawal(storage)
    assert read(storage)["memberships"][0]["state"] == "controlled_withdrawal"
    change(storage, Namespace, fence_epoch=1)
    item = read(storage)["memberships"][0]
    assert item["state"] == "unknown" and item["consistency"] == "stale"


@pytest.mark.parametrize(
    "field,value", [("ownership_epoch", 2), ("join_id", uid(88)), ("last_applied_fingerprint", "x" * 64)]
)
def test_withdrawal_mismatched_receipt_not_accepted(storage, field, value):
    withdrawal(storage)
    change(storage, Membership, **{field: value})
    assert read(storage)["memberships"][0]["state"] == "unknown"


@pytest.mark.parametrize(
    "field,value",
    [
        ("baseline_json", "x" * 65536),
        ("desired_roles_json", '{"schema_version":1,"schema_version":1}'),
        ("last_applied_roles_json", "界" * 22000),
        (
            "desired_roles_json",
            canonical(
                dict(
                    schema_version=1,
                    backend="local",
                    target_role="owner",
                    builtin_id="owner",
                    role_ids=["owner"],
                    fence_epoch=0,
                )
            ),
        ),
    ],
    ids=["oversize-baseline", "duplicate-desired", "utf8-applied", "owner-target"],
)
def test_ledger_corruption_is_unknown(storage, field, value):
    change(storage, Membership, **{field: value})
    assert read(storage)["memberships"][0]["state"] == "unknown"


def test_foreign_identity_never_fallback(storage):
    change(storage, Identity, account_id=uid(500))
    result = read(storage)
    assert result["binding"] == "unlinked"
    assert result["memberships"][0]["state"] == "unknown"
    assert result["memberships"][0]["identity_id"] is None
    other = RequestContext("other", None, uid(500), uid(2))
    with storage.factory.begin() as session:
        account = Account(name="foreign secret", email="foreign@example.test")
        account.id = uid(500)
        session.add(account)
    foreign = storage.service.get(other, identity_after=UUID(uid(5)))
    assert len(foreign["identities"]) == 1 and foreign["memberships"] == []
    assert read(storage, identity_after=UUID(uid(5)))["identities"] == []


def test_recheck_detects_changed_scalar(storage, monkeypatch):
    original = CasdoorSelfIdentityRepository.recheck

    def drift(reader):
        with storage.engine.begin() as connection:
            connection.execute(sa.update(Identity).values(sync_generation=3))
        original(reader)

    monkeypatch.setattr(CasdoorSelfIdentityRepository, "recheck", drift)
    with pytest.raises(CasdoorSelfReadConflict):
        read(storage)


@pytest.fixture
def http(storage, monkeypatch):
    app = Flask(__name__)
    app.config.update(TESTING=True, SERVER_NAME="console.example.test", SECRET_KEY="synthetic-flask-only")
    state = {"account": storage.account}
    app.login_manager = SimpleNamespace(
        load_user_from_request_context=lambda: None,
        unauthorized=lambda: Response('{"private":"native unauthorized"}', status=401, mimetype="application/json"),
    )

    @app.before_request
    def inject_fixture_user():
        g._login_user = state["account"]

    app.extensions["application_services"] = SimpleNamespace(casdoor_self_identity=storage.service)
    app.register_blueprint(bp)
    for key, value in {
        "SECRET_KEY": "synthetic-csrf-only-key-32bytes-minimum",
        "LOGIN_DISABLED": False,
        "ADMIN_API_KEY_ENABLE": False,
        "CONSOLE_WEB_URL": "http://console.example.test",
        "CONSOLE_API_URL": "http://console.example.test",
        "COOKIE_DOMAIN": "",
    }.items():
        monkeypatch.setattr(dify_config, key, value)
    monkeypatch.setattr(wraps, "_is_setup_completed", lambda: True)
    monkeypatch.setattr(SystemFeatureService, "get_license_status", lambda: LicenseStatus.ACTIVE)
    client = app.test_client()
    token = generate_csrf_token(storage.account.id)
    client.set_cookie(COOKIE_NAME_CSRF_TOKEN, token, domain="console.example.test")
    return SimpleNamespace(
        app=app, client=client, state=state, headers={HEADER_NAME_CSRF_TOKEN: token}, storage=storage
    )


def get(http, query="", **kwargs):
    return http.client.get(PATH + query, headers=http.headers, **kwargs)


def private(response):
    assert response.headers["Cache-Control"] == "no-store"
    assert {"Cookie", "Authorization"} <= set(response.vary)


def test_mounted_admission_real_csrf_and_context(http):
    response = get(http, "?limit=1")
    assert response.status_code == 200
    assert response.json["identities"][0]["id"] == uid(6)
    private(response)
    denied = http.client.get(PATH)
    assert denied.status_code == 401
    private(denied)
    assert denied.json == {"code": "casdoor_self_request_rejected"}


@pytest.mark.parametrize(
    "query",
    [
        "?limit=0",
        "?limit=51",
        "?limit=01",
        "?limit=+1",
        "?limit=1.0",
        "?limit=1&limit=2",
        "?limit=",
        "?account_id=foreign",
        "?identity_after=BAD",
        "?identity_after=" + uid(100).replace("-", ""),
        "?membership_after=" + uid(100) + "&membership_after=" + uid(101),
    ],
)
def test_strict_actual_query_rejections(http, query):
    response = get(http, query)
    assert response.status_code == 400
    assert response.json == {"code": "casdoor_self_invalid_query"}
    private(response)


@pytest.mark.parametrize("method,suffix,status", [("POST", "", 405), ("GET", "/child", 404), ("HEAD", "", 405)])
def test_routing_errors_private_and_fixed(http, method, suffix, status):
    response = http.client.open(PATH + suffix, method=method, headers=http.headers)
    assert response.status_code == status
    if method != "HEAD":
        assert response.json == {"code": "casdoor_self_request_rejected"}
    private(response)


@pytest.mark.parametrize(
    "kind,status",
    [("anonymous", 401), ("uninitialized", 400), ("license", 401), ("nonaccount", 400), ("missing_workspace", 500)],
)
def test_native_rejections_keep_status_and_private_body(http, monkeypatch, kind, status):
    if kind == "anonymous":
        http.state["account"] = None
    elif kind == "uninitialized":
        http.state["account"].status = AccountStatus.UNINITIALIZED
    elif kind == "license":
        monkeypatch.setattr(SystemFeatureService, "get_license_status", lambda: LicenseStatus.EXPIRED)
    elif kind == "nonaccount":
        http.state["account"] = SimpleNamespace(id=uid(1), is_authenticated=True)
    else:
        http.state["account"]._current_tenant = None
    # Native failures may propagate under Flask TESTING; production uses its
    # existing error conversion. Do not weaken or replace the real decorator.
    http.app.config["PROPAGATE_EXCEPTIONS"] = False
    response = get(http)
    assert response.status_code == status
    assert response.json == {"code": "casdoor_self_request_rejected"}
    private(response)


@pytest.mark.parametrize(
    "error,status,code",
    [
        (CasdoorSelfReadConflict(), 409, "casdoor_self_read_conflict"),
        (RuntimeError("PRIVATE_SQL_INPUT"), 503, "casdoor_self_unavailable"),
    ],
)
def test_fixed_service_errors(http, monkeypatch, error, status, code):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(http.storage.service, "get", fail)
    response = get(http)
    assert response.status_code == status and response.json == {"code": code}
    private(response)


def test_schema_closed_query_and_original_profile_unchanged():
    schema = controller.CasdoorSelfIdentityStatusResponse.model_json_schema()
    assert schema["additionalProperties"] is False
    assert all(definition.get("additionalProperties") is False for definition in schema["$defs"].values())
    docs = controller.CasdoorSelfIdentityApi.get.__apidoc__
    assert set(docs["params"]) == {"identity_after", "membership_after", "current_membership_after", "limit"}
    assert all(param.get("in") == "query" for param in docs["params"].values())
    from fields.member_fields import AccountResponse

    assert "casdoor_self_identity" not in AccountResponse.model_fields
    assert "CasdoorSelfIdentityStatusResponse" in console_ns.models


@pytest.mark.parametrize("suffix,protected", [("", True), ("/child", True), ("-lookalike", False), ("other", False)])
def test_actual_debug_signals_never_read_protected_bodies(monkeypatch, caplog, suffix, protected):
    app = Flask(__name__)
    marker = "SYNTHETIC_PRIVATE_BODY"
    path = PATH + suffix

    @app.post(path)
    def echo():
        return Response('{"value":"' + marker + '"}', mimetype="application/json")

    monkeypatch.setattr(dify_config, "ENABLE_REQUEST_LOGGING", True)
    monkeypatch.setattr(ext_request_logging, "get_trace_id_from_otel_context", lambda: None)
    ext_request_logging.init_app(app)
    caplog.set_level(logging.DEBUG, logger=ext_request_logging.logger.name)
    original_request_data = app.request_class.get_data
    original_response_data = Response.get_data

    def request_data(self, *args, **kwargs):
        assert not protected
        return original_request_data(self, *args, **kwargs)

    def response_data(self, *args, **kwargs):
        assert not protected
        return original_response_data(self, *args, **kwargs)

    monkeypatch.setattr(app.request_class, "get_data", request_data)
    monkeypatch.setattr(Response, "get_data", response_data)
    response = app.test_client().post(path, data='{"value":"' + marker + '"}', content_type="application/json")
    assert response.status_code == 200
    assert (marker not in caplog.text) is protected
    records = [r for r in caplog.records if r.name == ext_request_logging.logger.name]
    assert len(records) == 3


def test_inherited_native_loader_bookkeeping_is_not_new_reader_zero_write(storage, monkeypatch):
    # Deliberately exercise the native loader separately from the new SELECT-only
    # guards: it commits current-join repair and last-active bookkeeping.
    change(storage, TenantAccountJoin, current=False)
    change(storage, Account, last_active_at=NOW.replace(tzinfo=None) - timedelta(days=20))
    from services import account_service

    calls = []
    monkeypatch.setattr(account_service, "naive_utc_now", lambda: NOW.replace(tzinfo=None))
    monkeypatch.setattr(
        account_service,
        "redis_client",
        SimpleNamespace(set=lambda *args, **kwargs: calls.append((args, kwargs)) or True),
    )
    commits = []
    with storage.factory() as session:
        sa.event.listen(session, "after_commit", lambda _session: commits.append(True))
        account = AccountService.load_user(uid(1), session)
        assert account.current_tenant_id == uid(2)
    assert len(calls) == 1 and len(commits) == 2
    assert uid(1) in calls[0][0][0]
    assert calls[0][1]["nx"] is True
    # Actual native owner made a Redis SET call against an in-process fake;
    # no live Redis or authentication acceptance is claimed.


def add_binding(storage, offset, *, account_id=None):
    """Clone only synthetic fixture rows into a distinct namespace/workspace."""
    account_id = account_id or uid(1)
    with storage.factory.begin() as session:
        for model, original, changes in (
            (Tenant, uid(2), dict(id=uid(offset + 2))),
            (
                Namespace,
                uid(4),
                dict(id=uid(offset + 4), core_fingerprint=hashlib.sha256(str(offset).encode()).hexdigest()),
            ),
            (
                Revision,
                uid(5),
                dict(
                    id=uid(offset + 5),
                    namespace_id=uid(offset + 4),
                    revision_number=offset,
                    default_workspace_id=uid(offset + 2),
                ),
            ),
            (Identity, uid(6), dict(id=uid(offset + 6), namespace_id=uid(offset + 4), account_id=account_id)),
            (
                TenantAccountJoin,
                uid(7),
                dict(id=uid(offset + 7), tenant_id=uid(offset + 2), account_id=account_id, current=False),
            ),
            (
                Membership,
                uid(8),
                dict(
                    id=uid(offset + 8),
                    namespace_id=uid(offset + 4),
                    identity_id=uid(offset + 6),
                    workspace_id=uid(offset + 2),
                    join_id=uid(offset + 7),
                    revision_id=uid(offset + 5),
                    account_id=account_id,
                ),
            ),
        ):
            table = model.__table__
            row = dict(session.execute(sa.select(table).where(table.c.id == original)).mappings().one())
            row.update(changes)
            session.execute(sa.insert(table).values(**row))


def test_three_independent_limit_sentinels_mixed_namespaces_and_foreign_cursor(storage):
    add_binding(storage, 100)
    add_binding(storage, 200, account_id=uid(500))
    first = read(storage, limit=1)
    assert first["identity_next"] == uid(6) and first["identity_has_more"]
    assert first["membership_next"] == uid(8) and first["membership_has_more"]
    assert first["current_membership_next"] == uid(7) and first["current_membership_has_more"]
    second = read(
        storage,
        limit=1,
        identity_after=UUID(first["identity_next"]),
        membership_after=UUID(first["membership_next"]),
        current_membership_after=UUID(first["current_membership_next"]),
    )
    assert second["identities"][0]["id"] == uid(106)
    assert second["identities"][0]["activity"] == "inactive"
    assert second["memberships"][0]["id"] == uid(108)
    assert second["current_memberships"][0]["id"] == uid(107)
    assert not any(second[k] for k in ("identity_has_more", "membership_has_more", "current_membership_has_more"))
    foreign_cursor = read(
        storage,
        limit=1,
        identity_after=UUID(uid(206)),
        membership_after=UUID(uid(208)),
        current_membership_after=UUID(uid(207)),
    )
    assert foreign_cursor["binding"] == "linked"
    assert foreign_cursor["identities"] == foreign_cursor["memberships"] == foreign_cursor["current_memberships"] == []


def test_multiple_namespace_history_same_workspace_is_not_claimed_owned(storage):
    add_binding(storage, 100)
    with storage.factory.begin() as session:
        session.execute(
            sa.update(Membership).where(Membership.id == uid(108)).values(workspace_id=uid(2), join_id=uid(7))
        )
    result = read(storage, limit=1)
    assert result["memberships"][0]["state"] == "unknown"
    assert result["current_memberships"][0]["state"] == "history_present"


@pytest.mark.parametrize("model", [TenantAccountJoin, Membership])
def test_duplicate_physical_rows_fail_closed(storage, model):
    # Simulate a damaged/legacy database by rebuilding ONE synthetic table
    # without its uniqueness constraint; production model/migrations unchanged.
    table = model.__table__
    with storage.engine.begin() as connection:
        rows = [dict(row) for row in connection.execute(sa.select(table)).mappings()]
        table.drop(connection)
        copied = table.to_metadata(sa.MetaData())
        for constraint in tuple(copied.constraints):
            if isinstance(constraint, sa.UniqueConstraint | sa.ForeignKeyConstraint):
                copied.constraints.remove(constraint)
        copied.create(connection)
        rows.append({**rows[0], "id": uid(999)})
        connection.execute(sa.insert(table), rows)
    result = read(storage)
    assert result["memberships"][0]["state"] == "unknown"
    if model is TenantAccountJoin:
        assert all(row["join_presence"] == "unknown" for row in result["current_memberships"])


def test_missing_workspace_does_not_become_unmanaged_or_withdrawal(storage):
    withdrawal(storage)
    with storage.factory.begin() as session:
        session.execute(sa.delete(Tenant))
    item = read(storage)["memberships"][0]
    assert item["state"] == "unknown" and item["workspace_id"] is None


def test_removed_join_id_reused_in_foreign_scope_blocks_controlled_withdrawal(storage):
    withdrawal(storage)
    with storage.factory.begin() as session:
        join = TenantAccountJoin(account_id=uid(500), tenant_id=uid(500))
        join.id = uid(7)
        session.add(join)
    result = read(storage)
    assert result["memberships"][0]["state"] == "unknown"
    assert uid(500) not in json.dumps(result)


@pytest.mark.parametrize("value", ["x\nprivate", "界" * 100])
def test_invalid_organization_is_scoped_unknown(storage, value):
    change(storage, Namespace, organization=value)
    change(storage, Identity, organization=value)
    assert read(storage)["identities"][0]["organization"] is None


def test_raw_bad_boolean_not_coerced_into_verified(storage):
    with storage.engine.begin() as connection:
        connection.execute(sa.update(Identity).values(email_verified=sa.literal_column("2")))
    assert read(storage)["identities"][0]["email"]["verified"] is None


def test_length_guard_repeated_on_materialization(storage, monkeypatch):
    original = CasdoorSelfIdentityRepository.read
    changed = False

    def mutate_after_probe(reader, statement):
        nonlocal changed
        rows = original(reader, statement)
        if (
            not changed
            and "length" in str(statement)
            and "last_applied_json" in str(statement)
            and "CASE" not in str(statement)
        ):
            changed = True
            with storage.engine.begin() as connection:
                connection.execute(sa.update(Identity).values(last_applied_json="x" * 10000))
        for row in rows:
            if "last_applied_json" in row and isinstance(row["last_applied_json"], str):
                assert len(row["last_applied_json"].encode()) <= 4096
        return rows

    monkeypatch.setattr(CasdoorSelfIdentityRepository, "read", mutate_after_probe)
    with pytest.raises(CasdoorSelfReadConflict):
        read(storage)
    assert changed


def test_native_setup_rejection_remains_before_account_read(http, monkeypatch):
    monkeypatch.setattr(wraps, "_is_setup_completed", lambda: False)
    response = get(http)
    assert response.status_code == 401
    private(response)
    assert response.json == {"code": "casdoor_self_request_rejected"}


def test_actual_original_profile_response_still_uses_original_owner(http):
    from fields.member_fields import AccountResponse

    account = http.storage.account
    calls = []
    http.app.extensions["application_services"].accounts = SimpleNamespace(
        profile=SimpleNamespace(get=lambda context: calls.append(context) or account)
    )
    response = http.client.get("/console/api/account/profile", headers=http.headers)
    assert response.status_code == 200
    assert response.json == AccountResponse.model_validate(account, from_attributes=True).model_dump(mode="json")
    assert calls[0].account_id == uid(1)
    assert "casdoor" not in json.dumps(response.json)
    assert response.headers.get("Cache-Control") != "no-store"


@pytest.mark.parametrize("method,status", [("HEAD", 405), ("OPTIONS", 204)])
def test_head_options_explicit_no_auth_or_reader(http, monkeypatch, method, status):
    from libs import login

    def forbidden(*args, **kwargs):
        pytest.fail("non-business method invoked authentication or reader")

    monkeypatch.setattr(login, "_resolve_current_user", forbidden)
    monkeypatch.setattr(http.storage.service, "get", forbidden)
    response = http.client.open(PATH, method=method)
    assert response.status_code == status and response.data == b""
    assert response.headers["Allow"] == "GET, OPTIONS"
    private(response)
    with http.app.test_request_context():
        from controllers.console import api

        operation = api.__schema__["paths"]["/account/casdoor-identity"]
    assert set(operation) == {"get"}
    assert operation["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "CasdoorSelfIdentityStatusResponse"
    )
    assert all(parameter["in"] == "query" for parameter in operation["get"]["parameters"])
    assert operation["get"].get("security") != []


def test_invalid_integration_boolean_does_not_display_active(storage):
    with storage.engine.begin() as connection:
        connection.execute(sa.update(Integration.__table__).values(enabled=sa.literal_column("2")))
    result = read(storage)
    assert result["identities"][0]["activity"] == "unknown"
    assert not any(result["actions"].values())


@pytest.mark.parametrize("field", ["expected_issuer", "organization", "application", "client_id"])
def test_active_revision_core_mismatch_does_not_display_active(storage, field):
    # Core SQL deliberately corrupts a synthetic immutable revision; the reader
    # must compare associations without loading any raw configuration values.
    with storage.engine.begin() as connection:
        connection.execute(
            sa.update(Revision.__table__)
            .where(Revision.__table__.c.id == uid(5))
            .values(**{field: "synthetic-inconsistent-core"})
        )
    result = read(storage)
    assert result["identities"][0]["activity"] == "unknown"
    assert not any(result["actions"].values())
