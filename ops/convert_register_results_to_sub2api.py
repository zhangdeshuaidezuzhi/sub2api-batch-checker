#!/usr/bin/env python3
"""Convert register-machine result records into a Sub2API import bundle.

This is the fallback path for the local registration console on
http://127.0.0.1:18766/.  The console normally converts and uploads accounts
itself, but transient network failures can leave valid local records unimported.

The generated bundle can be passed directly to import_sub2api_good_bundle.py.
Secrets are written only to the output JSON; command output is counts/paths only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SUMMARY_PREFIXES = ("汇总_", "原始汇总_")
GENERATED_PREFIXES = ("sub2api_",)
DEFAULT_RESULTS_DIR = Path(r"D:\注册机最新版\results")
DEFAULT_OUTPUT_DIR = Path(r"D:\注册机最新版\sub2JOSN出售")


def clean_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value).strip()


def first_non_empty(*values: Any) -> str:
    for value in values:
        text = clean_str(value)
        if text:
            return text
    return ""


def decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = clean_str(token).split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def iso_from_unix_seconds(value: Any) -> str:
    try:
        numeric = float(value)
    except Exception:
        return ""
    if numeric <= 0:
        return ""
    try:
        return datetime.fromtimestamp(numeric, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


def normalize_timestamp(value: Any) -> str:
    text = clean_str(value)
    if not text:
        return ""
    if re.fullmatch(r"\d+(\.\d+)?", text):
        numeric = float(text)
        return iso_from_unix_seconds(numeric / 1000 if numeric > 1e11 else numeric)
    try:
        text = text.replace(" ", "T")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00") if text.endswith("Z") else text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


def strip_empty(value: Any) -> Any:
    if isinstance(value, dict):
        result = {key: strip_empty(item) for key, item in value.items()}
        return {key: item for key, item in result.items() if item not in ("", None, [], {})}
    if isinstance(value, list):
        result = [strip_empty(item) for item in value]
        return [item for item in result if item not in ("", None, [], {})]
    return value


def email_key(email: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", clean_str(email).lower()).strip("_")


def record_source_key(record: dict[str, Any]) -> str:
    access_token = clean_str(record.get("access_token"))
    if access_token:
        return "access_sha256:" + hashlib.sha256(access_token.encode("utf-8")).hexdigest()
    for key in ("bind_email", "email", "phone"):
        value = clean_str(record.get(key)).lower()
        if value:
            return key + ":" + value
    return "source:" + clean_str(record.get("_source_file"))


def record_score(record: dict[str, Any]) -> tuple[int, float]:
    score = 0
    if clean_str(record.get("id_token")):
        score += 100
    if clean_str(record.get("refresh_token")):
        score += 50
    if record.get("cpa_ready"):
        score += 30
    if record.get("phase2_ok"):
        score += 20
    if clean_str(record.get("bind_email") or record.get("email")):
        score += 10
    try:
        mtime = float(record.get("_source_mtime") or 0)
    except Exception:
        mtime = 0
    return score, mtime


def iter_raw_records(results_dir: Path):
    for path in sorted(results_dir.rglob("*.json")):
        if path.name.startswith(SUMMARY_PREFIXES) or path.name.startswith(GENERATED_PREFIXES) or path.name == "_all.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            yield None, path, "json_parse_error:{0}".format(exc.__class__.__name__)
            continue
        rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            item["_source_file"] = str(path)
            try:
                item["_source_mtime"] = path.stat().st_mtime
            except Exception:
                item["_source_mtime"] = 0
            yield item, path, ""


def extract_claim_fields(access_payload: dict[str, Any], id_payload: dict[str, Any]) -> dict[str, str]:
    access_auth = access_payload.get("https://api.openai.com/auth") if isinstance(access_payload, dict) else {}
    id_auth = id_payload.get("https://api.openai.com/auth") if isinstance(id_payload, dict) else {}
    access_profile = access_payload.get("https://api.openai.com/profile") if isinstance(access_payload, dict) else {}
    access_auth = access_auth if isinstance(access_auth, dict) else {}
    id_auth = id_auth if isinstance(id_auth, dict) else {}
    access_profile = access_profile if isinstance(access_profile, dict) else {}
    return {
        "email": first_non_empty(access_profile.get("email"), access_payload.get("email"), id_payload.get("email")),
        "chatgpt_account_id": first_non_empty(access_auth.get("chatgpt_account_id"), id_auth.get("chatgpt_account_id")),
        "chatgpt_user_id": first_non_empty(
            access_auth.get("chatgpt_user_id"),
            id_auth.get("chatgpt_user_id"),
            access_auth.get("user_id"),
            id_auth.get("user_id"),
            access_payload.get("sub"),
            id_payload.get("sub"),
        ),
        "plan_type": first_non_empty(access_auth.get("chatgpt_plan_type"), id_auth.get("chatgpt_plan_type")),
    }


def default_org_id(access_payload: dict[str, Any], id_payload: dict[str, Any]) -> str:
    for payload in (id_payload, access_payload):
        auth = payload.get("https://api.openai.com/auth") if isinstance(payload, dict) else {}
        orgs = auth.get("organizations") if isinstance(auth, dict) else []
        if not isinstance(orgs, list):
            continue
        default = next((org for org in orgs if isinstance(org, dict) and org.get("is_default") and org.get("id")), None)
        first = default or next((org for org in orgs if isinstance(org, dict) and org.get("id")), None)
        if first:
            return clean_str(first.get("id"))
    return ""


def sanitize_filename(value: str) -> str:
    text = clean_str(value).lower()
    text = re.sub(r"\.[^.]+$", "", text)
    text = re.sub(r'[\\/:*?"<>|]+', "-", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "converted-account"


def output_name_for_account(account: dict[str, Any], index: int) -> str:
    credentials = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    base = first_non_empty(account.get("name"), extra.get("email"), credentials.get("email"), "account-{0}".format(index + 1))
    return "{0}.sub2api.json".format(sanitize_filename(base))


def single_account_document(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "proxies": [],
        "accounts": [account],
    }


def convert_record(record: dict[str, Any], require_id_token: bool = True) -> tuple[dict[str, Any] | None, str]:
    access_token = clean_str(record.get("access_token"))
    id_token = clean_str(record.get("id_token"))
    if not access_token:
        return None, "missing_access_token"
    if require_id_token and not id_token:
        return None, "missing_id_token"

    access_payload = decode_jwt_payload(access_token)
    id_payload = decode_jwt_payload(id_token)
    if not access_payload:
        return None, "invalid_access_token_jwt"
    if require_id_token and not id_payload:
        return None, "invalid_id_token_jwt"

    claims = extract_claim_fields(access_payload, id_payload)
    expires_at = first_non_empty(
        normalize_timestamp(record.get("expired")),
        normalize_timestamp(record.get("expires_at")),
        iso_from_unix_seconds(access_payload.get("exp")),
    )
    expires_in = ""
    if expires_at:
        try:
            expires_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            expires_in = str(max(0, int((expires_dt - datetime.now(timezone.utc)).total_seconds())))
        except Exception:
            expires_in = ""

    email = first_non_empty(record.get("bind_email"), record.get("email"), claims.get("email"), record.get("phone"))
    plan_type = first_non_empty(record.get("chatgpt_plan_type"), record.get("plan_type"), claims.get("plan_type"))
    account_id = first_non_empty(record.get("chatgpt_account_id"), record.get("account_id"), claims.get("chatgpt_account_id"))
    user_id = first_non_empty(record.get("chatgpt_user_id"), record.get("chatgpt_auth_user_id"), claims.get("chatgpt_user_id"))

    credentials = strip_empty(
        {
            "access_token": access_token,
            "id_token": id_token,
            "refresh_token": clean_str(record.get("refresh_token")),
            "chatgpt_account_id": account_id,
            "chatgpt_user_id": user_id,
            "email": email,
            "expires_at": expires_at,
            "expires_in": int(expires_in) if expires_in else "",
            "organization_id": default_org_id(access_payload, id_payload),
            "plan_type": plan_type,
        }
    )
    extra = strip_empty(
        {
            "email": email,
            "email_key": email_key(email),
            "last_refresh": normalize_timestamp(record.get("last_refresh")),
            "source_file": clean_str(record.get("_source_file")),
            "source_batch": clean_str(record.get("source_batch")),
            "phone": clean_str(record.get("phone")),
            "access_only": bool(access_token and not id_token),
            "cpa_ready": bool(record.get("cpa_ready")),
            "cpa_missing_reason": clean_str(record.get("cpa_missing_reason")),
            "chatgpt_field_source": clean_str(record.get("chatgpt_field_source")),
            "chatgpt_field_checked_at": clean_str(record.get("chatgpt_field_checked_at")),
            "register_console_url": "http://127.0.0.1:18766/",
        }
    )
    return (
        strip_empty(
            {
                "name": email or clean_str(record.get("name")) or clean_str(record.get("phone")) or "converted-account",
                "platform": "openai",
                "type": "oauth",
                "concurrency": 10,
                "priority": 1,
                "credentials": credentials,
                "extra": extra,
            }
        ),
        "",
    )


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 10000):
        candidate = path.with_name("{0}-{1}{2}".format(path.stem, i, path.suffix))
        if not candidate.exists():
            return candidate
    raise RuntimeError("cannot allocate unique path for {0}".format(path))


def convert_results(results_dir: Path, output_dir: Path, require_id_token: bool = True, split: bool = True) -> dict[str, Any]:
    raw_records = []
    skipped = []
    parse_errors = 0
    for record, path, error in iter_raw_records(results_dir):
        if error:
            parse_errors += 1
            skipped.append({"source_file": str(path), "reason": error})
            continue
        if record is not None:
            raw_records.append(record)

    best: dict[str, dict[str, Any]] = {}
    for record in raw_records:
        key = record_source_key(record)
        old = best.get(key)
        if old is None or record_score(record) > record_score(old):
            best[key] = record

    accounts = []
    for record in sorted(best.values(), key=lambda item: clean_str(item.get("_source_file"))):
        account, reason = convert_record(record, require_id_token=require_id_token)
        if account:
            accounts.append(account)
        else:
            skipped.append(
                {
                    "source_file": clean_str(record.get("_source_file")),
                    "phone": clean_str(record.get("phone")),
                    "email": clean_str(record.get("bind_email") or record.get("email")),
                    "reason": reason,
                }
            )

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "proxies": [],
        "accounts": accounts,
    }
    output_path = output_dir / "sub2api_register_fallback_{0}_{1}.json".format(len(accounts), stamp)
    skipped_path = output_dir / "sub2api_register_fallback_skipped_{0}_{1}.json".format(len(skipped), stamp)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    skipped_path.write_text(json.dumps(skipped, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    split_dir = output_dir / "sub2api_register_fallback_split_{0}_{1}".format(len(accounts), stamp)
    split_count = 0
    if split:
        split_dir.mkdir(parents=True, exist_ok=True)
        for index, account in enumerate(accounts):
            single_path = unique_path(split_dir / output_name_for_account(account, index))
            single_path.write_text(json.dumps(single_account_document(account), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            split_count += 1

    return {
        "raw_records": len(raw_records),
        "deduped_records": len(best),
        "accounts": len(accounts),
        "skipped": len(skipped),
        "parse_errors": parse_errors,
        "output_path": str(output_path),
        "skipped_path": str(skipped_path),
        "split_dir": str(split_dir) if split else "",
        "split_count": split_count,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert register-machine results to Sub2API bundle.")
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--allow-missing-id-token", action="store_true", help="Not recommended for OpenAI/Codex.")
    parser.add_argument("--no-split", action="store_true", help="Only write merged bundle.")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    summary = convert_results(
        args.results_dir,
        args.output_dir,
        require_id_token=not args.allow_missing_id_token,
        split=not args.no_split,
    )
    for key in ("raw_records", "deduped_records", "accounts", "skipped", "parse_errors", "output_path", "skipped_path", "split_dir", "split_count"):
        print("{0}={1}".format(key, summary[key]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
