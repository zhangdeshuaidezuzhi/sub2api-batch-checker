from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable
from urllib import parse
from urllib import error, request

from .classify import classify_failure
from .models import AccountRecord, CheckResult


OPENAI_API_BASE = "https://api.openai.com"
OPENAI_CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
OPENAI_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OPENAI_USERINFO_URL = "https://auth0.openai.com/userinfo"
CHATGPT_ME_URL = "https://chatgpt.com/backend-api/me"
CHATGPT_CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
SUB2API_OAUTH_COMPAT_URL = "sub2api://oauth-compatible"
CLAUDE_OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_MESSAGES_URL = "https://api.anthropic.com/v1/messages?beta=true"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
CLAUDE_CODE_USER_AGENT = "claude-cli/2.1.161 (external, cli)"
CLAUDE_BETA_HEADER = (
    "claude-code-20250219,oauth-2025-04-20,"
    "interleaved-thinking-2025-05-14,fine-grained-tool-streaming-2025-05-14"
)
CLAUDE_CODE_SYSTEM_PROMPT = "You are Claude Code, Anthropic's official CLI for Claude."
USER_AGENT = "sub2api-batch-checker/0.1"
CODEX_USER_AGENT = "codex-tui/0.135.0 (Mac OS 26.5.0; arm64) iTerm.app/3.6.10 (codex-tui; 0.135.0)"
CODEX_ORIGINATOR = "codex-tui"


def _client_request_id() -> str:
    return f"sub2api-batch-checker-{uuid.uuid4()}"


def _now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _extract_error(payload: bytes) -> tuple[str, str]:
    if not payload:
        return "", ""
    text = payload.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
    except Exception:
        return "", text[:500]
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        code = str(err.get("code") or err.get("type") or "")
        message = str(err.get("message") or text)
        return code, message[:500]
    return "", text[:500]


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(payload.decode("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _safe_token_meta(account: AccountRecord, token: str) -> dict[str, Any]:
    credentials = account.raw.get("credentials") if isinstance(account.raw.get("credentials"), dict) else {}
    extra = account.raw.get("extra") if isinstance(account.raw.get("extra"), dict) else {}
    claims = _decode_jwt_claims(token)
    audience = claims.get("aud")
    if isinstance(audience, list):
        audience_value: Any = [str(item) for item in audience]
    elif audience is None:
        audience_value = ""
    else:
        audience_value = str(audience)
    scope = str(claims.get("scope") or extra.get("scope") or "")
    return {
        "issuer": str(claims.get("iss") or ""),
        "audience": audience_value,
        "scope": scope,
        "plan_type": str(claims.get("https://api.openai.com/auth/plan_type") or credentials.get("plan_type") or ""),
        "has_account_id": bool(credentials.get("chatgpt_account_id") or claims.get("https://api.openai.com/auth/chatgpt_account_id")),
        "has_id_token": bool(credentials.get("id_token")),
        "source_format": str(extra.get("source_format") or ""),
    }


def _account_id_from_claims_or_credentials(account: AccountRecord, token: str) -> str:
    credentials = account.raw.get("credentials") if isinstance(account.raw.get("credentials"), dict) else {}
    claims = _decode_jwt_claims(token)
    return str(
        credentials.get("chatgpt_account_id")
        or claims.get("https://api.openai.com/auth/chatgpt_account_id")
        or account.account_id
        or ""
    )


def _post_json_no_auth(
    url: str,
    body: dict[str, Any],
    timeout: float,
    proxy_url: str = "",
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, bytes, int]:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": USER_AGENT,
        "X-Client-Request-Id": _client_request_id(),
    }
    if extra_headers:
        headers.update(extra_headers)
    req = request.Request(url, data=payload, method="POST", headers=headers)
    started = time.perf_counter()
    opener = _build_opener(proxy_url)
    try:
        with opener.open(req, timeout=timeout) as resp:
            latency_ms = int((time.perf_counter() - started) * 1000)
            return resp.status, resp.read(), latency_ms
    except error.HTTPError as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return exc.code, exc.read(), latency_ms


def _post_json(
    url: str,
    token: str,
    body: dict[str, Any],
    timeout: float,
    proxy_url: str = "",
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, bytes, int]:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "X-Client-Request-Id": _client_request_id(),
    }
    if extra_headers:
        headers.update(extra_headers)
    req = request.Request(
        url,
        data=payload,
        method="POST",
        headers=headers,
    )
    started = time.perf_counter()
    opener = _build_opener(proxy_url)
    try:
        with opener.open(req, timeout=timeout) as resp:
            latency_ms = int((time.perf_counter() - started) * 1000)
            return resp.status, resp.read(), latency_ms
    except error.HTTPError as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return exc.code, exc.read(), latency_ms


def _get_json_api_key(
    url: str,
    api_key: str,
    timeout: float,
    proxy_url: str = "",
) -> tuple[int, bytes, int]:
    req = request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "X-Client-Request-Id": _client_request_id(),
        },
    )
    started = time.perf_counter()
    opener = _build_opener(proxy_url)
    try:
        with opener.open(req, timeout=timeout) as resp:
            latency_ms = int((time.perf_counter() - started) * 1000)
            return resp.status, resp.read(), latency_ms
    except error.HTTPError as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return exc.code, exc.read(), latency_ms


def _post_form(
    url: str,
    body: dict[str, str],
    timeout: float,
    proxy_url: str = "",
) -> tuple[int, bytes, int]:
    payload = parse.urlencode(body).encode("utf-8")
    req = request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "X-Client-Request-Id": _client_request_id(),
        },
    )
    started = time.perf_counter()
    opener = _build_opener(proxy_url)
    try:
        with opener.open(req, timeout=timeout) as resp:
            latency_ms = int((time.perf_counter() - started) * 1000)
            return resp.status, resp.read(), latency_ms
    except error.HTTPError as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return exc.code, exc.read(), latency_ms


def _get_json(
    url: str,
    token: str,
    timeout: float,
    proxy_url: str = "",
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, bytes, int]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "X-Client-Request-Id": _client_request_id(),
    }
    if extra_headers:
        headers.update(extra_headers)
    req = request.Request(
        url,
        method="GET",
        headers=headers,
    )
    started = time.perf_counter()
    opener = _build_opener(proxy_url)
    try:
        with opener.open(req, timeout=timeout) as resp:
            latency_ms = int((time.perf_counter() - started) * 1000)
            return resp.status, resp.read(), latency_ms
    except error.HTTPError as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return exc.code, exc.read(), latency_ms


def _build_opener(proxy_url: str = ""):
    proxy_url = (proxy_url or "").strip()
    if not proxy_url:
        return request.build_opener(request.ProxyHandler({}))
    return request.build_opener(
        request.ProxyHandler(
            {
                "http": proxy_url,
                "https": proxy_url,
            }
        )
    )


def _refresh_openai_oauth(
    account: AccountRecord,
    credentials: dict[str, Any],
    timeout: float,
    model: str,
    proxy_url: str = "",
) -> tuple[str | None, CheckResult | None]:
    refresh_token = str(credentials.get("refresh_token") or "")
    client_id = str(credentials.get("client_id") or "")
    if not refresh_token or not client_id:
        return None, CheckResult(
            account=account,
            status="auth_invalid",
            ok=False,
            error_code="missing_refresh_fields",
            message="missing refresh_token or client_id",
            endpoint=OPENAI_OAUTH_TOKEN_URL,
            model=model,
        )

    try:
        refresh_status, refresh_payload, refresh_latency = _post_form(
            OPENAI_OAUTH_TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_token,
            },
            timeout=timeout,
            proxy_url=proxy_url,
        )
    except Exception as exc:
        return None, CheckResult(
            account=account,
            status=classify_failure(None, str(exc)),
            ok=False,
            error_code=exc.__class__.__name__,
            message=f"refresh_failed: {str(exc)[:450]}",
            endpoint=OPENAI_OAUTH_TOKEN_URL,
            model=model,
        )

    if not (200 <= refresh_status < 300):
        error_code, message = _extract_error(refresh_payload)
        return None, CheckResult(
            account=account,
            status=classify_failure(refresh_status, message, error_code),
            ok=False,
            http_status=refresh_status,
            latency_ms=refresh_latency,
            error_code=error_code or "refresh_failed",
            message=f"refresh_failed: {message}",
            endpoint=OPENAI_OAUTH_TOKEN_URL,
            model=model,
        )

    try:
        refresh_data = json.loads(refresh_payload.decode("utf-8"))
    except Exception as exc:
        return None, CheckResult(
            account=account,
            status="failed_unknown",
            ok=False,
            http_status=refresh_status,
            latency_ms=refresh_latency,
            error_code="refresh_parse_error",
            message=f"refresh response parse failed: {exc}",
            endpoint=OPENAI_OAUTH_TOKEN_URL,
            model=model,
        )

    new_access_token = refresh_data.get("access_token")
    if not new_access_token:
        return None, CheckResult(
            account=account,
            status="auth_invalid",
            ok=False,
            http_status=refresh_status,
            latency_ms=refresh_latency,
            error_code="refresh_missing_access_token",
            message="refresh response does not contain access_token",
            endpoint=OPENAI_OAUTH_TOKEN_URL,
            model=model,
        )

    credentials["access_token"] = new_access_token
    if refresh_data.get("refresh_token"):
        credentials["refresh_token"] = refresh_data["refresh_token"]
    if refresh_data.get("expires_in"):
        try:
            credentials["expires_in"] = int(refresh_data["expires_in"])
            credentials["expires_at"] = _now_epoch() + int(refresh_data["expires_in"])
        except Exception:
            pass

    return str(new_access_token), None


def _safe_claude_meta(account: AccountRecord) -> dict[str, Any]:
    credentials = account.raw.get("credentials") if isinstance(account.raw.get("credentials"), dict) else {}
    extra = account.raw.get("extra") if isinstance(account.raw.get("extra"), dict) else {}
    return {
        "email": str(credentials.get("email") or account.name or ""),
        "scope": str(credentials.get("scope") or extra.get("scope") or ""),
        "source_format": str(extra.get("source_format") or ""),
        "has_refresh_token": bool(credentials.get("refresh_token")),
        "has_access_token": bool(credentials.get("access_token")),
        "expires_at": credentials.get("expires_at") or "",
    }


def _refresh_claude_oauth(
    account: AccountRecord,
    credentials: dict[str, Any],
    timeout: float,
    model: str,
    proxy_url: str = "",
) -> tuple[str | None, CheckResult | None]:
    refresh_token = str(credentials.get("refresh_token") or "")
    if not refresh_token:
        return None, CheckResult(
            account=account,
            status="auth_invalid",
            ok=False,
            error_code="missing_refresh_fields",
            message="missing refresh_token",
            endpoint=CLAUDE_OAUTH_TOKEN_URL,
            model=model,
        )

    try:
        refresh_status, refresh_payload, refresh_latency = _post_json_no_auth(
            CLAUDE_OAUTH_TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": str(credentials.get("client_id") or CLAUDE_CLIENT_ID),
            },
            timeout=timeout,
            proxy_url=proxy_url,
            extra_headers={"User-Agent": "axios/1.13.6"},
        )
    except Exception as exc:
        return None, CheckResult(
            account=account,
            status=classify_failure(None, str(exc)),
            ok=False,
            error_code=exc.__class__.__name__,
            message=f"refresh_failed: {str(exc)[:450]}",
            endpoint=CLAUDE_OAUTH_TOKEN_URL,
            model=model,
        )

    if not (200 <= refresh_status < 300):
        error_code, message = _extract_error(refresh_payload)
        return None, CheckResult(
            account=account,
            status=classify_failure(refresh_status, message, error_code),
            ok=False,
            http_status=refresh_status,
            latency_ms=refresh_latency,
            error_code=error_code or "refresh_failed",
            message=f"refresh_failed: {message}",
            endpoint=CLAUDE_OAUTH_TOKEN_URL,
            model=model,
        )

    try:
        refresh_data = json.loads(refresh_payload.decode("utf-8"))
    except Exception as exc:
        return None, CheckResult(
            account=account,
            status="failed_unknown",
            ok=False,
            http_status=refresh_status,
            latency_ms=refresh_latency,
            error_code="refresh_parse_error",
            message=f"refresh response parse failed: {exc}",
            endpoint=CLAUDE_OAUTH_TOKEN_URL,
            model=model,
        )

    new_access_token = refresh_data.get("access_token")
    if not new_access_token:
        return None, CheckResult(
            account=account,
            status="auth_invalid",
            ok=False,
            http_status=refresh_status,
            latency_ms=refresh_latency,
            error_code="refresh_missing_access_token",
            message="refresh response does not contain access_token",
            endpoint=CLAUDE_OAUTH_TOKEN_URL,
            model=model,
        )

    credentials["access_token"] = new_access_token
    credentials.setdefault("client_id", CLAUDE_CLIENT_ID)
    if refresh_data.get("refresh_token"):
        credentials["refresh_token"] = refresh_data["refresh_token"]
    if refresh_data.get("scope"):
        credentials["scope"] = refresh_data["scope"]
    if refresh_data.get("token_type"):
        credentials["token_type"] = refresh_data["token_type"]
    if refresh_data.get("expires_in"):
        try:
            credentials["expires_in"] = int(refresh_data["expires_in"])
            credentials["expires_at"] = _now_epoch() + int(refresh_data["expires_in"])
        except Exception:
            pass

    return str(new_access_token), None


def _claude_session_user_id() -> str:
    return json.dumps(
        {
            "device_id": secrets.token_hex(32),
            "account_uuid": "",
            "session_id": str(uuid.uuid4()),
        },
        separators=(",", ":"),
    )


def _probe_claude_endpoint(
    endpoint: str,
    token: str,
    timeout: float,
    model: str,
    proxy_url: str = "",
) -> tuple[int, bytes, int]:
    url = endpoint.rstrip("/") if endpoint else CLAUDE_MESSAGES_URL
    if url == SUB2API_OAUTH_COMPAT_URL:
        url = CLAUDE_MESSAGES_URL
    body = {
        "model": model or CLAUDE_DEFAULT_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "hi",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ],
        "system": [
            {
                "type": "text",
                "text": CLAUDE_CODE_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "metadata": {"user_id": _claude_session_user_id()},
        "max_tokens": 1,
        "temperature": 1,
        "stream": True,
    }
    headers = {
        "Accept": "text/event-stream",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": CLAUDE_BETA_HEADER,
        "User-Agent": CLAUDE_CODE_USER_AGENT,
        "X-Stainless-Lang": "js",
        "X-Stainless-Package-Version": "0.94.0",
        "X-Stainless-OS": "Linux",
        "X-Stainless-Arch": "arm64",
        "X-Stainless-Runtime": "node",
        "X-Stainless-Runtime-Version": "v24.3.0",
        "X-Stainless-Retry-Count": "0",
        "X-Stainless-Timeout": "600",
        "X-App": "cli",
        "Anthropic-Dangerous-Direct-Browser-Access": "true",
    }
    return _post_json(url, token, body, timeout, proxy_url, headers)


def _account_base_url(account: AccountRecord) -> str:
    credentials = account.raw.get("credentials") if isinstance(account.raw.get("credentials"), dict) else {}
    return str(credentials.get("base_url") or OPENAI_API_BASE).rstrip("/")


def _api_key_endpoint(account: AccountRecord, endpoint: str, fallback_path: str) -> str:
    if endpoint and endpoint.startswith(("http://", "https://")):
        return endpoint.rstrip("/")
    base_url = _account_base_url(account)
    path = endpoint if endpoint else fallback_path
    if not path.startswith("/"):
        path = "/" + path
    return base_url + path


def _extract_first_model(payload: bytes) -> str:
    try:
        data = json.loads(payload.decode("utf-8", errors="replace"))
    except Exception:
        return ""
    models = data.get("data") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return ""
    preferred = []
    fallback = []
    for item in models:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            continue
        fallback.append(model_id)
        if any(token in model_id.lower() for token in ["gpt", "claude", "gemini", "deepseek", "qwen"]):
            preferred.append(model_id)
    return (preferred or fallback or [""])[0]


def _probe_api_key_endpoint(
    account: AccountRecord,
    endpoint: str,
    api_key: str,
    timeout: float,
    model: str,
    proxy_url: str = "",
) -> tuple[int, bytes, int]:
    url = _api_key_endpoint(account, endpoint, "/v1/models")
    if url.endswith("/v1/models"):
        return _get_json_api_key(url, api_key, timeout, proxy_url)

    body = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "stream": False,
    }
    return _post_json(url, api_key, body, timeout, proxy_url)


def _check_openai_api_key_sync(
    account: AccountRecord,
    timeout: float,
    endpoint: str,
    model: str,
    proxy_url: str = "",
) -> CheckResult:
    credentials = account.raw.get("credentials") or {}
    api_key = str(credentials.get("api_key") or "")
    if not api_key:
        return CheckResult(
            account=account,
            status="unsupported",
            ok=False,
            error_code="missing_api_key",
            message="unsupported account shape: missing api_key",
            endpoint=endpoint,
            model=model,
        )

    try:
        http_status, payload, latency_ms = _probe_api_key_endpoint(account, endpoint, api_key, timeout, model, proxy_url)
    except Exception as exc:
        return CheckResult(
            account=account,
            status=classify_failure(None, str(exc)),
            ok=False,
            error_code=exc.__class__.__name__,
            message=str(exc)[:500],
            endpoint=endpoint,
            model=model,
            raw_meta={"base_url": _account_base_url(account)},
        )

    if 200 <= http_status < 300:
        final_model = model
        raw_meta = {"base_url": _account_base_url(account)}
        if _api_key_endpoint(account, endpoint, "/v1/models").endswith("/v1/models"):
            model_from_list = _extract_first_model(payload)
            if model_from_list:
                raw_meta["sample_model"] = model_from_list
        return CheckResult(
            account=account,
            status="ok",
            ok=True,
            http_status=http_status,
            latency_ms=latency_ms,
            message="ok",
            endpoint=_api_key_endpoint(account, endpoint, "/v1/models"),
            model=final_model,
            raw_meta=raw_meta,
        )

    error_code, message = _extract_error(payload)
    status = classify_failure(http_status, message, error_code)
    return CheckResult(
        account=account,
        status=status,
        ok=False,
        http_status=http_status,
        latency_ms=latency_ms,
        error_code=error_code,
        message=message,
        endpoint=_api_key_endpoint(account, endpoint, "/v1/models"),
        model=model,
        raw_meta={"base_url": _account_base_url(account)},
    )


def _probe_openai_endpoint(
    endpoint: str,
    token: str,
    timeout: float,
    model: str,
    proxy_url: str = "",
    account_id: str = "",
) -> tuple[int, bytes, int]:
    url = endpoint.rstrip("/")
    if url == SUB2API_OAUTH_COMPAT_URL:
        return _get_json(CHATGPT_ME_URL, token, timeout, proxy_url)
    if url.endswith("/v1/models") or url.endswith("/userinfo") or url.endswith("/backend-api/me"):
        return _get_json(url, token, timeout, proxy_url)

    is_codex_backend = "chatgpt.com/backend-api/codex/" in url
    if is_codex_backend:
        body = {
            "model": model,
            "instructions": "Health check only. Reply with ok.",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hi"}],
                }
            ],
            "store": False,
            "stream": True,
        }
        headers = {
            "Accept": "text/event-stream",
            "Originator": CODEX_ORIGINATOR,
            "User-Agent": CODEX_USER_AGENT,
        }
        if account_id:
            headers["Chatgpt-Account-Id"] = account_id
    else:
        body = {
            "model": model,
            "input": "healthcheck",
            "max_output_tokens": 1,
        }
        headers = None
    return _post_json(url, token, body, timeout, proxy_url, headers)


def _check_claude_oauth_sync(
    account: AccountRecord,
    timeout: float,
    endpoint: str,
    model: str,
    local_expiry_guard_sec: int,
    refresh: bool,
    proxy_url: str = "",
) -> CheckResult:
    credentials = account.raw.get("credentials") or {}
    token = str(credentials.get("access_token") or "")
    expires_at = credentials.get("expires_at")
    refresh_token = str(credentials.get("refresh_token") or "")
    check_model = model if model and model != "gpt-4.1-nano" else CLAUDE_DEFAULT_MODEL

    if not token and not refresh_token:
        return CheckResult(
            account=account,
            status="unsupported",
            ok=False,
            error_code="missing_oauth_token",
            message="unsupported account shape: missing access_token and refresh_token",
            endpoint=endpoint,
            model=check_model,
        )

    try:
        expires_epoch = int(expires_at) if expires_at is not None else None
    except Exception:
        expires_epoch = None
    needs_refresh = refresh and (
        not token or (expires_epoch is not None and expires_epoch <= _now_epoch() + local_expiry_guard_sec)
    )

    if needs_refresh:
        new_token, refresh_error = _refresh_claude_oauth(account, credentials, timeout, check_model, proxy_url)
        if refresh_error:
            refresh_error.endpoint = endpoint
            return refresh_error
        token = str(new_token or token)
    elif expires_epoch and expires_epoch <= _now_epoch() + local_expiry_guard_sec:
        return CheckResult(
            account=account,
            status="expired_locally",
            ok=False,
            error_code="expired_locally",
            message="access_token expires_at is already expired or too close",
            endpoint=endpoint,
            model=check_model,
        )

    try:
        http_status, payload, latency_ms = _probe_claude_endpoint(endpoint, token, timeout, check_model, proxy_url)
    except Exception as exc:
        return CheckResult(
            account=account,
            status=classify_failure(None, str(exc)),
            ok=False,
            error_code=exc.__class__.__name__,
            message=str(exc)[:500],
            endpoint=endpoint,
            model=check_model,
        )

    attempts = 1
    if refresh and http_status == 401 and refresh_token:
        new_token, refresh_error = _refresh_claude_oauth(account, credentials, timeout, check_model, proxy_url)
        attempts = 2
        if refresh_error:
            refresh_error.attempts = attempts
            return refresh_error
        try:
            http_status, payload, latency_ms = _probe_claude_endpoint(
                endpoint,
                str(new_token or token),
                timeout,
                check_model,
                proxy_url,
            )
        except Exception as exc:
            return CheckResult(
                account=account,
                status=classify_failure(None, str(exc)),
                ok=False,
                error_code=exc.__class__.__name__,
                message=str(exc)[:500],
                endpoint=endpoint,
                model=check_model,
                attempts=attempts,
            )

    if 200 <= http_status < 300:
        return CheckResult(
            account=account,
            status="ok",
            ok=True,
            http_status=http_status,
            latency_ms=latency_ms,
            message="ok",
            endpoint=endpoint,
            model=check_model,
            attempts=attempts,
            raw_meta=_safe_claude_meta(account),
        )

    error_code, message = _extract_error(payload)
    status = classify_failure(http_status, message, error_code)
    return CheckResult(
        account=account,
        status=status,
        ok=False,
        http_status=http_status,
        latency_ms=latency_ms,
        error_code=error_code,
        message=message,
        endpoint=endpoint,
        model=check_model,
        attempts=attempts,
        raw_meta=_safe_claude_meta(account),
    )


def _check_openai_oauth_sync(
    account: AccountRecord,
    timeout: float,
    endpoint: str,
    model: str,
    local_expiry_guard_sec: int,
    refresh: bool,
    proxy_url: str = "",
) -> CheckResult:
    credentials = account.raw.get("credentials") or {}
    token = str(credentials.get("access_token") or "")
    expires_at = credentials.get("expires_at")
    refresh_token = str(credentials.get("refresh_token") or "")

    if not token and not refresh_token:
        return CheckResult(
            account=account,
            status="unsupported",
            ok=False,
            error_code="missing_oauth_token",
            message="unsupported account shape: missing access_token and refresh_token",
            endpoint=endpoint,
            model=model,
        )

    try:
        expires_epoch = int(expires_at) if expires_at is not None else None
    except Exception:
        expires_epoch = None
    needs_refresh = refresh and (
        not token or (expires_epoch is not None and expires_epoch <= _now_epoch() + local_expiry_guard_sec)
    )

    if needs_refresh:
        new_token, refresh_error = _refresh_openai_oauth(account, credentials, timeout, model, proxy_url)
        if refresh_error:
            refresh_error.endpoint = endpoint
            return refresh_error
        token = str(new_token or token)

    elif expires_epoch and expires_epoch <= _now_epoch() + local_expiry_guard_sec:
        return CheckResult(
            account=account,
            status="expired_locally",
            ok=False,
            error_code="expired_locally",
            message="access_token expires_at is already expired or too close",
            endpoint=endpoint,
            model=model,
        )

    try:
        account_id = _account_id_from_claims_or_credentials(account, token)
        http_status, payload, latency_ms = _probe_openai_endpoint(endpoint, token, timeout, model, proxy_url, account_id)
    except Exception as exc:
        return CheckResult(
            account=account,
            status=classify_failure(None, str(exc)),
            ok=False,
            error_code=exc.__class__.__name__,
            message=str(exc)[:500],
            endpoint=endpoint,
            model=model,
        )

    attempts = 1
    if refresh and http_status == 401 and refresh_token:
        new_token, refresh_error = _refresh_openai_oauth(account, credentials, timeout, model, proxy_url)
        attempts = 2
        if refresh_error:
            refresh_error.attempts = attempts
            return refresh_error
        try:
            account_id = _account_id_from_claims_or_credentials(account, str(new_token or token))
            http_status, payload, latency_ms = _probe_openai_endpoint(
                endpoint,
                str(new_token or token),
                timeout,
                model,
                proxy_url,
                account_id,
            )
        except Exception as exc:
            return CheckResult(
                account=account,
                status=classify_failure(None, str(exc)),
                ok=False,
                error_code=exc.__class__.__name__,
                message=str(exc)[:500],
                endpoint=endpoint,
                model=model,
                attempts=attempts,
            )

    if 200 <= http_status < 300:
        ok_status = "ok"
        if endpoint.rstrip("/").endswith("/backend-api/me"):
            ok_status = "codex_login_only"
        elif endpoint.rstrip("/") == SUB2API_OAUTH_COMPAT_URL:
            ok_status = "sub2api_compatible"
        return CheckResult(
            account=account,
            status=ok_status,
            ok=True,
            http_status=http_status,
            latency_ms=latency_ms,
            message="ok",
            endpoint=endpoint,
            model=model,
            attempts=attempts,
            raw_meta=_safe_token_meta(account, token),
        )

    error_code, message = _extract_error(payload)
    status = classify_failure(http_status, message, error_code)
    login_probe_ok = endpoint.rstrip("/").endswith("/v1/models") and status == "permission_or_scope_missing"
    if login_probe_ok:
        status = "codex_login_only"
    return CheckResult(
        account=account,
        status=status,
        ok=login_probe_ok,
        http_status=http_status,
        latency_ms=latency_ms,
        error_code=error_code,
        message=message,
        endpoint=endpoint,
        model=model,
        attempts=attempts,
        raw_meta=_safe_token_meta(account, token),
    )


async def check_account(
    account: AccountRecord,
    timeout: float,
    endpoint: str,
    model: str,
    local_expiry_guard_sec: int,
    refresh: bool,
    proxy_url: str = "",
) -> CheckResult:
    if account.platform.lower() == "openai" and account.account_type.lower() == "oauth":
        return await asyncio.to_thread(
            _check_openai_oauth_sync,
            account,
            timeout,
            endpoint,
            model,
            local_expiry_guard_sec,
            refresh,
            proxy_url,
        )
    if account.platform.lower() == "openai" and account.account_type.lower() in {"apikey", "api_key"}:
        return await asyncio.to_thread(
            _check_openai_api_key_sync,
            account,
            timeout,
            endpoint,
            model,
            proxy_url,
        )
    if account.platform.lower() == "anthropic" and account.account_type.lower() == "oauth":
        return await asyncio.to_thread(
            _check_claude_oauth_sync,
            account,
            timeout,
            endpoint,
            model,
            local_expiry_guard_sec,
            refresh,
            proxy_url,
        )

    return CheckResult(
        account=account,
        status="unsupported",
        ok=False,
        message=f"unsupported account platform/type: {account.platform}/{account.account_type}",
        endpoint=endpoint,
        model=model,
    )


async def check_many(
    accounts: list[AccountRecord],
    concurrency: int,
    timeout: float,
    endpoint: str,
    model: str,
    local_expiry_guard_sec: int,
    refresh: bool,
    proxy_url: str = "",
    progress: bool = True,
    progress_callback: Callable[[int, int, CheckResult], None] | None = None,
) -> list[CheckResult]:
    sem = asyncio.Semaphore(concurrency)
    results: list[CheckResult] = []
    total = len(accounts)
    done = 0

    async def one(account: AccountRecord) -> CheckResult:
        nonlocal done
        async with sem:
            result = await check_account(account, timeout, endpoint, model, local_expiry_guard_sec, refresh, proxy_url)
            done += 1
            if progress_callback:
                progress_callback(done, total, result)
            if progress:
                print(
                    f"[{done}/{total}] {result.status} "
                    f"name={account.name} platform={account.platform}/{account.account_type} "
                    f"http={result.http_status or ''} ms={result.latency_ms or ''}"
                )
            return result

    tasks = [asyncio.create_task(one(account)) for account in accounts]
    for task in asyncio.as_completed(tasks):
        results.append(await task)
    return results
