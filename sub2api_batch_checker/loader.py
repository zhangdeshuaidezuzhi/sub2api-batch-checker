from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapters import adapt_accounts
from .models import AccountRecord


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(account: dict[str, Any]) -> str:
    credentials = account.get("credentials") or {}
    platform = str(account.get("platform") or "")
    account_type = str(account.get("type") or "")
    api_key = str(credentials.get("api_key") or "")
    base_url = str(credentials.get("base_url") or "").strip().lower().rstrip("/")

    if account_type.lower() in {"apikey", "api_key"} and api_key and base_url:
        basis = "|".join([platform, "apikey", base_url, api_key])
    else:
        identity = ("empty", "")
        for label, value in (
            ("chatgpt_account_id", str(credentials.get("chatgpt_account_id") or "")),
            ("chatgpt_user_id", str(credentials.get("chatgpt_user_id") or "")),
            ("refresh_token", str(credentials.get("refresh_token") or "")),
            ("access_token", str(credentials.get("access_token") or "")),
            ("name", str(account.get("name") or "")),
        ):
            if value:
                identity = (label, value)
                break
        basis = "|".join([platform, account_type, identity[0], identity[1]])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _extract_raw_accounts(data: Any, source_name: str) -> tuple[list[dict[str, Any]], str | None]:
    return adapt_accounts(data, source_name)


def _iter_json_inputs(paths: list[Path]) -> tuple[list[tuple[str, str]], list[str]]:
    inputs: list[tuple[str, str]] = []
    errors: list[str] = []

    for path in paths:
        if path.is_dir():
            for file_path in sorted(path.rglob("*.json")):
                try:
                    inputs.append((str(file_path), file_path.read_text(encoding="utf-8-sig")))
                except Exception as exc:
                    errors.append(f"{file_path}: read_error: {exc}")
        elif path.is_file() and path.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(path) as archive:
                    for member in sorted(archive.namelist()):
                        member_path = Path(member)
                        if member_path.name.startswith(".") or member_path.suffix.lower() != ".json":
                            continue
                        try:
                            payload = archive.read(member)
                        except Exception as exc:
                            errors.append(f"{path}!{member}: read_error: {exc}")
                            continue
                        try:
                            text = payload.decode("utf-8-sig")
                        except UnicodeDecodeError:
                            text = payload.decode("utf-8", errors="replace")
                        inputs.append((f"{path}!{member}", text))
            except Exception as exc:
                errors.append(f"{path}: zip_error: {exc}")
        elif path.is_file():
            try:
                inputs.append((str(path), path.read_text(encoding="utf-8-sig")))
            except Exception as exc:
                errors.append(f"{path}: read_error: {exc}")
        else:
            errors.append(f"missing: {path}")

    return inputs, errors


def load_sub2api_accounts(paths: list[Path], dedupe: bool = True) -> tuple[list[AccountRecord], list[str]]:
    accounts: list[AccountRecord] = []
    inputs, errors = _iter_json_inputs(paths)
    seen: set[str] = set()

    for source_name, text in inputs:
        try:
            data = json.loads(text)
        except Exception as exc:
            errors.append(f"{source_name}: parse_error: {exc}")
            continue

        raw_accounts, shape_error = _extract_raw_accounts(data, source_name)
        if shape_error:
            errors.append(f"{source_name}: {shape_error}")
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
                    source_file=source_name,
                    index=idx,
                    raw=raw,
                    name=str(raw.get("name") or ""),
                    platform=str(raw.get("platform") or ""),
                    account_type=str(raw.get("type") or ""),
                    account_id=str(credentials.get("chatgpt_account_id") or credentials.get("base_url") or raw.get("id") or ""),
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
