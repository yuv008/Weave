"""Map HubSpot CRM objects into graph-ready records."""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from app.models.canonical import EXCERPT_LIMIT
from app.models.canonical_document import CanonicalDocument
from shared.ids import entity_id
from shared.schema import Label


@dataclass(frozen=True)
class HubspotCompany:
    tenant_id: str
    connection_id: str
    portal_id: str
    company_id: str
    name: str
    modified_at: datetime
    owner_email: str | None
    version: str

    @property
    def customer_id(self) -> str:
        return entity_id(self.tenant_id, Label.CUSTOMER, self.name)

    @property
    def document(self) -> CanonicalDocument:
        excerpt = f"Customer {self.name}"
        return CanonicalDocument(
            tenant_id=self.tenant_id,
            connection_id=self.connection_id,
            source="hubspot",
            workspace_id=self.portal_id,
            external_id=f"company:{self.company_id}",
            title=self.name[:200],
            occurred_at=self.modified_at,
            updated_at=self.modified_at,
            version=self.version,
            source_url=f"https://app.hubspot.com/contacts/{self.portal_id}/company/{self.company_id}",
            excerpt=excerpt[:EXCERPT_LIMIT],
            content_hash=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
            kind="company",
            author_external_id=self.owner_email or "",
            author_email=self.owner_email or "",
        )


@dataclass(frozen=True)
class HubspotDeal:
    tenant_id: str
    connection_id: str
    portal_id: str
    deal_id: str
    name: str
    stage: str
    renewal_date: str
    company_name: str | None
    company_id: str | None
    modified_at: datetime
    owner_email: str | None
    version: str

    @property
    def deal_entity_id(self) -> str:
        return entity_id(self.tenant_id, Label.DEAL, self.name)

    @property
    def customer_id(self) -> str | None:
        if not self.company_name:
            return None
        return entity_id(self.tenant_id, Label.CUSTOMER, self.company_name)

    @property
    def document(self) -> CanonicalDocument:
        excerpt = f"Deal {self.name} ({self.stage})"
        return CanonicalDocument(
            tenant_id=self.tenant_id,
            connection_id=self.connection_id,
            source="hubspot",
            workspace_id=self.portal_id,
            external_id=f"deal:{self.deal_id}",
            title=self.name[:200],
            occurred_at=self.modified_at,
            updated_at=self.modified_at,
            version=self.version,
            source_url=f"https://app.hubspot.com/contacts/{self.portal_id}/deal/{self.deal_id}",
            excerpt=excerpt[:EXCERPT_LIMIT],
            content_hash=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
            kind="deal",
            author_external_id=self.owner_email or "",
            author_email=self.owner_email or "",
        )


def _parse_modified(value: str) -> datetime:
    if value.isdigit():
        millis = int(value)
        return datetime.fromtimestamp(millis / 1000, tz=UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def normalize_company(
    raw: dict,
    *,
    tenant_id: str,
    connection_id: str,
    portal_id: str,
    owner_emails: dict[str, str],
) -> HubspotCompany | None:
    if not isinstance(raw, dict):
        return None
    company_id = raw.get("id")
    props = raw.get("properties") if isinstance(raw.get("properties"), dict) else {}
    name = props.get("name") if isinstance(props.get("name"), str) else ""
    modified = props.get("hs_lastmodifieddate")
    if not isinstance(company_id, str) or not company_id or not name or not isinstance(modified, str):
        return None
    owner = owner_emails.get(str(props.get("hubspot_owner_id") or ""))
    when = _parse_modified(modified)
    return HubspotCompany(
        tenant_id=tenant_id,
        connection_id=connection_id,
        portal_id=portal_id,
        company_id=company_id,
        name=name,
        modified_at=when,
        owner_email=owner,
        version=modified,
    )


def normalize_deal(
    raw: dict,
    *,
    tenant_id: str,
    connection_id: str,
    portal_id: str,
    company_names: dict[str, str],
    owner_emails: dict[str, str],
) -> HubspotDeal | None:
    if not isinstance(raw, dict):
        return None
    deal_id = raw.get("id")
    props = raw.get("properties") if isinstance(raw.get("properties"), dict) else {}
    name = props.get("dealname") if isinstance(props.get("dealname"), str) else ""
    modified = props.get("hs_lastmodifieddate")
    if not isinstance(deal_id, str) or not deal_id or not name or not isinstance(modified, str):
        return None
    company_id = props.get("associatedcompanyid")
    company_name = company_names.get(str(company_id)) if company_id else None
    stage = props.get("dealstage") if isinstance(props.get("dealstage"), str) else ""
    renewal = props.get("closedate") if isinstance(props.get("closedate"), str) else ""
    owner = owner_emails.get(str(props.get("hubspot_owner_id") or ""))
    when = _parse_modified(modified)
    return HubspotDeal(
        tenant_id=tenant_id,
        connection_id=connection_id,
        portal_id=portal_id,
        deal_id=deal_id,
        name=name,
        stage=stage,
        renewal_date=renewal,
        company_name=company_name,
        company_id=str(company_id) if company_id else None,
        modified_at=when,
        owner_email=owner,
        version=modified,
    )
