"""Upsert HubSpot companies and deals into the company graph."""

from datetime import UTC, datetime

from app.graph.policy import allowed_source, hubspot_source_id
from app.graph.upsert import UpsertResult
from app.graph.upsert_document import upsert_document
from app.hubspot.normalize import HubspotCompany, HubspotDeal
from shared.ids import person_id
from shared.config import source_acl
from shared.schema import Label, Prop, Rel
from shared.timefmt import to_ts


def upsert_hubspot_company(graph, company: HubspotCompany) -> UpsertResult:
    allowed = allowed_source("hubspot", company.portal_id, company.tenant_id)
    if allowed is None:
        return UpsertResult(node_id=company.customer_id, applied=False)
    group, policy = allowed
    now = to_ts(datetime.now(UTC))
    graph.query(
        f"""
MERGE (c:{Label.CUSTOMER} {{id: $customer_id}})
SET
  c.{Prop.TENANT_ID} = $tenant_id,
  c.{Prop.NAME} = $name,
  c.provisional = false
""",
        {"customer_id": company.customer_id, "tenant_id": company.tenant_id, "name": company.name},
    )
    doc = upsert_document(graph, company.document)
    if doc.applied:
        graph.query(
            f"""
MERGE (d:{Label.DOCUMENT}:{Label.ITEM} {{id: $doc_id}})
MERGE (cust:{Label.CUSTOMER} {{id: $customer_id}})
MERGE (d)-[:{Rel.MENTIONS}]->(cust)
""",
            {"doc_id": company.document.node_id, "customer_id": company.customer_id},
        )
    if company.owner_email and doc.applied:
        graph.query(
            f"""
MERGE (p:{Label.PERSON} {{id: $person_id}})
MERGE (cust:{Label.CUSTOMER} {{id: $customer_id}})
MERGE (p)-[:{Rel.OWNS}]->(cust)
""",
            {
                "person_id": person_id(company.tenant_id, company.owner_email),
                "customer_id": company.customer_id,
            },
        )
    _touch_source(graph, company, group, policy, now)
    return UpsertResult(node_id=company.customer_id, applied=True)


def upsert_hubspot_deal(graph, deal: HubspotDeal) -> UpsertResult:
    allowed = allowed_source("hubspot", deal.portal_id, deal.tenant_id)
    if allowed is None:
        return UpsertResult(node_id=deal.deal_entity_id, applied=False)
    group, policy = allowed
    now = to_ts(datetime.now(UTC))
    graph.query(
        f"""
MERGE (d:{Label.DEAL} {{id: $deal_id}})
SET
  d.{Prop.TENANT_ID} = $tenant_id,
  d.{Prop.NAME} = $name,
  d.{Prop.STAGE} = $stage,
  d.{Prop.RENEWAL_DATE} = $renewal,
  d.provisional = false
""",
        {
            "deal_id": deal.deal_entity_id,
            "tenant_id": deal.tenant_id,
            "name": deal.name,
            "stage": deal.stage,
            "renewal": deal.renewal_date or "",
        },
    )
    if deal.customer_id:
        graph.query(
            f"""
MERGE (deal:{Label.DEAL} {{id: $deal_id}})
MERGE (cust:{Label.CUSTOMER} {{id: $customer_id}})
MERGE (deal)-[:{Rel.FOR}]->(cust)
""",
            {"deal_id": deal.deal_entity_id, "customer_id": deal.customer_id},
        )
    doc = upsert_document(graph, deal.document)
    if doc.applied and deal.customer_id:
        graph.query(
            f"""
MERGE (d:{Label.DOCUMENT}:{Label.ITEM} {{id: $doc_id}})
MERGE (cust:{Label.CUSTOMER} {{id: $customer_id}})
MERGE (d)-[:{Rel.MENTIONS}]->(cust)
""",
            {"doc_id": deal.document.node_id, "customer_id": deal.customer_id},
        )
    if deal.owner_email and doc.applied:
        graph.query(
            f"""
MERGE (p:{Label.PERSON} {{id: $person_id}})
MERGE (deal:{Label.DEAL} {{id: $deal_id}})
MERGE (p)-[:{Rel.OWNS}]->(deal)
""",
            {
                "person_id": person_id(deal.tenant_id, deal.owner_email),
                "deal_id": deal.deal_entity_id,
            },
        )
    _touch_source(graph, deal, group, policy, now)
    return UpsertResult(node_id=deal.deal_entity_id, applied=True)


def _touch_source(graph, record, group: str, policy, now: str) -> None:
    portal = record.portal_id
    tenant_id = record.tenant_id
    graph.query(
        f"""
MERGE (s:{Label.SOURCE} {{id: $source_id}})
SET
  s.{Prop.TENANT_ID} = $tenant_id,
  s.{Prop.SOURCE} = 'hubspot',
  s.{Prop.EXTERNAL_ID} = $portal,
  s.{Prop.NAME} = $source_name,
  s.{Prop.ACL_GROUPS} = $acl_groups,
  s.{Prop.MIN_LEVEL} = $min_level,
  s.{Prop.CASCADE_POLICY} = $cascade_policy,
  s.{Prop.AUTHORITY} = $authority,
  s.last_sync_at = $now
""",
        {
            "source_id": hubspot_source_id(tenant_id, portal),
            "tenant_id": tenant_id,
            "portal": portal,
            "source_name": policy.name,
            "acl_groups": source_acl(group, policy),
            "min_level": policy.min_level,
            "cascade_policy": policy.cascade_policy,
            "authority": policy.authority,
            "now": now,
        },
    )
