import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "ops" / "import_sub2api_good_bundle.py"
spec = importlib.util.spec_from_file_location("import_sub2api_good_bundle", MODULE_PATH)
importer = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(importer)


def test_safe_tag_removes_shell_sensitive_characters() -> None:
    assert importer.safe_tag("wechat 2026/06/10; rm -rf *") == "wechat_2026_06_10_rm_-rf"


def test_parse_import_output() -> None:
    parsed = importer.parse_import_output(
        """
BEGIN
inserted|2114|user@example.com
skipped|old@example.com
summary|existing_or_inserted|2
COMMIT
"""
    )

    assert parsed["inserted"] == [{"id": "2114", "name": "user@example.com"}]
    assert parsed["skipped"] == [{"name": "old@example.com"}]
    assert parsed["summary"] == ["summary|existing_or_inserted|2"]
    assert parsed["other"] == []


def test_parse_verify_output() -> None:
    rows = importer.parse_verify_output("2114|user@example.com|active|t|美国ip-24|测试组\n")

    assert rows == [
        {
            "id": "2114",
            "name": "user@example.com",
            "status": "active",
            "schedulable": "t",
            "proxy": "美国ip-24",
            "groups": "测试组",
        }
    ]


def test_build_verify_sql_quotes_names() -> None:
    sql = importer.build_verify_sql(["normal@example.com", "o'hara@example.com"])

    assert "'normal@example.com'" in sql
    assert "'o''hara@example.com'" in sql
    assert "credentials" not in sql.lower()


def test_generate_sql_normalizes_legacy_api_key_type_to_cloud_apikey() -> None:
    sql = importer.sqlgen.build_insert_sql(
        {
            "name": "hub.example.com",
            "platform": "openai",
            "type": "api_key",
            "credentials": {
                "api_key": "sk-test",
                "base_url": "https://hub.example.com",
                "model_mapping": {},
            },
        },
        "unit-test",
        1,
    )

    assert "'apikey'" in sql
    assert "    'api_key',\n" not in sql


def test_generate_sql_keeps_cloud_apikey_type() -> None:
    sql = importer.sqlgen.build_insert_sql(
        {
            "name": "hub.example.com",
            "platform": "openai",
            "type": "apikey",
            "credentials": {
                "api_key": "sk-test",
                "base_url": "https://hub.example.com",
                "model_mapping": {},
            },
        },
        "unit-test",
        1,
    )

    assert "'apikey'" in sql
    assert "model_mapping" in sql


def test_dry_run_generates_sql_without_remote_calls(tmp_path, monkeypatch) -> None:
    bundle = tmp_path / "good.json"
    bundle.write_text(
        """
        {
          "accounts": [
            {
              "name": "user@example.com",
              "platform": "openai",
              "type": "oauth",
              "credentials": {"access_token": "dummy"}
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    def fail_remote(*args, **kwargs):
        raise AssertionError("remote command should not run during dry-run")

    monkeypatch.setattr(importer, "run_command", fail_remote)

    rc = importer.main(
        [
            str(bundle),
            "--dry-run",
            "--import-tag",
            "unit-test",
            "--keep-local-sql",
        ]
    )

    assert rc == 0
    assert (ROOT / "outputs" / "unit-test.sql").exists()
    (ROOT / "outputs" / "unit-test.sql").unlink()


def test_empty_bundle_does_not_create_sql(tmp_path) -> None:
    bundle = tmp_path / "empty.json"
    output_sql = tmp_path / "empty.sql"
    bundle.write_text('{"accounts":[]}', encoding="utf-8")

    try:
        importer.main([str(bundle), "--dry-run", "--output-sql", str(output_sql)])
    except RuntimeError as exc:
        assert "no accounts" in str(exc)
    else:
        raise AssertionError("empty bundle should fail")

    assert not output_sql.exists()
