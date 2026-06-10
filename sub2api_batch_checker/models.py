from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal


HealthStatus = Literal[
    "ok",
    "codex_login_only",
    "sub2api_compatible",
    "model_unsupported",
    "quota_or_rate_limited",
    "permission_or_scope_missing",
    "request_shape_error",
    "forbidden_or_banned",
    "auth_invalid",
    "expired_locally",
    "network_or_proxy",
    "unsupported",
    "failed_unknown",
]


@dataclass(slots=True)
class AccountRecord:
    source_file: str
    index: int
    raw: dict[str, Any]
    name: str = ""
    platform: str = ""
    account_type: str = ""
    account_id: str = ""
    fingerprint: str = ""


@dataclass(slots=True)
class CheckResult:
    account: AccountRecord
    status: HealthStatus
    ok: bool
    http_status: int | None = None
    latency_ms: int | None = None
    error_code: str = ""
    message: str = ""
    model: str = ""
    endpoint: str = ""
    attempts: int = 1
    raw_meta: dict[str, Any] = field(default_factory=dict)

    def to_csv_row(self) -> dict[str, Any]:
        extra = self.account.raw.get("extra") if isinstance(self.account.raw.get("extra"), dict) else {}
        return {
            "ok": self.ok,
            "status": self.status,
            "name": self.account.name,
            "platform": self.account.platform,
            "type": self.account.account_type,
            "source_format": str(extra.get("source_format") or ""),
            "account_id": self.account.account_id,
            "http_status": self.http_status,
            "latency_ms": self.latency_ms,
            "error_code": self.error_code,
            "message": self.message,
            "model": self.model,
            "endpoint": self.endpoint,
            "attempts": self.attempts,
            "raw_meta": json.dumps(self.raw_meta, ensure_ascii=False, sort_keys=True),
            "source_file": self.account.source_file,
            "fingerprint": self.account.fingerprint,
        }
