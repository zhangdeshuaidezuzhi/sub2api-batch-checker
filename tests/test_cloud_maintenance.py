import importlib.util
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "ops" / "sub2api_cloud_maintenance.py"
spec = importlib.util.spec_from_file_location("sub2api_cloud_maintenance", MODULE_PATH)
cloud = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(cloud)


def _args(threshold=3):
    return SimpleNamespace(
        apply=True,
        probe_limit=10,
        probe_min_interval_hours=1,
        recover_probe_limit=10,
        probe_model="gpt-5.5",
        probe_timeout=20,
        temporary_usage_limit_max_seconds=12 * 60 * 60,
        recover_delete_after_failures=threshold,
    )


def _row(previous_failures):
    return {
        "id": "101",
        "credentials": '{"access_token":"dummy"}',
        "recovery_probe_failures": str(previous_failures),
        "historical_recovery_probe_failures": "0",
    }


def _active_row(previous_failures):
    row = _row(previous_failures)
    row["previous_probe_result"] = "temporary_rate_limit" if previous_failures else ""
    return row


def _result(result_name):
    return {
        "account_id": 101,
        "result": result_name,
        "http_status": 429 if result_name != "ok" else 200,
        "error_code": "rate_limit",
        "message": "temporary rate limit",
    }


def test_recovery_probe_failure_below_threshold_pauses_again(monkeypatch) -> None:
    recorded = []
    monkeypatch.setattr(cloud, "ensure_probe_state_table", lambda apply: None)
    monkeypatch.setattr(cloud, "load_expired_temporary_pause_candidates", lambda limit: [_row(1)])
    monkeypatch.setattr(cloud, "active_probe_account", lambda *args: _result("temporary_rate_limit"))
    monkeypatch.setattr(cloud, "record_probe_state", lambda result, apply, count=None: recorded.append(count))

    decisions, results, candidate_count, recovered_count = cloud.run_expired_pause_recovery(_args(), "2026-06-10T00:00:00+00:00")

    assert candidate_count == 1
    assert recovered_count == 0
    assert recorded == [2]
    assert len(results) == 1
    assert len(decisions) == 1
    assert decisions[0].action == "pause_usage_limited"
    assert decisions[0].reason == "temporary_rate_limit_still_limited"
    assert decisions[0].evidence_count == 2


def test_recovery_probe_failure_at_threshold_soft_deletes(monkeypatch) -> None:
    recorded = []
    monkeypatch.setattr(cloud, "ensure_probe_state_table", lambda apply: None)
    monkeypatch.setattr(cloud, "load_expired_temporary_pause_candidates", lambda limit: [_row(2)])
    monkeypatch.setattr(cloud, "active_probe_account", lambda *args: _result("temporary_rate_limit"))
    monkeypatch.setattr(cloud, "record_probe_state", lambda result, apply, count=None: recorded.append(count))

    decisions, _, _, recovered_count = cloud.run_expired_pause_recovery(_args(), "2026-06-10T00:00:00+00:00")

    assert recovered_count == 0
    assert recorded == [3]
    assert len(decisions) == 1
    assert decisions[0].action == "soft_delete"
    assert decisions[0].reason == "expired_pause_recovery_failed_3_times"
    assert decisions[0].evidence_count == 3


def test_recovery_probe_ok_resets_failure_count_and_recovers(monkeypatch) -> None:
    recorded = []
    recovered = []
    monkeypatch.setattr(cloud, "ensure_probe_state_table", lambda apply: None)
    monkeypatch.setattr(cloud, "load_expired_temporary_pause_candidates", lambda limit: [_row(2)])
    monkeypatch.setattr(cloud, "active_probe_account", lambda *args: _result("ok"))
    monkeypatch.setattr(cloud, "record_probe_state", lambda result, apply, count=None: recorded.append(count))
    monkeypatch.setattr(cloud, "apply_recovery", lambda result, now_iso: recovered.append(result["account_id"]))

    decisions, _, _, recovered_count = cloud.run_expired_pause_recovery(_args(), "2026-06-10T00:00:00+00:00")

    assert decisions == []
    assert recovered_count == 1
    assert recorded == [0]
    assert recovered == [101]


def test_active_probe_failure_below_threshold_pauses_and_counts(monkeypatch) -> None:
    recorded = []
    monkeypatch.setattr(cloud, "ensure_probe_state_table", lambda apply: None)
    monkeypatch.setattr(cloud, "load_active_probe_candidates", lambda limit, interval: [_active_row(1)])
    monkeypatch.setattr(cloud, "active_probe_account", lambda *args: _result("temporary_rate_limit"))
    monkeypatch.setattr(cloud, "record_probe_state", lambda result, apply, count=None: recorded.append(count))

    decisions, results, candidate_count = cloud.run_active_probes(_args(), "2026-06-10T00:00:00+00:00")

    assert candidate_count == 1
    assert recorded == [2]
    assert len(results) == 1
    assert len(decisions) == 1
    assert decisions[0].action == "pause_usage_limited"
    assert decisions[0].reason == "active_probe_temporary_rate_limit"
    assert decisions[0].evidence_count == 2


def test_active_probe_failure_at_threshold_soft_deletes(monkeypatch) -> None:
    recorded = []
    monkeypatch.setattr(cloud, "ensure_probe_state_table", lambda apply: None)
    monkeypatch.setattr(cloud, "load_active_probe_candidates", lambda limit, interval: [_active_row(2)])
    monkeypatch.setattr(cloud, "active_probe_account", lambda *args: _result("temporary_rate_limit"))
    monkeypatch.setattr(cloud, "record_probe_state", lambda result, apply, count=None: recorded.append(count))

    decisions, _, _ = cloud.run_active_probes(_args(), "2026-06-10T00:00:00+00:00")

    assert recorded == [3]
    assert len(decisions) == 1
    assert decisions[0].action == "soft_delete"
    assert decisions[0].reason == "active_probe_failed_3_times"
    assert decisions[0].evidence_count == 3


def test_active_probe_ok_resets_failure_count(monkeypatch) -> None:
    recorded = []
    monkeypatch.setattr(cloud, "ensure_probe_state_table", lambda apply: None)
    monkeypatch.setattr(cloud, "load_active_probe_candidates", lambda limit, interval: [_active_row(2)])
    monkeypatch.setattr(cloud, "active_probe_account", lambda *args: _result("ok"))
    monkeypatch.setattr(cloud, "record_probe_state", lambda result, apply, count=None: recorded.append(count))

    decisions, _, candidate_count = cloud.run_active_probes(_args(), "2026-06-10T00:00:00+00:00")

    assert candidate_count == 1
    assert decisions == []
    assert recorded == [0]


def test_active_probe_hard_quota_soft_deletes_immediately(monkeypatch) -> None:
    recorded = []
    monkeypatch.setattr(cloud, "ensure_probe_state_table", lambda apply: None)
    monkeypatch.setattr(cloud, "load_active_probe_candidates", lambda limit, interval: [_active_row(0)])
    monkeypatch.setattr(cloud, "active_probe_account", lambda *args: _result("usage_quota_exhausted"))
    monkeypatch.setattr(cloud, "record_probe_state", lambda result, apply, count=None: recorded.append(count))

    decisions, _, _ = cloud.run_active_probes(_args(), "2026-06-10T00:00:00+00:00")

    assert recorded == [1]
    assert len(decisions) == 1
    assert decisions[0].action == "soft_delete"
    assert decisions[0].reason == "active_probe_usage_quota_exhausted"
    assert decisions[0].evidence_count == 1


def test_active_probe_auth_invalid_soft_deletes_immediately(monkeypatch) -> None:
    recorded = []
    monkeypatch.setattr(cloud, "ensure_probe_state_table", lambda apply: None)
    monkeypatch.setattr(cloud, "load_active_probe_candidates", lambda limit, interval: [_active_row(0)])
    monkeypatch.setattr(cloud, "active_probe_account", lambda *args: _result("auth_invalid_probe_only"))
    monkeypatch.setattr(cloud, "record_probe_state", lambda result, apply, count=None: recorded.append(count))

    decisions, _, _ = cloud.run_active_probes(_args(), "2026-06-10T00:00:00+00:00")

    assert recorded == [1]
    assert len(decisions) == 1
    assert decisions[0].action == "soft_delete"
    assert decisions[0].reason == "active_probe_auth_invalid_probe_only"
    assert decisions[0].evidence_count == 1


def test_maintenance_auth_evidence_soft_deletes() -> None:
    decision = cloud.classify(
        101,
        [
            {
                "source": "ops_account_maintenance_auth_audits",
                "status_code": "401",
                "error_type": "pause_auth_invalid",
                "provider_error_code": "active_probe_auth_invalid_probe_only",
                "message": "Your authentication token has been invalidated.",
                "n": "1",
                "last_seen_at": "2026-06-10 00:00:00+00",
            }
        ],
        "2026-06-10T00:00:00+00:00",
        2,
        12 * 60 * 60,
    )

    assert decision is not None
    assert decision.action == "soft_delete"
    assert decision.reason == "maintenance_auth_invalid_probe"
    assert decision.evidence_count == 1
