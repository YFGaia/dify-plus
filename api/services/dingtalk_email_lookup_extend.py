"""Enterprise email lookup shared by DingTalk login and draft configuration tests."""

import json
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from core.helper import ssrf_proxy


def extract_email(data: Any, path: str) -> str:
    """Resolve dotted fields and array indices without evaluating expressions."""
    current = data
    for part in re.sub(r"\[(\d+)\]", r".\1", path).split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return ""
    if isinstance(current, str) and "@" in current and not any(c.isspace() for c in current):
        return current
    return ""


def validate_email_lookup(config: dict) -> None:
    """Validate configured lookups without altering legacy or extension fields."""
    if not isinstance(config.get("enabled", False), bool):
        raise ValueError("Email lookup enabled must be a boolean.")
    if not config.get("enabled", False):
        return
    url = config.get("url", "")
    if not isinstance(url, str) or urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).hostname:
        raise ValueError("Email lookup requires an HTTP(S) API URL.")
    if str(config.get("method", "GET")).upper() not in {"GET", "POST", "PUT", "DELETE"}:
        raise ValueError("Unsupported email lookup request method.")
    for field, default in (
        ("request_param_field", "userId"),
        ("response_email_field", "data[0].userName"),
    ):
        if not isinstance(config.get(field, default), str) or not config.get(field, default).strip():
            raise ValueError("Email lookup requires a user ID parameter and response email path.")
    headers = config.get("headers", {})
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise ValueError("Email lookup headers must be a JSON object of strings.")
    auth = config.get("authorization", {})
    if not isinstance(auth, dict) or auth.get("type", "none") not in {
        "none",
        "bearer",
        "basic",
    }:
        raise ValueError("Unsupported email lookup authentication.")
    if not all(isinstance(auth.get(key, ""), str) for key in ("token", "username", "password")):
        raise ValueError("Email lookup authentication values must be strings.")
    if config.get("body_type", "raw") not in {
        "raw",
        "form-data",
        "x-www-form-urlencoded",
    }:
        raise ValueError("Unsupported email lookup request body type.")
    body = config.get("body_data", {})
    if not isinstance(body, dict):
        raise ValueError("Email lookup body data must be a JSON object.")
    for key in ("form_data", "urlencoded"):
        if key in body:
            items = body[key]
            if not isinstance(items, list) or not all(
                isinstance(item, dict)
                and isinstance(item.get("key", ""), str)
                and isinstance(item.get("value", ""), str)
                for item in items
            ):
                raise ValueError("Email lookup form data must be a list of string key/value pairs.")
    if config.get("body_type", "raw") == "raw" and body.get("raw"):
        try:
            raw = json.loads(body["raw"])
        except (ValueError, TypeError):
            raise ValueError("Email lookup raw body must contain a JSON object.") from None
        if not isinstance(raw, dict):
            raise ValueError("Email lookup raw body must contain a JSON object.")


def lookup_email(userid: str, config: dict) -> dict:
    """Query an email only; never mutate accounts or expose upstream response bodies."""
    validate_email_lookup({**config, "enabled": True})
    if not userid.strip():
        raise ValueError("A DingTalk user ID is required for the email lookup test.")
    method = str(config.get("method", "GET")).upper()
    param = config.get("request_param_field", "userId")
    headers = dict(config.get("headers", {}))
    authorization = config.get("authorization", {})
    auth = None
    if authorization.get("type") == "bearer" and authorization.get("token"):
        headers["Authorization"] = f"Bearer {authorization['token']}"
    elif authorization.get("type") == "basic":
        auth = httpx.BasicAuth(authorization.get("username", ""), authorization.get("password", ""))
    body = config.get("body_data", {})
    kwargs: dict[str, Any] = {
        "headers": headers,
        "auth": auth,
        "timeout": 10,
        "follow_redirects": False,
    }
    if method == "GET":
        kwargs["params"] = httpx.QueryParams(urlsplit(config["url"]).query).set(param, userid)
    elif config.get("body_type", "raw") in {"form-data", "x-www-form-urlencoded"}:
        key = "form_data" if config["body_type"] == "form-data" else "urlencoded"
        values = {
            item["key"]: item.get("value", "")
            for item in body.get(key, [])
            if isinstance(item, dict) and item.get("key")
        }
        kwargs["data"] = {**values, param: userid}
    else:
        values = json.loads(body["raw"]) if body.get("raw") else {}
        kwargs["json"] = {**values, param: userid}
    try:
        response = ssrf_proxy.make_request(method, config["url"], max_retries=0, **kwargs)
        if response.status_code != 200:
            return {
                "result": "failed",
                "status_code": response.status_code,
                "message": "Email API returned a non-200 response.",
            }
        email = extract_email(response.json(), config.get("response_email_field", "data[0].userName"))
        if not email:
            return {
                "result": "failed",
                "status_code": 200,
                "message": "No valid email found at the configured response path.",
            }
        return {
            "result": "success",
            "status_code": 200,
            "email": email,
            "message": "Email lookup succeeded.",
        }
    except Exception:
        # Provider exceptions can contain URLs, credentials or raw response data.
        return {
            "result": "failed",
            "message": "Email API request failed or returned invalid JSON.",
        }
