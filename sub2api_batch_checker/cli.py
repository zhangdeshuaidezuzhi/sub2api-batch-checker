from __future__ import annotations

import argparse
import asyncio
import csv
import os
from collections import Counter
from pathlib import Path

from .checker import (
    CLAUDE_DEFAULT_MODEL,
    CLAUDE_MESSAGES_URL,
    CHATGPT_CODEX_RESPONSES_URL,
    CHATGPT_ME_URL,
    OPENAI_USERINFO_URL,
    SUB2API_OAUTH_COMPAT_URL,
    check_many,
)
from .loader import load_sub2api_accounts, write_sub2api_bundle


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "ok",
        "status",
        "name",
        "platform",
        "type",
        "source_format",
        "account_id",
        "http_status",
        "latency_ms",
        "error_code",
        "message",
        "model",
        "endpoint",
        "attempts",
        "raw_meta",
        "source_file",
        "fingerprint",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sub2api-batch-checker",
        description="Batch health check Sub2API exported JSON account files.",
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="Sub2API JSON file(s) or directories containing JSON files.",
    )
    parser.add_argument(
        "--mode",
        choices=["api-login", "api-real", "oidc-login", "codex-login", "codex-real", "sub2api-oauth", "claude-oauth"],
        default="sub2api-oauth",
        help=(
            "Preset probe mode. api-login uses /v1/models, api-real uses /v1/responses, "
            "oidc-login uses the official OpenAI OIDC userinfo endpoint, codex-login uses ChatGPT login diagnostics, "
            "codex-real uses the Codex backend and may consume quota, sub2api-oauth checks Sub2API OAuth compatibility, "
            "claude-oauth refreshes/probes Claude OAuth accounts."
        ),
    )
    parser.add_argument(
        "--endpoint",
        default="",
        help="Probe endpoint. Default /v1/models checks whether auth is alive. Use /v1/responses for real inference.",
    )
    parser.add_argument(
        "--model",
        default="",
        help="Probe model. Defaults to gpt-5.5 for codex-real and gpt-4.1-nano otherwise.",
    )
    parser.add_argument("--concurrency", type=int, default=10, help="Concurrent checks.")
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-account HTTP timeout seconds.")
    parser.add_argument(
        "--local-expiry-guard-sec",
        type=int,
        default=60,
        help="Treat tokens expiring within this many seconds as expired before network probing.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh OAuth access tokens when needed. Slower and may be unsafe for reused CPA/Codex refresh tokens.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Only check the first N loaded accounts.")
    parser.add_argument("--no-dedupe", action="store_true", help="Do not deduplicate accounts.")
    parser.add_argument("--quiet", action="store_true", help="Hide per-account progress logs.")
    parser.add_argument(
        "--proxy",
        default=os.environ.get("SUB2API_CHECKER_PROXY", "http://127.0.0.1:7897"),
        help="Optional HTTP proxy URL, for example http://127.0.0.1:7890.",
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="Disable the default proxy.",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("outputs/results.csv"),
        help="CSV result path.",
    )
    parser.add_argument(
        "--good-bundle",
        type=Path,
        default=Path("outputs/sub2api_good_accounts.json"),
        help="Sub2API import bundle containing only ok accounts.",
    )
    parser.add_argument(
        "--bad-bundle",
        type=Path,
        default=Path("outputs/sub2api_bad_accounts.json"),
        help="Sub2API import bundle containing non-ok accounts.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.endpoint:
        endpoint = args.endpoint
    elif args.mode == "api-real":
        endpoint = "https://api.openai.com/v1/responses"
    elif args.mode == "oidc-login":
        endpoint = OPENAI_USERINFO_URL
    elif args.mode == "codex-login":
        endpoint = CHATGPT_ME_URL
    elif args.mode == "codex-real":
        endpoint = CHATGPT_CODEX_RESPONSES_URL
    elif args.mode == "claude-oauth":
        endpoint = CLAUDE_MESSAGES_URL
    elif args.mode == "sub2api-oauth":
        endpoint = SUB2API_OAUTH_COMPAT_URL
    else:
        endpoint = "https://api.openai.com/v1/models"
    if not args.model:
        if args.mode == "codex-real":
            args.model = "gpt-5.5"
        elif args.mode == "claude-oauth":
            args.model = CLAUDE_DEFAULT_MODEL
        elif args.mode == "api-real":
            args.model = "gpt-4.1-nano"
        else:
            args.model = "gpt-4.1-nano"

    accounts, errors = load_sub2api_accounts(args.inputs, dedupe=not args.no_dedupe)
    if args.limit and len(accounts) > args.limit:
        accounts = accounts[: args.limit]

    print(f"loaded_accounts={len(accounts)} parse_errors={len(errors)}")
    for err in errors[:20]:
        print(f"parse_error: {err}")
    if not accounts:
        return 2

    results = asyncio.run(
        check_many(
            accounts=accounts,
            concurrency=max(1, args.concurrency),
            timeout=args.timeout,
            endpoint=endpoint,
            model=args.model,
            local_expiry_guard_sec=args.local_expiry_guard_sec,
            refresh=args.refresh,
            proxy_url="" if args.no_proxy else args.proxy,
            progress=not args.quiet,
        )
    )

    result_by_fp = {r.account.fingerprint: r for r in results}
    ok_accounts = [a for a in accounts if result_by_fp.get(a.fingerprint) and result_by_fp[a.fingerprint].ok]
    bad_accounts = [a for a in accounts if not (result_by_fp.get(a.fingerprint) and result_by_fp[a.fingerprint].ok)]

    _write_csv(args.csv, [result.to_csv_row() for result in results])
    write_sub2api_bundle(args.good_bundle, ok_accounts)
    write_sub2api_bundle(args.bad_bundle, bad_accounts)

    counts = Counter(result.status for result in results)
    print("")
    print("summary:")
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")
    print(f"csv={args.csv}")
    print(f"good_bundle={args.good_bundle} accounts={len(ok_accounts)}")
    print(f"bad_bundle={args.bad_bundle} accounts={len(bad_accounts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
