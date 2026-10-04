"""Initial consumer through actual owners; only original external seams use memory."""

import contextvars
import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import anyio
import httpx
import pytest
import sentry_sdk
import sqlalchemy as sa
from graphon.file import helpers as file_helpers
from opentelemetry.instrumentation.httpx import AsyncOpenTelemetryTransport
from opentelemetry.instrumentation.utils import is_http_instrumentation_enabled
from sqlalchemy.orm import Session
from test_casdoor_avatar_file_extend import picture
from test_casdoor_avatar_intent_extend import URL, pending, storage_fixture
from test_casdoor_avatar_intent_extend import avatar as original_avatar
from test_casdoor_profile_repository_extend import NOW

import core.db.session_factory as factory
from core.casdoor.crypto import CasdoorCrypto
from core.casdoor.permissions import CasdoorManagementPolicy
from core.file import remote_fetcher
from extensions.ext_storage import storage
from models.account import Account, TenantAccountJoin
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.model import UploadFile
from services.account_avatar_file_gateway import SQLAlchemyAccountAvatarFileGateway
from services.casdoor_avatar_consumer_service_extend import CasdoorAvatarConsumerService
from services.casdoor_configuration_service_extend import CasdoorConfigurationService
from tests.unit_tests.core.file.test_remote_fetcher_sensitive_extend import (
    Body,
    MemoryTransport,
    install_client,
)
from tests.unit_tests.core.file.test_remote_fetcher_sensitive_extend import telemetry as original_telemetry

avatar_fixture = original_avatar
storage_fixture = storage_fixture
telemetry = original_telemetry


@pytest.fixture
def consumer(avatar_fixture, monkeypatch):
    s = avatar_fixture
    with s.session.begin():
        s.session.add(TenantAccountJoin(tenant_id=s.revision.default_workspace_id, account_id=s.account.id))
        s.session.flush()
        s.intent_id = pending(s).intent_id
    monkeypatch.setattr(factory, "_session_maker", factory._session_maker)
    factory.configure_session_factory(s.session.get_bind())
    s.maker = factory.get_session_maker()
    s.sessions, s.calls, s.data = [], [], {}
    s.utc, s.offset = NOW, 0.0
    s.close_hook = s.commit_hook = s.save_hook = None
    s.storage_fault = None
    cls = s.maker.class_
    original_init, original_close, original_commit = cls.__init__, cls.close, cls.commit

    def init(session, *args, **kwargs):
        original_init(session, *args, **kwargs)
        session.consumer_index = len(s.sessions) + 1
        session.consumer_closed = False
        s.sessions.append(session)

    def close(session):
        original_close(session)
        session.consumer_closed = True
        if s.close_hook:
            s.close_hook(session.consumer_index)

    def commit(session):
        original_commit(session)
        if s.commit_hook:
            s.commit_hook(session.consumer_index)

    monkeypatch.setattr(cls, "__init__", init)
    monkeypatch.setattr(cls, "close", close)
    monkeypatch.setattr(cls, "commit", commit)

    def no_sql():
        assert not s.session.in_transaction()
        assert all(x.consumer_closed and not x.in_transaction() for x in s.sessions)

    s.no_sql = no_sql

    class Stream:
        def __init__(self, data):
            self.chunks = iter([data])

        def __iter__(self):
            return self

        def __next__(self):
            no_sql()
            if s.storage_fault == "read":
                raise RuntimeError(URL)
            return next(self.chunks)

        def close(self):
            no_sql()
            s.calls.append("storage_close")
            if s.storage_fault == "close":
                raise RuntimeError(URL)

    class MemoryStorage:
        def save(self, key, data):
            no_sql()
            s.calls.append("save")
            s.data[key] = data
            if s.save_hook:
                s.save_hook()
            if s.storage_fault == "save":
                raise RuntimeError(URL)

        def load_stream(self, key):
            no_sql()
            s.calls.append("readback")
            return Stream(s.data[key])

        def delete(self, key):
            pytest.fail("consumer cannot delete")

    monkeypatch.setattr(storage, "storage_runner", MemoryStorage(), raising=False)
    monkeypatch.setattr(remote_fetcher.dify_config, "SSRF_PROXY_ALL_URL", "http://proxy.example.invalid:3128")
    monkeypatch.setattr(remote_fetcher.dify_config, "STORAGE_TYPE", "local")
    monkeypatch.setattr(remote_fetcher.dify_config, "FILES_URL", "https://files.example.invalid")
    monkeypatch.setattr(remote_fetcher.dify_config, "INTERNAL_FILES_URL", "https://internal-files.example.invalid")
    from PIL import Image

    original_open = Image.open

    def image_open(*args, **kwargs):
        no_sql()
        s.calls.append("codec")
        return original_open(*args, **kwargs)

    monkeypatch.setattr(Image, "open", image_open)
    original_decrypt = CasdoorCrypto.decrypt

    def decrypt(crypto, *args, **kwargs):
        no_sql()
        assert remote_fetcher._sensitive_file_request.get()
        assert not is_http_instrumentation_enabled()
        s.calls.append("decrypt")
        return original_decrypt(crypto, *args, **kwargs)

    monkeypatch.setattr(CasdoorCrypto, "decrypt", decrypt)
    configuration = CasdoorConfigurationService(
        session_factory=s.maker,
        management_policy=CasdoorManagementPolicy.from_deployment(""),
        secret_key="synthetic-profile-key",
        rbac_enabled=False,
    )
    s.consumer = CasdoorAvatarConsumerService(
        session_factory=s.maker,
        configuration_service=configuration,
        now=lambda: s.utc,
        monotonic=lambda: time.monotonic() + s.offset,
    )

    class Transport(MemoryTransport):
        async def handle_async_request(self, request):
            no_sql()
            s.calls.append("http")
            return await super().handle_async_request(request)

    def http(*, mode="ok", content=None, status=200):
        s.transport = Transport(Body([picture() if content is None else content], mode), status=status)
        install_client(monkeypatch, s.transport)
        return s.transport

    s.http = http
    http()
    yield s
    assert all(x.consumer_closed and not x.in_transaction() for x in s.sessions)


def run(s):
    result = s.consumer._consume_initial(s.intent_id)
    assert URL not in repr(result)
    return result


def row(s):
    with Session(s.session.get_bind()) as reader:
        return dict(reader.execute(sa.select(Intent.__table__)).mappings().one())


def unknown(s, reason):
    value = row(s)
    assert value["operation_state"] == "unknown"
    assert value["termination_state"] == "manual_recovery"
    assert value["error_code"] == reason
    assert value["attempt_count"] == 1
    assert value["retry_at"] is value["terminated_at"] is value["termination_proof_kind"] is None
    assert len(json.loads(value["desired_json"])["reservations"]) == 1
    with Session(s.session.get_bind()) as reader:
        assert reader.scalar(sa.select(sa.func.count()).select_from(UploadFile)) == 0
        audit = reader.scalar(sa.select(Audit).where(Audit.action == "avatar_finish"))
        assert audit.result_code == "unknown"
    assert s.calls.count("save") <= 1


def drift(s, kind):
    with Session(s.session.get_bind()) as session, session.begin():
        if kind == "config":
            session.execute(sa.update(Integration).values(enabled=False))
        elif kind == "generation":
            session.execute(sa.update(Identity).values(sync_generation=2))
        elif kind == "profile":
            session.execute(sa.update(Identity).values(profile_sync_json="{}"))
        elif kind == "join":
            session.execute(sa.delete(TenantAccountJoin))
        elif kind == "proof":
            session.execute(sa.update(Intent).values(proof_ref=str(uuid4())))
        else:
            session.execute(sa.update(Account).values(avatar=str(uuid4())))


def test_actual_complete_flow_fresh_gateway_and_strict_replay(consumer, monkeypatch):
    s = consumer
    result = run(s)
    assert result.code == "applied" and type(result.file_id) is UUID
    assert len(s.sessions) == 6
    assert s.calls.count("save") == s.calls.count("readback") == s.calls.count("storage_close") == 1
    assert s.calls.count("decrypt") == s.calls.count("http") == 1
    with Session(s.session.get_bind()) as reader:
        file = reader.get(UploadFile, str(result.file_id))
        assert file.created_by == s.account.id and file.tenant_id == s.revision.default_workspace_id
        assert file.source_url == "" and file.used
        assert file.hash == hashlib.sha3_256(s.data[file.key]).hexdigest() and file.size == len(s.data[file.key])
        assert reader.get(Account, s.account.id).avatar == file.id
        assert reader.scalar(sa.select(sa.func.count()).select_from(Audit).where(Audit.action == "avatar_attach")) == 1
    monkeypatch.setattr(file_helpers, "get_signed_file_url", lambda **kw: "signed:" + kw["upload_file_id"])
    gateway = SQLAlchemyAccountAvatarFileGateway(session_factory=s.maker)
    assert gateway.get_owned_signed_url(account_id=s.account.id, upload_file_id=str(result.file_id)) == (
        "signed:" + str(result.file_id)
    )
    drift(s, "config")
    drift(s, "avatar")
    before = s.calls.copy()
    assert run(s) == result and s.calls == before
    drift(s, "proof")
    assert run(s).code == "unknown" and s.calls == before


@pytest.mark.parametrize("stage", ["claim", "attach"])
def test_actual_commit_success_then_lost_ack(consumer, stage):
    s = consumer

    def lost(index):
        if index == (2 if stage == "claim" else 5):
            if stage == "attach":
                drift(s, "config")
                drift(s, "generation")
                drift(s, "join")
                drift(s, "avatar")
            raise RuntimeError(URL)

    s.commit_hook = lost
    result = run(s)
    if stage == "claim":
        assert result.code == "unknown" and not s.calls
        assert row(s)["operation_state"] == "in_flight" and len(s.sessions) == 2
    else:
        assert result.code == "applied" and len(s.sessions) == 6 and s.calls.count("save") == 1
        assert row(s)["resource_id"] == str(result.file_id)


@pytest.mark.parametrize("stage,kind", [(3, "config"), (3, "profile"), (3, "join"), (3, "generation"), (4, "config")])
def test_fresh_authority_drift_stops_storage_or_attach(consumer, stage, kind):
    s = consumer
    s.close_hook = lambda index: drift(s, kind) if index == stage else None
    assert run(s).code == "unknown"
    unknown(s, "attachment_lost")
    assert s.calls.count("save") == (1 if stage == 4 else 0)


@pytest.mark.parametrize("mode", ["before_fetch", "before_save", "wall_reversal", "late_storage"])
def test_deadline_never_extends_and_late_storage_never_attaches(consumer, mode):
    s = consumer

    def advance():
        s.offset += 16
        s.utc += timedelta(seconds=-120 if mode == "wall_reversal" else 301)

    if mode == "late_storage":
        s.save_hook = advance
    else:
        s.close_hook = lambda index: advance() if index == (3 if mode == "before_fetch" else 4) else None
    assert run(s).code == "unknown"
    unknown(s, "lease_expired")
    assert s.calls.count("save") == (1 if mode == "late_storage" else 0)
    if mode == "before_fetch":
        assert not s.calls


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("proxy", "fetch_rejected"),
        ("error", "fetch_failed"),
        ("cancel", "fetch_cancelled"),
        ("close_error", "fetch_unknown"),
        ("image", "image_rejected"),
        ("save", "storage_unknown"),
        ("read", "storage_unknown"),
        ("close", "storage_unknown"),
    ],
)
def test_actual_fetch_codec_storage_negative_paths(consumer, monkeypatch, mode, reason):
    s = consumer
    if mode == "proxy":
        for name in ("SSRF_PROXY_ALL_URL", "SSRF_PROXY_HTTP_URL", "SSRF_PROXY_HTTPS_URL"):
            monkeypatch.setattr(remote_fetcher.dify_config, name, "")
    elif mode == "error":
        def supplier_failure(*args, **kwargs):
            raise ValueError("synthetic local supplier failure")

        monkeypatch.setattr(CasdoorCrypto, "decrypt", supplier_failure)
    elif mode == "image":
        s.http(content=b"invalid-image")
    elif mode in ("save", "read", "close"):
        s.storage_fault = mode
    else:
        s.http(mode=mode)
    assert run(s).code == "unknown"
    if mode in ("proxy", "error", "image"):
        value = row(s)
        assert value["operation_state"] == "failed" and value["termination_state"] == "confirmed"
        assert value["termination_proof_kind"] == "avatar_pre_storage" and value["error_code"] == reason
        assert json.loads(value["desired_json"])["cleanup_state"] == "complete"
    else:
        unknown(s, reason)
    if mode == "proxy":
        assert not s.calls


def test_attachment_audit_fault_rolls_back_entire_root(consumer):
    s = consumer
    with s.session.begin():
        s.session.execute(
            sa.text(
                "CREATE TRIGGER attach_fault AFTER INSERT ON casdoor_audit_extend "
                "WHEN NEW.action='avatar_attach' BEGIN SELECT RAISE(ABORT, 'synthetic fault'); END"
            )
        )
    assert run(s).code == "unknown"
    unknown(s, "attachment_lost")
    assert s.calls.count("save") == 1 and s.data
    with Session(s.session.get_bind()) as reader:
        assert reader.get(Account, s.account.id).avatar is None


@pytest.mark.parametrize(
    "site,signal",
    [("supplier", KeyboardInterrupt), ("stream", SystemExit), ("codec", KeyboardInterrupt), ("storage", SystemExit)],
)
def test_sanitized_shutdown_survives_finish_and_close_faults(consumer, monkeypatch, site, signal):
    s = consumer

    def stop(*args, **kwargs):
        raise signal(URL)

    if site == "supplier":
        monkeypatch.setattr(CasdoorCrypto, "decrypt", stop)
    elif site == "stream":
        s.http(mode="exit")
    elif site == "codec":
        from PIL import Image

        monkeypatch.setattr(Image, "open", stop)
    else:
        s.save_hook = stop
    with s.session.begin():
        s.session.execute(
            sa.text(
                "CREATE TRIGGER finish_fault AFTER INSERT ON casdoor_audit_extend "
                "WHEN NEW.action='avatar_finish' BEGIN SELECT RAISE(ABORT, 'synthetic finish fault'); END"
            )
        )
    s.close_hook = lambda index: stop() if index >= 4 else None
    with pytest.raises(signal) as caught:
        run(s)
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert URL not in repr(caught.value) and str(caught.value) in ("avatar consumption interrupted", "1")
    assert row(s)["operation_state"] == "in_flight"
    assert s.calls.count("save") <= 1


def test_private_decrypt_telemetry_and_concurrent_ordinary_provider(consumer, monkeypatch, telemetry, caplog):
    s = consumer
    client, sink, exporter, provider = telemetry
    caplog.set_level(logging.DEBUG)
    transport = s.http()
    install_client(monkeypatch, AsyncOpenTelemetryTransport(transport, tracer_provider=provider))
    entered, released = threading.Event(), threading.Event()
    original = CasdoorCrypto.decrypt

    def decrypt(*args, **kwargs):
        assert not is_http_instrumentation_enabled()
        assert sentry_sdk.get_client() is not client
        value = original(*args, **kwargs)
        logging.getLogger("httpx").warning(value)
        try:
            raise ValueError(value)
        except ValueError as error:
            sentry_sdk.capture_exception(error)
        entered.set()
        assert released.wait(3)
        return value

    monkeypatch.setattr(CasdoorCrypto, "decrypt", decrypt)
    ordinary = httpx.AsyncClient(
        transport=AsyncOpenTelemetryTransport(MemoryTransport(Body([b"public"])), tracer_provider=provider)
    )

    def provider_request():
        assert entered.wait(3)
        try:

            async def request():
                await ordinary.get("https://ordinary.example.invalid/provider")
                await ordinary.aclose()

            anyio.run(request)
            logging.getLogger("httpx").warning("ordinary consumer provider visible")
            sentry_sdk.capture_message("ordinary consumer provider visible")
        finally:
            released.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(contextvars.copy_context().run, provider_request)
        result = run(s)
        task.result(timeout=4)
    assert result.code == "applied"
    sentinel = "synthetic-sensitive-value"
    assert sentinel not in caplog.text + repr(result)
    assert "ordinary consumer provider visible" in caplog.text
    spans = exporter.get_finished_spans()
    assert spans and sentinel not in repr([span.to_json() for span in spans])
    assert any("ordinary.example.invalid" in repr(span.attributes) for span in spans)
    assert sink.payloads and all(sentinel.encode() not in payload for payload in sink.payloads)
    assert any(b"ordinary consumer provider visible" in payload for payload in sink.payloads)


@pytest.mark.parametrize("malformed", [False, True])
def test_weak_claim_replay_race_requires_new_strict_reader(consumer, malformed):
    s = consumer
    race = SimpleNamespace(result=None, calls=None)

    def competitor(index):
        if index == 1:
            s.close_hook = None
            race.result = run(s)
            assert race.result.code == "applied"
            if malformed:
                drift(s, "proof")
            race.calls = s.calls.copy()

    s.close_hook = competitor
    result = run(s)
    assert result.code == ("unknown" if malformed else "applied")
    assert s.calls == race.calls and s.calls.count("save") == 1
    assert len(s.sessions) == 9


def test_successful_attach_ack_still_checks_exact_original_hash(consumer):
    s = consumer

    def corrupt(index):
        if index == 5:
            with Session(s.session.get_bind()) as session, session.begin():
                session.execute(sa.update(UploadFile).values(hash="0" * 64))

    s.commit_hook = corrupt
    assert run(s).code == "unknown"
    assert row(s)["operation_state"] == "applied" and s.calls.count("save") == 1
    assert len(s.sessions) == 7
