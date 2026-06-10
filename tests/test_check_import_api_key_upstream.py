import importlib.util
from pathlib import Path

from sub2api_batch_checker.models import AccountRecord, CheckResult


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "ops" / "check_import_api_key_upstream.py"
spec = importlib.util.spec_from_file_location("check_import_api_key_upstream", MODULE_PATH)
api_import = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(api_import)


def _parser_args(*args: str):
    return api_import.build_parser().parse_args(
        [
            "--base-url",
            "https://hub.example.com",
            "--api-key",
            "sk-test",
            "--import-tag",
            "unit_test",
            "--no-proxy",
            *args,
        ]
    )


def _account() -> AccountRecord:
    raw = {
        "name": "hub.example.com",
        "platform": "openai",
        "type": "apikey",
        "credentials": {
            "api_key": "sk-test",
            "base_url": "https://hub.example.com",
            "model_mapping": {},
        },
    }
    return AccountRecord(
        source_file="api-key.json",
        index=0,
        raw=raw,
        name="hub.example.com",
        platform="openai",
        account_type="apikey",
        fingerprint="api-key-fp",
    )


def _result(ok: bool, status: str = "ok", sample_model: str = "") -> CheckResult:
    return CheckResult(
        account=_account(),
        status=status,
        ok=ok,
        http_status=200 if ok else 403,
        raw_meta={"sample_model": sample_model} if sample_model else {},
    )


def _write_temp_input(tmp_path: Path):
    created = []

    def fake_write_temp_input(base_url, api_key, name):
        path = tmp_path / "input.json"
        path.write_text(
            '{"name":"%s","base_url":"%s","api_key":"%s"}' % (name, base_url, api_key),
            encoding="utf-8",
        )
        created.append(path)
        return path

    return created, fake_write_temp_input


def test_failed_compat_result_discards_without_cloud_import(tmp_path, monkeypatch) -> None:
    created, fake_write_temp_input = _write_temp_input(tmp_path)
    called = {"cloud": False}

    async def fake_check_one(accounts, endpoint, model, timeout, proxy_url):
        assert endpoint == "/v1/models"
        return _result(False, "forbidden_or_banned")

    def fake_cloud_import(args):
        called["cloud"] = True
        return 0

    monkeypatch.setattr(api_import, "write_temp_input", fake_write_temp_input)
    monkeypatch.setattr(api_import, "check_one", fake_check_one)
    monkeypatch.setattr(api_import, "cloud_import_bundle", fake_cloud_import)
    monkeypatch.setattr(api_import.tempfile, "gettempdir", lambda: str(tmp_path))

    rc = api_import.run(_parser_args())

    assert rc == 1
    assert called["cloud"] is False
    assert created and not created[0].exists()
    assert not (tmp_path / "unit_test_good.json").exists()


def test_successful_compat_and_real_result_calls_cloud_import(tmp_path, monkeypatch) -> None:
    created, fake_write_temp_input = _write_temp_input(tmp_path)
    calls = {"checks": [], "cloud_bundle": None}

    async def fake_check_one(accounts, endpoint, model, timeout, proxy_url):
        calls["checks"].append((endpoint, model))
        if endpoint == "/v1/models":
            return _result(True, sample_model="gpt-test")
        return _result(True)

    def fake_cloud_import(args):
        calls["cloud_bundle"] = args.bundle
        return 0

    monkeypatch.setattr(api_import, "write_temp_input", fake_write_temp_input)
    monkeypatch.setattr(api_import, "check_one", fake_check_one)
    monkeypatch.setattr(api_import, "cloud_import_bundle", fake_cloud_import)
    monkeypatch.setattr(api_import.tempfile, "gettempdir", lambda: str(tmp_path))

    rc = api_import.run(_parser_args())

    assert rc == 0
    assert calls["checks"] == [("/v1/models", "auto"), ("/v1/chat/completions", "gpt-test")]
    assert calls["cloud_bundle"] == tmp_path / "unit_test_good.json"
    assert created and not created[0].exists()
    assert not (tmp_path / "unit_test_good.json").exists()


def test_env_var_input_path_works_without_cli_api_key(tmp_path, monkeypatch) -> None:
    captured = {}
    created, fake_write_temp_input = _write_temp_input(tmp_path)

    async def fake_check_one(accounts, endpoint, model, timeout, proxy_url):
        return _result(True, sample_model="gpt-test")

    def capture_write_temp_input(base_url, api_key, name):
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        return fake_write_temp_input(base_url, api_key, name)

    monkeypatch.setenv("SUB2API_TEST_BASE_URL", "https://env.example.com/")
    monkeypatch.setenv("SUB2API_TEST_API_KEY", "sk-env-test")
    monkeypatch.setattr(api_import, "write_temp_input", capture_write_temp_input)
    monkeypatch.setattr(api_import, "check_one", fake_check_one)
    monkeypatch.setattr(api_import.tempfile, "gettempdir", lambda: str(tmp_path))

    args = api_import.build_parser().parse_args(["--import-tag", "unit_test", "--no-import", "--no-proxy"])
    rc = api_import.run(args)

    assert rc == 0
    assert captured == {"base_url": "https://env.example.com", "api_key": "sk-env-test"}
    assert created and not created[0].exists()
    assert not (tmp_path / "unit_test_good.json").exists()


def test_keep_good_bundle_preserves_good_file(tmp_path, monkeypatch) -> None:
    created, fake_write_temp_input = _write_temp_input(tmp_path)

    async def fake_check_one(accounts, endpoint, model, timeout, proxy_url):
        return _result(True, sample_model="gpt-test")

    monkeypatch.setattr(api_import, "write_temp_input", fake_write_temp_input)
    monkeypatch.setattr(api_import, "check_one", fake_check_one)
    monkeypatch.setattr(api_import.tempfile, "gettempdir", lambda: str(tmp_path))

    rc = api_import.run(_parser_args("--no-import", "--keep-good-bundle"))

    assert rc == 0
    assert created and not created[0].exists()
    assert (tmp_path / "unit_test_good.json").exists()
