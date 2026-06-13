import base64
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "ops" / "convert_register_results_to_sub2api.py"
spec = importlib.util.spec_from_file_location("convert_register_results_to_sub2api", MODULE_PATH)
converter = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(converter)

SQLGEN_PATH = ROOT / "ops" / "generate_sub2api_import_sql.py"
sql_spec = importlib.util.spec_from_file_location("generate_sub2api_import_sql", SQLGEN_PATH)
sqlgen = importlib.util.module_from_spec(sql_spec)
assert sql_spec.loader is not None
sql_spec.loader.exec_module(sqlgen)


def _jwt(payload):
    header = {"alg": "none", "typ": "JWT"}

    def enc(data):
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return "{0}.{1}.".format(enc(header), enc(payload))


def test_convert_register_results_bundle_can_generate_import_sql(tmp_path):
    results_dir = tmp_path / "results" / "batch"
    results_dir.mkdir(parents=True)
    access = _jwt(
        {
            "exp": 2000000000,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acc_123",
                "chatgpt_user_id": "user_123",
                "chatgpt_plan_type": "free",
            },
            "https://api.openai.com/profile": {"email": "user@example.com"},
        }
    )
    id_token = _jwt({"email": "user@example.com"})
    missing_id_access = _jwt(
        {
            "exp": 2000000000,
            "https://api.openai.com/profile": {"email": "skip@example.com"},
        }
    )
    (results_dir / "ok.json").write_text(
        json.dumps(
            {
                "access_token": access,
                "id_token": id_token,
                "refresh_token": "refresh",
                "bind_email": "user@example.com",
                "phone": "123",
                "cpa_ready": True,
            }
        ),
        encoding="utf-8",
    )
    (results_dir / "missing_id.json").write_text(
        json.dumps({"access_token": missing_id_access, "bind_email": "skip@example.com"}),
        encoding="utf-8",
    )

    summary = converter.convert_results(results_dir, tmp_path / "out", split=True)

    assert summary["raw_records"] == 2
    assert summary["accounts"] == 1
    assert summary["skipped"] == 1
    bundle = Path(summary["output_path"])
    payload = json.loads(bundle.read_text(encoding="utf-8"))
    assert payload["accounts"][0]["platform"] == "openai"
    assert payload["accounts"][0]["type"] == "oauth"
    assert payload["accounts"][0]["credentials"]["id_token"] == id_token
    assert Path(summary["split_dir"]).exists()

    accounts = sqlgen.load_accounts(bundle)
    sql = sqlgen.build_sql(str(bundle), accounts, "unit-test")
    assert "INSERT INTO accounts" in sql
    assert "user@example.com" in sql


def test_convert_register_results_ignores_generated_sub2api_outputs(tmp_path):
    results_dir = tmp_path / "results"
    results_dir.mkdir(parents=True)
    access = _jwt({"exp": 2000000000, "https://api.openai.com/profile": {"email": "user@example.com"}})
    id_token = _jwt({"email": "user@example.com"})
    generated = {
        "accounts": [
            {
                "name": "generated@example.com",
                "platform": "openai",
                "type": "oauth",
                "credentials": {"access_token": access, "id_token": id_token},
            }
        ]
    }
    (results_dir / "sub2api_register_fallback_1_20260613_000000.json").write_text(
        json.dumps(generated),
        encoding="utf-8",
    )

    summary = converter.convert_results(results_dir, tmp_path / "out")

    assert summary["raw_records"] == 0
    assert summary["accounts"] == 0


def test_convert_register_results_dedupes_by_account_id_before_access_token(tmp_path):
    results_dir = tmp_path / "results"
    results_dir.mkdir(parents=True)
    id_token = _jwt({"email": "user@example.com"})
    older_access = _jwt(
        {
            "exp": 2000000000,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acc_same",
                "chatgpt_user_id": "user_same",
            },
            "https://api.openai.com/profile": {"email": "old@example.com"},
        }
    )
    newer_access = _jwt(
        {
            "exp": 2000000000,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acc_same",
                "chatgpt_user_id": "user_same",
            },
            "https://api.openai.com/profile": {"email": "new@example.com"},
        }
    )
    old_file = results_dir / "old.json"
    new_file = results_dir / "new.json"
    old_file.write_text(json.dumps({"access_token": older_access, "id_token": id_token}), encoding="utf-8")
    new_file.write_text(
        json.dumps({"access_token": newer_access, "id_token": id_token, "refresh_token": "refresh"}),
        encoding="utf-8",
    )

    summary = converter.convert_results(results_dir, tmp_path / "out")

    assert summary["raw_records"] == 2
    assert summary["deduped_records"] == 1
    payload = json.loads(Path(summary["output_path"]).read_text(encoding="utf-8"))
    assert payload["accounts"][0]["credentials"]["access_token"] == newer_access
