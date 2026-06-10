import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def sql_string(value):
    return "'" + str(value).replace("'", "''") + "'"


def stable_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_epoch(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        epoch = int(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        if not text.isdigit():
            return None
        epoch = int(text)
    if epoch > 9999999999:
        epoch = int(epoch / 1000)
    if epoch <= 0:
        return None
    return epoch


def normalize_iso(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def expires_sql(account):
    account_epoch = normalize_epoch(account.get("expires_at"))
    if account_epoch is not None:
        return "to_timestamp({0})".format(account_epoch)

    credentials = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
    credentials_epoch = normalize_epoch(credentials.get("expires_at"))
    if credentials_epoch is not None:
        return "to_timestamp({0})".format(credentials_epoch)

    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    extra_iso = normalize_iso(extra.get("expires_at_iso"))
    if extra_iso is not None:
        return "{0}::timestamptz".format(sql_string(extra_iso))

    return "NULL"


def account_name(account, index):
    name = str(account.get("name") or "").strip()
    if name:
        return name
    return "imported-account-{0}".format(index)


def normalize_account_type(account):
    platform = str(account.get("platform") or "openai")
    account_type = str(account.get("type") or "oauth")
    if platform.lower() == "openai" and account_type.lower() == "api_key":
        return "apikey"
    return account_type


def load_accounts(bundle_path):
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        raise ValueError("bundle missing accounts list")
    normalized = []
    for index, account in enumerate(accounts, start=1):
        if not isinstance(account, dict):
            continue
        normalized.append(account)
    return normalized


def build_insert_sql(account, import_tag, index):
    credentials = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
    extra = dict(account.get("extra")) if isinstance(account.get("extra"), dict) else {}
    extra.setdefault("cloud_import_tag", import_tag)
    extra.setdefault("cloud_import_source", "local_sql_generator")

    values = {
        "name": account_name(account, index),
        "platform": str(account.get("platform") or "openai"),
        "type": normalize_account_type(account),
        "credentials": stable_json(credentials),
        "extra": stable_json(extra),
        "concurrency": int(account.get("concurrency") or 1),
        "priority": int(account.get("priority") or 50),
        "status": str(account.get("status") or "active"),
        "schedulable": "true" if bool(account.get("schedulable", True)) else "false",
        "expires_at_sql": expires_sql(account),
        "auto_pause_on_expired": "true" if bool(account.get("auto_pause_on_expired", True)) else "false",
        "rate_multiplier": str(account.get("rate_multiplier") or 1),
    }

    return """WITH inserted AS (
  INSERT INTO accounts (
    name,
    platform,
    type,
    credentials,
    extra,
    concurrency,
    priority,
    status,
    schedulable,
    expires_at,
    auto_pause_on_expired,
    rate_multiplier
  )
  SELECT
    {name},
    {platform},
    {type},
    {credentials}::jsonb,
    {extra}::jsonb,
    {concurrency},
    {priority},
    {status},
    {schedulable},
    {expires_at_sql},
    {auto_pause_on_expired},
    {rate_multiplier}::numeric
  WHERE NOT EXISTS (
    SELECT 1
    FROM accounts
    WHERE deleted_at IS NULL
      AND name = {name}
  )
  RETURNING id, name
)
SELECT coalesce((SELECT 'inserted|' || id::text || '|' || name FROM inserted), 'skipped|' || {name});""".format(
        name=sql_string(values["name"]),
        platform=sql_string(values["platform"]),
        type=sql_string(values["type"]),
        credentials=sql_string(values["credentials"]),
        extra=sql_string(values["extra"]),
        concurrency=values["concurrency"],
        priority=values["priority"],
        status=sql_string(values["status"]),
        schedulable=values["schedulable"],
        expires_at_sql=values["expires_at_sql"],
        auto_pause_on_expired=values["auto_pause_on_expired"],
        rate_multiplier=values["rate_multiplier"],
    )


def build_sql(bundle_path, accounts, import_tag):
    lines = []
    lines.append("-- generated_from={0}".format(bundle_path))
    lines.append("-- import_tag={0}".format(import_tag))
    lines.append("BEGIN;")
    for index, account in enumerate(accounts, start=1):
        lines.append(build_insert_sql(account, import_tag, index))
    names = [sql_string(account_name(account, index)) for index, account in enumerate(accounts, start=1)]
    names_sql = ", ".join(names) if names else "NULL"
    lines.append(
        """SELECT 'summary|existing_or_inserted|' || count(*)
FROM accounts
WHERE deleted_at IS NULL
  AND name IN ({0});""".format(names_sql)
    )
    lines.append("COMMIT;")
    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Generate cloud-safe SQL import script for a Sub2API good bundle.")
    parser.add_argument("bundle", type=Path, help="Path to the good bundle JSON.")
    parser.add_argument("--output", type=Path, required=True, help="Where to write the generated SQL.")
    parser.add_argument("--import-tag", default="", help="Optional import tag written into account.extra.")
    args = parser.parse_args()

    accounts = load_accounts(args.bundle)
    import_tag = args.import_tag.strip() or args.output.stem
    sql_text = build_sql(str(args.bundle), accounts, import_tag)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(sql_text, encoding="utf-8", newline="\n")
    print("bundle={0}".format(args.bundle))
    print("accounts={0}".format(len(accounts)))
    print("output={0}".format(args.output))
    print("import_tag={0}".format(import_tag))


if __name__ == "__main__":
    main()
