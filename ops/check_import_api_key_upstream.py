import argparse
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from import_sub2api_good_bundle import import_bundle as cloud_import_bundle
from sub2api_batch_checker.checker import check_many
from sub2api_batch_checker.loader import load_sub2api_accounts, write_sub2api_bundle


def safe_name(base_url):
    host = base_url.replace("https://", "").replace("http://", "").strip("/").split("/")[0]
    return host or "api-key-upstream"


def write_temp_input(base_url, api_key, name):
    fd, filename = tempfile.mkstemp(prefix="sub2api_api_key_", suffix=".json")
    os.close(fd)
    path = Path(filename)
    payload = {"name": name, "base_url": base_url.rstrip("/"), "api_key": api_key}
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


async def check_one(accounts, endpoint, model, timeout, proxy_url):
    results = await check_many(
        accounts=accounts,
        concurrency=1,
        timeout=timeout,
        endpoint=endpoint,
        model=model,
        local_expiry_guard_sec=60,
        refresh=False,
        proxy_url=proxy_url,
        progress=True,
    )
    return results[0]


def make_import_args(args, good_bundle):
    parser = argparse.Namespace(
        bundle=good_bundle,
        import_tag=args.import_tag,
        ssh_key=args.ssh_key,
        ssh_target=args.ssh_target,
        remote_sql_dir=args.remote_sql_dir,
        remote_project_dir=args.remote_project_dir,
        psql_command=args.psql_command,
        output_sql=None,
        keep_local_sql=args.keep_local_sql,
        keep_remote_sql=args.keep_remote_sql,
        dry_run=args.dry_run_import,
        quiet=args.quiet_import,
        timeout=args.timeout,
    )
    return parser


def run(args):
    base_url = (args.base_url or os.environ.get("SUB2API_TEST_BASE_URL") or "").strip().rstrip("/")
    api_key = (args.api_key or os.environ.get("SUB2API_TEST_API_KEY") or "").strip()
    if not base_url:
        raise RuntimeError("missing base_url: pass --base-url or set SUB2API_TEST_BASE_URL")
    if not api_key:
        raise RuntimeError("missing api_key: pass --api-key or set SUB2API_TEST_API_KEY")

    name = args.name or safe_name(base_url)
    temp_input = write_temp_input(base_url, api_key, name)
    temp_good = Path(tempfile.gettempdir()) / "{0}_good.json".format(args.import_tag)

    try:
        accounts, errors = load_sub2api_accounts([temp_input])
        print("loaded_accounts={0} parse_errors={1}".format(len(accounts), len(errors)))
        for err in errors[:5]:
            print("parse_error: {0}".format(err))
        if not accounts:
            return 2

        proxy_url = "" if args.no_proxy else args.proxy
        login = asyncio.run(check_one(accounts, "/v1/models", args.model, args.timeout, proxy_url))
        print("compat_status={0} http={1}".format(login.status, login.http_status or ""))
        if not login.ok:
            print("result=discard")
            return 1

        model = args.model
        if model == "auto":
            model = str(login.raw_meta.get("sample_model") or "").strip() or "gpt-4.1-nano"
        print("real_model={0}".format(model))
        real = asyncio.run(check_one(accounts, "/v1/chat/completions", model, args.timeout, proxy_url))
        print("real_status={0} http={1}".format(real.status, real.http_status or ""))
        if not real.ok:
            print("result=discard")
            return 1

        write_sub2api_bundle(temp_good, accounts)
        print("result=usable")
        print("good_bundle={0} accounts={1}".format(temp_good, len(accounts)))
        if args.no_import:
            print("cloud_import=skipped")
            return 0

        return cloud_import_bundle(make_import_args(args, temp_good))
    finally:
        temp_input.unlink(missing_ok=True)
        if not args.keep_good_bundle:
            temp_good.unlink(missing_ok=True)


def build_parser():
    parser = argparse.ArgumentParser(description="Check an OpenAI-compatible API key upstream and import it if usable.")
    parser.add_argument("--base-url", default="", help="OpenAI-compatible upstream base URL.")
    parser.add_argument("--api-key", default="", help="API key. Prefer SUB2API_TEST_API_KEY to avoid shell history.")
    parser.add_argument("--name", default="", help="Cloud account name. Defaults to host from base_url.")
    parser.add_argument("--model", default="auto", help="Real-call model, or auto from /v1/models.")
    parser.add_argument("--import-tag", default="api_key_{0}".format(datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")))
    parser.add_argument("--proxy", default=os.environ.get("SUB2API_CHECKER_PROXY", "http://127.0.0.1:7897"))
    parser.add_argument("--no-proxy", action="store_true")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--no-import", action="store_true", help="Only check; do not import cloud.")
    parser.add_argument("--keep-good-bundle", action="store_true")
    parser.add_argument("--dry-run-import", action="store_true")
    parser.add_argument("--quiet-import", action="store_true")
    parser.add_argument("--ssh-key", default=os.environ.get("SUB2API_CLOUD_SSH_KEY", ""))
    parser.add_argument("--ssh-target", default=os.environ.get("SUB2API_CLOUD_SSH_TARGET", ""))
    parser.add_argument("--remote-sql-dir", default="/opt/sub2api/data")
    parser.add_argument("--remote-project-dir", default="/opt/sub2api")
    parser.add_argument(
        "--psql-command",
        default="docker exec -i sub2api-postgres psql -U sub2api -d sub2api -At",
    )
    parser.add_argument("--keep-local-sql", action="store_true")
    parser.add_argument("--keep-remote-sql", action="store_true")
    return parser


def main(argv=None):
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
