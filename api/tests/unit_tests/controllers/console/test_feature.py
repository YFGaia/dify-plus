from pytest_mock import MockerFixture
from werkzeug.exceptions import Unauthorized


def unwrap(func):
    # extend: fork 装饰器（如 money_limit）会引入 __wrapped__ 链，解包后重新绑定实例
    bound_self = getattr(func, "__self__", None)
    while hasattr(func, "__wrapped__"):
        func = func.__wrapped__
    if bound_self is not None:
        return func.__get__(bound_self, bound_self.__class__)
    return func


class TestFeatureApi:
    def test_get_tenant_features_success(self, mocker: MockerFixture):
        from controllers.console.feature import FeatureApi

        mocker.patch(
            "controllers.console.feature.current_account_with_tenant",
            return_value=("account_id", "tenant_123"),
        )

        mocker.patch("controllers.console.feature.FeatureService.get_features").return_value.model_dump.return_value = {
            "features": {"feature_a": True}
        }

        api = FeatureApi()

        raw_get = unwrap(FeatureApi.get)
        result = raw_get(api)

        assert result == {"features": {"feature_a": True}}


# extend: CVE-2025-63387 — fork 用 LoginConfigApi（JWT 门禁）替代上游 SystemFeatureApi
class TestSystemFeatureApi:
    def test_get_system_features_authenticated(self, mocker: MockerFixture):
        """
        current_user.is_authenticated == True
        """

        from flask import Flask

        from controllers.console.feature import LoginConfigApi

        fake_user = mocker.Mock()
        fake_user.is_authenticated = True

        mocker.patch(
            "controllers.console.feature.current_user",
            fake_user,
        )
        mocker.patch("controllers.console.feature._verify_login_config_token", return_value=True)

        mocker.patch(
            "controllers.console.feature.FeatureService.get_system_features"
        ).return_value.model_dump.return_value = {"features": {"sys_feature": True}}

        api = LoginConfigApi()
        flask_app = Flask(__name__)
        with flask_app.test_request_context("/console/api/login_config"):
            result = unwrap(api.get)()

        assert result == {"features": {"sys_feature": True}}

    def test_get_system_features_unauthenticated(self, mocker: MockerFixture):
        """
        current_user.is_authenticated raises Unauthorized
        """

        from flask import Flask

        from controllers.console.feature import LoginConfigApi

        fake_user = mocker.Mock()
        type(fake_user).is_authenticated = mocker.PropertyMock(side_effect=Unauthorized())

        mocker.patch(
            "controllers.console.feature.current_user",
            fake_user,
        )
        mocker.patch("controllers.console.feature._verify_login_config_token", return_value=True)

        mocker.patch(
            "controllers.console.feature.FeatureService.get_system_features"
        ).return_value.model_dump.return_value = {"features": {"sys_feature": False}}

        api = LoginConfigApi()
        flask_app = Flask(__name__)
        with flask_app.test_request_context("/console/api/login_config"):
            result = unwrap(api.get)()

        assert result == {"features": {"sys_feature": False}}
