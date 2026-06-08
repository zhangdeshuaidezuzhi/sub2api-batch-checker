from __future__ import annotations

from .models import HealthStatus


def classify_failure(http_status: int | None, message: str, error_code: str = "") -> HealthStatus:
    text = f"{error_code} {message}".lower()

    if http_status == 401 or any(
        token in text
        for token in [
            "unauthorized",
            "invalid_token",
            "invalid token",
            "expired",
            "oauth",
            "refresh",
            "app_session_terminated",
            "session terminated",
            "session has ended",
            "log in again",
            "login again",
            "sign in again",
            "authentication",
            "认证",
            "授权",
            "过期",
            "失效",
        ]
    ):
        return "auth_invalid"

    if http_status == 403 or any(
        token in text
        for token in [
            "forbidden",
            "suspended",
            "banned",
            "disabled",
            "not allowed",
            "封",
            "禁用",
            "风控",
        ]
    ):
        return "forbidden_or_banned"

    if http_status == 429 or any(
        token in text
        for token in [
            "rate limit",
            "rate_limit",
            "quota",
            "insufficient_quota",
            "usage limit",
            "billing",
            "too many requests",
            "额度",
            "余额",
            "用尽",
            "限流",
        ]
    ):
        return "quota_or_rate_limited"

    if any(
        token in text
        for token in [
            "timeout",
            "timed out",
            "proxy",
            "connect",
            "connection",
            "dns",
            "tls",
            "ssl",
            "eof occurred",
            "unexpected_eof",
            "network",
            "网络",
            "代理",
            "超时",
            "连接",
        ]
    ):
        return "network_or_proxy"

    return "failed_unknown"
