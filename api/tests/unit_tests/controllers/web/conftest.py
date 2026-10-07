"""Shared fixtures for controllers.web unit tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from flask import Flask


@pytest.fixture(autouse=True)
def _bypass_web_login_and_quota_extend(monkeypatch: pytest.MonkeyPatch):
    """extend: 单测统一绕过 fork 的 WebApp Console 登录态与额度前置校验（有专属用例覆盖）。"""
    monkeypatch.setattr(
        "controllers.web.completion.is_end_login",
        lambda end_user: SimpleNamespace(id="test-account-id"),
    )
    monkeypatch.setattr("controllers.web.completion.is_money_limit", lambda end_user: False)
    # workflow.py 以 from-import 方式引用，需同步替换其模块内引用
    monkeypatch.setattr(
        "controllers.web.workflow.is_end_login",
        lambda end_user: SimpleNamespace(id="test-account-id"),
    )
    monkeypatch.setattr("controllers.web.workflow.is_money_limit", lambda end_user: False)


@pytest.fixture
def app() -> Flask:
    """Minimal Flask app for request contexts."""
    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    return flask_app
