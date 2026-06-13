#!/usr/bin/env python3
"""Audit and soft-delete duplicate cloud OAuth accounts.

The cleanup key is the OAuth access token.  The report never prints token
values; samples use MD5 prefixes only to make duplicate groups recognizable.
Default mode is dry-run.  --apply soft-deletes duplicate rows and keeps the
best row per token.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path


DEFAULT_SSH_KEY = os.environ.get("SUB2API_CLOUD_SSH_KEY", "")
DEFAULT_SSH_TARGET = os.environ.get("SUB2API_CLOUD_SSH_TARGET", "")
DEFAULT_REMOTE_PROJECT_DIR = "/opt/sub2api"
DEFAULT_PSQL_COMMAND = "docker exec -i sub2api-postgres psql -U sub2api -d sub2api -qAt"


def run_command(args, input_text=None, timeout=180, quiet=False):
    if not quiet:
        preview = args[:2] + (["..."] if len(args) > 2 else [])
        print("+ {0}".format(" ".join(str(arg) for arg in preview)))
    completed = subprocess.run(
        args,
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "command failed rc={0}\nstdout:\n{1}\nstderr:\n{2}".format(
                completed.returncode,
                completed.stdout.strip(),
                completed.stderr.strip(),
            )
        )
    return completed.stdout


def ssh_args(args):
    base = ["ssh"]
    if args.ssh_key:
        base.extend(["-i", str(args.ssh_key)])
    base.extend(["-o", "IdentitiesOnly=yes", args.ssh_target])
    return base


def run_cloud_sql(sql: str, args) -> str:
    if not args.ssh_target:
        raise RuntimeError("missing SSH target: set SUB2API_CLOUD_SSH_TARGET or pass --ssh-target")
    command = "cd {0} && {1}".format(args.remote_project_dir, args.psql_command)
    return run_command(ssh_args(args) + [command], input_text=sql, timeout=args.timeout, quiet=args.quiet)


def ranked_cte() -> str:
    return """
WITH base AS (
  SELECT
    a.id,
    a.name,
    a.status,
    a.schedulable,
    a.created_at,
    a.updated_at,
    nullif(a.credentials ->> 'access_token', '') AS access_token,
    nullif(a.credentials ->> 'chatgpt_account_id', '') AS chatgpt_account_id,
    nullif(a.credentials ->> 'chatgpt_user_id', '') AS chatgpt_user_id,
    nullif(lower(a.credentials ->> 'email'), '') AS email,
    coalesce(string_agg(g.name, ',' ORDER BY g.name), '') AS groups
  FROM accounts a
  LEFT JOIN account_groups ag ON ag.account_id = a.id
  LEFT JOIN groups g ON g.id = ag.group_id AND g.deleted_at IS NULL
  WHERE a.deleted_at IS NULL
    AND a.platform = 'openai'
    AND a.type = 'oauth'
  GROUP BY a.id
),
dup_keys AS (
  SELECT access_token
  FROM base
  WHERE access_token IS NOT NULL
  GROUP BY access_token
  HAVING count(*) > 1
),
ranked AS (
  SELECT
    b.*,
    md5(b.access_token) AS token_hash,
    count(*) OVER (PARTITION BY b.access_token) AS duplicate_count,
    row_number() OVER (
      PARTITION BY b.access_token
      ORDER BY
        CASE WHEN b.status = 'active' AND b.schedulable = true THEN 0 ELSE 1 END,
        CASE WHEN b.status = 'active' THEN 0 ELSE 1 END,
        CASE WHEN b.groups LIKE '%GPTFREE%' THEN 0 ELSE 1 END,
        CASE WHEN b.groups LIKE '%限流账号%' THEN 1 ELSE 0 END,
        b.id DESC
    ) AS keep_rank
  FROM base b
  JOIN dup_keys d ON d.access_token = b.access_token
)
"""


def audit_sql(sample_limit: int) -> str:
    return (
        ranked_cte()
        + """
SELECT 'summary|duplicate_groups|' || count(DISTINCT token_hash) || '|duplicate_rows|' || count(*) || '|soft_delete_candidates|' || count(*) FILTER (WHERE keep_rank > 1)
FROM ranked
UNION ALL
SELECT 'summary|keep_schedulable|' || count(*) FROM ranked WHERE keep_rank = 1 AND status = 'active' AND schedulable = true
UNION ALL
SELECT 'summary|keep_unschedulable|' || count(*) FROM ranked WHERE keep_rank = 1 AND status = 'active' AND schedulable = false
UNION ALL
SELECT 'summary|delete_schedulable_active|' || count(*) FROM ranked WHERE keep_rank > 1 AND status = 'active' AND schedulable = true
UNION ALL
SELECT 'summary|delete_unschedulable_active|' || count(*) FROM ranked WHERE keep_rank > 1 AND status = 'active' AND schedulable = false
UNION ALL
SELECT 'summary|delete_nonactive|' || count(*) FROM ranked WHERE keep_rank > 1 AND status <> 'active';
"""
        + ranked_cte()
        + """
SELECT 'sample|' || left(token_hash, 10) || '|' ||
       string_agg(id::text || ':' || status || ':' || schedulable::text || ':' || groups || ':keep=' || keep_rank::text, ';' ORDER BY keep_rank, id)
FROM ranked
GROUP BY token_hash
ORDER BY count(*) DESC, min(id)
LIMIT {0};
""".format(
            int(sample_limit)
        )
    )


def apply_sql() -> str:
    return (
        ranked_cte()
        + """
, to_delete AS (
  SELECT id
  FROM ranked
  WHERE keep_rank > 1
), deleted_links AS (
  DELETE FROM account_groups ag
  USING to_delete d
  WHERE ag.account_id = d.id
  RETURNING ag.account_id
), updated_accounts AS (
  UPDATE accounts a
  SET deleted_at = now(),
      schedulable = false,
      error_message = 'dedupe-cloud-oauth: soft-deleted duplicate OAuth account',
      temp_unschedulable_reason = 'dedupe-cloud-oauth: duplicate OAuth account',
      updated_at = now()
  FROM to_delete d
  WHERE a.id = d.id
    AND a.deleted_at IS NULL
  RETURNING a.id
)
SELECT 'applied|soft_deleted|' || count(*) FROM updated_accounts;
"""
    )


def parse_lines(output: str):
    return [line.strip() for line in output.splitlines() if line.strip()]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Audit or soft-delete duplicate cloud OAuth accounts.")
    parser.add_argument("--apply", action="store_true", help="Soft-delete duplicate rows. Default is dry-run audit.")
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--ssh-key", default=DEFAULT_SSH_KEY)
    parser.add_argument("--ssh-target", default=DEFAULT_SSH_TARGET)
    parser.add_argument("--remote-project-dir", default=DEFAULT_REMOTE_PROJECT_DIR)
    parser.add_argument("--psql-command", default=DEFAULT_PSQL_COMMAND)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    lines = parse_lines(run_cloud_sql(audit_sql(args.sample_limit), args))
    if args.apply:
        lines.extend(parse_lines(run_cloud_sql(apply_sql(), args)))
        lines.extend(parse_lines(run_cloud_sql(audit_sql(args.sample_limit), args)))

    if args.json:
        print(json.dumps({"mode": "apply" if args.apply else "dry-run", "lines": lines}, ensure_ascii=False, indent=2))
    else:
        print("mode={0}".format("apply" if args.apply else "dry-run"))
        for line in lines:
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
