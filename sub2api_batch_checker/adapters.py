from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class AdaptedAccounts:
    accounts: list[dict[str, Any]]
    source_format: str


class AccountFormatAdapter(Protocol):
    source_format: str

    def match(self, data: Any) -> bool:
        ...

    def adapt(self, data: Any, source_name: str) -> AdaptedAccounts:
        ...


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _decode_jwt_claims(token: Any) -> dict[str, Any]:
    if not isinstance(token, str):
        return {}
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


def _nested_text(value: Any, *keys: str) -> str:
    current = value
    for key in keys:
        if isinstance(current, list):
            try:
                current = current[int(key)]
            except Exception:
                return ""
            continue
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return _first_text(current)


def _epoch_from_value(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number = number / 1000
        return int(number)
    try:
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit():
            number = int(text)
            return number // 1000 if number > 10_000_000_000 else number
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return int(datetime.fromisoformat(text).timestamp())
    except Exception:
        return None


def _iso_from_epoch(value: Any) -> str:
    epoch = _epoch_from_value(value)
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _strip_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "")}


def _copy_account_with_source(raw: dict[str, Any], source_format: str) -> dict[str, Any]:
    account = dict(raw)
    credentials = account.get("credentials")
    account["credentials"] = dict(credentials) if isinstance(credentials, dict) else {}

    extra = account.get("extra")
    extra = dict(extra) if isinstance(extra, dict) else {}
    extra.setdefault("source_format", source_format)
    account["extra"] = extra

    account.setdefault("platform", "openai")
    account.setdefault("type", "oauth")
    account.setdefault("concurrency", 1)
    account.setdefault("priority", 1)
    account.setdefault("rate_multiplier", 1)
    account.setdefault("auto_pause_on_expired", True)
    account.setdefault("group_ids", [])
    return account


def _looks_like_sub2api_account(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    credentials = value.get("credentials")
    return isinstance(credentials, dict) and bool(
        value.get("platform")
        or value.get("type")
        or credentials.get("access_token")
        or credentials.get("refresh_token")
        or credentials.get("id_token")
        or credentials.get("api_key")
    )


class Sub2ApiBundleAdapter:
    source_format = "sub2api_bundle"

    def match(self, data: Any) -> bool:
        return isinstance(data, dict) and isinstance(data.get("accounts"), list)

    def adapt(self, data: Any, source_name: str) -> AdaptedAccounts:
        raw_accounts = data.get("accounts") if isinstance(data, dict) else []
        accounts = [
            _copy_account_with_source(raw, self.source_format)
            for raw in raw_accounts
            if _looks_like_sub2api_account(raw)
        ]
        return AdaptedAccounts(accounts=accounts, source_format=self.source_format)


class Sub2ApiAccountListAdapter:
    source_format = "sub2api_account_list"

    def match(self, data: Any) -> bool:
        return isinstance(data, list) and any(_looks_like_sub2api_account(item) for item in data)

    def adapt(self, data: Any, source_name: str) -> AdaptedAccounts:
        raw_accounts = data if isinstance(data, list) else []
        accounts = [
            _copy_account_with_source(raw, self.source_format)
            for raw in raw_accounts
            if _looks_like_sub2api_account(raw)
        ]
        return AdaptedAccounts(accounts=accounts, source_format=self.source_format)


class CpaTokenFileAdapter:
    source_format = "cpa_token_json"

    def match(self, data: Any) -> bool:
        if not isinstance(data, dict):
            return False
        token = data.get("token") if isinstance(data.get("token"), dict) else {}
        return bool(
            data.get("access_token")
            or data.get("refresh_token")
            or data.get("rt")
            or token.get("access_token")
            or token.get("accessToken")
            or token.get("refresh_token")
            or token.get("refreshToken")
        )

    def adapt(self, data: Any, source_name: str) -> AdaptedAccounts:
        if not isinstance(data, dict):
            return AdaptedAccounts(accounts=[], source_format=self.source_format)

        return AdaptedAccounts(accounts=[self._adapt_one(data, source_name)], source_format=self.source_format)

    def _adapt_one(self, data: dict[str, Any], source_name: str) -> dict[str, Any]:
        source_type = _first_text(data.get("type")).lower()
        if not source_type and data.get("rt") and data.get("password") and not data.get("access_token"):
            source_type = "claude"
        if not source_type:
            source_type = "codex"
        if source_type in {"openai", "chatgpt"}:
            source_type = "codex"
        if source_type == "claude":
            platform = "anthropic"
        elif source_type == "antigravity":
            platform = "antigravity"
        elif source_type == "gemini":
            platform = "gemini"
        else:
            platform = "openai"
            source_type = "codex"

        oauth_start = data.get("oauth_start") if isinstance(data.get("oauth_start"), dict) else {}
        token = data.get("token") if isinstance(data.get("token"), dict) else {}
        access_token = _first_text(data.get("access_token"), token.get("access_token"), token.get("accessToken"))
        refresh_token = _first_text(data.get("refresh_token"), data.get("rt"), token.get("refresh_token"), token.get("refreshToken"))
        id_token = _first_text(data.get("id_token"), token.get("id_token"))
        access_claims = _decode_jwt_claims(access_token)
        id_claims = _decode_jwt_claims(id_token)
        profile = access_claims.get("https://api.openai.com/profile")
        profile = profile if isinstance(profile, dict) else {}

        email = _first_text(
            data.get("email"),
            token.get("email"),
            profile.get("email"),
            id_claims.get("email"),
            Path(source_name).stem,
        )
        account_id = _first_text(
            data.get("account_id"),
            data.get("chatgpt_account_id"),
            access_claims.get("https://api.openai.com/auth/chatgpt_account_id"),
            id_claims.get("https://api.openai.com/auth/chatgpt_account_id"),
        )
        expires_at = _epoch_from_value(
            _first_text(
                data.get("expired"),
                data.get("expires_at"),
                token.get("expiry"),
                token.get("expires_at"),
                token.get("expiration"),
                access_claims.get("exp"),
            )
        )

        credentials = _strip_empty(
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "client_id": _first_text(data.get("client_id"), oauth_start.get("client_id")),
                "chatgpt_account_id": account_id,
                "chatgpt_user_id": _first_text(
                    data.get("chatgpt_user_id"),
                    access_claims.get("https://api.openai.com/auth/chatgpt_user_id"),
                    id_claims.get("https://api.openai.com/auth/chatgpt_user_id"),
                ),
                "email": email,
                "id_token": id_token,
                "organization_id": _first_text(data.get("organization_id"), _nested_text(id_claims, "orgs", "0", "id")),
                "plan_type": _first_text(data.get("plan_type"), access_claims.get("https://api.openai.com/auth/plan_type")),
                "expires_at": expires_at,
                "expires_in": data.get("expires_in") if isinstance(data.get("expires_in"), (int, float)) else None,
                "project_id": _first_text(data.get("project_id"), token.get("project_id")),
                "scope": _first_text(data.get("scope"), oauth_start.get("scope"), token.get("scope")),
                "token_type": _first_text(data.get("token_type"), token.get("token_type"), token.get("tokenType")),
                "password": data.get("password") if platform == "anthropic" else "",
            }
        )

        account = {
            "name": email,
            "platform": platform,
            "type": "oauth",
            "credentials": credentials,
            "extra": _strip_empty(
                {
                    "source_format": self.source_format,
                    "source_type": source_type,
                    "last_refresh": data.get("last_refresh"),
                    "scope": _first_text(data.get("scope"), oauth_start.get("scope"), token.get("scope")),
                    "token_type": _first_text(data.get("token_type"), token.get("token_type"), token.get("tokenType")),
                    "recommended_check_mode": "sub2api-oauth" if platform == "openai" else "unsupported_by_checker",
                    "expires_at_iso": _iso_from_epoch(expires_at),
                }
            ),
            "concurrency": 1,
            "priority": 1,
            "rate_multiplier": 1,
            "auto_pause_on_expired": True,
            "group_ids": [],
        }
        return account


class CpaTokenListAdapter:
    source_format = "cpa_token_list_json"

    def match(self, data: Any) -> bool:
        return isinstance(data, list) and any(CpaTokenFileAdapter().match(item) for item in data)

    def adapt(self, data: Any, source_name: str) -> AdaptedAccounts:
        adapter = CpaTokenFileAdapter()
        accounts = [
            adapter._adapt_one(item, source_name)
            for item in data
            if isinstance(item, dict) and adapter.match(item)
        ]
        for account in accounts:
            account["extra"]["source_format"] = self.source_format
        return AdaptedAccounts(accounts=accounts, source_format=self.source_format)


class OpenAiApiKeyAdapter:
    source_format = "openai_api_key_json"

    def match(self, data: Any) -> bool:
        if not isinstance(data, dict):
            return False
        return bool(_first_text(data.get("api_key"), data.get("key")) and _first_text(data.get("base_url"), data.get("baseUrl"), data.get("url")))

    def adapt(self, data: Any, source_name: str) -> AdaptedAccounts:
        if not isinstance(data, dict):
            return AdaptedAccounts(accounts=[], source_format=self.source_format)

        base_url = _first_text(data.get("base_url"), data.get("baseUrl"), data.get("url")).rstrip("/")
        api_key = _first_text(data.get("api_key"), data.get("key"))
        name = _first_text(data.get("name"), data.get("email"), Path(source_name).stem)
        account = {
            "name": name,
            "platform": "openai",
            "type": "apikey",
            "credentials": _strip_empty(
                {
                    "api_key": api_key,
                    "base_url": base_url,
                    "model_mapping": data.get("model_mapping") if isinstance(data.get("model_mapping"), dict) else {},
                }
            ),
            "extra": _strip_empty(
                {
                    "source_format": self.source_format,
                    "recommended_check_mode": "api-login",
                }
            ),
            "concurrency": int(data.get("concurrency") or 1),
            "priority": int(data.get("priority") or 50),
            "rate_multiplier": data.get("rate_multiplier") or 1,
            "auto_pause_on_expired": True,
            "group_ids": [],
        }
        return AdaptedAccounts(accounts=[account], source_format=self.source_format)


ADAPTERS: tuple[AccountFormatAdapter, ...] = (
    Sub2ApiBundleAdapter(),
    Sub2ApiAccountListAdapter(),
    CpaTokenListAdapter(),
    CpaTokenFileAdapter(),
    OpenAiApiKeyAdapter(),
)


def describe_json_shape(data: Any) -> str:
    if isinstance(data, dict):
        keys = ",".join(sorted(str(key) for key in data.keys())[:20])
        return f"object_keys={keys}"
    if isinstance(data, list):
        return f"list_len={len(data)}"
    return f"type={type(data).__name__}"


def adapt_accounts(data: Any, source_name: str) -> tuple[list[dict[str, Any]], str | None]:
    for adapter in ADAPTERS:
        if adapter.match(data):
            adapted = adapter.adapt(data, source_name)
            if adapted.accounts:
                return adapted.accounts, None
            return [], f"empty_{adapted.source_format}"
    return [], f"unsupported_json_shape: {describe_json_shape(data)}"
