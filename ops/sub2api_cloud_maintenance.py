import argparse
import base64
import json
import re
import subprocess
from datetime import datetime, timezone
from urllib.parse import quote


PSQL = ["docker", "exec", "-i", "sub2api-postgres", "psql", "-U", "sub2api", "-d", "sub2api", "-qAt"]
CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
CODEX_USER_AGENT = "codex-tui/0.135.0 (Mac OS 26.5.0; arm64) iTerm.app/3.6.10 (codex-tui; 0.135.0)"
CODEX_ORIGINATOR = "codex-tui"
DEFAULT_TEMPORARY_USAGE_LIMIT_MAX_SECONDS = 12 * 60 * 60
DEFAULT_RECOVER_DELETE_AFTER_FAILURES = 3

AUTH_INVALID_PATTERNS = [
    "invalidated oauth token",
    "authentication token has been invalidated",
    "token has been invalidated",
    "has been invalidated",
    "invalid token",
    "invalid api key",
    "invalid_api_key",
    "api key is disabled",
    "api_key_disabled",
    "token_invalidated",
    "token revoked",
    "token_revoked",
    "无效的令牌",
]
BANNED_PATTERNS = [
    "deactivated_workspace",
    "access forbidden",
    "forbidden",
    "banned",
    "workspace deactivated",
]
HARD_USAGE_QUOTA_EXHAUSTED_PATTERNS = [
    "weekly limit",
    "weekly_limit",
    "weekly quota",
    "week limit",
    "usage quota",
    "quota exhausted",
    "insufficient_quota",
    "api_key_quota_exhausted",
    "current quota",
    "额度已用完",
    "周限额",
    "本周限额",
]
GENERIC_USAGE_LIMIT_PATTERNS = [
    "usage limit",
    "usage_limit",
    "usage_limit_reached",
    "limit has been reached",
    "usage_or_weekly_limit",
]
TEMPORARY_RATE_LIMIT_PATTERNS = [
    "rate limit",
    "rate_limit",
    "too many requests",
    "限流",
]
IGNORED_PATTERNS = [
    "concurrency limit",
    "concurrency_limit",
    "model is not supported",
    "instructions are required",
    "no tool call found",
    "context window",
    "failed to read request body",
    "context canceled",
    "system cpu overloaded",
    "service temporarily unavailable",
    "upstream service temporarily unavailable",
    "upstream request failed",
    "recovered upstream error 500",
    "recovered upstream error 502",
    "recovered upstream error 530",
    "gateway time-out",
    "internal server error",
    "网站请求超时",
    "error code: 1033",
    "failed to read frame header",
    "connect: connection refused",
    "invalid 'input",
    "image generation is not enabled",
    "billing service temporarily unavailable",
    "api key is required in authorization header",
    "master data plane is disabled",
    "no available channel",
    "stream usage incomplete",
    "openai stream ended before a terminal event",
    "rate_limit_429_fallback_used",
    "using_default",
    "并发限制",
]
TEMPORARY_LIMIT_SIGNALS = (
    "temporary_limit",
    "temporary_rate_limit",
    "temporary_usage_limit",
    "usage_limit_unknown_reset",
    "usage_or_weekly_limit",
)
ACTIVE_PROBE_COUNTED_FAILURES = (
    "auth_invalid_probe_only",
    "probe_skipped_missing_access_token",
    "probe_failed_unknown",
    "probe_network_or_proxy",
)


class Decision:
    def __init__(self, account_id, action, reason, evidence_count, last_seen_at, sample, reset_sql=None, reset_source=None):
        self.account_id = account_id
        self.action = action
        self.reason = reason
        self.evidence_count = evidence_count
        self.last_seen_at = last_seen_at
        self.sample = sample
        self.reset_sql = reset_sql
        self.reset_source = reset_source

    def as_dict(self):
        payload = {
            "account_id": self.account_id,
            "action": self.action,
            "reason": self.reason,
            "evidence_count": self.evidence_count,
            "last_seen_at": self.last_seen_at,
            "sample": self.sample,
        }
        if self.reset_source:
            payload["reset_source"] = self.reset_source
        return payload


def sql_string(value):
    return "'" + str(value).replace("'", "''") + "'"


def run_sql(sql):
    proc = subprocess.run(
        PSQL,
        input=sql,
        universal_newlines=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return proc.stdout


def normalize(text):
    return text.lower().strip()


def matches(text, patterns):
    value = normalize(text)
    return any(pattern in value for pattern in patterns)


def status_int(value):
    try:
        return int(str(value or "").strip())
    except Exception:
        return 0


def parse_reset_seconds(text):
    if not text:
        return None, None

    resets_in_values = []
    for match in re.finditer(r'"?resets_in_seconds"?\s*[:=]\s*"?(\d{1,8})"?', text):
        try:
            value = int(match.group(1))
        except Exception:
            continue
        if value > 0:
            resets_in_values.append(value)
    if resets_in_values:
        return min(resets_in_values), "resets_in_seconds"

    now_epoch = int(datetime.now(timezone.utc).timestamp())
    resets_at_values = []
    for match in re.finditer(r'"?resets_at"?\s*[:=]\s*"?(\d{10,13})"?', text):
        try:
            epoch = int(match.group(1))
        except Exception:
            continue
        if epoch > 9999999999:
            epoch = int(epoch / 1000)
        if epoch > now_epoch:
            resets_at_values.append(epoch - now_epoch)
    if resets_at_values:
        return min(resets_at_values), "resets_at"

    return None, None


def classify_limit_signal(text, http_status=None, temporary_usage_limit_max_seconds=DEFAULT_TEMPORARY_USAGE_LIMIT_MAX_SECONDS):
    if matches(text, IGNORED_PATTERNS):
        return None
    if matches(text, HARD_USAGE_QUOTA_EXHAUSTED_PATTERNS):
        return "usage_quota_exhausted"
    if matches(text, GENERIC_USAGE_LIMIT_PATTERNS):
        seconds, reset_source = parse_reset_seconds(text)
        if seconds is None:
            return "usage_limit_unknown_reset"
        if seconds <= int(temporary_usage_limit_max_seconds):
            return "temporary_usage_limit"
        return "usage_quota_exhausted"
    if status_int(http_status) == 429 or matches(text, TEMPORARY_RATE_LIMIT_PATTERNS):
        return "temporary_rate_limit"
    return None


def is_temporary_rate_limit_reason(value):
    reason = normalize(value)
    return any(signal in reason for signal in TEMPORARY_LIMIT_SIGNALS)


def temporary_limit_sql_condition(expression):
    parts = []
    for signal in TEMPORARY_LIMIT_SIGNALS:
        parts.append("lower({0}) LIKE '%{1}%'".format(expression, signal))
    return "(" + " OR ".join(parts) + ")"


def not_temporary_limit_sql_condition(expression):
    parts = []
    for signal in TEMPORARY_LIMIT_SIGNALS:
        parts.append("lower({0}) NOT LIKE '%{1}%'".format(expression, signal))
    return " AND ".join(parts)


def parse_reset_hint(text):
    if not text:
        return None, None

    resets_at = re.search(r'"?resets_at"?\s*[:=]\s*"?(\d{10,13})"?', text)
    if resets_at:
        epoch = int(resets_at.group(1))
        if epoch > 9999999999:
            epoch = int(epoch / 1000)
        if epoch > 0:
            return "to_timestamp({0})".format(epoch), "resets_at"

    resets_in = re.search(r'"?resets_in_seconds"?\s*[:=]\s*"?(\d{1,8})"?', text)
    if resets_in:
        seconds = int(resets_in.group(1))
        if seconds > 0:
            return "now() + interval '{0} seconds'".format(seconds), "resets_in_seconds"

    return None, None


def is_truthy(value):
    return str(value or "").lower() in ("t", "true", "1", "yes", "y")


def truncate(value, limit):
    text = str(value or "")
    return text[:limit]


def sanitize_text(text, secrets):
    safe = str(text or "")
    for secret in secrets:
        if secret:
            safe = safe.replace(str(secret), "***")
    safe = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "sk-***", safe)
    safe = re.sub(
        r"(Bearer|access_token|refresh_token|id_token|token)[\"'=:\s]+[A-Za-z0-9._-]{12,}",
        r"\1=***",
        safe,
        flags=re.IGNORECASE,
    )
    safe = re.sub(r"[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}", "jwt-***", safe)
    return safe[:500]


def ensure_audit_table(apply):
    sql = """
CREATE TABLE IF NOT EXISTS ops_account_maintenance_audits (
  id BIGSERIAL PRIMARY KEY,
  account_id BIGINT NOT NULL,
  action VARCHAR(64) NOT NULL,
  reason TEXT NOT NULL,
  evidence_count INTEGER NOT NULL DEFAULT 0,
  last_seen_at TIMESTAMPTZ,
  sample_message TEXT,
  applied BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""
    if apply:
        run_sql(sql)


def ensure_probe_state_table(apply):
    create_sql = """
CREATE TABLE IF NOT EXISTS ops_account_active_probe_state (
  account_id BIGINT PRIMARY KEY,
  last_probe_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_result VARCHAR(64) NOT NULL,
  last_http_status INTEGER,
  last_error_code TEXT,
  last_message TEXT,
  recovery_probe_failures INTEGER NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""
    if apply:
        run_sql(create_sql)
        run_sql(
            """
ALTER TABLE IF EXISTS ops_account_active_probe_state
  ADD COLUMN IF NOT EXISTS recovery_probe_failures INTEGER NOT NULL DEFAULT 0;
"""
        )


def load_error_evidence(lookback_hours):
    sql = f"""
COPY (
  WITH raw AS (
    SELECT account_id,
           coalesce(status_code::text, '') AS status_code,
           coalesce(upstream_status_code::text, '') AS upstream_status_code,
           coalesce(error_type, '') AS error_type,
           coalesce(provider_error_code, '') AS provider_error_code,
           coalesce(provider_error_type, '') AS provider_error_type,
           created_at,
           coalesce(status_code::text, '') || ' ' ||
           coalesce(upstream_status_code::text, '') || ' ' ||
           coalesce(error_type, '') || ' ' ||
           coalesce(provider_error_code, '') || ' ' ||
           coalesce(provider_error_type, '') || ' ' ||
           coalesce(upstream_error_message::text, '') || ' ' ||
           coalesce(error_message::text, '') || ' ' ||
           coalesce(error_body::text, '') || ' ' ||
           coalesce(upstream_error_detail::text, '') || ' ' ||
           coalesce(upstream_errors::text, '') AS full_message
    FROM ops_error_logs
    WHERE created_at >= now() - interval '{int(lookback_hours)} hours'
      AND account_id IS NOT NULL
  )
  SELECT account_id,
         status_code,
         error_type,
         provider_error_code || ' ' || provider_error_type || ' ' || upstream_status_code AS provider_error_code,
         left(full_message, 1200) AS message,
         count(*) AS n,
         max(created_at) AS last_seen_at
  FROM raw
  GROUP BY account_id, status_code, error_type, provider_error_code, provider_error_type, upstream_status_code, left(full_message, 1200)
  ORDER BY max(created_at) DESC
) TO STDOUT WITH CSV HEADER;
"""
    out = run_sql(sql)
    lines = out.splitlines()
    if len(lines) <= 1:
        return {}
    import csv
    from io import StringIO

    evidence = {}
    for row in csv.DictReader(StringIO(out)):
        row["source"] = "ops_error_logs"
        account_id = int(row["account_id"])
        evidence.setdefault(account_id, []).append(row)
    return evidence


def load_system_log_limit_evidence(lookback_hours):
    sql = f"""
COPY (
  SELECT account_id,
         '' AS status_code,
         coalesce(component, '') AS error_type,
         '' AS provider_error_code,
         left(coalesce(message, '') || ' ' || coalesce(extra::text, ''), 1000) AS message,
         count(*) AS n,
         max(created_at) AS last_seen_at
  FROM ops_system_logs
  WHERE created_at >= now() - interval '{int(lookback_hours)} hours'
    AND account_id IS NOT NULL
    AND (
      message ILIKE '%usage_limit_reached%' OR
      message ILIKE '%usage limit%' OR
      message ILIKE '%quota%' OR
      message ILIKE '%rate_limit%' OR
      message ILIKE '%rate limit%' OR
      message ILIKE '%weekly limit%' OR
      message ILIKE '%week limit%' OR
      message ILIKE '%limit has been reached%' OR
      message ILIKE '%invalidated%' OR
      message ILIKE '%invalid api key%' OR
      message ILIKE '%api key is disabled%' OR
      message ILIKE '%deactivated_workspace%' OR
      message ILIKE '%access forbidden%' OR
      message ILIKE '%authentication failed%' OR
      message ILIKE '%resets_at%' OR
      message ILIKE '%resets_in_seconds%' OR
      extra::text ILIKE '%usage_limit_reached%' OR
      extra::text ILIKE '%usage limit%' OR
      extra::text ILIKE '%quota%' OR
      extra::text ILIKE '%rate_limit%' OR
      extra::text ILIKE '%rate limit%' OR
      extra::text ILIKE '%weekly limit%' OR
      extra::text ILIKE '%week limit%' OR
      extra::text ILIKE '%limit has been reached%' OR
      extra::text ILIKE '%invalidated%' OR
      extra::text ILIKE '%invalid api key%' OR
      extra::text ILIKE '%api key is disabled%' OR
      extra::text ILIKE '%deactivated_workspace%' OR
      extra::text ILIKE '%access forbidden%' OR
      extra::text ILIKE '%authentication failed%' OR
      extra::text ILIKE '%resets_at%' OR
      extra::text ILIKE '%resets_in_seconds%'
    )
  GROUP BY account_id, coalesce(component, ''), left(coalesce(message, '') || ' ' || coalesce(extra::text, ''), 1000)
  ORDER BY max(created_at) DESC
) TO STDOUT WITH CSV HEADER;
"""
    out = run_sql(sql)
    lines = out.splitlines()
    if len(lines) <= 1:
        return {}
    import csv
    from io import StringIO

    evidence = {}
    for row in csv.DictReader(StringIO(out)):
        row["source"] = "ops_system_logs"
        account_id = int(row["account_id"])
        evidence.setdefault(account_id, []).append(row)
    return evidence


def load_error_dictionary_rows(lookback_hours, limit):
    sql = f"""
COPY (
  WITH raw AS (
    SELECT account_id,
           coalesce(status_code::text, '') AS status_code,
           coalesce(upstream_status_code::text, '') AS upstream_status_code,
           coalesce(error_type, '') AS error_type,
           coalesce(provider_error_code, '') AS provider_error_code,
           coalesce(provider_error_type, '') AS provider_error_type,
           created_at,
           coalesce(status_code::text, '') || ' ' ||
           coalesce(upstream_status_code::text, '') || ' ' ||
           coalesce(error_type, '') || ' ' ||
           coalesce(provider_error_code, '') || ' ' ||
           coalesce(provider_error_type, '') || ' ' ||
           coalesce(upstream_error_message::text, '') || ' ' ||
           coalesce(error_message::text, '') || ' ' ||
           coalesce(error_body::text, '') || ' ' ||
           coalesce(upstream_error_detail::text, '') || ' ' ||
           coalesce(upstream_errors::text, '') AS message
    FROM ops_error_logs
    WHERE created_at >= now() - interval '{int(lookback_hours)} hours'
  ),
  grouped AS (
    SELECT status_code,
           upstream_status_code,
           error_type,
           provider_error_code,
           provider_error_type,
           left(message, 1200) AS message,
           count(*) AS n,
           count(DISTINCT account_id) FILTER (WHERE account_id IS NOT NULL) AS accounts,
           max(created_at) AS last_seen_at
    FROM raw
    WHERE trim(message) <> ''
    GROUP BY status_code, upstream_status_code, error_type, provider_error_code, provider_error_type, left(message, 1200)
  )
  SELECT status_code,
         upstream_status_code,
         error_type,
         provider_error_code || ' ' || provider_error_type AS provider_error_code,
         message,
         n,
         accounts,
         last_seen_at
  FROM grouped
  ORDER BY n DESC, accounts DESC, last_seen_at DESC
  LIMIT {int(limit)}
) TO STDOUT WITH CSV HEADER;
"""
    out = run_sql(sql)
    import csv
    from io import StringIO

    return list(csv.DictReader(StringIO(out)))


def dictionary_category(row, temporary_usage_limit_max_seconds):
    if int(row.get("accounts") or 0) <= 0:
        return "observe_no_account_context"
    message = " ".join(
        [
            row.get("status_code", ""),
            row.get("upstream_status_code", ""),
            row.get("error_type", ""),
            row.get("provider_error_code", ""),
            row.get("message", ""),
        ]
    )
    http_status = row.get("status_code") or row.get("upstream_status_code")
    if matches(message, IGNORED_PATTERNS):
        return "ignore_request_or_system"
    if matches(message, BANNED_PATTERNS):
        return "soft_delete_banned_or_forbidden"
    if matches(message, AUTH_INVALID_PATTERNS) or status_int(http_status) == 401:
        return "pause_or_delete_auth_invalid"
    limit_signal = classify_limit_signal(message, http_status, temporary_usage_limit_max_seconds)
    if limit_signal == "usage_quota_exhausted":
        return "soft_delete_usage_quota_or_long_reset"
    if limit_signal == "temporary_rate_limit":
        return "pause_temporary_429_then_probe"
    if limit_signal == "temporary_usage_limit":
        return "pause_short_usage_limit_then_probe"
    if limit_signal == "usage_limit_unknown_reset":
        return "pause_unknown_usage_limit_and_report"
    return "unknown_needs_sampling"


def build_error_dictionary_report(args):
    rows = load_error_dictionary_rows(args.lookback_hours, args.error_report_limit)
    report_rows = []
    counts = {}
    unknown = []
    for row in rows:
        category = dictionary_category(row, args.temporary_usage_limit_max_seconds)
        counts[category] = counts.get(category, 0) + int(row.get("n") or 0)
        safe_sample = sanitize_text(row.get("message", ""), [])
        item = {
            "category": category,
            "rows": int(row.get("n") or 0),
            "accounts": int(row.get("accounts") or 0),
            "status_code": row.get("status_code", ""),
            "upstream_status_code": row.get("upstream_status_code", ""),
            "error_type": row.get("error_type", ""),
            "provider_error_code": row.get("provider_error_code", ""),
            "last_seen_at": row.get("last_seen_at", ""),
            "sample": safe_sample,
        }
        report_rows.append(item)
        if category == "unknown_needs_sampling":
            unknown.append(item)
    return {
        "lookback_hours": args.lookback_hours,
        "temporary_usage_limit_max_seconds": args.temporary_usage_limit_max_seconds,
        "category_row_counts": counts,
        "rows": report_rows,
        "unknown_rows": unknown,
    }


def load_cloud_maintenance_usage_evidence(lookback_hours):
    audit_table_exists = run_sql("SELECT to_regclass('public.ops_account_maintenance_audits');").strip()
    if not audit_table_exists:
        return {}
    text_expr = "coalesce(reason, '') || ' ' || coalesce(sample_message, '')"
    sql = f"""
COPY (
  SELECT account_id,
         '' AS status_code,
         action AS error_type,
         reason AS provider_error_code,
         left(coalesce(sample_message, '') || ' ' || coalesce(reason, ''), 1000) AS message,
         evidence_count AS n,
         created_at AS last_seen_at
  FROM ops_account_maintenance_audits
  WHERE created_at >= now() - interval '{int(lookback_hours)} hours'
    AND applied = true
    AND action = 'pause_usage_limited'
    AND {not_temporary_limit_sql_condition(text_expr)}
    AND (
      lower({text_expr}) LIKE '%usage%'
      OR lower({text_expr}) LIKE '%quota%'
      OR lower({text_expr}) LIKE '%weekly%'
      OR coalesce(sample_message, '') LIKE '%额度%'
      OR coalesce(sample_message, '') LIKE '%周限额%'
    )
    AND account_id IS NOT NULL
) TO STDOUT WITH CSV HEADER;
"""
    out = run_sql(sql)
    lines = out.splitlines()
    if len(lines) <= 1:
        return {}
    import csv
    from io import StringIO

    evidence = {}
    for row in csv.DictReader(StringIO(out)):
        row["source"] = "ops_account_maintenance_audits"
        account_id = int(row["account_id"])
        evidence.setdefault(account_id, []).append(row)
    return evidence


def load_cloud_maintenance_auth_evidence(lookback_hours):
    audit_table_exists = run_sql("SELECT to_regclass('public.ops_account_maintenance_audits');").strip()
    if not audit_table_exists:
        return {}
    text_expr = "coalesce(reason, '') || ' ' || coalesce(sample_message, '')"
    sql = f"""
COPY (
  SELECT account_id,
         '401' AS status_code,
         action AS error_type,
         reason AS provider_error_code,
         left(coalesce(sample_message, '') || ' ' || coalesce(reason, ''), 1000) AS message,
         evidence_count AS n,
         created_at AS last_seen_at
  FROM ops_account_maintenance_audits
  WHERE created_at >= now() - interval '{int(lookback_hours)} hours'
    AND applied = true
    AND action = 'pause_auth_invalid'
    AND (
      lower({text_expr}) LIKE '%invalid%'
      OR lower({text_expr}) LIKE '%token_invalidated%'
      OR lower({text_expr}) LIKE '%missing_access_token%'
      OR lower({text_expr}) LIKE '%authentication token has been invalidated%'
      OR coalesce(sample_message, '') LIKE '%无效%'
    )
    AND account_id IS NOT NULL
) TO STDOUT WITH CSV HEADER;
"""
    out = run_sql(sql)
    lines = out.splitlines()
    if len(lines) <= 1:
        return {}
    import csv
    from io import StringIO

    evidence = {}
    for row in csv.DictReader(StringIO(out)):
        row["source"] = "ops_account_maintenance_auth_audits"
        account_id = int(row["account_id"])
        evidence.setdefault(account_id, []).append(row)
    return evidence


def merge_evidence(*sources):
    merged = {}
    for source in sources:
        for account_id, rows in source.items():
            merged.setdefault(account_id, []).extend(rows)
    for rows in merged.values():
        rows.sort(key=lambda row: row.get("last_seen_at", ""), reverse=True)
    return merged


def load_accounts():
    state_table_exists = run_sql("SELECT to_regclass('public.ops_account_active_probe_state');").strip()
    if state_table_exists:
        state_join = "LEFT JOIN ops_account_active_probe_state s ON s.account_id = a.id"
        state_columns = """
         coalesce(s.last_probe_at::text, '') AS last_probe_at,
         coalesce(s.last_result, '') AS last_probe_result,
"""
    else:
        state_join = ""
        state_columns = """
         '' AS last_probe_at,
         '' AS last_probe_result,
"""
    sql = """
COPY (
  SELECT a.id,
         a.name,
         a.status,
         a.schedulable,
         coalesce(a.rate_limit_reset_at::text, '') AS rate_limit_reset_at,
         coalesce(a.temp_unschedulable_until::text, '') AS temp_unschedulable_until,
         coalesce((a.temp_unschedulable_until > now())::text, 'false') AS temp_pause_active,
         coalesce(a.temp_unschedulable_reason, '') AS temp_unschedulable_reason,
{state_columns}
         coalesce(a.updated_at::text, '') AS updated_at
  FROM accounts a
  {state_join}
  WHERE a.deleted_at IS NULL
) TO STDOUT WITH CSV HEADER;
""".format(state_columns=state_columns, state_join=state_join)
    out = run_sql(sql)
    import csv
    from io import StringIO

    return {int(row["id"]): row for row in csv.DictReader(StringIO(out))}


def load_error_status_evidence():
    sql = """
COPY (
  SELECT id AS account_id,
         '' AS status_code,
         status AS error_type,
         '' AS provider_error_code,
         left(coalesce(error_message, '') || ' ' || coalesce(temp_unschedulable_reason, ''), 1000) AS message,
         1 AS n,
         updated_at AS last_seen_at
  FROM accounts
  WHERE deleted_at IS NULL
    AND status = 'error'
    AND coalesce(error_message, '') <> ''
) TO STDOUT WITH CSV HEADER;
"""
    out = run_sql(sql)
    lines = out.splitlines()
    if len(lines) <= 1:
        return {}
    import csv
    from io import StringIO

    evidence = {}
    for row in csv.DictReader(StringIO(out)):
        row["source"] = "accounts_error_status"
        account_id = int(row["account_id"])
        evidence.setdefault(account_id, []).append(row)
    return evidence


def load_long_paused_rate_limit_evidence(delete_after_days):
    if int(delete_after_days) <= 0:
        return {}
    text_expr = "coalesce(temp_unschedulable_reason, '') || ' ' || coalesce(error_message, '')"
    sql = f"""
COPY (
  SELECT id AS account_id,
         '' AS status_code,
         'long_rate_limit_pause' AS error_type,
         'rate_limit_reset_at' AS provider_error_code,
         'rate_limit_reset_at=' || coalesce(rate_limit_reset_at::text, '') AS message,
         1 AS n,
         updated_at AS last_seen_at
  FROM accounts
  WHERE deleted_at IS NULL
    AND status = 'active'
    AND schedulable = false
    AND rate_limit_reset_at IS NOT NULL
    AND rate_limit_reset_at > now() + interval '{int(delete_after_days)} days'
    AND {not_temporary_limit_sql_condition(text_expr)}
) TO STDOUT WITH CSV HEADER;
"""
    out = run_sql(sql)
    lines = out.splitlines()
    if len(lines) <= 1:
        return {}
    import csv
    from io import StringIO

    evidence = {}
    for row in csv.DictReader(StringIO(out)):
        row["source"] = "accounts_long_paused_rate_limit"
        account_id = int(row["account_id"])
        evidence.setdefault(account_id, []).append(row)
    return evidence


def load_active_probe_candidates(limit, min_interval_hours):
    if int(limit) <= 0:
        return []
    state_table_exists = run_sql("SELECT to_regclass('public.ops_account_active_probe_state');").strip()
    if state_table_exists:
        state_join = "LEFT JOIN ops_account_active_probe_state s ON s.account_id = a.id"
        state_column = """
         coalesce(s.last_probe_at::text, '') AS last_probe_at,
         coalesce(s.recovery_probe_failures::text, '0') AS recovery_probe_failures,
         coalesce(s.last_result, '') AS previous_probe_result
"""
        state_filter = "AND (s.last_probe_at IS NULL OR s.last_probe_at < now() - interval '{0} hours')".format(
            int(min_interval_hours)
        )
        state_order = "s.last_probe_at ASC NULLS FIRST,"
    else:
        state_join = ""
        state_column = """
         '' AS last_probe_at,
         '0' AS recovery_probe_failures,
         '' AS previous_probe_result
"""
        state_filter = ""
        state_order = ""
    sql = f"""
COPY (
  SELECT a.id,
         a.name,
         a.platform,
         a.type,
         a.credentials::text AS credentials,
         coalesce(a.extra::text, '{{}}') AS extra,
         coalesce(p.protocol, '') AS proxy_protocol,
         coalesce(p.host, '') AS proxy_host,
         coalesce(p.port::text, '') AS proxy_port,
         coalesce(p.username, '') AS proxy_username,
         coalesce(p.password, '') AS proxy_password,
         {state_column}
  FROM accounts a
  LEFT JOIN proxies p ON p.id = a.proxy_id AND p.deleted_at IS NULL AND p.status = 'active'
  {state_join}
  WHERE a.deleted_at IS NULL
    AND a.status = 'active'
    AND a.schedulable = true
    AND a.platform = 'openai'
    AND a.type = 'oauth'
    {state_filter}
  ORDER BY {state_order} coalesce(a.last_used_at, a.created_at) ASC NULLS FIRST, a.id ASC
  LIMIT {int(limit)}
) TO STDOUT WITH CSV HEADER;
"""
    out = run_sql(sql)
    import csv
    from io import StringIO

    return list(csv.DictReader(StringIO(out)))


def load_expired_temporary_pause_candidates(limit):
    if int(limit) <= 0:
        return []
    text_expr = "coalesce(a.temp_unschedulable_reason, '') || ' ' || coalesce(a.error_message, '')"
    state_table_exists = run_sql("SELECT to_regclass('public.ops_account_active_probe_state');").strip()
    audit_table_exists = run_sql("SELECT to_regclass('public.ops_account_maintenance_audits');").strip()
    if state_table_exists:
        state_join = "LEFT JOIN ops_account_active_probe_state s ON s.account_id = a.id"
        state_column = "coalesce(s.recovery_probe_failures::text, '0') AS recovery_probe_failures,"
    else:
        state_join = ""
        state_column = "'0' AS recovery_probe_failures,"
    if audit_table_exists:
        historical_column = """
         coalesce((
           SELECT count(*)::text
           FROM ops_account_maintenance_audits aud
           WHERE aud.account_id = a.id
             AND aud.applied = true
             AND aud.action IN ('pause_usage_limited', 'pause_auth_invalid')
             AND (
               aud.reason LIKE '%still_limited'
               OR aud.reason = 'temporary_rate_limit_probe_inconclusive'
               OR aud.reason = 'expired_pause_auth_invalid_probe_only'
             )
             AND aud.created_at > coalesce((
               SELECT max(ok_aud.created_at)
               FROM ops_account_maintenance_audits ok_aud
               WHERE ok_aud.account_id = a.id
                 AND ok_aud.action = 'recover_temporary_rate_limit'
             ), 'epoch'::timestamptz)
         ), '0') AS historical_recovery_probe_failures
"""
    else:
        historical_column = "'0' AS historical_recovery_probe_failures"
    sql = f"""
COPY (
  SELECT a.id,
         a.name,
         a.platform,
         a.type,
         a.credentials::text AS credentials,
         coalesce(a.extra::text, '{{}}') AS extra,
         coalesce(p.protocol, '') AS proxy_protocol,
         coalesce(p.host, '') AS proxy_host,
         coalesce(p.port::text, '') AS proxy_port,
         coalesce(p.username, '') AS proxy_username,
         coalesce(p.password, '') AS proxy_password,
         coalesce(a.temp_unschedulable_until::text, '') AS temp_unschedulable_until,
         coalesce(a.temp_unschedulable_reason, '') AS temp_unschedulable_reason,
         {state_column}
         {historical_column}
  FROM accounts a
  LEFT JOIN proxies p ON p.id = a.proxy_id AND p.deleted_at IS NULL AND p.status = 'active'
  {state_join}
  WHERE a.deleted_at IS NULL
    AND a.status = 'active'
    AND a.schedulable = false
    AND a.platform = 'openai'
    AND a.type = 'oauth'
    AND a.temp_unschedulable_until IS NOT NULL
    AND a.temp_unschedulable_until <= now()
    AND coalesce(a.temp_unschedulable_reason, '') LIKE 'cloud-maintenance:%'
    AND {temporary_limit_sql_condition(text_expr)}
  ORDER BY a.temp_unschedulable_until ASC, a.id ASC
  LIMIT {int(limit)}
) TO STDOUT WITH CSV HEADER;
"""
    out = run_sql(sql)
    import csv
    from io import StringIO

    return list(csv.DictReader(StringIO(out)))


def shorten_long_temporary_pauses(apply, temporary_rate_pause_minutes):
    reset_sql = "now() + interval '{0} minutes'".format(max(1, int(temporary_rate_pause_minutes)))
    text_expr = "coalesce(temp_unschedulable_reason, '') || ' ' || coalesce(error_message, '')"
    dry_sql = f"""
SELECT count(*)
FROM accounts
WHERE deleted_at IS NULL
  AND status = 'active'
  AND schedulable = false
  AND temp_unschedulable_until IS NOT NULL
  AND temp_unschedulable_until > ({reset_sql})
  AND coalesce(temp_unschedulable_reason, '') LIKE 'cloud-maintenance:%'
  AND {temporary_limit_sql_condition(text_expr)};
"""
    if not apply:
        return int((run_sql(dry_sql).strip() or "0"))
    sql = f"""
WITH shortened AS (
  UPDATE accounts
  SET rate_limit_reset_at = CASE
        WHEN rate_limit_reset_at IS NULL OR rate_limit_reset_at > ({reset_sql}) THEN ({reset_sql})
        ELSE rate_limit_reset_at
      END,
      temp_unschedulable_until = ({reset_sql}),
      updated_at = now()
  WHERE deleted_at IS NULL
    AND status = 'active'
    AND schedulable = false
    AND temp_unschedulable_until IS NOT NULL
    AND temp_unschedulable_until > ({reset_sql})
    AND coalesce(temp_unschedulable_reason, '') LIKE 'cloud-maintenance:%'
    AND {temporary_limit_sql_condition(text_expr)}
  RETURNING id
)
SELECT count(*) FROM shortened;
"""
    return int((run_sql(sql).strip() or "0"))


def should_skip_account(account):
    if account.get("status") == "error":
        return False
    if account.get("status") not in ("active", ""):
        return True
    schedulable = str(account.get("schedulable", "")).lower()
    if schedulable in ("f", "false") and account.get("rate_limit_reset_at"):
        return True
    reason = account.get("temp_unschedulable_reason", "")
    pause_active = is_truthy(account.get("temp_pause_active", ""))
    return pause_active and reason.startswith("cloud-maintenance:")


def comparable_db_time(value):
    return str(value or "").replace("T", " ").replace("Z", "+00:00")


def has_newer_ok_probe(account, decision):
    if account.get("last_probe_result") != "ok":
        return False
    last_probe_at = comparable_db_time(account.get("last_probe_at"))
    last_seen_at = comparable_db_time(decision.last_seen_at)
    return bool(last_probe_at and last_seen_at and last_probe_at >= last_seen_at)


def should_skip_decision(account, decision):
    if decision is None:
        return True
    if (
        decision.action == "pause_usage_limited"
        and is_temporary_rate_limit_reason(decision.reason)
        and has_newer_ok_probe(account, decision)
    ):
        return True
    if account.get("status") == "error":
        return False
    if account.get("status") not in ("active", ""):
        return True
    if decision.action == "soft_delete":
        return False
    return should_skip_account(account)


def decode_jwt_claims(token):
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def account_id_from_credentials(credentials, token, fallback):
    claims = decode_jwt_claims(token)
    return str(
        credentials.get("chatgpt_account_id")
        or claims.get("https://api.openai.com/auth/chatgpt_account_id")
        or fallback
        or ""
    )


def proxy_url_from_row(row):
    host = row.get("proxy_host") or ""
    port = row.get("proxy_port") or ""
    if not host or not port:
        return ""
    protocol = (row.get("proxy_protocol") or "http").lower()
    if protocol == "socks5":
        protocol = "socks5h"
    username = row.get("proxy_username") or ""
    password = row.get("proxy_password") or ""
    auth = ""
    if username:
        auth = quote(username, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        auth += "@"
    return "{0}://{1}{2}:{3}".format(protocol, auth, host, port)


def classify_probe_result(http_status, message, error_code, temporary_usage_limit_max_seconds=DEFAULT_TEMPORARY_USAGE_LIMIT_MAX_SECONDS):
    combined = " ".join([str(http_status or ""), str(error_code or ""), str(message or "")])
    http_code = status_int(http_status)
    if 200 <= http_code < 300:
        return "ok"
    if matches(combined, IGNORED_PATTERNS):
        return "ignored_transient"
    if matches(combined, BANNED_PATTERNS):
        return "banned_or_forbidden"
    limit_signal = classify_limit_signal(combined, http_code, temporary_usage_limit_max_seconds)
    if limit_signal:
        return limit_signal
    if http_code == 401 or matches(combined, AUTH_INVALID_PATTERNS):
        return "auth_invalid_probe_only"
    return "probe_failed_unknown"


def extract_error_from_response(response, text):
    try:
        data = response.json()
    except Exception:
        return "", truncate(text, 500)
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("code") or err.get("type") or ""), truncate(err.get("message") or text, 500)
    return "", truncate(text, 500)


def active_probe_account(row, model, timeout, temporary_usage_limit_max_seconds=DEFAULT_TEMPORARY_USAGE_LIMIT_MAX_SECONDS):
    import requests

    credentials = json.loads(row.get("credentials") or "{}")
    token = str(credentials.get("access_token") or "")
    if not token:
        return {
            "account_id": int(row["id"]),
            "result": "probe_skipped_missing_access_token",
            "http_status": None,
            "error_code": "missing_access_token",
            "message": "missing access_token",
        }

    proxy_url = proxy_url_from_row(row)
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    chatgpt_account_id = account_id_from_credentials(credentials, token, row.get("id"))
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Originator": CODEX_ORIGINATOR,
        "User-Agent": CODEX_USER_AGENT,
    }
    if chatgpt_account_id:
        headers["Chatgpt-Account-Id"] = chatgpt_account_id
    body = {
        "model": model,
        "instructions": "Health check only. Reply with ok.",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "ok"}],
            }
        ],
        "store": False,
        "stream": True,
    }
    secrets = [token, row.get("proxy_password") or ""]
    try:
        response = requests.post(
            CODEX_RESPONSES_URL,
            headers=headers,
            json=body,
            timeout=float(timeout),
            proxies=proxies,
            stream=True,
        )
        if 200 <= response.status_code < 300:
            try:
                chunk = next(response.iter_content(chunk_size=512), b"")
            except StopIteration:
                chunk = b""
            text = chunk.decode("utf-8", errors="replace")
            error_code = ""
            message = "ok"
        else:
            text = response.text[:1000]
            error_code, message = extract_error_from_response(response, text)
        combined_message = (str(message or "") + " " + str(text or "")).strip()
        result = classify_probe_result(
            response.status_code,
            combined_message,
            error_code,
            temporary_usage_limit_max_seconds,
        )
        http_status = response.status_code
        response.close()
        return {
            "account_id": int(row["id"]),
            "result": result,
            "http_status": http_status,
            "error_code": sanitize_text(error_code, secrets),
            "message": sanitize_text(combined_message, secrets),
        }
    except Exception as exc:
        message = sanitize_text(str(exc), secrets)
        return {
            "account_id": int(row["id"]),
            "result": "probe_network_or_proxy",
            "http_status": None,
            "error_code": exc.__class__.__name__,
            "message": message,
        }


def record_probe_state(result, apply, recovery_failure_count=None):
    if not apply:
        return
    status_value = "NULL" if result.get("http_status") is None else str(int(result.get("http_status")))
    recovery_value = "0" if recovery_failure_count is None else str(max(0, int(recovery_failure_count)))
    recovery_update = "false" if recovery_failure_count is None else "true"
    sql = f"""
INSERT INTO ops_account_active_probe_state
  (account_id, last_probe_at, last_result, last_http_status, last_error_code, last_message, recovery_probe_failures, updated_at)
VALUES
  ({int(result["account_id"])}, now(), {sql_string(result.get("result", ""))}, {status_value}, {sql_string(result.get("error_code", ""))}, {sql_string(result.get("message", ""))}, {recovery_value}, now())
ON CONFLICT (account_id) DO UPDATE SET
  last_probe_at = excluded.last_probe_at,
  last_result = excluded.last_result,
  last_http_status = excluded.last_http_status,
  last_error_code = excluded.last_error_code,
  last_message = excluded.last_message,
  recovery_probe_failures = CASE
    WHEN {recovery_update} THEN excluded.recovery_probe_failures
    ELSE ops_account_active_probe_state.recovery_probe_failures
  END,
  updated_at = now();
"""
    run_sql(sql)


def is_temporary_limit_result(result_name):
    return result_name in TEMPORARY_LIMIT_SIGNALS


def recovery_failure_count_before(row):
    counts = []
    for key in ("recovery_probe_failures", "historical_recovery_probe_failures"):
        try:
            counts.append(int(row.get(key) or 0))
        except Exception:
            counts.append(0)
    return max(counts or [0])


def should_delete_after_recovery_failure(failure_count, threshold):
    return int(threshold) > 0 and int(failure_count) >= int(threshold)


def active_probe_failure_count_before(row):
    try:
        return max(0, int(row.get("recovery_probe_failures") or 0))
    except Exception:
        return 0


def is_counted_active_probe_failure(result_name):
    return is_temporary_limit_result(result_name) or result_name in ACTIVE_PROBE_COUNTED_FAILURES


def run_active_probes(args, now_iso):
    ensure_probe_state_table(args.apply)
    candidates = load_active_probe_candidates(args.probe_limit, args.probe_min_interval_hours)
    if not args.apply:
        return [], [], len(candidates)

    decisions = []
    results = []
    for row in candidates:
        previous_failures = active_probe_failure_count_before(row)
        result = active_probe_account(
            row,
            args.probe_model,
            args.probe_timeout,
            args.temporary_usage_limit_max_seconds,
        )
        results.append(result)

        if result["result"] == "ok":
            record_probe_state(result, True, 0)
            continue
        if result["result"] == "ignored_transient":
            record_probe_state(result, True)
            continue

        probe_failures = previous_failures + 1 if is_counted_active_probe_failure(result["result"]) else previous_failures
        if result["result"] in ("usage_quota_exhausted", "banned_or_forbidden"):
            probe_failures = previous_failures + 1
        record_probe_state(result, True, probe_failures)

        if result["result"] == "usage_quota_exhausted":
            decisions.append(
                Decision(
                    result["account_id"],
                    "soft_delete",
                    "active_probe_usage_quota_exhausted",
                    max(1, probe_failures),
                    now_iso,
                    result.get("message", ""),
                )
            )
        elif result["result"] == "banned_or_forbidden":
            decisions.append(
                Decision(
                    result["account_id"],
                    "soft_delete",
                    "active_probe_banned_or_forbidden",
                    max(1, probe_failures),
                    now_iso,
                    result.get("message", ""),
                )
            )
        elif is_temporary_limit_result(result["result"]):
            if should_delete_after_recovery_failure(probe_failures, args.recover_delete_after_failures):
                decisions.append(
                    Decision(
                        result["account_id"],
                        "soft_delete",
                        "active_probe_failed_{0}_times".format(probe_failures),
                        probe_failures,
                        now_iso,
                        result.get("message", ""),
                    )
                )
            else:
                reset_sql, reset_source = parse_reset_hint(result.get("message", ""))
                decisions.append(
                    Decision(
                        result["account_id"],
                        "pause_usage_limited",
                        "active_probe_" + result["result"],
                        probe_failures,
                        now_iso,
                        result.get("message", ""),
                        reset_sql,
                        reset_source,
                    )
                )
        elif result["result"] in ("auth_invalid_probe_only", "probe_skipped_missing_access_token"):
            decisions.append(
                Decision(
                    result["account_id"],
                    "soft_delete",
                    "active_probe_auth_invalid_probe_only",
                    max(1, probe_failures),
                    now_iso,
                    result.get("message", ""),
                )
            )
        elif result["result"] in ("probe_failed_unknown", "probe_network_or_proxy"):
            if should_delete_after_recovery_failure(probe_failures, args.recover_delete_after_failures):
                decisions.append(
                    Decision(
                        result["account_id"],
                        "soft_delete",
                        "active_probe_failed_{0}_times".format(probe_failures),
                        probe_failures,
                        now_iso,
                        result.get("message", ""),
                    )
                )
    return decisions, results, len(candidates)


def evidence_message(row):
    return " ".join(
        [
            row.get("status_code", ""),
            row.get("error_type", ""),
            row.get("provider_error_code", ""),
            row.get("message", ""),
        ]
    )


def auth_evidence_is_superseded(rows, auth_last_seen_at):
    if not auth_last_seen_at:
        return False
    auth_seen = comparable_db_time(auth_last_seen_at)
    for row in rows:
        row_seen = comparable_db_time(row.get("last_seen_at", ""))
        if not row_seen or row_seen <= auth_seen:
            continue
        message = evidence_message(row)
        if matches(message, AUTH_INVALID_PATTERNS):
            continue
        return True
    return False


def classify(account_id, rows, now_iso, min_hard_failures, temporary_usage_limit_max_seconds):
    if not rows:
        return None

    total_auth = 0
    total_banned = 0
    total_usage_quota = 0
    total_temporary_rate = 0
    total_long_rate_pause = 0
    total_error_status_auth = 0
    total_maintenance_auth = 0
    newest = rows[0]
    sample = newest.get("message", "")
    auth_sample = ""
    auth_last_seen_at = ""
    banned_sample = ""
    banned_last_seen_at = ""
    long_rate_sample = ""
    long_rate_last_seen_at = ""
    usage_quota_sample = ""
    usage_quota_last_seen_at = ""
    temporary_rate_sample = ""
    temporary_rate_last_seen_at = ""
    reset_sql = None
    reset_source = None

    for row in rows:
        message = evidence_message(row)
        count = int(row.get("n") or 0)
        row_reset_sql, row_reset_source = parse_reset_hint(message)
        if row_reset_sql and not reset_sql:
            reset_sql = row_reset_sql
            reset_source = row_reset_source
        if matches(message, IGNORED_PATTERNS):
            continue
        if row.get("source") == "accounts_long_paused_rate_limit":
            total_long_rate_pause += count
            if not long_rate_sample:
                long_rate_sample = row.get("message", "")
                long_rate_last_seen_at = row.get("last_seen_at", "")
        elif matches(message, BANNED_PATTERNS):
            total_banned += count
            if not banned_sample:
                banned_sample = row.get("message", "")
                banned_last_seen_at = row.get("last_seen_at", "")
        elif matches(message, AUTH_INVALID_PATTERNS):
            total_auth += count
            if not auth_sample:
                auth_sample = row.get("message", "")
                auth_last_seen_at = row.get("last_seen_at", "")
            if row.get("source") == "accounts_error_status":
                total_error_status_auth += count
            if row.get("source") == "ops_account_maintenance_auth_audits":
                total_maintenance_auth += count
        else:
            limit_signal = classify_limit_signal(message, row.get("status_code"), temporary_usage_limit_max_seconds)
            if limit_signal == "usage_quota_exhausted":
                total_usage_quota += count
                if not usage_quota_sample:
                    usage_quota_sample = row.get("message", "")
                    usage_quota_last_seen_at = row.get("last_seen_at", "")
            elif is_temporary_limit_result(limit_signal):
                total_temporary_rate += count
                if not temporary_rate_sample:
                    temporary_rate_sample = row.get("message", "")
                    temporary_rate_last_seen_at = row.get("last_seen_at", "")

    last_seen_at = newest.get("last_seen_at", now_iso)
    if total_banned >= 1:
        return Decision(account_id, "soft_delete", "banned_or_forbidden", total_banned, banned_last_seen_at or last_seen_at, banned_sample or sample)
    if total_usage_quota >= 1:
        return Decision(
            account_id,
            "soft_delete",
            "usage_quota_exhausted",
            total_usage_quota,
            usage_quota_last_seen_at or last_seen_at,
            usage_quota_sample or sample,
        )
    if total_long_rate_pause >= 1:
        return Decision(
            account_id,
            "soft_delete",
            "long_rate_limit_pause",
            total_long_rate_pause,
            long_rate_last_seen_at or last_seen_at,
            long_rate_sample or sample,
        )
    if total_temporary_rate >= 1:
        return Decision(
            account_id,
            "pause_usage_limited",
            "temporary_limit",
            total_temporary_rate,
            temporary_rate_last_seen_at or last_seen_at,
            temporary_rate_sample or sample,
            reset_sql,
            reset_source,
        )
    if total_auth > 0 and auth_evidence_is_superseded(rows, auth_last_seen_at):
        return None
    if total_error_status_auth >= 1:
        return Decision(account_id, "soft_delete", "error_status_auth_invalid", total_error_status_auth, auth_last_seen_at or last_seen_at, auth_sample or sample)
    if total_maintenance_auth >= 1:
        return Decision(account_id, "soft_delete", "maintenance_auth_invalid_probe", total_maintenance_auth, auth_last_seen_at or last_seen_at, auth_sample or sample)
    if total_auth >= min_hard_failures:
        return Decision(account_id, "soft_delete", "repeated_auth_invalid", total_auth, auth_last_seen_at or last_seen_at, auth_sample or sample)
    if total_auth > 0:
        return Decision(account_id, "pause_auth_invalid", "auth_invalid_single_or_low_count", total_auth, auth_last_seen_at or last_seen_at, auth_sample or sample)
    return None


def apply_decision(decision, usage_pause_days, temporary_rate_pause_minutes):
    reason = f"cloud-maintenance:{decision.reason}; evidence={decision.evidence_count}; last_seen={decision.last_seen_at}"
    if is_temporary_rate_limit_reason(decision.reason):
        fallback_reset_sql = "now() + interval '{0} minutes'".format(max(1, int(temporary_rate_pause_minutes)))
        force_reset = True
    else:
        fallback_reset_sql = "now() + interval '{0} days'".format(int(usage_pause_days))
        force_reset = False
    usage_reset_sql = decision.reset_sql or fallback_reset_sql
    if decision.reset_sql:
        force_reset = False
    audit_sql = f"""
INSERT INTO ops_account_maintenance_audits (account_id, action, reason, evidence_count, last_seen_at, sample_message, applied)
VALUES ({decision.account_id}, {sql_string(decision.action)}, {sql_string(decision.reason)}, {decision.evidence_count}, {sql_string(decision.last_seen_at)}::timestamptz, {sql_string(decision.sample)}, true);
"""
    if decision.action == "soft_delete":
        sql = f"""
BEGIN;
UPDATE accounts
SET schedulable = false,
    status = 'disabled',
    deleted_at = now(),
    error_message = {sql_string(reason)},
    temp_unschedulable_reason = {sql_string(reason)},
    updated_at = now()
WHERE id = {decision.account_id}
  AND deleted_at IS NULL;
{audit_sql}
COMMIT;
"""
    elif decision.action == "pause_usage_limited":
        if force_reset:
            reset_assignment = """
    rate_limit_reset_at = ({0}),
    temp_unschedulable_until = ({0}),
""".format(usage_reset_sql)
        else:
            reset_assignment = """
    rate_limit_reset_at = CASE
      WHEN rate_limit_reset_at IS NULL OR rate_limit_reset_at < ({0}) THEN ({0})
      ELSE rate_limit_reset_at
    END,
    temp_unschedulable_until = CASE
      WHEN temp_unschedulable_until IS NULL OR temp_unschedulable_until < ({0}) THEN ({0})
      ELSE temp_unschedulable_until
    END,
""".format(usage_reset_sql)
        sql = f"""
BEGIN;
UPDATE accounts
SET schedulable = false,
    rate_limited_at = coalesce(rate_limited_at, now()),
{reset_assignment}
    temp_unschedulable_reason = {sql_string(reason)},
    error_message = {sql_string(reason)},
    updated_at = now()
WHERE id = {decision.account_id}
  AND deleted_at IS NULL
  AND (schedulable = true OR coalesce(temp_unschedulable_reason, '') LIKE 'cloud-maintenance:%');
{audit_sql}
COMMIT;
"""
    else:
        sql = f"""
BEGIN;
UPDATE accounts
SET schedulable = false,
    temp_unschedulable_until = NULL,
    temp_unschedulable_reason = {sql_string(reason)},
    error_message = {sql_string(reason)},
    updated_at = now()
WHERE id = {decision.account_id}
  AND deleted_at IS NULL;
{audit_sql}
COMMIT;
"""
    run_sql(sql)


def apply_recovery(result, now_iso):
    account_id = int(result["account_id"])
    sample = result.get("message", "")
    audit_sql = f"""
INSERT INTO ops_account_maintenance_audits (account_id, action, reason, evidence_count, last_seen_at, sample_message, applied)
VALUES ({account_id}, 'recover_temporary_rate_limit', 'probe_ok', 1, {sql_string(now_iso)}::timestamptz, {sql_string(sample)}, true);
"""
    sql = f"""
BEGIN;
UPDATE accounts
SET schedulable = true,
    rate_limit_reset_at = NULL,
    temp_unschedulable_until = NULL,
    temp_unschedulable_reason = NULL,
    error_message = CASE
      WHEN coalesce(error_message, '') LIKE 'cloud-maintenance:%' THEN NULL
      ELSE error_message
    END,
    updated_at = now()
WHERE id = {account_id}
  AND deleted_at IS NULL
  AND status = 'active';
{audit_sql}
COMMIT;
"""
    run_sql(sql)


def run_expired_pause_recovery(args, now_iso):
    ensure_probe_state_table(args.apply)
    candidates = load_expired_temporary_pause_candidates(args.recover_probe_limit)
    if not args.apply:
        return [], [], len(candidates), 0

    decisions = []
    results = []
    recovered_count = 0
    for row in candidates:
        previous_recovery_failures = recovery_failure_count_before(row)
        result = active_probe_account(
            row,
            args.probe_model,
            args.probe_timeout,
            args.temporary_usage_limit_max_seconds,
        )
        results.append(result)
        if result["result"] == "ok":
            record_probe_state(result, True, 0)
            apply_recovery(result, now_iso)
            recovered_count += 1
            continue

        recovery_failures = previous_recovery_failures + 1
        record_probe_state(result, True, recovery_failures)

        if result["result"] == "usage_quota_exhausted":
            decisions.append(
                Decision(
                    result["account_id"],
                    "soft_delete",
                    "expired_pause_usage_quota_exhausted",
                    1,
                    now_iso,
                    result.get("message", ""),
                )
            )
        elif is_temporary_limit_result(result["result"]):
            if should_delete_after_recovery_failure(recovery_failures, args.recover_delete_after_failures):
                decisions.append(
                    Decision(
                        result["account_id"],
                        "soft_delete",
                        "expired_pause_recovery_failed_{0}_times".format(recovery_failures),
                        recovery_failures,
                        now_iso,
                        result.get("message", ""),
                    )
                )
            else:
                reset_sql, reset_source = parse_reset_hint(result.get("message", ""))
                decisions.append(
                    Decision(
                        result["account_id"],
                        "pause_usage_limited",
                        result["result"] + "_still_limited",
                        recovery_failures,
                        now_iso,
                        result.get("message", ""),
                        reset_sql,
                        reset_source,
                    )
                )
        elif result["result"] == "banned_or_forbidden":
            decisions.append(
                Decision(
                    result["account_id"],
                    "soft_delete",
                    "expired_pause_banned_or_forbidden",
                    1,
                    now_iso,
                    result.get("message", ""),
                )
            )
        elif result["result"] in ("auth_invalid_probe_only", "probe_skipped_missing_access_token"):
            if should_delete_after_recovery_failure(recovery_failures, args.recover_delete_after_failures):
                decisions.append(
                    Decision(
                        result["account_id"],
                        "soft_delete",
                        "expired_pause_recovery_failed_{0}_times".format(recovery_failures),
                        recovery_failures,
                        now_iso,
                        result.get("message", ""),
                    )
                )
            else:
                decisions.append(
                    Decision(
                        result["account_id"],
                        "pause_auth_invalid",
                        "expired_pause_auth_invalid_probe_only",
                        recovery_failures,
                        now_iso,
                        result.get("message", ""),
                    )
                )
        else:
            if should_delete_after_recovery_failure(recovery_failures, args.recover_delete_after_failures):
                decisions.append(
                    Decision(
                        result["account_id"],
                        "soft_delete",
                        "expired_pause_recovery_failed_{0}_times".format(recovery_failures),
                        recovery_failures,
                        now_iso,
                        result.get("message", ""),
                    )
                )
            else:
                decisions.append(
                    Decision(
                        result["account_id"],
                        "pause_usage_limited",
                        "temporary_rate_limit_probe_inconclusive",
                        recovery_failures,
                        now_iso,
                        result.get("message", ""),
                    )
                )
    return decisions, results, len(candidates), recovered_count


def main():
    parser = argparse.ArgumentParser(description="Maintain Sub2API cloud accounts based on recent ops errors.")
    parser.add_argument("--apply", action="store_true", help="Apply database changes. Default is dry-run.")
    parser.add_argument("--lookback-hours", type=int, default=24)
    parser.add_argument("--min-hard-failures", type=int, default=2)
    parser.add_argument("--usage-pause-days", type=int, default=7)
    parser.add_argument("--temporary-rate-pause-minutes", type=int, default=20)
    parser.add_argument("--temporary-usage-limit-max-seconds", type=int, default=DEFAULT_TEMPORARY_USAGE_LIMIT_MAX_SECONDS)
    parser.add_argument("--long-pause-delete-days", type=int, default=3)
    parser.add_argument("--probe-active", action="store_true", help="Actively probe a rotating sample of schedulable OAuth accounts.")
    parser.add_argument("--probe-limit", type=int, default=0, help="Maximum active probes this run. 0 disables active probing.")
    parser.add_argument("--recover-probe-limit", type=int, default=10, help="Maximum expired temporary 429 pauses to probe this run.")
    parser.add_argument("--recover-delete-after-failures", type=int, default=DEFAULT_RECOVER_DELETE_AFTER_FAILURES, help="Soft-delete an expired temporary pause after this many consecutive recovery probe failures. 0 disables.")
    parser.add_argument("--probe-min-interval-hours", type=int, default=24)
    parser.add_argument("--probe-timeout", type=float, default=20.0)
    parser.add_argument("--probe-model", default="gpt-5.5")
    parser.add_argument("--report-errors", action="store_true", help="Report observed upstream errors and classifier categories without changing accounts.")
    parser.add_argument("--error-report-limit", type=int, default=80)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    now_iso = datetime.now(timezone.utc).isoformat()
    if args.report_errors:
        report = build_error_dictionary_report(args)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print("observed_upstream_error_dictionary lookback_hours={0}".format(report["lookback_hours"]))
            print("category_row_counts={0}".format(report["category_row_counts"]))
            for row in report["rows"]:
                print(
                    "{category}\trows={rows}\taccounts={accounts}\tstatus={status_code}/{upstream_status_code}\t"
                    "type={error_type}\tlast={last_seen_at}\t{sample}".format(**row)
                )
        return

    ensure_audit_table(args.apply)
    accounts = load_accounts()
    evidence = merge_evidence(
        load_error_evidence(args.lookback_hours),
        load_system_log_limit_evidence(args.lookback_hours),
        load_error_status_evidence(),
        load_cloud_maintenance_usage_evidence(args.lookback_hours),
        load_cloud_maintenance_auth_evidence(args.lookback_hours),
        load_long_paused_rate_limit_evidence(args.long_pause_delete_days),
    )
    decisions = []
    for account_id, rows in evidence.items():
        if account_id not in accounts:
            continue
        decision = classify(
            account_id,
            rows,
            now_iso,
            args.min_hard_failures,
            args.temporary_usage_limit_max_seconds,
        )
        if should_skip_decision(accounts[account_id], decision):
            continue
        decisions.append(decision)
    active_probe_results = []
    active_probe_candidate_count = 0
    if args.probe_active and args.probe_limit > 0:
        active_probe_decisions, active_probe_results, active_probe_candidate_count = run_active_probes(args, now_iso)
        decisions.extend(active_probe_decisions)
    shortened_temporary_pause_count = shorten_long_temporary_pauses(args.apply, args.temporary_rate_pause_minutes)
    recovery_decisions, recovery_results, recovery_candidate_count, recovered_count = run_expired_pause_recovery(args, now_iso)
    decisions.extend(recovery_decisions)

    if args.apply:
        ensure_audit_table(True)
        for decision in decisions:
            apply_decision(decision, args.usage_pause_days, args.temporary_rate_pause_minutes)

    payload = {
        "mode": "apply" if args.apply else "dry-run",
        "lookback_hours": args.lookback_hours,
        "min_hard_failures": args.min_hard_failures,
        "usage_pause_days": args.usage_pause_days,
        "temporary_rate_pause_minutes": args.temporary_rate_pause_minutes,
        "temporary_usage_limit_max_seconds": args.temporary_usage_limit_max_seconds,
        "long_pause_delete_days": args.long_pause_delete_days,
        "active_probe_enabled": bool(args.probe_active and args.probe_limit > 0),
        "active_probe_candidate_count": active_probe_candidate_count,
        "active_probe_result_count": len(active_probe_results),
        "expired_pause_recovery_candidate_count": recovery_candidate_count,
        "expired_pause_recovery_result_count": len(recovery_results),
        "recover_delete_after_failures": args.recover_delete_after_failures,
        "shortened_temporary_pause_count": shortened_temporary_pause_count,
        "decision_count": len(decisions),
        "recovered_count": recovered_count,
        "decisions": [decision.as_dict() for decision in decisions],
        "active_probe_results": active_probe_results,
        "expired_pause_recovery_results": recovery_results,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"mode={payload['mode']} decisions={len(decisions)} recovered={recovered_count}")
        for decision in decisions:
            print(
                f"{decision.account_id}\t{decision.action}\t{decision.reason}\t"
                f"n={decision.evidence_count}\tlast={decision.last_seen_at}\t{decision.sample[:120]}"
            )


if __name__ == "__main__":
    main()
