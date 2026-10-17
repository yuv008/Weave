import json

import pytest

from app.hubspot.client import HubspotClient, HubspotRequestError
from app.hubspot.normalize import normalize_company, normalize_deal
from shared.ids import entity_id
from shared.schema import Label


TOKEN = "pat-test"


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, body):
        self.calls.append(json.loads(body.decode()))
        status, payload, hdr = self.responses.pop(0)
        return status, hdr, json.dumps(payload).encode()


def test_hubspot_company_and_deal_normalize():
    company = normalize_company(
        {
            "id": "51",
            "properties": {"name": "Acme Corp", "hs_lastmodifieddate": "1700000000000"},
        },
        tenant_id="acme",
        connection_id="hubspot-main",
        portal_id="demo-portal",
        owner_emails={},
    )
    assert company is not None
    assert company.customer_id == entity_id("acme", Label.CUSTOMER, "Acme Corp")
    deal = normalize_deal(
        {
            "id": "77",
            "properties": {
                "dealname": "Acme renewal",
                "dealstage": "negotiation",
                "closedate": "2026-12-01",
                "hs_lastmodifieddate": "1700000001000",
                "associatedcompanyid": "51",
            },
        },
        tenant_id="acme",
        connection_id="hubspot-main",
        portal_id="demo-portal",
        company_names={"51": "Acme Corp"},
        owner_emails={},
    )
    assert deal is not None
    assert deal.customer_id == entity_id("acme", Label.CUSTOMER, "Acme Corp")
    assert len(deal.document.excerpt) <= 280


def test_hubspot_search_posts_filter():
    transport = Transport([(200, {"results": [], "paging": {}}, {})])
    client = HubspotClient(TOKEN, transport=transport)
    client.search_companies(1700000000000)
    assert transport.calls[0]["filterGroups"][0]["filters"][0]["propertyName"] == "hs_lastmodifieddate"


def test_hubspot_401_not_retried():
    transport = Transport([(401, {"message": "bad token"}, {})])
    client = HubspotClient(TOKEN, transport=transport)
    with pytest.raises(HubspotRequestError) as caught:
        client.search_deals(0)
    assert caught.value.status == 401
