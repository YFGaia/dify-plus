"""M04 regression gates. External model, Redis and Celery I/O are replaced; hooks run real code."""

import ast
import importlib
from contextlib import ExitStack, nullcontext
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from flask import Flask
from sqlalchemy import event, select

from core.app.entities.app_invoke_entities import InvokeFrom
from core.app.entities.queue_entities import QueueWorkflowStartedEvent
from graphon.entities import WorkflowStartReason
from models.account import Account
from models.account_money_extend import AccountMoneyExtend
from models.api_token_money_extend import ApiTokenMessageJoinsExtend, ApiTokenMoneyExtend
from models.model_extend import AppExtend, EndUserAccountJoinsExtend, MessageContextExtend


@pytest.fixture(autouse=True)
def quota_uuid_default(sqlite_session):
    sqlite_session.connection().connection.driver_connection.create_function(
        "uuid_generate_v4", 0, lambda: str(uuid4())
    )


@pytest.mark.parametrize("mode", ["workflow", "advanced_chat"])
def test_initial_join_persists_once_and_resume_does_not_insert(mode, sqlite_session, sqlite_session_factory):
    fixture = importlib.import_module(f"tests.unit_tests.core.app.apps.{mode}.test_generate_task_pipeline_core")
    module = importlib.import_module(f"core.app.apps.{mode}.generate_task_pipeline")
    pipeline = fixture._make_pipeline()
    if mode == "advanced_chat":
        fixture._persist_message(sqlite_session)
    pipeline._application_generate_entity.extras["app_token_id"] = "token"
    pipeline._resolve_graph_runtime_state = Mock()
    pipeline._extract_workflow_run_id = Mock(return_value="run-id")
    pipeline._workflow_response_converter.workflow_start_to_stream_response = lambda **_kwargs: "started"
    pipeline._database_session = sqlite_session_factory
    pipeline._save_workflow_app_log = Mock()
    with (
        patch.object(module, "db", SimpleNamespace(engine=sqlite_session.bind), create=True),
        patch("models.api_token_money_extend.db", SimpleNamespace(session=sqlite_session)),
    ):
        for reason in [WorkflowStartReason.INITIAL, WorkflowStartReason.RESUMPTION, WorkflowStartReason.RESUMPTION]:
            assert list(pipeline._handle_workflow_started_event(QueueWorkflowStartedEvent(reason=reason))) == [
                "started"
            ]
            rows = sqlite_session.scalars(select(ApiTokenMessageJoinsExtend)).all()
            assert len(rows) == 1
            assert (rows[0].app_token_id, rows[0].record_id, rows[0].app_mode) == (
                "token",
                "run-id",
                mode.replace("_", "-"),
            )


class StopBeforeWorkerError(Exception):
    pass


@pytest.mark.parametrize(("mode", "prefix"), [("workflow", "Workflow"), ("advanced_chat", "AdvancedChat")])
def test_generator_materializes_extras_in_request_thread(mode, prefix, sqlite_session):
    import threading

    module = importlib.import_module(f"core.app.apps.{mode}.app_generator")
    generator = getattr(module, f"{prefix}AppGenerator")()
    captured = {}
    thread_id = threading.get_ident()

    def capture(**kwargs):
        captured.update(kwargs)
        assert threading.get_ident() == thread_id
        raise StopBeforeWorkerError()

    app = SimpleNamespace(id="app", tenant_id="tenant")
    user = Account(name="user", email="user@example.com")
    workflow = SimpleNamespace(features_dict={}, id="workflow")
    with ExitStack() as stack:
        stack.enter_context(patch.object(generator, "_bind_file_access_scope", return_value=nullcontext()))
        stack.enter_context(patch.object(generator, "_prepare_user_inputs", return_value={}))
        stack.enter_context(patch.object(module.FileUploadConfigManager, "convert", return_value=None))
        stack.enter_context(patch.object(module.file_factory, "build_from_mappings", return_value=[]))
        stack.enter_context(
            patch.object(
                getattr(module, f"{prefix}AppConfigManager"),
                "get_app_config",
                return_value=SimpleNamespace(variables=[]),
            )
        )
        stack.enter_context(patch.object(module, "TraceQueueManager"))
        stack.enter_context(patch.object(module, f"{prefix}AppGenerateEntity", side_effect=capture))
        args = {
            "app_model": app,
            "workflow": workflow,
            "user": user,
            "args": {"inputs": {}, "query": "hi", "api_token": SimpleNamespace(id="token"), "account_id": "payer"},
            "invoke_from": InvokeFrom.SERVICE_API,
            "workflow_run_id": "run",
        }
        if mode == "advanced_chat":
            args["session"] = sqlite_session
        with pytest.raises(StopBeforeWorkerError):
            generator.generate(**args)
    assert captured["extras"]["app_token_id"] == "token"
    assert captured["extras"]["account_id"] == "payer"


@pytest.mark.parametrize(
    ("mode", "prefix"), [("chat", "Chat"), ("agent_chat", "AgentChat"), ("completion", "Completion")]
)
def test_message_join_commits_on_injected_session_before_queue(mode, prefix, sqlite_session):
    module = importlib.import_module(f"core.app.apps.{mode}.app_generator")
    generator = getattr(module, f"{prefix}AppGenerator")()
    app = SimpleNamespace(id="app", tenant_id="tenant", mode=mode)
    user = Account(name="user", email="user@example.com")
    conversation = SimpleNamespace(id="conv", mode=mode)
    message = SimpleNamespace(id="message")
    commits = []
    event.listen(sqlite_session, "after_commit", lambda _session: commits.append("commit"))

    def queue(**_kwargs):
        assert commits == ["commit"]
        row = sqlite_session.scalar(select(ApiTokenMessageJoinsExtend))
        assert (row.app_token_id, row.record_id, row.app_mode) == ("token", "message", mode)
        raise StopBeforeWorkerError()

    with ExitStack() as stack:
        stack.enter_context(patch.object(generator, "_bind_file_access_scope", return_value=nullcontext()))
        stack.enter_context(patch.object(generator, "_get_app_model_config", return_value=Mock(app_id="app")))
        stack.enter_context(patch.object(generator, "_prepare_user_inputs", return_value={}))
        records = stack.enter_context(
            patch.object(generator, "_init_generate_records", return_value=(conversation, message))
        )
        stack.enter_context(patch.object(module, "load_annotation_reply_config", return_value=None))
        stack.enter_context(patch.object(module.FileUploadConfigManager, "convert", return_value=None))
        stack.enter_context(
            patch.object(
                getattr(module, f"{prefix}AppConfigManager"),
                "get_app_config",
                return_value=SimpleNamespace(
                    variables=[], external_data_variables=[], app_model_config_dict={}, app_mode=mode
                ),
            )
        )
        stack.enter_context(patch.object(module.ModelConfigConverter, "convert", return_value=None))
        stack.enter_context(patch.object(module, "TraceQueueManager"))
        entity = stack.enter_context(
            patch.object(module, f"{prefix}AppGenerateEntity", side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
        )
        stack.enter_context(patch.object(module, "MessageBasedAppQueueManager", side_effect=queue))
        with pytest.raises(StopBeforeWorkerError):
            generator.generate(
                app_model=app,
                user=user,
                args={"inputs": {}, "query": "hi", "api_token": SimpleNamespace(id="token"), "account_id": "payer"},
                invoke_from=InvokeFrom.SERVICE_API,
                session=sqlite_session,
            )
    assert records.call_args.kwargs["session"] is sqlite_session
    assert entity.call_args.kwargs["extras"]["account_id"] == "payer"


@pytest.mark.parametrize("payer_kind", ["console", "web_account", "mapped", "anonymous"])
@pytest.mark.parametrize("currency", ["USD", "RMB"])
def test_message_signal_payer_currency_and_token_charge(sqlite_session, payer_kind, currency):
    from controllers.web.completion import is_money_limit
    from events.event_handlers import update_account_money_when_messaeg_created_extend as handler
    from events.message_event import message_was_created

    end_user_id = "anonymous" if payer_kind == "anonymous" else "end-user"
    payer = "account" if payer_kind in {"console", "mapped"} else end_user_id
    if payer_kind == "web_account":
        account = Account(name="payer", email="payer@example.com")
        account.id = end_user_id
        sqlite_session.add(account)
    if payer_kind == "mapped":
        sqlite_session.add(EndUserAccountJoinsExtend(end_user_id=end_user_id, account_id=payer, app_id="app"))
    quota = ApiTokenMoneyExtend(
        app_token_id="token",
        accumulated_quota=0,
        day_used_quota=0,
        month_used_quota=0,
        day_limit_quota=-1,
        month_limit_quota=-1,
    )
    sqlite_session.add_all(
        [quota, ApiTokenMessageJoinsExtend(app_token_id="token", record_id="message", app_mode="chat")]
    )
    sqlite_session.commit()
    message = SimpleNamespace(
        id="message",
        from_account_id=payer if payer_kind == "console" else None,
        from_end_user_id=end_user_id,
        total_price=Decimal("7.26"),
        currency=currency,
    )
    assert handler.handle in list(message_was_created.receivers_for(message))
    with (
        patch.object(handler, "db", SimpleNamespace(session=sqlite_session)),
        patch("controllers.web.completion.db", SimpleNamespace(session=sqlite_session)),
    ):
        if payer_kind == "anonymous":
            assert is_money_limit(SimpleNamespace(id=end_user_id)) is False
        handler.handle(message)
    expected = 7.26 if currency == "USD" else 7.26 / float(handler.dify_config.RMB_TO_USD_RATE)
    money = sqlite_session.scalar(select(AccountMoneyExtend))
    assert money.account_id == payer
    assert float(money.used_quota) == pytest.approx(expected)
    sqlite_session.refresh(quota)
    for field in ["accumulated_quota", "day_used_quota", "month_used_quota"]:
        assert float(getattr(quota, field)) == pytest.approx(expected)


def test_anonymous_limit_read_failure_denies():
    from controllers.web.completion import is_money_limit

    with patch("controllers.web.completion.db") as db:
        db.session.query.side_effect = RuntimeError("read failure")
        assert is_money_limit(SimpleNamespace(id="anonymous")) is True


@pytest.mark.parametrize("retention", ["missing", None, 1, 10])
def test_retention_missing_null_and_anonymous_context(sqlite_session, retention):
    from core.app.apps.base_app_runner import AppRunner

    if retention != "missing":
        sqlite_session.add(AppExtend(app_id="app", retention_number=retention))
        sqlite_session.commit()
    redis = Mock()
    redis.get.return_value = None
    with (
        patch("extensions.ext_database.db", SimpleNamespace(session=sqlite_session)),
        patch("extensions.ext_redis.redis_client", redis),
    ):
        AppRunner.add_messages_context(object(), [object(), object()], "app", "anonymous-conversation", "message")
    rows = sqlite_session.scalars(select(MessageContextExtend)).all()
    assert len(rows) == (1 if retention == 1 else 0)
    if rows:
        assert rows[0].conversation_id == "anonymous-conversation"
    if retention in {"missing", None}:
        redis.set.assert_not_called()


@pytest.mark.parametrize("enabled", [True, False])
def test_three_quota_beats_unique_switch_schedule_and_queue(enabled, config_overrides):
    from extensions import ext_celery

    config_overrides(ENABLE_EXTEND_QUOTA_RESET_TASKS=enabled)
    celery = Mock()
    with (
        patch.object(ext_celery, "Celery", return_value=celery),
        patch.object(ext_celery, "setup_workflow_warm_shutdown_handler"),
    ):
        ext_celery.init_app(Flask(__name__))
    config = {}
    for call in celery.conf.update.call_args_list:
        config.update(call.kwargs)
    names = [
        "update_account_used_quota",
        "update_api_token_daily_used_quota_task_extend",
        "update_api_token_monthly_used_quota_task_extend",
    ]
    schedule = config["beat_schedule"]
    for name in names:
        assert (name in schedule) is enabled
    imports = [s for s in config["imports"] if "quota" in s and "extend" in s]
    assert len(imports) == (3 if enabled else 0)
    assert len(set(imports)) == len(imports)
    if enabled:
        for name, module in zip(names, imports, strict=True):
            entry = schedule[name]
            assert entry["task"] == module + "." + module.split(".")[-1]
            assert entry["schedule"].hour == {0}
            assert entry["schedule"].minute == {0}
            assert entry["schedule"].day_of_month == (set(range(1, 32)) if "daily" in name else {1})
            source = Path(__file__).parents[4] / (module.replace(".", "/") + ".py")
            tree = ast.parse(source.read_text())
            task = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == module.split(".")[-1])
            assert any(
                isinstance(d, ast.Call)
                and any(k.arg == "queue" and ast.literal_eval(k.value) == "extend_low" for k in d.keywords)
                for d in task.decorator_list
            )


def test_node_success_dispatches_once_retry_failure_and_resume_do_not(monkeypatch):
    from core.app.workflow.layers import persistence
    from graphon.enums import BuiltinNodeTypes
    from graphon.graph_events import (
        GraphRunStartedEvent,
        NodeRunFailedEvent,
        NodeRunRetryEvent,
        NodeRunStartedEvent,
        NodeRunSucceededEvent,
    )
    from graphon.node_events import NodeRunResult
    from tests.unit_tests.core.app.workflow.test_persistence_layer import _make_layer, _naive_utc_now

    layer, _, _, _ = _make_layer()
    layer._user_from = persistence.UserFrom.END_USER
    task = Mock()
    monkeypatch.setattr(persistence, "update_account_money_when_workflow_node_execution_created_extend", task)
    layer._handle_graph_run_started()
    fields = {"id": "exec", "node_id": "node", "node_type": BuiltinNodeTypes.LLM, "start_at": _naive_utc_now()}
    layer._handle_node_started(NodeRunStartedEvent(**fields, node_title="LLM"))
    layer._handle_node_retry(
        NodeRunRetryEvent(**fields, node_title="LLM", retry_index=1, error="retry", node_run_result=NodeRunResult())
    )
    task.delay.assert_not_called()
    layer._handle_node_succeeded(
        NodeRunSucceededEvent(
            **fields, node_run_result=NodeRunResult(outputs={"usage": {"total_price": "1", "currency": "USD"}})
        )
    )
    task.delay.assert_called_once()
    payload = task.delay.call_args.args[0]
    assert payload["workflow_run_id"] == "run-id"
    assert payload["created_by"] == "user"
    assert payload["created_by_role"] in {"account", "end_user"}
    layer._handle_node_failed(NodeRunFailedEvent(**fields, error="failed", node_run_result=NodeRunResult()))
    layer._handle_graph_run_started(GraphRunStartedEvent(reason=WorkflowStartReason.RESUMPTION))
    task.delay.assert_called_once()


@pytest.mark.parametrize("controlled", [True, False])
def test_anonymous_memory_context_control_and_assistant_id(sqlite_session, controlled):
    from core.memory import token_buffer_memory as memory_module
    from tests.unit_tests.core.memory.test_token_buffer_memory import (
        _make_app,
        _make_conversation,
        _make_model_instance,
        _persist_message,
    )

    app = _make_app()
    conversation = _make_conversation(app_id=app.id)
    sqlite_session.add_all([app, conversation])
    sqlite_session.commit()
    database = SimpleNamespace(session=sqlite_session, engine=sqlite_session.bind)
    first = _persist_message(database, conversation.id, query="one", answer="answer-one")
    second = _persist_message(database, conversation.id, query="two", answer="answer-two")
    second.parent_message_id = first.id
    sqlite_session.add(MessageContextExtend(conversation_id=conversation.id, message_id=first.id))
    sqlite_session.commit()
    memory = memory_module.TokenBufferMemory(conversation=conversation, model_instance=_make_model_instance())
    with patch.object(memory_module, "db", database), patch("models.model.db", database):
        prompts = memory.get_history_prompt_messages(control_registers=controlled)
    if controlled:
        assert [p.content for p in prompts] == ["two", "answer-two"]
        assert all(p.name is None for p in prompts)
    else:
        assert [p.content for p in prompts] == ["one", "answer-one", "two", "answer-two"]
        assert prompts[1].name == first.id
        assert prompts[3].name == second.id


def test_save_message_signal_observes_usage_and_precommitted_join(sqlite_session, monkeypatch):
    from core.app.task_pipeline import easy_ui_based_generate_task_pipeline as module
    from events.event_handlers import update_account_money_when_messaeg_created_extend as handler
    from tests.unit_tests.core.app.task_pipeline import test_easy_ui_based_generate_task_pipeline_core as fixture

    conversation, message = fixture._make_conversation(fixture.AppMode.CHAT), fixture._make_message()
    message.from_account_id = "payer"
    sqlite_session.add_all(
        [
            conversation,
            message,
            ApiTokenMessageJoinsExtend(app_token_id="token", record_id=message.id, app_mode="chat"),
            ApiTokenMoneyExtend(
                app_token_id="token",
                accumulated_quota=0,
                day_used_quota=0,
                month_used_quota=0,
                day_limit_quota=-1,
                month_limit_quota=-1,
            ),
        ]
    )
    sqlite_session.commit()
    pipeline = module.EasyUIBasedGenerateTaskPipeline(
        application_generate_entity=fixture._make_entity(fixture.ChatAppGenerateEntity, fixture.AppMode.CHAT),
        queue_manager=fixture._FakeQueueManager(),
        conversation=conversation,
        message=message,
        stream=False,
    )
    fixture._set_method(pipeline, "_model_config", fixture._ModelConfigMode(mode="chat"))
    pipeline._task_state.llm_result.message = fixture.AssistantPromptMessage(content="answer")
    pipeline._task_state.llm_result.usage = fixture.LLMUsage.from_metadata(
        {"prompt_tokens": 3, "completion_tokens": 5, "total_price": "1.23", "currency": "USD"}
    )
    order = []
    event.listen(sqlite_session, "after_commit", lambda _session: order.append("commit"))

    def signal(sender, **kwargs):
        assert sender.total_price == Decimal("1.23")
        assert sqlite_session.scalar(select(ApiTokenMessageJoinsExtend)).record_id == sender.id
        order.append("signal")
        handler.handle(sender, **kwargs)

    monkeypatch.setattr(module.message_was_created, "send", signal)
    with patch.object(handler, "db", SimpleNamespace(session=sqlite_session)):
        pipeline._save_message(session=sqlite_session)
    assert order == ["signal", "commit"]
    assert sqlite_session.scalar(select(AccountMoneyExtend)).used_quota == Decimal("1.2300000")
