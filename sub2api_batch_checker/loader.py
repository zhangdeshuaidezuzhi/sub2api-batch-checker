from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import AccountRecord


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(account: dict[str, Any]) -> str:
    credentials = account.get("credentials") or {}
    basis = "|".join(
        [
            str(account.get("platform") or ""),
            str(account.get("type") or ""),
            str(credentials.get("chatgpt_account_id") or ""),
            str(credentials.get("refresh_token") or ""),
            str(credentials.get("access_token") or ""),
            str(account.get("name") or ""),
        ]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _epoch_from_iso(value: Any) -> int | None:
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return int(datetime.fromisoformat(text).timestamp())
    except Exception:
        return None


def _single_token_to_sub2api_account(data: dict[str, Any], file_path: Path) -> dict[str, Any] | None:
    credentials_keys = {"access_token", "refresh_token", "client_id"}
    if not credentials_keys.issubset(data.keys()):
        return None

    email = str(data.get("email") or file_path.stem)
    account_id = str(data.get("account_id") or data.get("chatgpt_account_id") or "")
    expires_at = _epoch_from_iso(data.get("expired") or data.get("expires_at"))

    credentials: dict[str, Any] = {
        "access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token"),
        "client_id": data.get("client_id"),
        "chatgpt_account_id": account_id,
        "organization_id": data.get("organization_id") or "",
        "plan_type": data.get("plan_type") or "",
    }
    if data.get("id_token"):
        credentials["id_token"] = data.get("id_token")
    if expires_at:
        credentials["expires_at"] = expires_at

    return {
        "name": email,
        "platform": "openai",
        "type": "oauth",
        "credentials": credentials,
        "extra": {
            "source_format": "single_token_json",
            "source_type": data.get("type") or "",
            "last_refresh": data.get("last_refresh") or "",
        },
        "concurrency": 1,
        "priority": 1,
        "rate_multiplier": 1,
        "auto_pause_on_expired": True,
        "group_ids": [],
    }


def _extract_raw_accounts(data: Any, file_path: Path) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(data, dict):
        return [], "unsupported_json_shape"

    raw_accounts = data.get("accounts")
    if isinstance(raw_accounts, list):
        return [raw for raw in raw_accounts if isinstance(raw, dict)], None

    single = _single_token_to_sub2api_account(data, file_path)
    if single:
        return [single], None

    return [], "unsupported_json_shape"


def load_sub2api_accounts(paths: list[Path], dedupe: bool = True) -> tuple[list[AccountRecord], list[str]]:
    accounts: list[AccountRecord] = []
    errors: list[str] = []
    seen: set[str] = set()

    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.rglob("*.json")))
        elif path.is_file():
            files.append(path)
        else:
            errors.append(f"missing: {path}")

    for file_path in files:
        try:
            data = json.loads(file_path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            errors.append(f"{file_path}: parse_error: {exc}")
            continue

        raw_accounts, shape_error = _extract_raw_accounts(data, file_path)
        if shape_error:
            errors.append(f"{file_path}: {shape_error}")
            continue

        for idx, raw in enumerate(raw_accounts):
            if not isinstance(raw, dict):
                continue
            fp = _fingerprint(raw)
            if dedupe and fp in seen:
                continue
            seen.add(fp)

            credentials = raw.get("credentials") or {}
            accounts.append(
                AccountRecord(
                    source_file=str(file_path),
                    index=idx,
                    raw=raw,
                    name=str(raw.get("name") or ""),
                    platform=str(raw.get("platform") or ""),
                    account_type=str(raw.get("type") or ""),
                    account_id=str(credentials.get("chatgpt_account_id") or raw.get("id") or ""),
                    fingerprint=fp,
                )
            )

    return accounts, errors


def write_sub2api_bundle(path: Path, accounts: list[AccountRecord]) -> None:
    bundle = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "proxies": [],
        "accounts": [account.raw for account in accounts],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_stable_json(bundle), encoding="utf-8")
