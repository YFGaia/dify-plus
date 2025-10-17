import json
import logging
from enum import Enum

from extensions.ext_database import db

from .types import StringUUID


class ForwardingExtend(db.Model):
    __tablename__ = "forwarding_extend"
    __table_args__ = (
        db.PrimaryKeyConstraint("id", name="forwarding_extend_pkey"),
        db.Index("idx_forwarding_path", "path"),
    )

    id = db.Column(StringUUID, server_default=db.text("uuid_generate_v4()"))
    path = db.Column(db.String(255), nullable=False)
    address = db.Column(db.String(255), nullable=False)
    header = db.Column(db.Text, nullable=False, server_default=db.text("'[]'"))
    description = db.Column(db.Text, nullable=False, server_default=db.text("''::character varying"))


class RequestContentType(Enum):
    TypeNone = 0
    """Content-Type: none"""

    FormData = 1
    """Content-Type: form-data"""

    UrlEncoded = 2
    """Content-Type: x-www-form-urlencoded"""

    RawText = 3
    """Content-Type: text/plain"""

    ApplicationJavaScript = 4
    """Content-Type: application/javascript"""

    ApplicationJson = 5
    """Content-Type: application/json"""

    TextHtml = 6
    """Content-Type: text/html"""

    ApplicationXml = 7
    """Content-Type: application/xml"""

    @staticmethod
    def value_of(value):
        for member in RequestContentType:
            if member.value == value:
                return member
        raise ValueError(f"No matching enum found for value '{value}'")


class ForwardingAddressBillingExtend:
    def __init__(self, remark: str, para: str, operation: int, benchmark: str, price: float, children: list):
        self.remark = remark  # 计费备注
        self.para = para  # 参数路径
        self.operation = operation  # 运算符 1: > ,2: < ,3: == , 4: >= , 5: <=, 6: +, 7: -, 8: *, 9: /
        self.benchmark = benchmark  # 计费基准
        self.price = price  # 价格
        self.children = children  # 子数据集


def find_in_tree(data, path: str):
    # 分离路径
    keys = path.replace("]", "").split(".")
    for key in keys:
        # 处理数组索引
        if "[" in key:
            key, index = key.split("[")
            index = int(index)
            data = data[key][index]
        else:
            data = data[key]
    return data


class ForwardingAddressExtend(db.Model):
    __tablename__ = "forwarding_address_extend"
    __table_args__ = (
        db.PrimaryKeyConstraint("id", name="forwarding_address_pkey"),
        db.Index("idx_forwarding_address_id", "forwarding_id"),
        db.Index("idx_forwarding_address_status", "status"),
        db.Index("idx_forwarding_address_path", "path"),
    )

    id = db.Column(StringUUID, server_default=db.text("uuid_generate_v4()"))
    forwarding_id = db.Column(StringUUID, nullable=False)
    path = db.Column(db.String(255), nullable=False)
    models = db.Column(db.String(255), nullable=False)
    status = db.Column(db.Boolean, nullable=True, server_default=db.text("true"))
    description = db.Column(db.Text, nullable=False, server_default=db.text("''::character varying"))
    content_type = db.Column(db.Integer, nullable=False, server_default=db.text("0"))
    billing = db.Column(db.Text, nullable=False, server_default=db.text("'[]'"))

    @property
    def encode(self):
        return json.dumps(self.billing)

    @property
    def decode_billing(self) -> list[ForwardingAddressBillingExtend]:
        return [ForwardingAddressBillingExtend(**item) for item in json.loads(self.billing)]

    def funds_settlement(self, data, billing_list: list[ForwardingAddressBillingExtend]) -> (dict, int):
        money = 0
        funds = {}
        # differentiate request types
        for i in billing_list:
            # 判断路径是否存在
            try:
                path_value = find_in_tree(data, i.para)
                if path_value is None:
                    continue
                # 判断当前是否符合条件
                # 0: != , 1: > ,2: < ,3: == , 4: >= , 5: <=, 6: +, 7: -, 8: *, 9: /
                try:
                    if i.price and len(path_value) > 0:
                        funds[i.para] = path_value
                    if i.operation == 0 and i.benchmark != path_value:
                        # != 不等于
                        money += float(i.price)
                    if i.operation == 1 and i.benchmark > path_value:
                        # > 大于
                        money += float(i.price)
                    elif i.operation == 2 and i.benchmark < path_value:
                        # < 小于
                        money += float(i.price)
                    elif i.operation == 3 and i.benchmark == path_value:
                        # == 等于
                        money += float(i.price)
                    elif i.operation == 4 and i.benchmark >= path_value:
                        # >= 大于等于
                        money += float(i.price)
                    elif i.operation == 5 and i.benchmark <= path_value:
                        # <= 小于等于
                        money += float(i.price)
                    elif i.operation == 6:
                        # + 加
                        money += float(i.price)
                    elif i.operation == 7:
                        # - 减
                        money -= float(i.price)
                    elif i.operation == 8:
                        # * 乘
                        money += float(i.price) * float(path_value)
                    elif i.operation == 9:
                        # / 除
                        money += float(i.price) / float(path_value)
                except Exception as e:
                    logging.debug(e, "billing error", i.price, path_value)
                # 判断是否有子集
                if len(i.children) > 0:
                    # 有子集回调
                    cache_funds, cache_money = self.funds_settlement(data, i.children)
                    funds.update(cache_funds)
                    money += cache_money
            except:
                pass
        return funds, money
