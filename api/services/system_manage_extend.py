"""
Extend: 系统集成管理 Service 层
迁移自 Admin Center (Go+Vue) 至 Dify Console 原生技术栈
"""

import base64
import json
import logging
import re
import secrets
import urllib.parse
from datetime import UTC, datetime

import requests
from Crypto.Cipher import Blowfish
from Crypto.Util.Padding import pad, unpad

from core.helper import ssrf_proxy
from services.dingtalk_email_lookup_extend import validate_email_lookup
from configs import dify_config
from extensions.ext_database import db
from extensions.ext_redis import redis_client
from models.system_extend import CodeExecutionControlExtend, SystemIntegrationClassify, SystemIntegrationExtend

logger = logging.getLogger(__name__)


def _mask_string(s: str) -> str:
    """将字符串部分替换为星号，保留前后各 2 位"""
    if not s or len(s) <= 4:
        return "****"
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def _encrypt_blowfish(plaintext: str, key: str) -> str:
    """使用 Blowfish CBC 加密并 base64 编码（与 Go 侧 EncryptBlowfish 兼容）"""
    key_bytes = key.encode("utf-8")
    cipher = Blowfish.new(key_bytes, Blowfish.MODE_CBC)
    iv = cipher.iv
    padded = pad(plaintext.encode("utf-8"), Blowfish.block_size)
    encrypted = cipher.encrypt(padded)
    return base64.b64encode(iv + encrypted).decode("utf-8")


def _decrypt_blowfish(encoded: str, key: str) -> str:
    """使用 Blowfish CBC 解密（与 Go 侧 DecryptBlowfish 兼容）"""
    if not encoded:
        return ""
    ciphertext = base64.b64decode(encoded)
    if len(ciphertext) < Blowfish.block_size:
        raise ValueError("Invalid ciphertext")
    iv = ciphertext[: Blowfish.block_size]
    ciphertext = ciphertext[Blowfish.block_size :]
    cipher = Blowfish.new(key.encode("utf-8"), Blowfish.MODE_CBC, iv)
    plaintext = cipher.decrypt(ciphertext)
    plaintext = unpad(plaintext, Blowfish.block_size)
    return plaintext.decode("utf-8")


class SystemIntegrationManageService:
    """系统集成配置管理服务"""

    @staticmethod
    def _get_or_create_record(classify: int) -> SystemIntegrationExtend:
        """获取指定 classify 的配置记录，不存在则创建"""
        record = db.session.query(SystemIntegrationExtend).filter(SystemIntegrationExtend.classify == classify).first()
        if not record:
            record = SystemIntegrationExtend(classify=classify, status=False)
            db.session.add(record)
            db.session.commit()
        return record

    @staticmethod
    def get_config(classify: int) -> dict:
        """获取指定分类的集成配置，敏感字段做脱敏处理"""
        record = SystemIntegrationManageService._get_or_create_record(classify)

        result: dict = {
            "status": record.status or False,
            "corp_id": _mask_string(record.corp_id or ""),
            "agent_id": record.agent_id or "",
            "app_key": record.app_key or "",
            "app_id": record.app_id or "",
            "app_secret": "",
            "config": {},
        }

        # 解密 app_secret 并脱敏
        if record.app_secret:
            try:
                secret = _decrypt_blowfish(record.app_secret, dify_config.SECRET_KEY)
                result["app_secret"] = _mask_string(secret)
            except Exception:
                result["app_secret"] = "****"

        # 解析 config JSON
        if record.config:
            try:
                result["config"] = json.loads(record.config)
            except (json.JSONDecodeError, TypeError):
                result["config"] = {}

        return result

    @staticmethod
    def set_config(classify: int, data: dict) -> None:
        """保存指定分类的集成配置"""
        config = data.get("config")
        if classify == SystemIntegrationClassify.SYSTEM_INTEGRATION_DINGTALK and isinstance(config, dict):
            email_api = config.get("email_api")
            if email_api is not None:
                if not isinstance(email_api, dict):
                    raise ValueError("Email lookup configuration must be a JSON object.")
                validate_email_lookup(email_api)
        record = SystemIntegrationManageService._get_or_create_record(classify)

        # 处理 status
        if "status" in data:
            record.status = bool(data["status"])

        # 处理 corp_id — 若包含星号则说明是脱敏后的值，不更新
        if "corp_id" in data and "*" not in data["corp_id"]:
            record.corp_id = data["corp_id"]

        # 处理 agent_id
        if "agent_id" in data:
            record.agent_id = data["agent_id"]

        # 处理 app_key
        if "app_key" in data:
            record.app_key = data["app_key"]

        # 处理 app_id
        if "app_id" in data:
            record.app_id = data["app_id"]

        # 处理 app_secret — 若包含星号则说明是脱敏后的值，不更新
        if "app_secret" in data and "*" not in data["app_secret"]:
            record.app_secret = _encrypt_blowfish(data["app_secret"], dify_config.SECRET_KEY)

        # 处理 config JSON
        if "config" in data:
            record.config = json.dumps(data["config"], ensure_ascii=False)

        db.session.commit()

    @staticmethod
    def test_dingtalk_connection() -> dict:
        """测试钉钉 AppKey/AppSecret 是否有效"""
        record = SystemIntegrationManageService._get_or_create_record(
            SystemIntegrationClassify.SYSTEM_INTEGRATION_DINGTALK
        )

        app_key = record.app_key
        if not app_key:
            raise ValueError("AppKey 未配置")

        app_secret = ""
        if record.app_secret:
            try:
                app_secret = _decrypt_blowfish(record.app_secret, dify_config.SECRET_KEY)
            except Exception:
                raise ValueError("AppSecret 解密失败")

        if not app_secret:
            raise ValueError("AppSecret 未配置")

        # 调用钉钉 gettoken 验证
        params = urllib.parse.urlencode({"appkey": app_key, "appsecret": app_secret})
        resp = requests.get(f"https://oapi.dingtalk.com/gettoken?{params}", timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("errcode", -1) != 0:
            raise ValueError(f"钉钉连接失败: errcode={data.get('errcode')}, errmsg={data.get('errmsg')}")

        return {"result": "success", "message": "钉钉连接测试成功"}

    @staticmethod
    def dingtalk_test_callback(code: str) -> dict:
        """处理钉钉测试回调，用授权码获取用户信息"""
        record = SystemIntegrationManageService._get_or_create_record(
            SystemIntegrationClassify.SYSTEM_INTEGRATION_DINGTALK
        )

        app_key = record.app_key
        if not app_key or not record.app_secret:
            raise ValueError("钉钉配置不完整")

        app_secret = _decrypt_blowfish(record.app_secret, dify_config.SECRET_KEY)

        # 获取 access_token
        params = urllib.parse.urlencode({"appkey": app_key, "appsecret": app_secret})
        token_resp = requests.get(f"https://oapi.dingtalk.com/gettoken?{params}", timeout=10)
        token_data = token_resp.json()
        if token_data.get("errcode", -1) != 0:
            raise ValueError(f"获取 access_token 失败: {token_data.get('errmsg')}")

        access_token = token_data["access_token"]

        # 使用授权码获取用户信息
        user_resp = requests.post(
            "https://oapi.dingtalk.com/topapi/v2/user/getuserinfo",
            params={"access_token": access_token},
            json={"code": code},
            timeout=10,
        )
        user_data = user_resp.json()
        if user_data.get("errcode", -1) != 0:
            raise ValueError(f"获取用户信息失败: {user_data.get('errmsg')}")

        return {
            "result": "success",
            "user_info": user_data.get("result", {}),
        }

    @staticmethod
    def test_oauth2_connection(data: dict) -> dict:
        """测试 OAuth2 连接"""
        config = data.get("config", {})
        server_url = config.get("server_url", "")
        token_url = config.get("token_url", "")

        if not server_url or not token_url:
            raise ValueError("请填写完整的 OAuth2 配置信息（server_url 和 token_url）")

        # 简单测试 token endpoint 是否可达
        full_url = f"{server_url.rstrip('/')}{token_url}"
        try:
            resp = requests.options(full_url, timeout=10)
            # 只要服务器有响应即认为连接成功（可能返回 405 但说明服务可达）
            return {"result": "success", "message": f"OAuth2 服务可达 (HTTP {resp.status_code})"}
        except requests.RequestException as e:
            raise ValueError(f"OAuth2 服务连接失败: {e}")

    @staticmethod
    def test_email_api(api_url: str, api_key: str) -> dict:
        """测试邮箱 API 连通性"""
        if not api_url:
            raise ValueError("API 地址不能为空")

        try:
            headers = {}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            resp = ssrf_proxy.get(api_url, headers=headers, timeout=10, max_retries=0)
            return {
                "result": "success" if resp.status_code < 400 else "failed",
                "status_code": resp.status_code,
                "message": f"API 响应状态码: {resp.status_code}",
            }
        except Exception:
            raise ValueError("Email API connection failed.") from None

    @staticmethod
    def get_forward_tokens() -> list:
        """获取转发 Token 列表"""
        record = SystemIntegrationManageService._get_or_create_record(
            SystemIntegrationClassify.SYSTEM_INTEGRATION_DINGTALK
        )
        if not record.config:
            return []

        try:
            config = json.loads(record.config)
            forward_config = config.get("forward_config", {})
            return forward_config.get("tokens", [])
        except (json.JSONDecodeError, TypeError):
            return []

    @staticmethod
    def create_forward_token(name: str) -> dict:
        """创建转发 Token"""
        if not name or not name.strip():
            raise ValueError("Token 名称不能为空")

        record = SystemIntegrationManageService._get_or_create_record(
            SystemIntegrationClassify.SYSTEM_INTEGRATION_DINGTALK
        )

        config: dict = {}
        if record.config:
            try:
                config = json.loads(record.config)
            except (json.JSONDecodeError, TypeError):
                config = {}

        forward_config = config.setdefault("forward_config", {})
        tokens: list = forward_config.setdefault("tokens", [])

        # 计算下一个 seq
        max_seq = max((t.get("seq", 0) for t in tokens), default=0)
        new_token = {
            "seq": max_seq + 1,
            "name": name.strip(),
            "token": secrets.token_hex(32),
            "created_at": datetime.now(UTC).isoformat(),
        }
        tokens.append(new_token)

        record.config = json.dumps(config, ensure_ascii=False)
        db.session.commit()

        return new_token

    @staticmethod
    def delete_forward_token(seq: int) -> None:
        """删除指定 seq 的转发 Token"""
        record = SystemIntegrationManageService._get_or_create_record(
            SystemIntegrationClassify.SYSTEM_INTEGRATION_DINGTALK
        )

        if not record.config:
            raise ValueError("未找到配置")

        try:
            config = json.loads(record.config)
        except (json.JSONDecodeError, TypeError):
            raise ValueError("配置解析失败")

        forward_config = config.get("forward_config", {})
        tokens: list = forward_config.get("tokens", [])

        original_len = len(tokens)
        tokens = [t for t in tokens if t.get("seq") != seq]

        if len(tokens) == original_len:
            raise ValueError(f"未找到 seq={seq} 的 Token")

        forward_config["tokens"] = tokens
        config["forward_config"] = forward_config
        record.config = json.dumps(config, ensure_ascii=False)
        db.session.commit()


# ==================== 代码执行控制（sandbox-full 授权名单） ====================

# 简单邮箱格式校验：非空本地部分 @ 非空域名（含点）。名单为人工维护的小名单，无需 RFC 级校验。
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# redis 投影缓存键：JSON 邮箱数组，persistent 无 TTL。
# 读侧为 core.workflow.nodes.code.control_extend.ExecutionControl.check_code。
CONTROL_MAIL_CACHE_KEY = "control_mail"


class CodeExecutionControlService:
    """code 节点 sandbox-full 授权名单管理（p5-admin-decommission，替代 GVA SyncExecuteCode 写入链路）。

    数据库表 code_execution_control_extend 是 source of truth；每次写操作在 DB commit 后
    全量重建 redis 键 control_mail。redis 写失败不回滚 DB，只返回 cache_synced=False
    （controller 据此在响应中提示重试/重建）。
    """

    @staticmethod
    def list_emails() -> list[CodeExecutionControlExtend]:
        """返回全部授权记录，按 created_at 升序（同刻按 id 保证稳定排序）。"""
        return (
            db.session.query(CodeExecutionControlExtend)
            .order_by(CodeExecutionControlExtend.created_at.asc(), CodeExecutionControlExtend.id.asc())
            .all()
        )

    @staticmethod
    def add_email(email: str, created_by: str | None) -> tuple[CodeExecutionControlExtend, bool]:
        """添加授权邮箱。

        邮箱先 strip+lower 规范化再校验/查重。格式非法或已存在时抛 ValueError（controller 转 400）。
        DB commit 后重建 redis 缓存，返回 (记录, cache_synced)。
        """
        normalized = (email or "").strip().lower()
        if not _EMAIL_PATTERN.match(normalized):
            raise ValueError(f"Invalid email format: {email!r}")

        existing = (
            db.session.query(CodeExecutionControlExtend).filter(CodeExecutionControlExtend.email == normalized).first()
        )
        if existing:
            raise ValueError(f"Email already exists: {normalized}")

        record = CodeExecutionControlExtend(email=normalized, created_by=created_by)
        db.session.add(record)
        db.session.commit()
        logger.info("Code execution control email added: %s by account %s", normalized, created_by)

        cache_synced = CodeExecutionControlService.rebuild_control_mail_cache()
        return record, cache_synced

    @staticmethod
    def remove_email(record_id: str) -> bool:
        """删除授权记录。记录不存在时抛 ValueError（controller 转 404）。

        DB commit 后重建 redis 缓存，返回 cache_synced。
        """
        record = db.session.query(CodeExecutionControlExtend).filter(CodeExecutionControlExtend.id == record_id).first()
        if not record:
            raise ValueError(f"Record not found: {record_id}")

        email = record.email
        db.session.delete(record)
        db.session.commit()
        logger.info("Code execution control email removed: %s (record %s)", email, record_id)

        return CodeExecutionControlService.rebuild_control_mail_cache()

    @staticmethod
    def rebuild_control_mail_cache() -> bool:
        """幂等重建 redis 投影缓存：DB 全量名单 → SET control_mail <json array>（无 TTL）。

        供写操作、存量数据迁移命令与故障恢复复用。redis 异常时记 error 日志并返回 False，
        不抛出（DB 已 commit 的写入不回滚，名单以 DB 为准，可重跑本方法收敛）。
        """
        emails = [
            row.email
            for row in db.session.query(CodeExecutionControlExtend.email)
            .order_by(CodeExecutionControlExtend.created_at.asc(), CodeExecutionControlExtend.id.asc())
            .all()
        ]
        try:
            redis_client.set(CONTROL_MAIL_CACHE_KEY, json.dumps(emails))
        except Exception:
            # 广捕获以保证「绝不因缓存同步中断管理写路径」的契约；redis 客户端异常类型
            # 因部署形态（哨兵/集群/单机）而异，精确列举易漏。
            logger.exception("Failed to rebuild %s cache in redis; DB is source of truth", CONTROL_MAIL_CACHE_KEY)
            return False
        return True


# ==================== 用户额度管理 ====================


class QuotaManageService:
    """用户额度管理 Service — 迁移自 Admin QuotaService"""

    @staticmethod
    def get_quota_list(page: int, page_size: int, keyword: str = "") -> dict:
        """
        分页查询用户额度列表，按已使用配额从高到低、同值按账户 ID 稳定排序。
        keyword 非空时按 accounts.name 或 accounts.email 模糊搜索。
        """
        from sqlalchemy import or_

        from models.account import Account
        from models.account_money_extend import AccountMoneyExtend

        page = max(1, page)
        page_size = max(1, min(100, page_size))
        offset = (page - 1) * page_size

        query = db.session.query(AccountMoneyExtend).order_by(
            AccountMoneyExtend.used_quota.desc(), AccountMoneyExtend.account_id.asc()
        )

        # keyword 过滤：先从 accounts 查匹配 account_id，再筛选
        if keyword and keyword.strip():
            kw = f"%{keyword.strip()}%"
            matched_ids = (
                db.session.query(Account.id).filter(or_(Account.name.ilike(kw), Account.email.ilike(kw))).all()
            )
            id_list = [str(row.id) for row in matched_ids]
            if id_list:
                query = query.filter(AccountMoneyExtend.account_id.in_(id_list))
            else:
                # 无匹配结果
                return {"list": [], "total": 0, "page": page, "page_size": page_size}

        total = query.count()
        rows = query.offset(offset).limit(page_size).all()

        # 批量查询账户信息，避免 N+1
        account_ids = [r.account_id for r in rows]
        accounts: dict = {}
        if account_ids:
            accs = db.session.query(Account).filter(Account.id.in_(account_ids)).all()
            accounts = {str(a.id): a for a in accs}

        result = []
        for i, row in enumerate(rows):
            acc = accounts.get(str(row.account_id))
            if acc is None:
                logger.warning("account_money_extend 有孤儿记录 account_id=%s", row.account_id)
                continue
            used = float(row.used_quota or 0)
            total_q = float(row.total_quota or 0)
            result.append(
                {
                    "account_id": str(row.account_id),
                    "ranking": offset + i + 1,
                    "name": acc.name,
                    "email": acc.email,
                    "avatar": acc.avatar,
                    "used_quota": used,
                    "total_quota": total_q,
                    "balance": total_q - used,
                }
            )

        return {"list": result, "total": total, "page": page, "page_size": page_size}

    @staticmethod
    def set_user_quota(account_id: str, quota: float) -> None:
        """
        设置指定用户的总额度（UPSERT）。
        若 account_money_extend 无记录则自动创建。
        两种部署数据库使用各自的原子 UPSERT；已有消费额保持不变。
        """
        from sqlalchemy.dialects.mysql import insert as mysql_insert
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from models.account_money_extend import AccountMoneyExtend

        if quota < 0:
            raise ValueError("quota 不能为负数")

        dialect = db.session.get_bind().dialect.name
        values = {"account_id": account_id, "total_quota": quota, "used_quota": 0}
        if dialect in {"mysql", "mariadb"}:
            stmt = mysql_insert(AccountMoneyExtend).values(**values).on_duplicate_key_update(total_quota=quota)
        elif dialect == "postgresql":
            stmt = (
                pg_insert(AccountMoneyExtend)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["account_id"],
                    set_={"total_quota": quota},
                )
            )
        else:
            raise ValueError(f"Unsupported quota database dialect: {dialect}")
        db.session.execute(stmt)
        db.session.commit()
