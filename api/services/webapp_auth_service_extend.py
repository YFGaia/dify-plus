"""extend: WebApp 访问认证开关（per-app）。

背景：fork 的 WebApp 认证 = 复用 Console 登录态（`controllers/web/completion.py::is_end_login`
读取 Console access_token cookie，未登录在 completion / chat / workflow run 三个生成端点抛 401；
前端 `web/app/(shareLayout)/components/authenticated-layout.tsx` 也会主动跳 /signin）。

本模块为该行为提供 per-app 开关，存储于 `AppExtend.webapp_auth_enabled`：
- NULL / True：访问需登录 Console（默认，保持 fork 既有行为）；
- False：允许匿名访问（回到上游 Dify 的公开 WebApp 语义）。

读路径使用 redis 投影缓存；授权决策始终重查 DB，避免失效失败造成旧配置继续生效。
写路径由 Console 站点设置接口
（`controllers/console/app/site.py`）调用，使用调用方注入的 session，不自行 commit。

注意：与上游企业版 `system_features.webapp_auth`（WEB_SSO / access_mode）语义无关，勿混用。
"""

import logging
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from extensions.ext_database import db
from extensions.ext_redis import redis_client
from models.model_extend import AppExtend

logger = logging.getLogger(__name__)

_CACHE_KEY_TEMPLATE = "webapp_auth_enabled_extend:{app_id}"


class WebAppAuthExtendService:
    """WebApp 访问认证开关的读写入口（读带 redis 缓存，写后失效缓存）。"""

    @staticmethod
    def _cache_key(app_id: str) -> str:
        return _CACHE_KEY_TEMPLATE.format(app_id=app_id)

    @classmethod
    def is_webapp_auth_enabled(cls, app_id: str) -> bool:
        """返回该 app 的 WebApp 访问是否需要 Console 登录（无记录/列为 NULL 时默认 True）。

        redis 不可用时降级为直接查库；查库失败按安全默认值 True（需认证）处理。
        """
        cache_key = cls._cache_key(app_id)
        try:
            cached = redis_client.get(cache_key)
        except Exception:
            logger.exception("read webapp_auth_enabled cache failed, fallback to db. app_id=%s", app_id)
            cached = None

        # Redis is a projection, not an authorization source: either direction of
        # a committed switch change must be visible even when invalidation fails.
        try:
            app_extend = db.session.scalar(
                select(AppExtend).where(AppExtend.app_id == app_id).execution_options(populate_existing=True)
            )
        except Exception:
            logger.exception("query webapp_auth_enabled failed, default to enabled. app_id=%s", app_id)
            return True

        if app_extend is None or app_extend.webapp_auth_enabled is None:
            enabled = True
        else:
            enabled = bool(app_extend.webapp_auth_enabled)
        try:
            value = "1" if enabled else "0"
            if cached not in (value, value.encode()):
                redis_client.set(cache_key, value, ex=60)
        except Exception:
            logger.exception("write webapp_auth_enabled cache failed. app_id=%s", app_id)
        return bool(enabled)

    @classmethod
    def set_webapp_auth_enabled(cls, app_id: str, enabled: bool, *, session: Session) -> None:
        """只写入开关；调用方提交成功后必须调用 invalidate_after_commit。

        使用调用方注入的 session，由 AppSiteCommandRepository 的事务统一提交。
        """
        app_extend = session.scalar(select(AppExtend).where(AppExtend.app_id == app_id))
        if app_extend is None:
            session.add(AppExtend(id=str(uuid4()), app_id=app_id, webapp_auth_enabled=enabled))
        else:
            app_extend.webapp_auth_enabled = enabled

    @classmethod
    def invalidate_after_commit(cls, app_id: str) -> None:
        """Invalidate only after commit; stale public cache entries never authorize a request."""
        try:
            redis_client.delete(cls._cache_key(app_id))
        except Exception:
            logger.exception("invalidate webapp_auth_enabled cache failed. app_id=%s", app_id)
