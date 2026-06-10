from __future__ import annotations

from sub2api_batch_checker.classify import classify_failure


def test_weekly_limit_is_temporary_quota_state() -> None:
    assert classify_failure(429, "weekly quota has been reached") == "quota_or_rate_limited"
    assert classify_failure(429, "已达到本周限额") == "quota_or_rate_limited"


def test_usage_limit_is_not_auth_invalid_even_when_refresh_is_mentioned() -> None:
    assert (
        classify_failure(429, "usage limit reached; manual refresh may recover this account")
        == "quota_or_rate_limited"
    )
