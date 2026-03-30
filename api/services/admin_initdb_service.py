"""
Trigger gin-vue-admin InitDB after Dify console setup.

Uses the same DB_* settings as the API container and the admin password from the setup form.
Does not read or write local config files; outbound call only if ADMIN_INITDB_ENABLED is true.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import httpx

from configs import dify_config

logger = logging.getLogger(__name__)

_ADMIN_ALREADY_INIT_MSG = "已存在数据库配置"
_DEFAULT_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


def _admin_db_type(db_type: str) -> Literal["pgsql", "mysql"]:
    if db_type == "postgresql":
        return "pgsql"
    return "mysql"


def _build_payload(admin_password: str) -> dict[str, Any]:
    db_type = _admin_db_type(dify_config.DB_TYPE)
    return {
        "adminPassword": admin_password,
        "dbType": db_type,
        "host": dify_config.DB_HOST,
        "port": str(dify_config.DB_PORT),
        "userName": dify_config.DB_USERNAME,
        "password": dify_config.DB_PASSWORD,
        "dbName": dify_config.DB_DATABASE,
        "dbPath": "",
    }


def _is_acceptable_admin_response(body: dict[str, Any]) -> bool:
    code = body.get("code")
    msg = body.get("msg") or ""
    if code == 0:
        return True
    if msg == _ADMIN_ALREADY_INIT_MSG:
        return True
    return False


def trigger_admin_initdb_if_configured(*, admin_password: str) -> None:
    """
    Best-effort POST to Admin InitDB. Logs warnings on failure; never raises.
    """
    if not dify_config.ADMIN_INITDB_ENABLED:
        return
    if dify_config.DB_TYPE not in ("postgresql", "mysql", "oceanbase", "seekdb"):
        logger.warning(
            "skip admin initdb: unsupported DB_TYPE for admin bridge: %s",
            dify_config.DB_TYPE,
        )
        return

    url = (dify_config.ADMIN_INITDB_URL or "").strip()
    if not url:
        logger.warning("ADMIN_INITDB_ENABLED is true but ADMIN_INITDB_URL is empty, skip admin initdb")
        return

    payload = _build_payload(admin_password)
    try:
        with httpx.Client(timeout=_DEFAULT_TIMEOUT, follow_redirects=True) as client:
            response = client.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
    except httpx.RequestError as exc:
        logger.warning("admin initdb request failed: %s", exc)
        return

    if response.status_code != 200:
        logger.warning(
            "admin initdb HTTP %s: %s",
            response.status_code,
            (response.text or "")[:500],
        )
        return

    try:
        body = response.json()
    except ValueError:
        logger.warning("admin initdb returned non-JSON body: %s", (response.text or "")[:500])
        return

    if not isinstance(body, dict):
        logger.warning("admin initdb returned unexpected JSON: %s", body)
        return

    if _is_acceptable_admin_response(body):
        logger.info("admin initdb finished: %s", body.get("msg"))
        return

    logger.warning("admin initdb rejected: %s", body)
