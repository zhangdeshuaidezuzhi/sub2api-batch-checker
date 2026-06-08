from __future__ import annotations

import argparse
import asyncio
import csv
from collections import Counter
from pathlib import Path

from .checker import check_many
from .loader import load_sub2api_accounts, write_sub2api_bundle


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "ok",
        "status",
        "name",
        "platform",
        "type",
        "account_id",
        "http_status",
        "latency_ms",
        "error_code",
        "message",
        "model",
        "endpoint",
        "attempts",
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
        "--endpoint",
        default="https://api.openai.com/v1/responses",
        help="Probe endpoint. Use /v1/responses for real inference or /v1/models for lighter auth check.",
    )
    parser.add_argument("--model", default="gpt-4.1-nano", help="Probe model for /v1/responses.")
    parser.add_argument("--concurrency", type=int, default=10, help="Concurrent checks.")
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-account HTTP timeout seconds.")
    parser.add_argument(
        "--local-expiry-guard-sec",
        type=int,
        default=60,
        help="Treat tokens expiring within this many seconds as expired before network probing.",
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Do not refresh OAuth access tokens before probing.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Only check the first N loaded accounts.")
    parser.add_argument("--no-dedupe", action="store_true", help="Do not deduplicate accounts.")
    parser.add_argument("--quiet", action="store_true", help="Hide per-account progress logs.")
    parser.add_argument(
        "--proxy",
        default="",
        help="Optional HTTP proxy URL, for example http://127.0.0.1:7890.",
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
            endpoint=args.endpoint,
            model=args.model,
            local_expiry_guard_sec=args.local_expiry_guard_sec,
            refresh=not args.no_refresh,
            proxy_url=args.proxy,
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
