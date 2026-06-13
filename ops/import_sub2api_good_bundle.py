import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_sub2api_import_sql as sqlgen


DEFAULT_SSH_KEY = os.environ.get("SUB2API_CLOUD_SSH_KEY", "")
DEFAULT_SSH_TARGET = os.environ.get("SUB2API_CLOUD_SSH_TARGET", "")
DEFAULT_REMOTE_SQL_DIR = "/opt/sub2api/data"
DEFAULT_REMOTE_PROJECT_DIR = "/opt/sub2api"
DEFAULT_PSQL_COMMAND = "docker exec -i sub2api-postgres psql -U sub2api -d sub2api -At"


def safe_tag(value):
    tag = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).strip())
    tag = tag.strip("._-")
    return tag or "sub2api_import"


def sql_list(values):
    return ", ".join(sqlgen.sql_string(value) for value in values) if values else "NULL"


def build_verify_sql(import_tag):
    return """
SELECT
  a.id::text || '|' ||
  a.name || '|' ||
  a.status || '|' ||
  a.schedulable::text || '|' ||
  coalesce(p.name, '') || '|' ||
  coalesce(string_agg(g.name, ',' ORDER BY g.name), '')
FROM accounts a
LEFT JOIN proxies p ON p.id = a.proxy_id
LEFT JOIN account_groups ag ON ag.account_id = a.id
LEFT JOIN groups g ON g.id = ag.group_id
WHERE a.deleted_at IS NULL
  AND a.extra ->> 'cloud_import_tag' = {0}
GROUP BY a.id, a.name, a.status, a.schedulable, p.name
ORDER BY a.id;
""".format(
        sqlgen.sql_string(import_tag)
    )


def parse_import_output(output):
    inserted = []
    skipped = []
    summary = []
    other = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("|", 2)
        if parts[0] == "inserted" and len(parts) == 3:
            inserted.append({"id": parts[1], "name": parts[2]})
        elif parts[0] == "skipped" and len(parts) == 2:
            skipped.append({"name": parts[1]})
        elif parts[0] == "summary":
            summary.append(line)
        elif line in {"BEGIN", "COMMIT"}:
            continue
        else:
            other.append(line)
    return {
        "inserted": inserted,
        "skipped": skipped,
        "summary": summary,
        "other": other,
    }


def parse_verify_output(output):
    rows = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("|", 5)
        if len(parts) != 6:
            rows.append({"raw": line})
            continue
        rows.append(
            {
                "id": parts[0],
                "name": parts[1],
                "status": parts[2],
                "schedulable": parts[3],
                "proxy": parts[4],
                "groups": parts[5],
            }
        )
    return rows


def run_command(args, input_text=None, timeout=120, quiet=False):
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


def scp_args(args, local_sql, remote_sql):
    base = ["scp"]
    if args.ssh_key:
        base.extend(["-i", str(args.ssh_key)])
    base.extend(["-o", "IdentitiesOnly=yes", str(local_sql), "{0}:{1}".format(args.ssh_target, remote_sql)])
    return base


def remote_exec_import_command(remote_sql, args):
    quoted_project = shlex.quote(args.remote_project_dir)
    quoted_sql = shlex.quote(remote_sql)
    return "cd {0} && cat {1} | {2}".format(quoted_project, quoted_sql, args.psql_command)


def remote_remove_command(remote_sql):
    return "rm -f {0}".format(shlex.quote(remote_sql))


def run_psql_stdin(sql_text, args):
    command = "{0} -F '|'".format(args.psql_command)
    return run_command(ssh_args(args) + [command], input_text=sql_text, timeout=args.timeout, quiet=args.quiet)


def make_local_sql(bundle_path, import_tag, keep_local_sql, output_sql):
    accounts = sqlgen.load_accounts(bundle_path)
    if not accounts:
        raise RuntimeError("bundle has no accounts")
    sql_text = sqlgen.build_sql(str(bundle_path), accounts, import_tag)

    if output_sql:
        local_sql = output_sql
        local_sql.parent.mkdir(parents=True, exist_ok=True)
        local_sql.write_text(sql_text, encoding="utf-8", newline="\n")
        cleanup_local = False
    elif keep_local_sql:
        local_sql = Path("outputs") / "{0}.sql".format(import_tag)
        local_sql.parent.mkdir(parents=True, exist_ok=True)
        local_sql.write_text(sql_text, encoding="utf-8", newline="\n")
        cleanup_local = False
    else:
        temp_dir = Path(tempfile.gettempdir())
        fd, temp_name = tempfile.mkstemp(prefix="{0}_".format(import_tag), suffix=".sql", dir=str(temp_dir))
        os.close(fd)
        local_sql = Path(temp_name)
        local_sql.write_text(sql_text, encoding="utf-8", newline="\n")
        cleanup_local = True

    return accounts, local_sql, cleanup_local


def import_bundle(args):
    if not args.dry_run and not args.ssh_target:
        raise RuntimeError("missing SSH target: set SUB2API_CLOUD_SSH_TARGET or pass --ssh-target")
    bundle_path = args.bundle.resolve()
    import_tag = safe_tag(args.import_tag or "{0}_{1}".format(bundle_path.stem, datetime.now().strftime("%Y%m%d_%H%M%S")))
    accounts, local_sql, cleanup_local = make_local_sql(bundle_path, import_tag, args.keep_local_sql, args.output_sql)
    remote_sql = "{0}/{1}.sql".format(args.remote_sql_dir.rstrip("/"), import_tag)

    print("bundle={0}".format(bundle_path))
    print("accounts={0}".format(len(accounts)))
    print("import_tag={0}".format(import_tag))

    try:
        if args.dry_run:
            print("dry_run=true")
            print("local_sql={0}".format(local_sql))
            print("remote_sql={0}".format(remote_sql))
            return 0

        run_command(scp_args(args, local_sql, remote_sql), timeout=args.timeout, quiet=args.quiet)
        output = run_command(
            ssh_args(args) + [remote_exec_import_command(remote_sql, args)],
            timeout=args.timeout,
            quiet=args.quiet,
        )
        parsed = parse_import_output(output)

        print("inserted={0}".format(len(parsed["inserted"])))
        for item in parsed["inserted"]:
            print("  inserted id={0} name={1}".format(item["id"], item["name"]))
        print("skipped={0}".format(len(parsed["skipped"])))
        for item in parsed["skipped"]:
            print("  skipped name={0}".format(item["name"]))
        for line in parsed["summary"]:
            print(line)
        for line in parsed["other"]:
            print("notice={0}".format(line))

        verify_rows = parse_verify_output(run_psql_stdin(build_verify_sql(import_tag), args))
        print("verified={0}".format(len(verify_rows)))
        for row in verify_rows:
            if "raw" in row:
                print("  verify_raw={0}".format(row["raw"]))
                continue
            print(
                "  account id={id} name={name} status={status} schedulable={schedulable} proxy={proxy} groups={groups}".format(
                    **row
                )
            )

        return 0
    finally:
        if not args.keep_remote_sql and not args.dry_run:
            try:
                run_command(ssh_args(args) + [remote_remove_command(remote_sql)], timeout=args.timeout, quiet=True)
                print("remote_sql_removed=true")
            except Exception as exc:
                print("remote_sql_removed=false error={0}".format(exc))
        if cleanup_local:
            try:
                local_sql.unlink(missing_ok=True)
                print("local_sql_removed=true")
            except Exception as exc:
                print("local_sql_removed=false error={0}".format(exc))


def build_parser():
    parser = argparse.ArgumentParser(description="Upload and import a Sub2API good bundle into the cloud account pool.")
    parser.add_argument("bundle", type=Path, help="Local good bundle JSON produced by the checker.")
    parser.add_argument("--import-tag", default="", help="Tag written to account.extra and used for remote SQL filename.")
    parser.add_argument("--ssh-key", default=DEFAULT_SSH_KEY, help="SSH private key path, or SUB2API_CLOUD_SSH_KEY.")
    parser.add_argument("--ssh-target", default=DEFAULT_SSH_TARGET, help="SSH target, or SUB2API_CLOUD_SSH_TARGET.")
    parser.add_argument("--remote-sql-dir", default=DEFAULT_REMOTE_SQL_DIR, help="Remote directory for temporary SQL upload.")
    parser.add_argument("--remote-project-dir", default=DEFAULT_REMOTE_PROJECT_DIR, help="Remote Sub2API compose directory.")
    parser.add_argument("--psql-command", default=DEFAULT_PSQL_COMMAND, help="Remote psql command.")
    parser.add_argument("--output-sql", type=Path, default=None, help="Write local SQL here and keep it.")
    parser.add_argument("--keep-local-sql", action="store_true", help="Keep generated local SQL under outputs/.")
    parser.add_argument("--keep-remote-sql", action="store_true", help="Keep uploaded remote SQL after import.")
    parser.add_argument("--dry-run", action="store_true", help="Generate SQL and print planned paths without SSH/SCP.")
    parser.add_argument("--quiet", action="store_true", help="Hide helper command progress.")
    parser.add_argument("--timeout", type=int, default=180, help="Command timeout seconds.")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return import_bundle(args)


if __name__ == "__main__":
    raise SystemExit(main())
