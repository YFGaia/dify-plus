import base64

from Crypto.Cipher import Blowfish
from Crypto.Util.Padding import unpad

from configs import dify_config

from .engine import db
from .types import StringUUID


class SystemIntegrationClassify:
    SYSTEM_INTEGRATION_DINGTALK = 1  # 钉钉
    SYSTEM_INTEGRATION_WEIXIN = 2  # 微信
    SYSTEM_INTEGRATION_FEI_SU = 3  # 飞书
    SYSTEM_INTEGRATION_OAUTH_TWO = 4  # OAuth2


class CodeExecutionControlExtend(db.Model):
    """code 节点 sandbox-full 授权邮箱名单（p5-admin-decommission）。

    DB 为名单的 source of truth；redis 键 ``control_mail`` 仅是投影缓存，
    每次写操作后由 CodeExecutionControlService.rebuild_control_mail_cache 全量重建。
    读侧热路径（core.workflow.nodes.code.control_extend.ExecutionControl）只读 redis，不查本表。
    """

    __tablename__ = "code_execution_control_extend"
    __table_args__ = (
        db.PrimaryKeyConstraint("id", name="code_execution_control_extend_pkey"),
        db.UniqueConstraint("email", name="code_execution_control_extend_email_key"),
    )

    id = db.Column(StringUUID, server_default=db.text("uuid_generate_v4()"))
    email = db.Column(db.String(255), nullable=False)
    created_by = db.Column(StringUUID, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, server_default=db.text("CURRENT_TIMESTAMP(0)"))

    def __repr__(self) -> str:
        return f"<CodeExecutionControlExtend id={self.id} email={self.email}>"


class SystemIntegrationExtend(db.Model):
    __tablename__ = "system_integration_extend"
    __table_args__ = (
        db.PrimaryKeyConstraint("id", name="system_integration_joins_pkey"),
        db.Index("system_integration_joins_classify_idx", "classify"),
    )
    id = db.Column(db.BigInteger, db.Sequence("system_integration_extend_id_seq"), primary_key=True, autoincrement=True)
    classify = db.Column(db.Integer, nullable=False, server_default=db.text("1"))
    status = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))
    corp_id = db.Column(db.String(120), nullable=True)
    agent_id = db.Column(db.String(120), nullable=True)
    app_id = db.Column(db.String(120), nullable=True)
    app_key = db.Column(db.String(120), nullable=True)
    app_secret = db.Column(db.Text, nullable=True)
    config = db.Column(db.Text, nullable=True)

    def decodeSecret(self):  # noqa: N802 - 二开公共 API，多处外部调用依赖该方法名，不改名
        if len(self.app_secret) == 0:
            return ""
        # Decode the base64 encoded text
        ciphertext = base64.b64decode(self.app_secret)

        # Ensure the text length is sufficient
        if len(ciphertext) < Blowfish.block_size:
            raise ValueError("Invalid ciphertext")

        # Extract the initialization vector (IV) from the beginning of the ciphertext
        iv = ciphertext[: Blowfish.block_size]
        ciphertext = ciphertext[Blowfish.block_size :]

        # Create the cipher object and decrypt the plaintext
        cipher = Blowfish.new(dify_config.SECRET_KEY.encode("utf-8"), Blowfish.MODE_CBC, iv)
        plaintext = cipher.decrypt(ciphertext)

        # Unpad the plaintext using PKCS7 unpadding
        try:
            plaintext = unpad(plaintext, Blowfish.block_size)
        except ValueError as e:
            raise ValueError("Invalid padding") from e

        return plaintext.decode("utf-8")
