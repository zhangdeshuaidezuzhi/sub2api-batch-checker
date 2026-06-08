from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib import parse
from urllib import error, request

from .classify import classify_failure
from .models import AccountRecord, CheckResult


OPENAI_API_BASE = "https://api.openai.com"
OPENAI_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"


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


def _post_json(
    url: str,
    token: str,
    body: dict[str, Any],
    timeout: float,
    proxy_url: str = "",
) -> tuple[int, bytes, int]:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "sub2api-batch-checker/0.1",
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
            "User-Agent": "sub2api-batch-checker/0.1",
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
) -> tuple[int, bytes, int]:
    req = request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "sub2api-batch-checker/0.1",
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


def _probe_openai_endpoint(
    endpoint: str,
    token: str,
    timeout: float,
    model: str,
    proxy_url: str = "",
) -> tuple[int, bytes, int]:
    url = endpoint.rstrip("/")
    if url.endswith("/v1/models"):
        return _get_json(url, token, timeout, proxy_url)

    body = {
        "model": model,
        "input": "healthcheck",
        "max_output_tokens": 1,
    }
    return _post_json(url, token, body, timeout, proxy_url)


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
            status="auth_invalid",
            ok=False,
            error_code="missing_oauth_token",
            message="missing access_token and refresh_token",
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
        http_status, payload, latency_ms = _probe_openai_endpoint(endpoint, token, timeout, model, proxy_url)
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
            http_status, payload, latency_ms = _probe_openai_endpoint(
                endpoint,
                str(new_token or token),
                timeout,
                model,
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
                model=model,
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
            model=model,
            attempts=attempts,
        )

    error_code, message = _extract_error(payload)
    return CheckResult(
        account=account,
        status=classify_failure(http_status, message, error_code),
        ok=False,
        http_status=http_status,
        latency_ms=latency_ms,
        error_code=error_code,
        message=message,
        endpoint=endpoint,
        model=model,
        attempts=attempts,
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
