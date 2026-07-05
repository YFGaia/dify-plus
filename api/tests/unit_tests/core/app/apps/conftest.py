"""Shared fixtures for app runner/generator unit tests."""

import pytest


@pytest.fixture(autouse=True)
def _bypass_messages_context_extend(monkeypatch: pytest.MonkeyPatch):
    """extend: 单测统一绕过 fork 的记忆上下文登记（依赖 redis/db，运行时行为有专属用例覆盖）。"""
    monkeypatch.setattr(
        "core.app.apps.base_app_runner.AppRunner.add_messages_context",
        lambda self, prompt_messages, app_id, conversation_id, message_id: None,
    )
