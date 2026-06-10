#!/usr/bin/env bash
set -euo pipefail

cd /opt/sub2api
mkdir -p /opt/sub2api/logs

exec flock -n /tmp/sub2api-cloud-maintenance.lock \
  python3 /opt/sub2api/ops/sub2api_cloud_maintenance.py \
    --lookback-hours 24 \
    --min-hard-failures 2 \
    --usage-pause-days 7 \
    --temporary-rate-pause-minutes 20 \
    --probe-active \
    --probe-limit 2 \
    --recover-probe-limit 3 \
    --probe-min-interval-hours 24 \
    --probe-timeout 20 \
    --probe-model gpt-5.5 \
    --apply
