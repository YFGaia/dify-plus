"""Integration tests for ChatMessageApi permission verification."""

import uuid
from types import SimpleNamespace
from unittest import mock

import pytest
from flask.testing import FlaskClient
from werkzeug.exceptions import Forbidden

from controllers.console.app import completion as completion_api
from controllers.console.app import message as message_api
from controllers.console.app import wraps
from libs import login as login_lib
from libs.datetime_utils import naive_utc_now
from models import App, Tenant
from models.account import Account, TenantAccountJoin, TenantAccountRole
from models.enums import ConversationFromSource
from models.model import AppMode
from services.app_generate_service import AppGenerateService


class _CurrentUserProxy:
    def __init__(self, user: Account):
        self._user = user

    def _get_current_object(self) -> Account:
        return self._user

    def __getattr__(self, name: str):
        return getattr(self._user, name)


class _MockChatMessagePayload:
    response_mode = "blocking"

    def model_dump(self, *, exclude_none: bool, by_alias: bool):
        return {
            "inputs": {},
            "query": "Hello, world!",
            "model_config": {
                "model": {"provider": "openai", "name": "gpt-4", "mode": "chat", "completion_params": {}}
            },
            "response_mode": "blocking",
        }


def _post_with_edit_permission_only():
    handler = completion_api.ChatMessageApi.post
    for _ in range(5):
        handler = handler.__wrapped__
    return handler


class TestChatMessageApiPermissions:
    """Test permission verification for ChatMessageApi endpoint."""

    @pytest.fixture
    def mock_app_model(self):
        """Create a mock App model for testing."""
        app = App()
        app.id = str(uuid.uuid4())
        app.mode = AppMode.CHAT
        app.tenant_id = str(uuid.uuid4())
        app.status = "normal"
        return app

    @pytest.fixture
    def mock_account(self, monkeypatch: pytest.MonkeyPatch):
        """Create a mock Account for testing."""

        account = Account(
            name="Test User",
            email="test@example.com",
        )
        account.last_active_at = naive_utc_now()
        account.created_at = naive_utc_now()
        account.updated_at = naive_utc_now()
        account.id = str(uuid.uuid4())

        # Create mock tenant
        tenant = Tenant(name="Test Tenant")
        tenant.id = str(uuid.uuid4())

        mock_session_instance = mock.MagicMock()

        mock_tenant_join = TenantAccountJoin(
            tenant_id=tenant.id,
            account_id=account.id,
            role=TenantAccountRole.OWNER,
        )
        monkeypatch.setattr(mock_session_instance, "scalar", mock.Mock(return_value=mock_tenant_join))

        mock_scalars_result = mock.Mock()
        mock_scalars_result.one.return_value = tenant
        monkeypatch.setattr(mock_session_instance, "scalars", mock.Mock(return_value=mock_scalars_result))

        mock_session_context = mock.MagicMock()
        mock_session_context.__enter__.return_value = mock_session_instance
        monkeypatch.setattr("models.account.Session", lambda _, expire_on_commit: mock_session_context)

        account._current_tenant = tenant
        account.role = TenantAccountRole.OWNER
        account.test_current_user_proxy = _CurrentUserProxy(account)
        return account

    @pytest.mark.parametrize(
        ("role", "status"),
        [
            (TenantAccountRole.OWNER, 200),
            (TenantAccountRole.ADMIN, 200),
            (TenantAccountRole.EDITOR, 200),
            (TenantAccountRole.NORMAL, 403),
            (TenantAccountRole.DATASET_OPERATOR, 403),
        ],
    )
    def test_post_with_owner_role_succeeds(
        self,
        test_client: FlaskClient,
        monkeypatch,
        mock_app_model,
        mock_account,
        role: TenantAccountRole,
        status: int,
    ):
        """Test that OWNER role can access chat-messages endpoint."""

        """Setup common mocks for testing."""
        # Mock app loading

        mock_load_app_model = mock.Mock(return_value=mock_app_model)
        monkeypatch.setattr(wraps, "_load_app_model", mock_load_app_model)

        # Mock current user
        monkeypatch.setattr(login_lib, "current_user", mock_account.test_current_user_proxy)
        monkeypatch.setattr(login_lib, "check_csrf_token", lambda *args, **kwargs: None)
        monkeypatch.setattr(completion_api, "current_user", mock_account)
        monkeypatch.setattr(completion_api.ChatMessagePayload, "model_validate", mock.Mock(return_value=_MockChatMessagePayload()))

        mock_generate = mock.Mock(return_value={"message": "Test response"})
        monkeypatch.setattr(AppGenerateService, "generate", mock_generate)

        # Set user role to OWNER
        mock_account.role = role

        with test_client.application.test_request_context(
            f"/console/api/apps/{mock_app_model.id}/chat-messages",
            method="POST",
            json={
                "inputs": {},
                "query": "Hello, world!",
                "model_config": {
                    "model": {"provider": "openai", "name": "gpt-4", "mode": "chat", "completion_params": {}}
                },
                "response_mode": "blocking",
            },
        ):
            post_handler = _post_with_edit_permission_only()
            if status == 200:
                response = post_handler(completion_api.ChatMessageApi(), mock_app_model)
                assert response.status_code == status
            else:
                with pytest.raises(Forbidden) as exc_info:
                    post_handler(completion_api.ChatMessageApi(), mock_app_model)
                assert exc_info.value.code == status

    @pytest.mark.parametrize(
        ("role", "status"),
        [
            (TenantAccountRole.OWNER, 200),
            (TenantAccountRole.ADMIN, 200),
            (TenantAccountRole.EDITOR, 200),
            (TenantAccountRole.NORMAL, 403),
            (TenantAccountRole.DATASET_OPERATOR, 403),
        ],
    )
    def test_get_requires_edit_permission(
        self,
        test_client: FlaskClient,
        auth_header,
        monkeypatch,
        mock_app_model,
        mock_account,
        role: TenantAccountRole,
        status: int,
    ):
        """Ensure GET chat-messages endpoint enforces edit permissions."""

        mock_load_app_model = mock.Mock(return_value=mock_app_model)
        monkeypatch.setattr(wraps, "_load_app_model", mock_load_app_model)

        conversation_id = uuid.uuid4()
        created_at = naive_utc_now()

        mock_conversation = SimpleNamespace(id=str(conversation_id), app_id=str(mock_app_model.id))
        mock_message = SimpleNamespace(
            id=str(uuid.uuid4()),
            conversation_id=str(conversation_id),
            inputs=[],
            query="hello",
            message=[{"text": "hello"}],
            message_tokens=0,
            re_sign_file_url_answer="",
            answer_tokens=0,
            provider_response_latency=0.0,
            from_source=ConversationFromSource.CONSOLE,
            from_end_user_id=None,
            from_account_id=mock_account.id,
            feedbacks=[],
            workflow_run_id=None,
            annotation=None,
            annotation_hit_history=None,
            created_at=created_at,
            agent_thoughts=[],
            message_files=[],
            message_metadata_dict={},
            status="normal",
            error="",
            parent_message_id=None,
        )

        class MockQuery:
            def __init__(self, model):
                self.model = model

            def where(self, *args, **kwargs):
                return self

            def first(self):
                if getattr(self.model, "__name__", "") == "Conversation":
                    return mock_conversation
                return None

            def order_by(self, *args, **kwargs):
                return self

            def limit(self, *_):
                return self

            def all(self):
                if getattr(self.model, "__name__", "") == "Message":
                    return [mock_message]
                return []

        mock_session = mock.Mock()
        mock_session.query.side_effect = MockQuery
        mock_session.scalar.return_value = False

        monkeypatch.setattr(message_api, "db", SimpleNamespace(session=mock_session))
        monkeypatch.setattr(login_lib, "current_user", mock_account.test_current_user_proxy)
        monkeypatch.setattr(login_lib, "check_csrf_token", lambda *args, **kwargs: None)

        class DummyPagination:
            def __init__(self, data, limit, has_more):
                self.data = data
                self.limit = limit
                self.has_more = has_more

        monkeypatch.setattr(message_api, "InfiniteScrollPagination", DummyPagination)

        mock_account.role = role

        response = test_client.get(
            f"/console/api/apps/{mock_app_model.id}/chat-messages",
            headers=auth_header,
            query_string={"conversation_id": str(conversation_id)},
        )

        assert response.status_code == status, response.get_json()
