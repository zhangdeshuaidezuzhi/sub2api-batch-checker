#!/usr/bin/env python3
"""Fallback import for register-console results missed by automatic upload.

Flow:
1. Convert local raw register results to a Sub2API bundle.
2. Import the bundle through the existing cloud SQL/SSH workflow.

This is intentionally a local-to-cloud fallback for http://127.0.0.1:18766/
upload misses. It does not use local Docker.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import convert_register_results_to_sub2api as converter
import import_sub2api_good_bundle as cloud_import


DEFAULT_RESULTS_DIR = Path(r"D:\注册机最新版\results")
DEFAULT_OUTPUT_DIR = Path(r"D:\注册机最新版\sub2JOSN出售")
DEFAULT_STATE_PATH = Path("outputs") / "register_results_fallback_state.json"


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def find_latest_batch(results_dir: Path) -> Path | None:
    if not results_dir.exists():
        return None
    candidates = [path for path in results_dir.iterdir() if path.is_dir() and path.name.startswith("批次_")]
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_mtime, path.name))


def resolve_results_dir(args) -> tuple[Path, str, dict]:
    explicit = args.results_dir is not None
    base_dir = args.results_dir or args.base_results_dir
    if args.full:
        return base_dir, "full_forced", {}
    if args.latest:
        latest = find_latest_batch(base_dir)
        return latest or base_dir, "latest_forced" if latest else "latest_missing_fallback_full", {}
    if explicit:
        return base_dir, "explicit", {}

    state = load_state(args.state_path)
    if state.get("initial_full_completed"):
        latest = find_latest_batch(base_dir)
        return latest or base_dir, "latest_after_initial" if latest else "latest_missing_fallback_full", state
    return base_dir, "initial_full", state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert register-machine results and import them into cloud Sub2API.")
    parser.add_argument("--results-dir", type=Path, default=None, help="Register results root or one batch folder.")
    parser.add_argument("--base-results-dir", type=Path, default=DEFAULT_RESULTS_DIR, help="Default results root for automatic first-full-then-latest mode.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Where converted bundle/skipped files are written.")
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH, help="Local state file for first-full-then-latest mode.")
    parser.add_argument("--full", action="store_true", help="Force scanning the full results root.")
    parser.add_argument("--latest", action="store_true", help="Force scanning only the latest 批次_* folder.")
    parser.add_argument("--import-tag", default="", help="Cloud import tag. Default is register_fallback_YYYYmmdd_HHMMSS.")
    parser.add_argument("--dry-run", action="store_true", help="Convert and generate SQL only; do not SSH import.")
    parser.add_argument("--allow-missing-id-token", action="store_true", help="Not recommended for OpenAI/Codex.")
    parser.add_argument("--no-split", action="store_true", help="Only write merged bundle.")
    parser.add_argument("--keep-local-sql", action="store_true")
    parser.add_argument("--keep-remote-sql", action="store_true")
    parser.add_argument("--ssh-key", default=cloud_import.DEFAULT_SSH_KEY)
    parser.add_argument("--ssh-target", default=cloud_import.DEFAULT_SSH_TARGET)
    parser.add_argument("--remote-sql-dir", default=cloud_import.DEFAULT_REMOTE_SQL_DIR)
    parser.add_argument("--remote-project-dir", default=cloud_import.DEFAULT_REMOTE_PROJECT_DIR)
    parser.add_argument("--psql-command", default=cloud_import.DEFAULT_PSQL_COMMAND)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--quiet", action="store_true")
    return parser


def make_import_args(args, bundle_path: Path, import_tag: str):
    return SimpleNamespace(
        bundle=bundle_path,
        import_tag=import_tag,
        ssh_key=args.ssh_key,
        ssh_target=args.ssh_target,
        remote_sql_dir=args.remote_sql_dir,
        remote_project_dir=args.remote_project_dir,
        psql_command=args.psql_command,
        output_sql=None,
        keep_local_sql=args.keep_local_sql or args.dry_run,
        keep_remote_sql=args.keep_remote_sql,
        dry_run=args.dry_run,
        quiet=args.quiet,
        timeout=args.timeout,
    )


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    resolved_results_dir, selection_mode, state = resolve_results_dir(args)
    print("selected_results_dir={0}".format(resolved_results_dir))
    print("selection_mode={0}".format(selection_mode))
    summary = converter.convert_results(
        resolved_results_dir,
        args.output_dir,
        require_id_token=not args.allow_missing_id_token,
        split=not args.no_split,
    )
    print("converted_accounts={0}".format(summary["accounts"]))
    print("skipped={0}".format(summary["skipped"]))
    print("bundle={0}".format(summary["output_path"]))
    print("skipped_path={0}".format(summary["skipped_path"]))
    if summary.get("split_dir"):
        print("split_dir={0}".format(summary["split_dir"]))

    if int(summary["accounts"]) <= 0:
        print("import_skipped=no_accounts")
        return 0

    import_tag = cloud_import.safe_tag(
        args.import_tag or "register_fallback_{0}".format(datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    )
    rc = cloud_import.import_bundle(make_import_args(args, Path(summary["output_path"]), import_tag))
    if rc == 0 and not args.dry_run and selection_mode in {"initial_full", "full_forced"}:
        state.update(
            {
                "initial_full_completed": True,
                "initial_full_completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "initial_full_results_dir": str(resolved_results_dir),
                "last_import_tag": import_tag,
            }
        )
        write_state(args.state_path, state)
        print("state_updated={0}".format(args.state_path))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
