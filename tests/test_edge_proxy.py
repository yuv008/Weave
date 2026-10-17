"""The public edge sends agent routes to the agent and everything else to the webhook."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROXY = (ROOT / "deploy" / "edge.nginx.conf").read_text(encoding="utf-8")
ENTRY = (ROOT / "deploy" / "edge.sh").read_text(encoding="utf-8")


def test_agent_paths_reach_the_agent_and_the_rest_reach_the_webhook():
    for path in ("/mcp", "/answer", "/actions", "/wisp", "/ops"):
        assert f"location {path} " in PROXY
    agent = PROXY.split("location / {", 1)[0]
    webhook = PROXY.split("location / {", 1)[1]
    assert agent.count("proxy_pass http://127.0.0.1:8765;") == 5
    assert "proxy_pass http://127.0.0.1:8000;" in webhook
    assert "X-Forwarded-Proto" in PROXY


def test_entrypoint_keeps_a_worker_command_and_trusts_the_local_proxy():
    assert 'exec "$@"' in ENTRY
    assert "--proxy-headers" in ENTRY
    assert "--forwarded-allow-ips=127.0.0.1" in ENTRY
    assert "nginx -c /app/deploy/edge.nginx.conf" in ENTRY
