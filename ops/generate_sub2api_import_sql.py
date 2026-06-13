import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_GROUP_NAME = "GPTFREE"


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


def _list_value(value):
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def account_group_ids(account):
    values = []
    values.extend(_list_value(account.get("group_ids")))
    values.extend(_list_value(account.get("group_id")))
    group_ids = []
    for value in values:
        try:
            group_id = int(str(value).strip())
        except Exception:
            continue
        if group_id > 0 and group_id not in group_ids:
            group_ids.append(group_id)
    return group_ids


def account_group_names(account):
    values = []
    values.extend(_list_value(account.get("group_names")))
    values.extend(_list_value(account.get("group_name")))
    values.extend(_list_value(account.get("groups")))
    values.extend(_list_value(account.get("group")))

    group_names = []
    for value in values:
        if isinstance(value, dict):
            text = str(value.get("name") or "").strip()
        else:
            text = str(value or "").strip()
        if text and not text.isdigit() and text not in group_names:
            group_names.append(text)

    if not group_names and not account_group_ids(account):
        group_names.append(DEFAULT_GROUP_NAME)
    return group_names


def group_values_sql(account):
    rows = []
    for group_id in account_group_ids(account):
        rows.append("({0}::bigint, NULL::varchar)".format(group_id))
    for group_name in account_group_names(account):
        rows.append("(NULL::bigint, {0}::varchar)".format(sql_string(group_name)))
    return ",\n    ".join(rows) if rows else "(NULL::bigint, {0}::varchar)".format(sql_string(DEFAULT_GROUP_NAME))


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

    return """WITH desired_groups(group_id, group_name) AS (
  VALUES
    {group_values}
),
incoming_account AS (
  SELECT
    {name}::varchar AS name,
    {platform}::varchar AS platform,
    {type}::varchar AS type,
    {credentials}::jsonb AS credentials
),
existing_account AS (
  SELECT a.id, a.name
  FROM accounts a
  CROSS JOIN incoming_account i
  WHERE a.deleted_at IS NULL
    AND a.platform = i.platform
    AND a.type = i.type
    AND (
      a.name = i.name
      OR (
        nullif(i.credentials ->> 'access_token', '') IS NOT NULL
        AND a.credentials ->> 'access_token' = i.credentials ->> 'access_token'
      )
      OR (
        nullif(i.credentials ->> 'chatgpt_account_id', '') IS NOT NULL
        AND a.credentials ->> 'chatgpt_account_id' = i.credentials ->> 'chatgpt_account_id'
      )
      OR (
        nullif(i.credentials ->> 'chatgpt_user_id', '') IS NOT NULL
        AND a.credentials ->> 'chatgpt_user_id' = i.credentials ->> 'chatgpt_user_id'
      )
      OR (
        nullif(i.credentials ->> 'email', '') IS NOT NULL
        AND lower(a.credentials ->> 'email') = lower(i.credentials ->> 'email')
      )
      OR (
        nullif(i.credentials ->> 'api_key', '') IS NOT NULL
        AND nullif(i.credentials ->> 'base_url', '') IS NOT NULL
        AND a.credentials ->> 'api_key' = i.credentials ->> 'api_key'
        AND regexp_replace(lower(coalesce(a.credentials ->> 'base_url', '')), '/+$', '') =
            regexp_replace(lower(i.credentials ->> 'base_url'), '/+$', '')
      )
    )
  ORDER BY
    CASE WHEN a.name = i.name THEN 0 ELSE 1 END,
    a.id
  LIMIT 1
),
resolved_groups AS (
  SELECT DISTINCT g.id
  FROM groups g
  JOIN desired_groups dg ON (
    (dg.group_id IS NOT NULL AND g.id = dg.group_id)
    OR (dg.group_id IS NULL AND g.name = dg.group_name)
  )
  WHERE g.deleted_at IS NULL
    AND g.status = 'active'
),
selected_proxy AS (
  SELECT proxy_id
  FROM (
    SELECT gp.proxy_id, 0 AS rank
    FROM ops_group_default_proxies gp
    JOIN resolved_groups rg ON rg.id = gp.group_id
    JOIN proxies p ON p.id = gp.proxy_id
    WHERE p.deleted_at IS NULL
      AND p.status = 'active'
    UNION ALL
    SELECT p.id AS proxy_id, 1 AS rank
    FROM proxies p
    WHERE p.deleted_at IS NULL
      AND p.status = 'active'
  ) candidates
  ORDER BY rank, proxy_id
  LIMIT 1
),
inserted AS (
  INSERT INTO accounts (
    name,
    platform,
    type,
    credentials,
    extra,
    proxy_id,
    concurrency,
    priority,
    status,
    schedulable,
    expires_at,
    auto_pause_on_expired,
    rate_multiplier
  )
  SELECT
    i.name,
    i.platform,
    i.type,
    i.credentials,
    {extra}::jsonb,
    (SELECT proxy_id FROM selected_proxy),
    {concurrency},
    {priority},
    {status},
    {schedulable},
    {expires_at_sql},
    {auto_pause_on_expired},
    {rate_multiplier}::numeric
  FROM incoming_account i
  WHERE NOT EXISTS (SELECT 1 FROM existing_account)
  RETURNING id, name
),
target_account AS (
  SELECT id, name FROM inserted
  UNION ALL
  SELECT id, name FROM existing_account
  WHERE NOT EXISTS (SELECT 1 FROM inserted)
  LIMIT 1
),
proxy_update AS (
  UPDATE accounts a
  SET proxy_id = (SELECT proxy_id FROM selected_proxy),
      updated_at = now()
  FROM target_account ta
  WHERE a.id = ta.id
    AND a.proxy_id IS NULL
    AND EXISTS (SELECT 1 FROM selected_proxy)
  RETURNING 1
),
group_links AS (
  INSERT INTO account_groups (account_id, group_id, priority, created_at)
  SELECT ta.id, rg.id, 100, now()
  FROM target_account ta
  CROSS JOIN resolved_groups rg
  WHERE NOT EXISTS (
    SELECT 1
    FROM account_groups ag
    WHERE ag.account_id = ta.id
      AND ag.group_id = rg.id
  )
  RETURNING 1
)
SELECT coalesce((SELECT 'inserted|' || id::text || '|' || name FROM inserted), 'skipped|' || {name});""".format(
        group_values=group_values_sql(account),
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
    lines.append(
        """CREATE TABLE IF NOT EXISTS ops_group_default_proxies (
  group_id bigint PRIMARY KEY,
  proxy_id bigint NOT NULL,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);"""
    )
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
