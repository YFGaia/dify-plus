"""App-key quota responses retain JSON numbers after loading database Decimal values."""

import inspect
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from controllers.console.apikey import BaseApiKeyListResource
from models.api_token_money_extend import ApiTokenMoneyExtend
from models.enums import ApiTokenType
from models.model import ApiToken, App, AppMode, IconType


@pytest.mark.parametrize("quota_state", ["persisted", "missing", "nullable"])
def test_app_key_quota_json_contract_after_orm_reload(sqlite_session: Session, quota_state: str) -> None:
    session = sqlite_session
    app = App(
        id="quota-json-app",
        tenant_id="quota-json-tenant",
        name="Quota JSON test",
        mode=AppMode.CHAT,
        icon_type=IconType.EMOJI,
        icon="chat",
        icon_background="#ffffff",
        enable_site=False,
        enable_api=True,
    )
    token = ApiToken(
        type=ApiTokenType.APP,
        token="synthetic-unit-test-token",
        app_id=app.id,
        tenant_id=app.tenant_id,
    )
    token.id = "quota-json-token"
    session.add_all([app, token])
    amounts = {
        "day_limit_quota": Decimal("1.1234567"),
        "month_limit_quota": Decimal("2.7654321"),
        "day_used_quota": Decimal("0.0000000"),
        "month_used_quota": Decimal("0.1234567"),
        "accumulated_quota": Decimal("3.2345678"),
    }
    quota = None
    if quota_state != "missing":
        quota = ApiTokenMoneyExtend(
            app_token_id=token.id,
            description="Quota response test",
            **(amounts if quota_state == "persisted" else dict.fromkeys(amounts)),
        )
        session.add(quota)
    session.commit()
    session.expire_all()

    resource = BaseApiKeyListResource()
    resource.resource_type = ApiTokenType.APP
    resource.resource_model = App
    resource.resource_id_field = "app_id"
    result = inspect.unwrap(BaseApiKeyListResource.get)(resource, session, app.id, app.tenant_id)
    item = result["data"][0]

    assert item["id"] == token.id
    assert item["token"] == token.token
    assert item["created_at"] is not None
    for field, amount in amounts.items():
        value = item[field]
        if quota_state == "nullable":
            assert value is None
        else:
            assert type(value) is float
            expected = float(amount) if quota_state == "persisted" else (-1.0 if "limit" in field else 0.0)
            assert value == expected
        if quota_state == "persisted":
            assert quota is not None
            assert isinstance(getattr(quota, field), Decimal)
            assert getattr(quota, field) == amount
