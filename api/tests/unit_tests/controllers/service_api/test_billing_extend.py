"""M04: real quota queries and every current decorator caller's call shape."""

import ast
import copy
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from flask import Flask
from sqlalchemy import select
from werkzeug.exceptions import Forbidden, Unauthorized

from controllers.service_api import wraps
from controllers.service_api.app.error_extend import (
    AccountNoMoneyErrorExtend,
    ApiTokenDayNoMoneyErrorExtend,
    ApiTokenMonthNoMoneyErrorExtend,
)
from models.account_money_extend import AccountMoneyExtend
from models.api_token_money_extend import ApiTokenMoneyExtend
from models.enums import ApiTokenType
from models.model import EndUser
from models.model_extend import EndUserAccountJoinsExtend
from tests.unit_tests.controllers.service_api.test_wraps import (
    _api_token,
    _app_model,
    _persist_workspace,
    _session_proxy,
)


@pytest.fixture(autouse=True)
def _bypass_token_quota_extend():
    """Override the legacy conftest bypass: all tests use actual quota code."""


@pytest.fixture
def identity(sqlite_session):
    sqlite_session.connection().connection.driver_connection.create_function(
        "uuid_generate_v4", 0, lambda: str(uuid4())
    )
    tenant, account, membership = _persist_workspace(sqlite_session)
    app = _app_model(tenant_id=tenant.id)
    token = _api_token(tenant_id=tenant.id, app_id=app.id, token_type=ApiTokenType.APP)
    sqlite_session.add_all([app, token])
    sqlite_session.commit()
    with patch.object(wraps.db, "session", _session_proxy(sqlite_session)):
        yield SimpleNamespace(
            session=sqlite_session, tenant=tenant, account=account, membership=membership, app=app, token=token
        )


@pytest.mark.parametrize(
    ("kind", "limit", "used", "error"),
    [
        ("account", 1, 0.9999999, None),
        ("account", 1, 1, AccountNoMoneyErrorExtend),
        ("account", 0, 0, AccountNoMoneyErrorExtend),
        ("account", -1, 0, AccountNoMoneyErrorExtend),
        ("day", 1, 0.9999999, None),
        ("day", 1, 1, ApiTokenDayNoMoneyErrorExtend),
        ("day", 0, 0, ApiTokenDayNoMoneyErrorExtend),
        ("day", -1, 999, None),
        ("month", 1, 0.9999999, None),
        ("month", 1, 1, ApiTokenMonthNoMoneyErrorExtend),
        ("month", 0, 0, ApiTokenMonthNoMoneyErrorExtend),
        ("month", -1, 999, None),
    ],
)
def test_quota_boundaries(identity, kind, limit, used, error):
    if kind == "account":
        row = AccountMoneyExtend(account_id=identity.account.id, total_quota=limit, used_quota=used)
    else:
        row = ApiTokenMoneyExtend(
            app_token_id=identity.token.id,
            accumulated_quota=999,
            day_limit_quota=-1,
            month_limit_quota=-1,
            day_used_quota=0,
            month_used_quota=0,
        )
        setattr(row, f"{kind}_limit_quota", limit)
        setattr(row, f"{kind}_used_quota", used)
    identity.session.add(row)
    identity.session.commit()
    if error:
        with pytest.raises(error):
            wraps.validate_token_quota_extend(identity.token)
    else:
        assert wraps.validate_token_quota_extend(identity.token).account_id == identity.account.id


def test_legacy_key_missing_quota_warns_and_uses_validated_app_tenant(identity, caplog):
    identity.token.tenant_id = None
    assert (
        wraps.validate_token_quota_extend(identity.token, tenant_id=identity.app.tenant_id).account_id
        == identity.account.id
    )
    assert identity.token.id in caplog.text


@pytest.mark.parametrize("state", ["archive", "missing_owner"])
def test_quota_rejects_inactive_owner(identity, state):
    if state == "archive":
        identity.tenant.status = "archive"
    else:
        identity.session.delete(identity.membership)
    identity.session.commit()
    with pytest.raises(Unauthorized):
        wraps.validate_token_quota_extend(identity.token)


def _callers():
    result = []
    root = Path(__file__).parents[4] / "controllers/service_api"
    for path in sorted(root.rglob("*.py")):
        for cls in (n for n in ast.parse(path.read_text()).body if isinstance(n, ast.ClassDef)):
            for method in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
                for deco in method.decorator_list:
                    fn = deco.func if isinstance(deco, ast.Call) else deco
                    if isinstance(fn, ast.Name) and fn.id == "validate_app_token":
                        result.append(
                            pytest.param(path, cls.name, method, deco, id=f"{path.stem}.{cls.name}.{method.name}")
                        )
    assert len(result) >= 30
    return result


@pytest.mark.parametrize(("path", "_cls_name", "method", "deco"), _callers())
def test_every_decorated_caller_binds_and_generation_receives_token(identity, path, _cls_name, method, deco):
    # Compile the actual signature with a probe body; business endpoint behavior is
    # covered separately. This fails on any unexpected injected kwarg or missing arg.
    probe_ast = copy.deepcopy(method)
    probe_ast.decorator_list = []
    probe_ast.returns = None
    for arg in [*probe_ast.args.posonlyargs, *probe_ast.args.args, *probe_ast.args.kwonlyargs]:
        arg.annotation = None
    probe_ast.body = ast.parse("return locals()").body
    namespace = {}
    exec(  # noqa: S102 - compile only a repository signature with a fixed probe body
        compile(ast.fix_missing_locations(ast.Module(body=[probe_ast], type_ignores=[])), str(path), "exec"), namespace
    )
    probe = namespace[method.name]
    env = {"FetchUserArg": wraps.FetchUserArg, "WhereisUserArg": wraps.WhereisUserArg}
    fetch = None
    if isinstance(deco, ast.Call) and deco.keywords:
        fetch = eval(compile(ast.Expression(deco.keywords[0].value), str(path), "eval"), env)  # noqa: S307
    view = wraps.validate_app_token(fetch_user_arg=fetch)(probe)
    supplied = {
        name: object()
        for name, param in inspect.signature(probe).parameters.items()
        if name not in {"app_model", "api_token", "end_user"} and param.default is inspect.Parameter.empty
    }
    end_user = EndUser(
        id=str(uuid4()), tenant_id=identity.tenant.id, app_id=identity.app.id, type="service_api", session_id="u"
    )
    flask_app = Flask(__name__)
    flask_app.login_manager = Mock()
    opts = (
        {"query_string": {"user": "u"}}
        if fetch and fetch.fetch_from == wraps.WhereisUserArg.QUERY
        else (
            {"data": {"user": "u"}}
            if fetch and fetch.fetch_from == wraps.WhereisUserArg.FORM
            else {"json": {"user": "u"}}
        )
    )
    with (
        flask_app.test_request_context("/", **opts),
        patch.object(wraps, "validate_and_get_api_token", return_value=identity.token),
        patch.object(wraps.EndUserService, "get_or_create_end_user", return_value=end_user),
        patch.object(wraps, "user_logged_in"),
    ):
        bound = view(**supplied)
        # Defaults must never swallow attribution on the real generation endpoints.
        if "api_token" in inspect.signature(probe).parameters:
            assert bound["api_token"] is identity.token
        if (
            path.stem in {"completion", "workflow"}
            and method.name == "post"
            and 'args["api_token"]' in ast.get_source_segment(path.read_text(), method)
        ):
            assert bound["api_token"] is identity.token
        if fetch:
            join = identity.session.scalar(
                select(EndUserAccountJoinsExtend).where(EndUserAccountJoinsExtend.end_user_id == end_user.id)
            )
            assert join.account_id == identity.account.id
            assert join.app_id == identity.app.id
            view(**supplied)
            assert len(identity.session.scalars(select(EndUserAccountJoinsExtend)).all()) == 1


def test_archive_denial_precedes_fork_quota(identity):
    identity.tenant.status = "archive"
    identity.session.commit()

    @wraps.validate_app_token
    def view(app_model):
        assert app_model is identity.app
        pytest.fail("archived workspace admitted")

    with (
        patch.object(wraps, "validate_and_get_api_token", return_value=identity.token),
        patch.object(wraps, "validate_token_quota_extend") as quota,
    ):
        with pytest.raises(Forbidden, match="archived"):
            view()
    quota.assert_not_called()


def test_dataset_binding_is_rechecked_with_same_cached_token(identity):
    from models.model import DatasetApiTokenBinding

    token = identity.token
    token.type = ApiTokenType.DATASET

    @wraps.validate_dataset_token
    def view(tenant_id):
        return tenant_id

    app = Flask(__name__)
    app.login_manager = Mock()
    with (
        app.test_request_context("/"),
        patch.object(wraps, "validate_and_get_api_token", return_value=token),
        patch.object(wraps, "user_logged_in"),
    ):
        assert view() == identity.tenant.id
        identity.session.add(DatasetApiTokenBinding(api_token_id=token.id, dataset_id="bound"))
        identity.session.commit()
        with pytest.raises(Forbidden, match="not authorized"):
            view()
