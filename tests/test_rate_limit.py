"""Per-token limits on the model routes. No graph or Postgres: a stub app behind BearerAuth."""

from starlette.testclient import TestClient

from agents.gate.resolve import NotAuthorized
from agents.mcp.server import PER_TOKEN_PER_MINUTE, BearerAuth


async def _ok(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _resolve(token, surface):
    if not token.startswith("good"):
        raise NotAuthorized()
    return object()


def _client():
    now = [1000.0]
    app = BearerAuth(_ok, _resolve, clock=lambda: now[0])
    return TestClient(app), now


def _post(client, path, token):
    return client.post(path, headers={"authorization": f"Bearer {token}"})


def test_a_token_over_its_answer_share_gets_429_until_the_minute_passes():
    client, now = _client()
    limit = PER_TOKEN_PER_MINUTE["/answer"]
    assert all(_post(client, "/answer", "good-a").status_code == 200 for _ in range(limit))

    refused = _post(client, "/answer", "good-a")
    assert refused.status_code == 429
    assert 1 <= int(refused.headers["retry-after"]) <= 61
    # Another token and another route keep their own share.
    assert _post(client, "/answer", "good-b").status_code == 200
    assert _post(client, "/wisp/cue", "good-a").status_code == 200

    now[0] += 61
    assert _post(client, "/answer", "good-a").status_code == 200


def test_cue_has_a_wider_share_than_answer():
    client, _now = _client()
    assert PER_TOKEN_PER_MINUTE["/wisp/cue"] > PER_TOKEN_PER_MINUTE["/answer"]
    for _ in range(PER_TOKEN_PER_MINUTE["/wisp/cue"]):
        assert _post(client, "/wisp/cue", "good-a").status_code == 200
    assert _post(client, "/wisp/cue", "good-a").status_code == 429


def test_a_bad_token_is_401_and_does_not_use_a_share():
    client, _now = _client()
    for _ in range(30):
        assert _post(client, "/answer", "bad").status_code == 401
    assert _post(client, "/answer", "good-a").status_code == 200
