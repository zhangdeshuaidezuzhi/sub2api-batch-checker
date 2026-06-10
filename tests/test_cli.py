import asyncio
import csv

from sub2api_batch_checker import cli
from sub2api_batch_checker.models import AccountRecord, CheckResult


def test_cli_uses_default_proxy(monkeypatch, tmp_path) -> None:
    token_file = tmp_path / "token.json"
    token_file.write_text(
        """
        {
          "access_token": "access-token",
          "refresh_token": "refresh-token",
          "client_id": "client-id",
          "email": "user@example.com"
        }
        """,
        encoding="utf-8",
    )
    csv_path = tmp_path / "results.csv"
    good_path = tmp_path / "good.json"
    bad_path = tmp_path / "bad.json"
    seen = {}

    async def fake_check_many(
        accounts: list[AccountRecord],
        concurrency: int,
        timeout: float,
        endpoint: str,
        model: str,
        local_expiry_guard_sec: int,
        refresh: bool,
        proxy_url: str = "",
        progress: bool = True,
        progress_callback=None,
    ):
        seen["proxy_url"] = proxy_url
        seen["refresh"] = refresh
        return [CheckResult(account=accounts[0], status="ok", ok=True, endpoint=endpoint, model=model)]

    monkeypatch.setattr(cli, "check_many", fake_check_many)
    monkeypatch.delenv("SUB2API_CHECKER_PROXY", raising=False)

    rc = cli.main(
        [
            str(token_file),
            "--csv",
            str(csv_path),
            "--good-bundle",
            str(good_path),
            "--bad-bundle",
            str(bad_path),
            "--quiet",
        ]
    )

    assert rc == 0
    assert seen["proxy_url"] == "http://127.0.0.1:7897"
    assert seen["refresh"] is False
    assert (rows := list(csv.DictReader(csv_path.open("r", encoding="utf-8-sig"))))
    assert rows[0]["endpoint"] == "sub2api://oauth-compatible"
    assert rows[0]["source_format"] == "cpa_token_json"
    assert rows[0]["ok"] == "True"


def test_cli_no_proxy_disables_default(monkeypatch, tmp_path) -> None:
    token_file = tmp_path / "token.json"
    token_file.write_text(
        '{"access_token":"access-token","refresh_token":"refresh-token","client_id":"client-id"}',
        encoding="utf-8",
    )
    seen = {}

    async def fake_check_many(*args, **kwargs):
        seen["proxy_url"] = kwargs["proxy_url"]
        account = kwargs["accounts"][0]
        return [CheckResult(account=account, status="ok", ok=True)]

    monkeypatch.setattr(cli, "check_many", fake_check_many)

    rc = cli.main(
        [
            str(token_file),
            "--no-proxy",
            "--csv",
            str(tmp_path / "results.csv"),
            "--good-bundle",
            str(tmp_path / "good.json"),
            "--bad-bundle",
            str(tmp_path / "bad.json"),
            "--quiet",
        ]
    )

    assert rc == 0
    assert seen["proxy_url"] == ""


def test_codex_real_defaults_to_gpt55(monkeypatch, tmp_path) -> None:
    token_file = tmp_path / "token.json"
    token_file.write_text(
        '{"access_token":"access-token","refresh_token":"refresh-token","client_id":"client-id"}',
        encoding="utf-8",
    )
    seen = {}

    async def fake_check_many(*args, **kwargs):
        seen["model"] = kwargs["model"]
        account = kwargs["accounts"][0]
        return [CheckResult(account=account, status="ok", ok=True, model=kwargs["model"])]

    monkeypatch.setattr(cli, "check_many", fake_check_many)

    rc = cli.main(
        [
            str(token_file),
            "--mode",
            "codex-real",
            "--csv",
            str(tmp_path / "results.csv"),
            "--good-bundle",
            str(tmp_path / "good.json"),
            "--bad-bundle",
            str(tmp_path / "bad.json"),
            "--quiet",
        ]
    )

    assert rc == 0
    assert seen["model"] == "gpt-5.5"
