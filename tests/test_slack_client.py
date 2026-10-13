import json

import pytest

from app.slack.client import SlackClient, SlackRequestError

TOKEN = "xoxb-test-token"


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, data=None):
        self.calls.append((url, headers, data))
        status, body, header = self.responses.pop(0)
        if isinstance(body, Exception):
            raise body
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        return status, header, payload


def _client(responses, random_fn=lambda: 0.0):
    transport = Transport(responses)
    sleeps = []
    client = SlackClient(TOKEN, transport=transport, sleep=sleeps.append, random_fn=random_fn)
    return client, transport, sleeps


def test_public_channel_list_sends_the_token_only_in_the_header():
    client, transport, _sleeps = _client([(200, {"ok": True, "channels": []}, {})])

    body = client.list_public_channels()

    assert body["channels"] == []
    url, headers, _data = transport.calls[0]
    assert "types=public_channel" in url
    assert "limit=200" in url
    assert TOKEN not in url
    assert headers["Authorization"] == f"Bearer {TOKEN}"


def test_history_and_replies_use_their_methods():
    client, transport, _sleeps = _client(
        [
            (200, {"ok": True, "messages": []}, {}),
            (200, {"ok": True, "messages": []}, {}),
        ]
    )

    client.history("C1", cursor="cursor-1", oldest="10.1")
    client.fetch_message("C1", "10.1")

    history_url = transport.calls[0][0]
    fetch_url = transport.calls[1][0]
    assert "conversations.history" in history_url
    assert "cursor=cursor-1" in history_url
    assert "oldest=10.1" in history_url
    assert "conversations.replies" in fetch_url
    assert "conversations.history" not in fetch_url


def test_rate_limit_retries_after_the_header():
    client, transport, sleeps = _client(
        [
            (429, {"ok": False, "error": "ratelimited"}, {"Retry-After": "2"}),
            (200, {"ok": True, "channels": []}, {}),
        ]
    )

    client.list_public_channels()

    assert sleeps == [2.0]
    assert len(transport.calls) == 2


def test_server_error_retries_then_succeeds():
    client, transport, sleeps = _client(
        [
            (503, b"unavailable", {}),
            (200, {"ok": True, "messages": []}, {}),
        ]
    )

    client.history("C1")

    assert len(sleeps) == 1
    assert len(transport.calls) == 2


def test_network_error_retries():
    client, transport, sleeps = _client(
        [
            (0, OSError("reset"), {}),
            (200, {"ok": True, "messages": []}, {}),
        ]
    )

    client.replies("C1", "10.1")

    assert len(sleeps) == 1
    assert len(transport.calls) == 2


def test_channel_not_found_is_not_retried():
    client, transport, sleeps = _client([(200, {"ok": False, "error": "channel_not_found"}, {})])

    with pytest.raises(SlackRequestError) as caught:
        client.history("C1")

    assert caught.value.error == "channel_not_found"
    assert sleeps == []
    assert len(transport.calls) == 1


def test_http_4xx_is_not_retried():
    client, transport, sleeps = _client([(400, {"ok": False, "error": "invalid_arguments"}, {})])

    with pytest.raises(SlackRequestError) as caught:
        client.list_public_channels()

    assert caught.value.status == 400
    assert sleeps == []
    assert len(transport.calls) == 1
