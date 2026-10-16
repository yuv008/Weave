from fastapi.testclient import TestClient

from app.main import create_app
from app.poll import poll_once
from agents.mcp.server import build_app
from agents.runtime import Runtime
from shared.config import load_config
from tests.gate_support import NOW, fixture_resolver
from fixtures.acme import build


def test_poll_skips_connectors_that_are_not_configured():
    results = poll_once(object(), object(), {})
    assert results == {"slack": "skip", "notion": "skip", "gdrive": "skip", "github": "skip"}


def test_webhook_health_does_not_need_a_token():
    response = TestClient(create_app(graph=object(), tenant_id="tenant1")).get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_agent_health_does_not_need_a_token():
    fx = build("acme")
    resolver, _store = fixture_resolver(fx)
    app = build_app(Runtime(lambda tenant: None, load_config(), resolver=resolver, clock=lambda: NOW))
    response = TestClient(app, base_url="http://127.0.0.1:8765").get("/health")
    assert response.status_code == 200
    assert response.text == "ok"
