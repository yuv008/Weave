import hashlib
import json
from datetime import UTC, datetime

import pytest

from app.gmail.client import GmailClient, GmailRequestError
from app.gmail.normalize import normalize_gmail_message, parse_email_address
from app.graph.upsert_gmail import upsert_gmail_message
from tests.test_upsert import FakeGraph


TOKEN = "gmail-test-token"


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, body=None):
        self.calls.append((url, headers))
        status, payload, hdr = self.responses.pop(0)
        raw = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        return status, hdr, raw


def test_parse_email_address():
    assert parse_email_address("Sam <sam@example.com>") == "sam@example.com"


def test_gmail_normalize_caps_excerpt():
    raw = {
        "id": "m1",
        "threadId": "t1",
        "internalDate": "1700000000000",
        "snippet": "x" * 400,
        "payload": {"headers": [{"name": "Subject", "value": "Hello"}, {"name": "From", "value": "a@b.com"}]},
    }
    message = normalize_gmail_message(
        raw, tenant_id="demo", connection_id="gmail-main", mailbox="sales@example.com"
    )
    assert len(message.excerpt) <= 280
    assert message.author_email == "a@b.com"


def test_gmail_upsert_uses_parameterized_excerpt():
    raw = {
        "id": "m2",
        "threadId": "t2",
        "internalDate": "1700000001000",
        "snippet": "body",
        "payload": {"headers": [{"name": "Subject", "value": "Hi"}, {"name": "From", "value": "a@b.com"}]},
    }
    message = normalize_gmail_message(
        raw, tenant_id="acme", connection_id="gmail-main", mailbox="sales@example.com"
    )
    graph = FakeGraph([[True]])
    result = upsert_gmail_message(graph, message)
    assert result.applied is True
    assert graph.calls[0][1]["excerpt"] == message.excerpt


def test_gmail_client_retries_503():
    transport = Transport([(503, {}, {}), (200, {"messages": []}, {})])
    client = GmailClient(
        "sales@example.com",
        token=TOKEN,
        transport=transport,
        sleep=lambda _: None,
        random_fn=lambda: 0.0,
    )
    page = client.list_messages()
    assert page["messages"] == []


def test_gmail_metadata_requests_each_header():
    transport = Transport([(200, {"id": "m1", "payload": {"headers": []}}, {})])
    client = GmailClient("sales@example.com", token=TOKEN, transport=transport)
    client.get_message("m1")
    url = transport.calls[0][0]
    assert url.count("metadataHeaders=") == 5
    assert "metadataHeaders=Subject" in url


def test_gmail_403_not_retried():
    transport = Transport([(403, {"error": {"message": "denied"}}, {})])
    client = GmailClient("sales@example.com", token=TOKEN, transport=transport)
    with pytest.raises(GmailRequestError) as caught:
        client.list_messages()
    assert caught.value.status == 403
