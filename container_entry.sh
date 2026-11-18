#!/bin/sh
# Container entry point: a library scan now and every SCAN_INTERVAL_HOURS (default weekly), plus the queue worker.
# Any arguments are passed to media_converterr.py instead (for example: status, retry 12, scan --dry-run).
set -eu
CONFIG=${MEDIA_CONVERTERR_CONFIG:-/config/media_converterr.toml}
APP="python3 /app/media_converterr.py --config $CONFIG"

if [ "$#" -gt 0 ]; then
    exec $APP "$@"
fi

if [ "${WEB_PORT:-8090}" != "0" ]; then
    python3 /app/web.py --config "$CONFIG" &
fi

interval=$(( ${SCAN_INTERVAL_HOURS:-168} * 3600 ))
(
    while true; do
        $APP scan || echo "media-converterr: scan failed (exit $?)" >&2
        sleep "$interval"
    done
) &

exec $APP worker
