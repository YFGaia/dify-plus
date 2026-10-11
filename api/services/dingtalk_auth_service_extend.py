"""Verified DingTalk onboarding and login with short, caller-owned SQL writes.

Provider I/O precedes SQL mutations. Configuration is one existing integration
row; private revision/validation metadata is server-owned. Redis transactions
are encrypted, browser/principal bound, short lived and consumed before exchange.
No provider email can claim an existing unbound local account.
"""

import base64
import hashlib
import json
import re
import secrets
import time
from copy import deepcopy
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from configs import dify_config
from core.helper import ssrf_proxy
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from extensions.ext_database import db
from extensions.ext_redis import redis_client
from libs.datetime_utils import naive_utc_now
from libs.sensitive_redis import sensitive_redis_call
from models.account import Account, AccountIntegrate, AccountStatus, Tenant, TenantAccountJoin, TenantStatus
from models.system_extend import SystemIntegrationExtend
from models.system_management_scope_extend import SystemManagementScopeExtend
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from services.account_service import AccountService, TenantService
from services.dingtalk_email_lookup_extend import lookup_email, validate_email_lookup
from services.system_manage_extend import _decrypt_blowfish, _encrypt_blowfish
from services.system_management_access_service_extend import SystemManagementAccessService

_META = "_dingtalk"
_EMAIL = "_email_api_encrypted"
_OPAQUE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_REGISTRATION_ROOT = "https://oapi.dingtalk.com/app/registration/"
_REGISTRATION_RETRY_SECONDS = 120
_CALLBACK_PATH = "/console/api/auth/dingtalk/callback"
_CONSUME = """
local raw = redis.call('GET', KEYS[1])
if not raw or raw ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1])
return 1
"""
_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""
_REGISTRATION_UPDATE = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[2], 'KEEPTTL')
return 1
"""


class DingTalkAuthError(ValueError):
    """Fixed errors only: credentials, provider bodies and identifiers stay private."""

    def __init__(self, code: str, status: int = 400):
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class DingTalkIdentity:
    subject: str
    email: str
    name: str


class DingTalkAuthService:
    def __init__(self, *, session_factory=None, redis=None, settings=None, clock=None):
        self.session_factory = session_factory or sessionmaker(bind=db.engine, expire_on_commit=False)
        self.redis = redis if redis is not None else redis_client
        self.settings = settings or dify_config
        self.clock = clock or time.time

    def _redis(self, operation):
        failed = False
        try:
            with sensitive_redis_call():
                result = operation()
        except Exception:
            failed = True
        if failed:
            raise DingTalkAuthError("storage_unavailable", 503) from None
        return result

    def _seal(self, value: dict, context: str) -> str:
        if not self.settings.SECRET_KEY:
            raise DingTalkAuthError("encryption_key_required", 503)
        key = hashlib.sha256(b"dify-plus:dingtalk:v1\0" + self.settings.SECRET_KEY.encode()).digest()
        nonce = secrets.token_bytes(12)
        data = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()
        return base64.urlsafe_b64encode(nonce + AESGCM(key).encrypt(nonce, data, context.encode())).decode()

    def _open(self, value: str | bytes, context: str) -> dict:
        try:
            raw = base64.urlsafe_b64decode(value)
            key = hashlib.sha256(b"dify-plus:dingtalk:v1\0" + self.settings.SECRET_KEY.encode()).digest()
            result = json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], context.encode()))
            if not isinstance(result, dict):
                raise ValueError
            return result
        except Exception:
            raise DingTalkAuthError("invalid_transaction") from None

    @staticmethod
    def _opaque(value: str) -> None:
        if not isinstance(value, str) or not _OPAQUE.fullmatch(value):
            raise DingTalkAuthError("invalid_transaction")

    def callback_url(self) -> str:
        origin = self.settings.CONSOLE_API_URL.rstrip("/")
        parsed = urlsplit(origin)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise DingTalkAuthError("invalid_callback_configuration")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http"
            and self.settings.DEPLOY_ENV == "DEVELOPMENT"
            and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise DingTalkAuthError("https_callback_required")
        return origin + _CALLBACK_PATH

    @staticmethod
    def _guard(account, session) -> str:
        membership = SystemManagementAccessService.membership(account, session=session)
        if membership is None:
            raise DingTalkAuthError("system_management_forbidden", 403)
        return membership.tenant_id

    @staticmethod
    def _record(session, *, create=False, lock=False):
        # Serialize first creation on the unique initialization anchor. The legacy
        # integration schema has only a non-unique classify index; ambiguity fails.
        if create:
            anchor = session.scalar(
                select(SystemManagementScopeExtend)
                .where(SystemManagementScopeExtend.id == "initialization")
                .with_for_update()
            )
            if anchor is None:
                raise DingTalkAuthError("initialization_workspace_unavailable")
        query = select(SystemIntegrationExtend).where(SystemIntegrationExtend.classify == 1)
        if lock:
            query = query.with_for_update()
        rows = session.scalars(query).all()
        if len(rows) > 1:
            raise DingTalkAuthError("ambiguous_configuration", 409)
        if not rows and create:
            record = SystemIntegrationExtend(
                classify=1, status=False, config=json.dumps({_META: {"revision": str(uuid4())}})
            )
            session.add(record)
            session.flush()
            return record
        return rows[0] if rows else None

    def _config(self, record) -> dict:
        try:
            config = json.loads(record.config or "{}")
            if not isinstance(config, dict):
                raise ValueError
            if _EMAIL in config:
                config["email_api"] = self._open(config[_EMAIL], f"email:{record.id}")
            return config
        except DingTalkAuthError:
            raise
        except Exception:
            raise DingTalkAuthError("invalid_saved_configuration") from None

    @staticmethod
    def _meta(config) -> dict:
        value = config.get(_META, {})
        return value if isinstance(value, dict) else {}

    def _revision(self, record, config) -> str:
        # A deterministic legacy fingerprint is displayable but never verified.
        return (
            self._meta(config).get("revision")
            or "legacy-"
            + hashlib.sha256(f"{record.id}:{record.app_key}:{record.app_secret}:{record.config}".encode()).hexdigest()
        )

    def _save_config(self, record, config: dict) -> None:
        config = deepcopy(config)
        if "email_api" in config:
            config[_EMAIL] = self._seal(config.pop("email_api"), f"email:{record.id}")
        record.config = json.dumps(config, ensure_ascii=False)

    def _view(self, record, config=None) -> dict:
        if record is None:
            return {
                "status": False,
                "revision": "",
                "config": {},
                "callback_url": self.callback_url(),
                "app_secret": "",
                "secret_configured": False,
                "verified": False,
                "connection_verified": False,
                "corp_id": "",
                "app_key": "",
                "agent_id": "",
                "app_id": "",
            }
        config = deepcopy(config if config is not None else self._config(record))
        meta = self._meta(config)
        revision = self._revision(record, config)
        verified = meta.get("verified_revision") == revision
        connection = meta.get("connection_revision") == revision
        config.pop(_META, None)
        config.pop(_EMAIL, None)
        if "email_api" in config:
            email = config["email_api"]
            auth = email.get("authorization", {})
            sensitive = bool(
                email.get("headers")
                or email.get("body_data")
                or any(auth.get(key) for key in ("token", "username", "password"))
            )
            # Credential-bearing header and body content must never round trip.
            email["headers"] = {}
            email["body_data"] = {}
            email["authorization"] = {"type": auth.get("type", "none")}
            email["credentials_configured"] = sensitive
        return {
            "status": bool(record.status and verified and connection),
            "corp_id": record.corp_id or "",
            "agent_id": record.agent_id or "",
            "app_key": record.app_key or "",
            "app_id": record.app_id or "",
            "app_secret": "",
            "config": config,
            "revision": revision,
            "connection_verified": connection,
            "verified": verified,
            "secret_configured": bool(record.app_secret),
            "callback_url": self.callback_url(),
        }

    def get(self, account) -> dict:
        with self.session_factory() as session, session.begin():
            self._guard(account, session)
            record = self._record(session, create=True, lock=True)
            config = self._config(record)
            if not self._meta(config).get("revision"):
                # Legacy enabled rows must be reverified. Normalize metadata and
                # migrate email credentials without returning their plaintext.
                config[_META] = {"revision": str(uuid4())}
                record.status = False
            self._save_config(record, config)
            return self._view(record, config)

    def save(self, account, payload: dict) -> dict:
        with self.session_factory() as session, session.begin():
            self._guard(account, session)
            record = self._record(session, create=True, lock=True)
            return self._save(record, payload)

    def _save(self, record, payload: dict) -> dict:
        if payload.get("status") is True:
            raise DingTalkAuthError("login_verification_required")
        previous = self._config(record)
        revision = self._revision(record, previous)
        if payload.get("expected_revision") is not None and payload["expected_revision"] != revision:
            raise DingTalkAuthError("configuration_changed", 409)
        public = {k: v for k, v in previous.items() if k not in {_META, _EMAIL}}
        new_config = deepcopy(payload.get("config", public))
        if any(k.startswith("_") for k in new_config):
            raise DingTalkAuthError("server_owned_configuration")
        if "email_api" in new_config:
            email = new_config["email_api"]
            if not isinstance(email, dict):
                raise DingTalkAuthError("invalid_email_lookup")
            old_email = public.get("email_api", {})
            # Reload DTOs carry only presence. Blank credentials preserve stored
            # values; replacing a credential requires an explicit nonempty input.
            email.pop("credentials_configured", None)
            for field in ("headers", "body_data"):
                if not email.get(field) and old_email.get(field):
                    email[field] = deepcopy(old_email[field])
            auth = email.setdefault("authorization", {})
            if isinstance(auth, dict):
                for field in ("token", "username", "password"):
                    if not auth.get(field) and old_email.get("authorization", {}).get(field):
                        auth[field] = old_email["authorization"][field]
            try:
                validate_email_lookup(email)
            except ValueError:
                raise DingTalkAuthError("invalid_email_lookup") from None
        changed = new_config != public
        for field in ("corp_id", "agent_id", "app_key", "app_id"):
            if field in payload and payload[field] != (getattr(record, field) or ""):
                setattr(record, field, payload[field])
                changed = True
        secret = payload.get("app_secret")
        if secret and "*" not in secret:
            old_secret = _decrypt_blowfish(record.app_secret, self.settings.SECRET_KEY) if record.app_secret else ""
            if secret != old_secret:
                record.app_secret = _encrypt_blowfish(secret, self.settings.SECRET_KEY)
                changed = True
        meta = self._meta(previous)
        if changed or not meta.get("revision"):
            meta = {"revision": str(uuid4())}
            record.status = False
        elif payload.get("status") is False:
            meta = {"revision": str(uuid4())}
            record.status = False
        new_config[_META] = meta
        self._save_config(record, new_config)
        return {**self._view(record, new_config), "result": "success"}

    def display(self) -> dict:
        with self.session_factory() as session:
            record = self._record(session)
            if record is None:
                enabled = False
            else:
                meta = self._meta(self._config(record))
                revision = meta.get("revision")
                enabled = bool(
                    revision
                    and record.status
                    and meta.get("verified_revision") == revision
                    and meta.get("connection_revision") == revision
                )
            return {"enabled": enabled, "button_text": "钉钉", "start_path": "/console/api/auth/dingtalk/start"}

    def _snapshot(self, account=None, *, enabled=False) -> dict:
        with self.session_factory() as session, session.begin():
            tenant = self._guard(account, session) if account is not None else None
            record = self._record(session)
            if record is None or not record.app_key or not record.app_secret:
                raise DingTalkAuthError("credentials_required")
            config = self._config(record)
            if enabled and not self._view(record, config)["status"]:
                raise DingTalkAuthError("login_disabled")
            return {
                "revision": self._revision(record, config),
                "app_key": record.app_key,
                "secret": _decrypt_blowfish(record.app_secret, self.settings.SECRET_KEY),
                "corp_id": record.corp_id or "",
                "tenant_id": tenant,
                "config": config,
            }

    def _request(self, method: str, url: str, **kwargs) -> dict:
        failed = False
        try:
            response = ssrf_proxy.make_request(
                method, url, max_retries=0, timeout=10, follow_redirects=False, ssl_verify=True, **kwargs
            )
            if response.status_code != 200:
                raise ValueError
            result = response.json()
            if not isinstance(result, dict) or result.get("errcode", 0) != 0:
                raise ValueError
        except Exception:
            failed = True
        if failed:
            raise DingTalkAuthError("provider_request_failed", 502) from None
        return result

    def _app_token(self, snapshot) -> str:
        data = self._request(
            "POST",
            "https://api.dingtalk.com/v1.0/oauth2/accessToken",
            json={"appKey": snapshot["app_key"], "appSecret": snapshot["secret"]},
        )
        token = data.get("accessToken")
        if not isinstance(token, str) or not token:
            raise DingTalkAuthError("invalid_provider_response", 502)
        return token

    def test(self, account) -> dict:
        snapshot = self._snapshot(account)
        self._app_token(snapshot)
        with self.session_factory() as session, session.begin():
            self._guard(account, session)
            record = self._record(session, lock=True)
            config = self._require_revision(record, snapshot["revision"])
            config[_META] = {**self._meta(config), "connection_revision": snapshot["revision"]}
            self._save_config(record, config)
        return {"result": "success", "message": "连接验证通过；请继续完成真实登录诊断。"}

    def test_email(self, account, userid: str, draft: dict) -> dict:
        """Reuse hidden saved credentials without disclosing them to the client."""
        with self.session_factory() as session, session.begin():
            self._guard(account, session)
            record = self._record(session)
            stored = self._config(record).get("email_api", {}) if record else {}
        config = deepcopy(draft)
        config.pop("credentials_configured", None)
        for field in ("headers", "body_data"):
            if not config.get(field) and stored.get(field):
                config[field] = deepcopy(stored[field])
        auth = config.setdefault("authorization", {})
        if isinstance(auth, dict):
            for field in ("token", "username", "password"):
                if not auth.get(field) and stored.get("authorization", {}).get(field):
                    auth[field] = stored["authorization"][field]
        return lookup_email(userid, config)

    def _require_revision(self, record, revision: str, *, enabled=False):
        if record is None:
            raise DingTalkAuthError("configuration_changed", 409)
        config = self._config(record)
        if self._revision(record, config) != revision:
            raise DingTalkAuthError("configuration_changed", 409)
        if enabled and not self._view(record, config)["status"]:
            raise DingTalkAuthError("login_disabled")
        return config

    def enable(self, account) -> dict:
        with self.session_factory() as session, session.begin():
            self._guard(account, session)
            record = self._record(session, lock=True)
            view = self._view(record)
            if not view["connection_verified"] or not view["verified"]:
                raise DingTalkAuthError("login_verification_required")
            record.status = True
            return self._view(record)

    def disable(self, account) -> dict:
        with self.session_factory() as session, session.begin():
            self._guard(account, session)
            record = self._record(session, lock=True)
            if record is not None:
                config = self._config(record)
                config[_META] = {"revision": str(uuid4())}
                record.status = False
                self._save_config(record, config)
            return self._view(record)

    def registration_start(self, account) -> dict:
        with self.session_factory() as session, session.begin():
            tenant = self._guard(account, session)
            record = self._record(session, create=True, lock=True)
            revision = self._revision(record, self._config(record))
        limit = f"dify:dingtalk:registration-start:{account.id}"
        if not self._redis(lambda: self.redis.set(limit, "1", nx=True, ex=10)):
            raise DingTalkAuthError("registration_start_throttled", 429)
        # Custom source markers require DingTalk approval; the default protocol
        # accepts an empty body and needs no preconfigured application identity.
        init = self._request("POST", _REGISTRATION_ROOT + "init", json={})
        nonce = init.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise DingTalkAuthError("invalid_provider_response", 502)
        begin = self._request("POST", _REGISTRATION_ROOT + "begin", json={"nonce": nonce})
        uri = begin.get("verification_uri_complete")
        parsed = urlsplit(uri) if isinstance(uri, str) else None
        if (
            parsed is None
            or parsed.scheme != "https"
            or not parsed.hostname
            or not (parsed.hostname == "dingtalk.com" or parsed.hostname.endswith(".dingtalk.com"))
            or parsed.username
            or parsed.password
        ):
            raise DingTalkAuthError("invalid_verification_uri", 502)
        device = begin.get("device_code")
        if not isinstance(device, str) or not device or len(device) > 4096:
            raise DingTalkAuthError("invalid_provider_response", 502)
        try:
            ttl = min(900, max(1, int(begin.get("expires_in", 300))))
            interval = min(60, max(5, int(begin.get("interval", 5))))
        except (ValueError, TypeError):
            raise DingTalkAuthError("invalid_provider_response", 502) from None
        # Check the initiating authority/revision again after the external calls.
        with self.session_factory() as session, session.begin():
            if self._guard(account, session) != tenant:
                raise DingTalkAuthError("configuration_changed", 409)
            self._require_revision(self._record(session), revision)
        tid = secrets.token_urlsafe(32)
        key = self._registration_key(tid)
        value = {
            "account_id": account.id,
            "tenant_id": tenant,
            "revision": revision,
            "device_code": device,
            "status": "WAITING",
            "interval": interval,
            "expires_at": self.clock() + ttl,
        }
        if not self._redis(lambda: self.redis.set(key, self._seal(value, key), nx=True, ex=ttl)):
            raise DingTalkAuthError("storage_unavailable", 503)
        return {
            "transaction_id": tid,
            "status": "WAITING",
            "verification_uri_complete": uri,
            "expires_in": ttl,
            "interval": interval,
        }

    def _registration_key(self, tid: str) -> str:
        self._opaque(tid)
        return "dify:dingtalk:registration:" + tid

    def registration_poll(self, account, tid: str, *, cancel=False) -> dict:
        key = self._registration_key(tid)
        lock_key = key + ":lock"
        lock_value = secrets.token_urlsafe(32)
        if not self._redis(lambda: self.redis.set(lock_key, lock_value, nx=True, ex=30)):
            raise DingTalkAuthError("registration_busy", 429)
        try:
            return self._registration_poll(account, tid, key, cancel=cancel)
        finally:
            self._redis(lambda: self.redis.eval(_RELEASE, 1, lock_key, lock_value))

    def _registration_poll(self, account, tid, key, *, cancel):
        raw = self._redis(lambda: self.redis.get(key))
        with self.session_factory() as session, session.begin():
            tenant = self._guard(account, session)
            if not raw:
                return {"transaction_id": tid, "status": "EXPIRED"}
            value = self._open(raw, key)
            if value.get("account_id") != account.id or value.get("tenant_id") != tenant:
                raise DingTalkAuthError("registration_owner_mismatch", 403)
            if value["status"] != "WAITING":
                return {"transaction_id": tid, "status": value["status"]}
            self._require_revision(self._record(session), value["revision"])
        if self.clock() >= value["expires_at"]:
            value["status"] = "EXPIRED"
        elif cancel:
            value["status"] = "CANCELLED"
        elif self.clock() - value.get("retry_started_at", self.clock()) >= _REGISTRATION_RETRY_SECONDS:
            value["status"] = "FAIL"
        else:
            if not self._redis(lambda: self.redis.set(key + ":poll", "1", nx=True, ex=value["interval"])):
                return {"transaction_id": tid, "status": "WAITING", "interval": value["interval"]}
            try:
                result = self._request("POST", _REGISTRATION_ROOT + "poll", json={"device_code": value["device_code"]})
            except DingTalkAuthError as error:
                if error.code != "provider_request_failed":
                    raise
                result = {}
            supplied_status = result.get("status")
            status = supplied_status.strip().upper() if isinstance(supplied_status, str) else "UNKNOWN"
            if self.clock() >= value["expires_at"]:
                status = "EXPIRED"
            elif status not in {"WAITING", "SUCCESS", "FAIL", "EXPIRED"}:
                # Official device auth tolerates short unknown/network states.
                # Only a validated SUCCESS may ever supply credentials.
                retry_started_at = value.setdefault("retry_started_at", self.clock())
                status = "FAIL" if self.clock() - retry_started_at >= _REGISTRATION_RETRY_SECONDS else "WAITING"
            else:
                value.pop("retry_started_at", None)
            value["status"] = status
            if status == "SUCCESS":
                client_id, client_secret = result.get("client_id"), result.get("client_secret")
                if (
                    not isinstance(client_id, str)
                    or not client_id
                    or not isinstance(client_secret, str)
                    or not client_secret
                ):
                    raise DingTalkAuthError("invalid_provider_response", 502)
                with self.session_factory() as session, session.begin():
                    if self._guard(account, session) != value["tenant_id"] or self.clock() >= value["expires_at"]:
                        raise DingTalkAuthError("registration_expired")
                    record = self._record(session, lock=True)
                    self._require_revision(record, value["revision"])
                    # Cancellation and SQL locking live in separate stores.
                    # This bounded Redis read inside the write UoW is required
                    # to reject cancellation that occurred while waiting for
                    # the row lock. No external provider I/O occurs here.
                    if self._redis(lambda: self.redis.get(key)) != raw:
                        raise DingTalkAuthError("registration_cancelled", 409)
                    metadata = {}
                    for field in ("corp_id", "agent_id"):
                        supplied = result.get(field)
                        metadata[field] = supplied if isinstance(supplied, str) and len(supplied) <= 120 else ""
                    # Registration's unified_app_id is the current application
                    # identifier; accept the older app_id only as a fallback.
                    metadata["app_id"] = next(
                        (
                            supplied
                            for supplied in (result.get("unified_app_id"), result.get("app_id"))
                            if isinstance(supplied, str) and supplied and len(supplied) <= 120
                        ),
                        "",
                    )
                    self._save(record, {"app_key": client_id, "app_secret": client_secret, "status": False, **metadata})
        if value["status"] != "WAITING":
            value.pop("device_code", None)
            value.pop("retry_started_at", None)
        if value["status"] != "SUCCESS":
            # Recheck authority/revision after provider I/O before persisting a
            # retry or terminal state, just as credential writes do above.
            with self.session_factory() as session, session.begin():
                if self._guard(account, session) != value["tenant_id"]:
                    raise DingTalkAuthError("configuration_changed", 409)
                self._require_revision(self._record(session), value["revision"])
        if self.clock() >= value["expires_at"]:
            return {"transaction_id": tid, "status": "EXPIRED"}
        sealed = self._seal(value, key)
        # Compare the original ciphertext so a cancelled/expired transaction
        # cannot be resurrected even if the polling lock lease has elapsed.
        # Retrying and terminal replay never extend the registration lifetime.
        if not self._redis(lambda: self.redis.eval(_REGISTRATION_UPDATE, 1, key, raw, sealed)):
            raise DingTalkAuthError("registration_cancelled", 409)
        return {"transaction_id": tid, "status": value["status"], "interval": value["interval"]}

    @staticmethod
    def cookie_name(state: str) -> str:
        return "dingtalk_auth_" + hashlib.sha256(state.encode()).hexdigest()[:24]

    def authorization_start(
        self, *, account=None, refresh_token=None, bind=False, server_ip=""
    ) -> tuple[str, str, str]:
        snapshot = self._snapshot(account, enabled=account is None)
        source = {}
        if account is not None:
            if (
                not refresh_token
                or self._redis(lambda: AccountService.resolve_refresh_token(refresh_token)) != account.id
            ):
                raise DingTalkAuthError("administrator_session_required", 403)
            source = {
                "account_id": account.id,
                "tenant_id": snapshot["tenant_id"],
                "refresh_digest": hashlib.sha256(refresh_token.encode()).hexdigest(),
                "bind": bool(bind),
            }
        limit = "dify:dingtalk:auth-rate:" + hashlib.sha256(server_ip.encode()).hexdigest()
        if not self._redis(lambda: self.redis.set(limit, "1", nx=True, ex=2)):
            raise DingTalkAuthError("authorization_throttled", 429)
        state, cookie = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        key = "dify:dingtalk:oauth:" + state
        transaction = {
            "revision": snapshot["revision"],
            "cookie_digest": hashlib.sha256(cookie.encode()).hexdigest(),
            "source": source,
            "expires_at": self.clock() + 300,
        }
        if not self._redis(lambda: self.redis.set(key, self._seal(transaction, key), ex=300, nx=True)):
            raise DingTalkAuthError("storage_unavailable", 503)
        uri = "https://login.dingtalk.com/oauth2/auth?" + urlencode(
            {
                "client_id": snapshot["app_key"],
                "redirect_uri": self.callback_url(),
                "response_type": "code",
                "scope": "openid",
                "prompt": "consent",
                "state": state,
            }
        )
        return uri, state, cookie

    def consume(self, state: str, cookie: str | None) -> dict:
        self._opaque(state)
        if not isinstance(cookie, str):
            raise DingTalkAuthError("invalid_transaction")
        self._opaque(cookie)
        key = "dify:dingtalk:oauth:" + state
        raw = self._redis(lambda: self.redis.get(key))
        if not raw:
            raise DingTalkAuthError("invalid_transaction")
        transaction = self._open(raw, key)
        if not secrets.compare_digest(transaction["cookie_digest"], hashlib.sha256(cookie.encode()).hexdigest()):
            raise DingTalkAuthError("invalid_transaction")
        if self.clock() >= transaction["expires_at"]:
            raise DingTalkAuthError("invalid_transaction")
        if self._redis(lambda: self.redis.eval(_CONSUME, 1, key, raw)) != 1:
            raise DingTalkAuthError("invalid_transaction")
        return transaction

    def _identity(self, snapshot, code: str) -> DingTalkIdentity:
        tokens = self._request(
            "POST",
            "https://api.dingtalk.com/v1.0/oauth2/userAccessToken",
            json={
                "clientId": snapshot["app_key"],
                "clientSecret": snapshot["secret"],
                "code": code,
                "grantType": "authorization_code",
            },
        )
        token = tokens.get("accessToken")
        if not isinstance(token, str) or not token:
            raise DingTalkAuthError("invalid_provider_response", 502)
        me = self._request(
            "GET", "https://api.dingtalk.com/v1.0/contact/users/me", headers={"x-acs-dingtalk-access-token": token}
        )
        union = me.get("unionId")
        if not isinstance(union, str) or not union or len(union) > 255:
            raise DingTalkAuthError("invalid_provider_identity", 502)
        app_token = self._app_token(snapshot)
        mapping = self._request(
            "POST",
            "https://oapi.dingtalk.com/topapi/user/getbyunionid",
            params={"access_token": app_token},
            json={"unionid": union},
        )
        userid = mapping.get("result", {}).get("userid")
        if not isinstance(userid, str) or not userid:
            raise DingTalkAuthError("enterprise_member_required")
        detail = self._request(
            "POST",
            "https://oapi.dingtalk.com/topapi/v2/user/get",
            params={"access_token": app_token},
            json={"userid": userid},
        ).get("result", {})
        if detail.get("unionid") != union or detail.get("userid") != userid or detail.get("active") is not True:
            raise DingTalkAuthError("enterprise_member_required")
        email_config = snapshot["config"].get("email_api", {})
        if email_config.get("enabled", False):
            email_result = lookup_email(userid, email_config)
            if email_result["result"] != "success":
                raise DingTalkAuthError("enterprise_email_lookup_failed")
            email = email_result.get("email", "")
        else:
            email = detail.get("org_email") or detail.get("email") or ""
        if not isinstance(email, str) or len(email) > 255 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            email = ""
        subject = hashlib.sha256(
            json.dumps([snapshot["app_key"], snapshot["corp_id"], union], separators=(",", ":")).encode()
        ).hexdigest()
        name = detail.get("name")
        return DingTalkIdentity(subject, email, name[:255] if isinstance(name, str) and name else "DingTalk")

    def complete(self, transaction: dict, code: str, *, account=None, refresh_token=None, ip_address=None):
        if not isinstance(code, str) or not 0 < len(code) < 500:
            raise DingTalkAuthError("invalid_authorization_code")
        source = transaction["source"]
        if source:
            self._diagnostic_source(source, account, refresh_token)
        snapshot = self._snapshot(account if source else None, enabled=not source)
        if snapshot["revision"] != transaction["revision"]:
            raise DingTalkAuthError("configuration_changed", 409)
        identity = self._identity(snapshot, code)
        if source:
            self._diagnostic_source(source, account, refresh_token)
            try:
                with self.session_factory() as session, session.begin():
                    self._guard(account, session)
                    record = self._record(session, lock=True)
                    config = self._require_revision(record, transaction["revision"])
                    if source["bind"]:
                        self._bind(session, identity.subject, account.id)
                    config[_META] = {
                        **self._meta(config),
                        "verified_revision": transaction["revision"],
                        "connection_revision": transaction["revision"],
                    }
                    self._save_config(record, config)
            except IntegrityError:
                raise DingTalkAuthError("identity_conflict", 409) from None
            return None
        return self._login(snapshot, identity, ip_address=ip_address)

    def _diagnostic_source(self, source, account, refresh_token):
        if not isinstance(account, Account) or account.id != source["account_id"] or not refresh_token:
            raise DingTalkAuthError("administrator_session_required", 403)
        if not secrets.compare_digest(hashlib.sha256(refresh_token.encode()).hexdigest(), source["refresh_digest"]):
            raise DingTalkAuthError("administrator_session_required", 403)
        if self._redis(lambda: AccountService.resolve_refresh_token(refresh_token)) != account.id:
            raise DingTalkAuthError("administrator_session_required", 403)
        with self.session_factory() as session, session.begin():
            if self._guard(account, session) != source["tenant_id"]:
                raise DingTalkAuthError("system_management_forbidden", 403)

    @staticmethod
    def _bind(session, subject, account_id):
        existing = session.scalar(
            select(AccountIntegrate)
            .where(AccountIntegrate.provider == "dingtalk", AccountIntegrate.open_id == subject)
            .with_for_update()
        )
        if existing is not None:
            if existing.account_id != account_id:
                raise DingTalkAuthError("identity_conflict", 409)
            return
        prior = session.scalar(
            select(AccountIntegrate.id).where(
                AccountIntegrate.provider == "dingtalk", AccountIntegrate.account_id == account_id
            )
        )
        if prior:
            raise DingTalkAuthError("identity_conflict", 409)
        session.add(AccountIntegrate(account_id=account_id, provider="dingtalk", open_id=subject, encrypted_token=""))
        session.flush()

    def _login(self, snapshot, identity, *, ip_address):
        # Read before shared preparation; no provider/seat I/O inside write UoW.
        with self.session_factory() as session:
            binding = session.scalar(
                select(AccountIntegrate.account_id).where(
                    AccountIntegrate.provider == "dingtalk", AccountIntegrate.open_id == identity.subject
                )
            )
        prepared = None
        if binding is None:
            if not identity.email:
                raise DingTalkAuthError("enterprise_email_required")
            prepared = AccountService.prepare_account_creation(
                identity.email, identity.name, self.settings.DEFAULT_LANGUAGE, password=None, ip_address=ip_address
            )
        try:
            with self.session_factory() as session, session.begin():
                record = self._record(session, lock=True)
                self._require_revision(record, snapshot["revision"], enabled=True)
                anchor = session.get(SystemManagementScopeExtend, "initialization")
                tenant = session.get(Tenant, anchor.tenant_id) if anchor else None
                if tenant is None or tenant.status != TenantStatus.NORMAL:
                    raise DingTalkAuthError("initialization_workspace_unavailable")
                bound = session.scalar(
                    select(AccountIntegrate)
                    .where(AccountIntegrate.provider == "dingtalk", AccountIntegrate.open_id == identity.subject)
                    .with_for_update()
                )
                new = bound is None
                if new:
                    if prepared is None:
                        raise DingTalkAuthError("identity_conflict", 409)
                    if AccountService.has_account_with_normalized_email(identity.email, session=session):
                        raise DingTalkAuthError("binding_required", 409)
                    account = AccountService.persist_account_creation(prepared, session=session)
                    account.status = AccountStatus.ACTIVE
                    account.initialized_at = naive_utc_now()
                    self._bind(session, identity.subject, account.id)
                    TenantService.persist_tenant_member(tenant, account, session, role="normal")
                else:
                    account = session.get(Account, bound.account_id, with_for_update=True)
                    if account is None or account.status != AccountStatus.ACTIVE or account.initialized_at is None:
                        raise DingTalkAuthError("account_unavailable", 403)
                membership = session.scalar(
                    select(TenantAccountJoin)
                    .where(TenantAccountJoin.account_id == account.id, TenantAccountJoin.tenant_id == tenant.id)
                    .with_for_update()
                )
                if membership is None:
                    raise DingTalkAuthError("workspace_membership_required", 403)
                # No restoration and no role mutation for existing members.
                for join in session.scalars(
                    select(TenantAccountJoin).where(TenantAccountJoin.account_id == account.id).with_for_update()
                ):
                    join.current = join.id == membership.id
                account.last_login_at = naive_utc_now()
                account.last_login_ip = ip_address
                account_id = account.id
        except IntegrityError:
            raise DingTalkAuthError("identity_conflict", 409) from None
        # The config/account/member locks serialize credential issuance with
        # disable and withdrawal. This bounded Redis operation inside the short
        # SQL transaction is necessary for that admission contract; provider I/O
        # remains outside SQL. Existing sessions keep ordinary Dify semantics.
        with self.session_factory() as session, session.begin():
            self._require_revision(self._record(session, lock=True), snapshot["revision"], enabled=True)
            account = session.get(Account, account_id, with_for_update=True)
            if account is None or account.status != AccountStatus.ACTIVE:
                raise DingTalkAuthError("account_unavailable", 403)
            tenant = session.get(Tenant, tenant.id, with_for_update=True)
            membership = (
                session.scalar(
                    select(TenantAccountJoin)
                    .where(TenantAccountJoin.account_id == account_id, TenantAccountJoin.tenant_id == tenant.id)
                    .with_for_update()
                )
                if tenant is not None
                else None
            )
            if tenant is None or tenant.status != TenantStatus.NORMAL or membership is None:
                raise DingTalkAuthError("workspace_membership_required", 403)
            return self._redis(lambda: AccountService.issue_token_pair(account_id))
