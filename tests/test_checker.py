import asyncio

from sub2api_batch_checker.checker import check_many
from sub2api_batch_checker.models import AccountRecord


def _account() -> AccountRecord:
    raw = {
        "name": "user@example.com",
        "platform": "openai",
        "type": "oauth",
        "credentials": {
            "access_token": "token",
            "refresh_token": "refresh",
            "client_id": "client",
        },
    }
    return AccountRecord(
        source_file="memory.json",
        index=0,
        raw=raw,
        name="user@example.com",
        platform="openai",
        account_type="oauth",
        fingerprint="fp",
    )


def _claude_account() -> AccountRecord:
    raw = {
        "name": "claude@example.com",
        "platform": "anthropic",
        "type": "oauth",
        "credentials": {
            "refresh_token": "refresh",
            "email": "claude@example.com",
        },
    }
    return AccountRecord(
        source_file="claude.json",
        index=0,
        raw=raw,
        name="claude@example.com",
        platform="anthropic",
        account_type="oauth",
        fingerprint="claude-fp",
    )


def _api_key_account() -> AccountRecord:
    raw = {
        "name": "hub.example.com",
        "platform": "openai",
        "type": "apikey",
        "credentials": {
            "api_key": "sk-test",
            "base_url": "https://hub.example.com",
        },
    }
    return AccountRecord(
        source_file="api-key.json",
        index=0,
        raw=raw,
        name="hub.example.com",
        platform="openai",
        account_type="apikey",
        fingerprint="api-key-fp",
    )


def test_api_key_models_probe_uses_account_base_url(monkeypatch) -> None:
    captured = {}

    def fake_get_json(url, api_key, timeout, proxy_url=""):
        captured["url"] = url
        captured["api_key"] = api_key
        return 200, b'{"data":[{"id":"gpt-test"}]}', 12

    monkeypatch.setattr("sub2api_batch_checker.checker._get_json_api_key", fake_get_json)

    results = asyncio.run(check_many([_api_key_account()], 1, 1, "/v1/models", "auto", 60, False, "", False))

    assert results[0].ok is True
    assert results[0].raw_meta["sample_model"] == "gpt-test"
    assert captured["url"] == "https://hub.example.com/v1/models"
    assert captured["api_key"] == "sk-test"


def test_api_key_real_probe_uses_chat_completions_shape(monkeypatch) -> None:
    captured = {}

    def fake_post_json(url, token, body, timeout, proxy_url="", extra_headers=None):
        captured["url"] = url
        captured["token"] = token
        captured["body"] = body
        return 200, b'{"choices":[{"message":{"content":"ok"}}]}', 12

    monkeypatch.setattr("sub2api_batch_checker.checker._post_json", fake_post_json)

    results = asyncio.run(
        check_many([_api_key_account()], 1, 1, "/v1/chat/completions", "gpt-test", 60, False, "", False)
    )

    assert results[0].ok is True
    assert captured["url"] == "https://hub.example.com/v1/chat/completions"
    assert captured["token"] == "sk-test"
    assert captured["body"]["messages"][0]["content"] == "ping"
    assert captured["body"]["model"] == "gpt-test"


def test_models_scope_error_counts_as_login_probe_ok(monkeypatch) -> None:
    def fake_probe(endpoint, token, timeout, model, proxy_url="", account_id=""):
        payload = (
            b'{"error":{"message":"You have insufficient permissions for this operation. '
            b'Missing scopes: api.model.read."}}'
        )
        return 403, payload, 12

    monkeypatch.setattr("sub2api_batch_checker.checker._probe_openai_endpoint", fake_probe)

    results = asyncio.run(
        check_many([_account()], 1, 1, "https://api.openai.com/v1/models", "gpt-4.1-nano", 60, False, "", False)
    )

    assert results[0].status == "codex_login_only"
    assert results[0].ok is True


def test_responses_scope_error_is_not_api_ok(monkeypatch) -> None:
    def fake_probe(endpoint, token, timeout, model, proxy_url="", account_id=""):
        payload = b'{"error":{"message":"Missing scopes: api.responses"}}'
        return 403, payload, 12

    monkeypatch.setattr("sub2api_batch_checker.checker._probe_openai_endpoint", fake_probe)

    results = asyncio.run(
        check_many([_account()], 1, 1, "https://api.openai.com/v1/responses", "gpt-4.1-nano", 60, False, "", False)
    )

    assert results[0].status == "permission_or_scope_missing"
    assert results[0].ok is False


def test_responses_scope_error_with_401_is_not_auth_invalid(monkeypatch) -> None:
    def fake_probe(endpoint, token, timeout, model, proxy_url="", account_id=""):
        payload = b'{"error":{"code":"invalid_request_error","message":"Missing scopes: api.responses.write"}}'
        return 401, payload, 12

    monkeypatch.setattr("sub2api_batch_checker.checker._probe_openai_endpoint", fake_probe)

    results = asyncio.run(
        check_many([_account()], 1, 1, "https://api.openai.com/v1/responses", "gpt-4.1-nano", 60, False, "", False)
    )

    assert results[0].status == "permission_or_scope_missing"
    assert results[0].ok is False


def test_codex_request_shape_error_is_not_auth_invalid(monkeypatch) -> None:
    def fake_probe(endpoint, token, timeout, model, proxy_url="", account_id=""):
        payload = b'{"detail":"Instructions are required"}'
        return 400, payload, 12

    monkeypatch.setattr("sub2api_batch_checker.checker._probe_openai_endpoint", fake_probe)

    results = asyncio.run(
        check_many(
            [_account()],
            1,
            1,
            "https://chatgpt.com/backend-api/codex/responses",
            "gpt-5.5",
            60,
            False,
            "",
            False,
        )
    )

    assert results[0].status == "request_shape_error"
    assert results[0].ok is False


def test_codex_stream_required_error_is_request_shape(monkeypatch) -> None:
    def fake_probe(endpoint, token, timeout, model, proxy_url="", account_id=""):
        payload = b'{"detail":"Stream must be set to true"}'
        return 400, payload, 12

    monkeypatch.setattr("sub2api_batch_checker.checker._probe_openai_endpoint", fake_probe)

    results = asyncio.run(
        check_many(
            [_account()],
            1,
            1,
            "https://chatgpt.com/backend-api/codex/responses",
            "gpt-5.5",
            60,
            False,
            "",
            False,
        )
    )

    assert results[0].status == "request_shape_error"
    assert results[0].ok is False


def test_codex_unsupported_parameter_is_request_shape(monkeypatch) -> None:
    def fake_probe(endpoint, token, timeout, model, proxy_url="", account_id=""):
        payload = b'{"detail":"Unsupported parameter: max_output_tokens"}'
        return 400, payload, 12

    monkeypatch.setattr("sub2api_batch_checker.checker._probe_openai_endpoint", fake_probe)

    results = asyncio.run(
        check_many(
            [_account()],
            1,
            1,
            "https://chatgpt.com/backend-api/codex/responses",
            "gpt-5.5",
            60,
            False,
            "",
            False,
        )
    )

    assert results[0].status == "request_shape_error"
    assert results[0].ok is False


def test_codex_probe_sends_required_instructions(monkeypatch) -> None:
    captured = {}

    def fake_post_json(url, token, body, timeout, proxy_url="", extra_headers=None):
        captured["url"] = url
        captured["body"] = body
        captured["headers"] = extra_headers or {}
        return 200, b'{"ok":true}', 12

    monkeypatch.setattr("sub2api_batch_checker.checker._post_json", fake_post_json)

    results = asyncio.run(
        check_many(
            [_account()],
            1,
            1,
            "https://chatgpt.com/backend-api/codex/responses",
            "gpt-5.5",
            60,
            False,
            "",
            False,
        )
    )

    assert results[0].status == "ok"
    assert captured["body"]["instructions"]
    assert captured["body"]["input"][0]["type"] == "message"
    assert "max_output_tokens" not in captured["body"]
    assert captured["body"]["stream"] is True
    assert captured["headers"]["Accept"] == "text/event-stream"
    assert captured["headers"]["Originator"] == "codex-tui"


def test_claude_refresh_then_probe_sends_oauth_shape(monkeypatch) -> None:
    captured = {"refresh": None, "probe": None}

    def fake_post_json_no_auth(url, body, timeout, proxy_url="", extra_headers=None):
        captured["refresh"] = {"url": url, "body": body, "headers": extra_headers or {}}
        return 200, b'{"access_token":"access","refresh_token":"new-refresh","expires_in":3600}', 11

    def fake_post_json(url, token, body, timeout, proxy_url="", extra_headers=None):
        captured["probe"] = {"url": url, "token": token, "body": body, "headers": extra_headers or {}}
        return 200, b'{"type":"message"}', 12

    monkeypatch.setattr("sub2api_batch_checker.checker._post_json_no_auth", fake_post_json_no_auth)
    monkeypatch.setattr("sub2api_batch_checker.checker._post_json", fake_post_json)

    results = asyncio.run(
        check_many(
            [_claude_account()],
            1,
            1,
            "https://api.anthropic.com/v1/messages?beta=true",
            "claude-sonnet-4-5-20250929",
            60,
            True,
            "",
            False,
        )
    )

    assert results[0].status == "ok"
    assert results[0].ok is True
    assert captured["refresh"]["body"]["grant_type"] == "refresh_token"
    assert captured["refresh"]["body"]["client_id"] == "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    assert captured["refresh"]["headers"]["User-Agent"] == "axios/1.13.6"
    assert captured["probe"]["token"] == "access"
    assert captured["probe"]["body"]["messages"][0]["content"][0]["text"] == "hi"
    assert captured["probe"]["body"]["stream"] is True
    assert captured["probe"]["headers"]["anthropic-version"] == "2023-06-01"
    assert "oauth-2025-04-20" in captured["probe"]["headers"]["anthropic-beta"]
    assert captured["probe"]["headers"]["User-Agent"].startswith("claude-cli/")


def test_claude_401_retries_after_refresh(monkeypatch) -> None:
    calls = {"probe": 0}

    def fake_post_json_no_auth(url, body, timeout, proxy_url="", extra_headers=None):
        return 200, b'{"access_token":"fresh-access"}', 11

    def fake_post_json(url, token, body, timeout, proxy_url="", extra_headers=None):
        calls["probe"] += 1
        if calls["probe"] == 1:
            return 401, b'{"error":{"message":"expired token"}}', 12
        return 200, b'{"type":"message"}', 13

    account = _claude_account()
    account.raw["credentials"]["access_token"] = "stale-access"
    monkeypatch.setattr("sub2api_batch_checker.checker._post_json_no_auth", fake_post_json_no_auth)
    monkeypatch.setattr("sub2api_batch_checker.checker._post_json", fake_post_json)

    results = asyncio.run(
        check_many(
            [account],
            1,
            1,
            "https://api.anthropic.com/v1/messages?beta=true",
            "claude-sonnet-4-5-20250929",
            60,
            True,
            "",
            False,
        )
    )

    assert results[0].ok is True
    assert results[0].attempts == 2
    assert calls["probe"] == 2
