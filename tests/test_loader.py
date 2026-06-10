from pathlib import Path
import zipfile

from sub2api_batch_checker.loader import load_sub2api_accounts


def test_load_cpa_token_json(tmp_path: Path) -> None:
    token_file = tmp_path / "user@example.com.json"
    token_file.write_text(
        """
        {
          "rt": "refresh-token",
          "refresh_token": "refresh-token",
          "access_token": "access-token",
          "id_token": "id-token",
          "account_id": "account-id",
          "email": "user@example.com",
          "type": "codex",
          "last_refresh": "2026-06-08T00:00:00Z",
          "expired": "2026-06-09T00:00:00Z",
          "scope": "openid email profile offline_access",
          "token_type": "bearer",
          "authorization_code": "authorization-code",
          "oauth_start": {
            "client_id": "app-client-id",
            "code_verifier": "code-verifier",
            "scope": "openid email profile offline_access"
          }
        }
        """,
        encoding="utf-8",
    )

    accounts, errors = load_sub2api_accounts([token_file], dedupe=True)

    assert errors == []
    assert len(accounts) == 1
    account = accounts[0]
    credentials = account.raw["credentials"]
    assert account.name == "user@example.com"
    assert account.platform == "openai"
    assert account.account_type == "oauth"
    assert credentials["access_token"] == "access-token"
    assert credentials["refresh_token"] == "refresh-token"
    assert credentials["client_id"] == "app-client-id"
    assert account.raw["extra"]["source_format"] == "cpa_token_json"
    assert "authorization_code" not in credentials
    assert "code_verifier" not in credentials


def test_load_sub2api_bundle_keeps_oauth_account(tmp_path: Path) -> None:
    bundle_file = tmp_path / "sub2api.json"
    bundle_file.write_text(
        """
        {
          "accounts": [
            {
              "name": "sub2api-user",
              "platform": "openai",
              "type": "oauth",
              "credentials": {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "client_id": "client-id",
                "chatgpt_account_id": "account-id"
              }
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    accounts, errors = load_sub2api_accounts([bundle_file], dedupe=True)

    assert errors == []
    assert len(accounts) == 1
    assert accounts[0].platform == "openai"
    assert accounts[0].account_type == "oauth"
    assert accounts[0].raw["credentials"]["client_id"] == "client-id"
    assert accounts[0].raw["extra"]["source_format"] == "sub2api_bundle"


def test_load_raw_sub2api_account_list(tmp_path: Path) -> None:
    list_file = tmp_path / "accounts.json"
    list_file.write_text(
        """
        [
          {
            "name": "list-user",
            "platform": "openai",
            "type": "oauth",
            "credentials": {
              "access_token": "access-token",
              "id_token": "id-token"
            }
          }
        ]
        """,
        encoding="utf-8",
    )

    accounts, errors = load_sub2api_accounts([list_file], dedupe=True)

    assert errors == []
    assert len(accounts) == 1
    assert accounts[0].raw["extra"]["source_format"] == "sub2api_account_list"


def test_load_cpa_without_refresh_token_for_login_diagnosis(tmp_path: Path) -> None:
    token_file = tmp_path / "codex-only.json"
    token_file.write_text(
        """
        {
          "access_token": "access-token",
          "id_token": "id-token",
          "account_id": "account-id",
          "email": "codex@example.com",
          "type": "codex",
          "oauth_start": {
            "client_id": "app-client-id"
          }
        }
        """,
        encoding="utf-8",
    )

    accounts, errors = load_sub2api_accounts([token_file], dedupe=True)

    assert errors == []
    assert len(accounts) == 1
    credentials = accounts[0].raw["credentials"]
    assert credentials["access_token"] == "access-token"
    assert "refresh_token" not in credentials
    assert accounts[0].raw["extra"]["recommended_check_mode"] == "sub2api-oauth"


def test_load_cpa_token_list_json(tmp_path: Path) -> None:
    token_file = tmp_path / "agi_offer.json"
    token_file.write_text(
        """
        [
          {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "id_token": "id-token",
            "account_id": "account-id",
            "email": "agi-offer@example.com",
            "type": "codex",
            "last_refresh": "2026-06-08T11:17:21.000Z",
            "expired": "2026-06-18T11:17:22.000Z"
          }
        ]
        """,
        encoding="utf-8",
    )

    accounts, errors = load_sub2api_accounts([token_file], dedupe=True)

    assert errors == []
    assert len(accounts) == 1
    account = accounts[0]
    credentials = account.raw["credentials"]
    assert account.name == "agi-offer@example.com"
    assert account.platform == "openai"
    assert account.account_type == "oauth"
    assert credentials["refresh_token"] == "refresh-token"
    assert account.raw["extra"]["source_format"] == "cpa_token_list_json"


def test_load_refresh_token_only_claude_json(tmp_path: Path) -> None:
    token_file = tmp_path / "refresh-only.json"
    token_file.write_text(
        """
        {
          "email": "refresh-only@example.com",
          "password": "not-imported",
          "rt": "refresh-token"
        }
        """,
        encoding="utf-8",
    )

    accounts, errors = load_sub2api_accounts([token_file], dedupe=True)

    assert errors == []
    assert len(accounts) == 1
    account = accounts[0]
    credentials = account.raw["credentials"]
    assert account.name == "refresh-only@example.com"
    assert account.platform == "anthropic"
    assert credentials["refresh_token"] == "refresh-token"
    assert credentials["password"] == "not-imported"
    assert account.raw["extra"]["source_format"] == "cpa_token_json"
    assert account.raw["extra"]["source_type"] == "claude"


def test_unsupported_json_shape_stays_parse_error(tmp_path: Path) -> None:
    api_key_file = tmp_path / "api-key-only.json"
    api_key_file.write_text('{"api_key":"sk-test"}', encoding="utf-8")

    accounts, errors = load_sub2api_accounts([api_key_file], dedupe=True)

    assert accounts == []
    assert len(errors) == 1
    assert "unsupported_json_shape" in errors[0]


def test_load_openai_api_key_with_base_url(tmp_path: Path) -> None:
    api_key_file = tmp_path / "api-key-upstream.json"
    api_key_file.write_text(
        '{"name":"hub","base_url":"https://hub.example.com/","api_key":"sk-test"}',
        encoding="utf-8",
    )

    accounts, errors = load_sub2api_accounts([api_key_file], dedupe=True)

    assert errors == []
    assert len(accounts) == 1
    account = accounts[0]
    assert account.platform == "openai"
    assert account.account_type == "apikey"
    assert account.raw["credentials"]["api_key"] == "sk-test"
    assert account.raw["credentials"]["base_url"] == "https://hub.example.com"
    assert account.raw["credentials"]["model_mapping"] == {}
    assert account.raw["extra"]["source_format"] == "openai_api_key_json"


def test_load_cpa_json_from_zip(tmp_path: Path) -> None:
    zip_file = tmp_path / "delivery.zip"
    token_json = """
        {
          "rt": "refresh-token",
          "access_token": "access-token",
          "account_id": "account-id",
          "email": "zip-user@example.com",
          "type": "codex",
          "expired": "2026-06-09T00:00:00Z",
          "oauth_start": {
            "client_id": "app-client-id"
          }
        }
        """
    with zipfile.ZipFile(zip_file, "w") as archive:
        archive.writestr("tokens/zip-user@example.com.json", token_json)
        archive.writestr("README.txt", "not json")

    accounts, errors = load_sub2api_accounts([zip_file], dedupe=True)

    assert errors == []
    assert len(accounts) == 1
    assert accounts[0].name == "zip-user@example.com"
    assert accounts[0].source_file.endswith("delivery.zip!tokens/zip-user@example.com.json")
    assert accounts[0].raw["credentials"]["client_id"] == "app-client-id"
