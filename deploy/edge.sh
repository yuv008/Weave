#!/bin/sh
# One public task: webhook on 8000, agent on 8765, proxy on 8080.
# Any other command, including the poll worker, runs as given.
set -eu

if [ -n "${GOOGLE_SERVICE_ACCOUNT_JSON:-}" ]; then
    printf '%s' "$GOOGLE_SERVICE_ACCOUNT_JSON" > /tmp/gdrive-service-account.json
    chmod 600 /tmp/gdrive-service-account.json
    export GOOGLE_SERVICE_ACCOUNT_FILE=/tmp/gdrive-service-account.json
fi

if [ "$#" -gt 0 ] && [ "$1" != "edge" ]; then
    exec "$@"
fi

mkdir -p /tmp/nginx-client /tmp/nginx-proxy /tmp/nginx-fastcgi /tmp/nginx-uwsgi /tmp/nginx-scgi

uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1 &
web_pid=$!
python -m agents.mcp --host 127.0.0.1 --port 8765 &
agent_pid=$!
nginx -c /app/deploy/edge.nginx.conf -g "daemon off;" &
proxy_pid=$!

trap 'kill "$web_pid" "$agent_pid" "$proxy_pid" 2>/dev/null || true' TERM INT

while kill -0 "$web_pid" 2>/dev/null && kill -0 "$agent_pid" 2>/dev/null && kill -0 "$proxy_pid" 2>/dev/null; do
    sleep 2
done
exit 1
