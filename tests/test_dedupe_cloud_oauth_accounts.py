import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "ops" / "dedupe_cloud_oauth_accounts.py"
spec = importlib.util.spec_from_file_location("dedupe_cloud_oauth_accounts", MODULE_PATH)
dedupe = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(dedupe)


def test_audit_sql_does_not_select_raw_tokens():
    sql = dedupe.audit_sql(5)

    assert "md5(b.access_token)" in sql
    assert "left(token_hash, 10)" in sql
    assert "SELECT access_token" in sql
    assert "'sample|' || access_token" not in sql


def test_apply_sql_soft_deletes_duplicates_only():
    sql = dedupe.apply_sql()

    assert "WHERE keep_rank > 1" in sql
    assert "deleted_at = now()" in sql
    assert "DELETE FROM account_groups" in sql
    assert "DROP" not in sql.upper()
    assert "TRUNCATE" not in sql.upper()


def test_parse_lines_ignores_empty_lines():
    assert dedupe.parse_lines("a\n\n b \n") == ["a", "b"]
