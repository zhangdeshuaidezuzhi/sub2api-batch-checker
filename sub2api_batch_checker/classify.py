from __future__ import annotations

from .models import HealthStatus


def classify_failure(http_status: int | None, message: str, error_code: str = "") -> HealthStatus:
    text = f"{error_code} {message}".lower()

    if any(
        token in text
        for token in [
            "model is not supported",
            "model not supported",
            "模型不支持",
        ]
    ):
        return "model_unsupported"

    if any(
        token in text
        for token in [
            "insufficient permissions",
            "missing scopes",
            "missing scope",
            "correct role",
            "api.model.read",
            "api.responses",
            "permission",
            "permissions",
            "scope",
            "scopes",
            "role",
            "权限不足",
            "缺少权限",
            "范围",
            "角色",
        ]
    ):
        return "permission_or_scope_missing"

    if any(
        token in text
        for token in [
            "instructions are required",
            "missing required parameter",
            "missing required field",
            "invalid request body",
            "invalid request format",
            "request body",
            "stream must be set to true",
            "unsupported parameter",
            "请求格式",
            "请求体",
            "缺少必填",
        ]
    ):
        return "request_shape_error"

    if http_status == 429 or any(
        token in text
        for token in [
            "rate limit",
            "rate_limit",
            "quota",
            "insufficient_quota",
            "usage limit",
            "weekly limit",
            "weekly quota",
            "week limit",
            "billing",
            "too many requests",
            "额度",
            "余额",
            "用尽",
            "限流",
            "周限额",
            "本周限额",
        ]
    ):
        return "quota_or_rate_limited"

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
