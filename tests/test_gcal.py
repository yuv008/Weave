import json

import pytest

from app.gcal.client import CalendarClient, CalendarRequestError
from app.gcal.normalize import normalize_calendar_event
from app.models.canonical_document import CanonicalDocument


TOKEN = "gcal-test-token"


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)

    def __call__(self, url, headers, body=None):
        status, payload, hdr = self.responses.pop(0)
        return status, hdr, json.dumps(payload).encode()


def test_calendar_event_excerpt_and_tombstone():
    live = normalize_calendar_event(
        {
            "id": "evt1",
            "updated": "2026-03-11T10:00:00.000Z",
            "summary": "Leadership review",
            "start": {"dateTime": "2026-03-11T15:00:00Z"},
            "end": {"dateTime": "2026-03-11T16:00:00Z"},
            "organizer": {"email": "ceo@example.com"},
        },
        tenant_id="demo",
        connection_id="gcal-main",
        calendar_id="primary",
    )
    assert isinstance(live, CanonicalDocument)
    assert live.kind == "event"
    assert "Leadership review" in live.excerpt
    cancelled = normalize_calendar_event(
        {
            "id": "evt1",
            "status": "cancelled",
            "updated": "2026-03-11T10:00:00.000Z",
            "summary": "Leadership review",
            "start": {"dateTime": "2026-03-11T15:00:00Z"},
            "end": {"dateTime": "2026-03-11T16:00:00Z"},
        },
        tenant_id="demo",
        connection_id="gcal-main",
        calendar_id="primary",
    )
    assert cancelled.deleted is True
    assert cancelled.excerpt == ""


def test_calendar_client_410_raises():
    transport = Transport([(410, {"error": {"message": "gone"}}, {})])
    client = CalendarClient("primary", token=TOKEN, transport=transport)
    with pytest.raises(CalendarRequestError) as caught:
        client.list_events("stale-token")
    assert caught.value.status == 410
