"""Poll HubSpot companies and deals."""

import logging
import time

from app.graph.policy import allowed_source, hubspot_source_id
from app.graph.upsert import ensure_indexes
from app.graph.upsert_hubspot import upsert_hubspot_company, upsert_hubspot_deal
from app.hubspot.normalize import normalize_company, normalize_deal
from app.ingest.chunks import index_source
from app.ingest.log import format_document_log
from app.ingest.records import SYNC_STATE, open_store
from app.ingest.state import write_connection

logger = logging.getLogger("weave.hubspot")


def hubspot_sync_id(tenant_id: str, portal_id: str) -> str:
    return f"{tenant_id}:hubspot:sync:{portal_id}"


def poll_portal(
    tenant_id: str,
    connection_id: str,
    portal_id: str,
    client,
    graph,
    store=None,
    *,
    owner_emails: dict[str, str] | None = None,
) -> None:
    found = allowed_source("hubspot", portal_id)
    if found is None:
        return
    group, _policy = found
    records = open_store(store)
    ensure_indexes(graph)
    write_connection(
        graph,
        tenant_id=tenant_id,
        connection_id=connection_id,
        workspace_id=portal_id,
        source="hubspot",
    )
    sync_id = hubspot_sync_id(tenant_id, portal_id)
    state = _read(records, sync_id)
    watermark = int(state.get("modified_after_ms") or 0)
    owners = owner_emails or {}
    company_names: dict[str, str] = {}
    started = time.perf_counter()
    greatest = watermark
    for kind, normalizer, upsert in (
        ("companies", normalize_company, upsert_hubspot_company),
        ("deals", normalize_deal, upsert_hubspot_deal),
    ):
        after = None
        while True:
            if kind == "companies":
                page = client.search_companies(watermark, after)
            else:
                page = client.search_deals(watermark, after)
            results = page.get("results") if isinstance(page.get("results"), list) else []
            for raw in results:
                if kind == "companies":
                    record = normalizer(
                        raw,
                        tenant_id=tenant_id,
                        connection_id=connection_id,
                        portal_id=portal_id,
                        owner_emails=owners,
                    )
                    if record is None:
                        continue
                    company_names[record.company_id] = record.name
                    upsert(graph, record)
                    greatest = max(greatest, int(record.version) if record.version.isdigit() else greatest)
                else:
                    record = normalizer(
                        raw,
                        tenant_id=tenant_id,
                        connection_id=connection_id,
                        portal_id=portal_id,
                        company_names=company_names,
                        owner_emails=owners,
                    )
                    if record is None:
                        continue
                    upsert(graph, record)
                    greatest = max(greatest, int(record.version) if record.version.isdigit() else greatest)
            paging = page.get("paging") if isinstance(page.get("paging"), dict) else {}
            next_after = paging.get("next", {}).get("after") if isinstance(paging.get("next"), dict) else None
            if not next_after:
                break
            after = str(next_after)
    _write(
        records,
        sync_id,
        {
            "tenant_id": tenant_id,
            "connection_id": connection_id,
            "portal_id": portal_id,
            "modified_after_ms": greatest,
            "status": "complete",
            "last_error_class": "",
        },
    )
    records.note_source_sync(tenant_id, hubspot_source_id(tenant_id, portal_id), group)
    index_source(graph, records, hubspot_source_id(tenant_id, portal_id))
    logger.info(
        "%s",
        format_document_log(
            tenant_id=tenant_id,
            folder_id=portal_id,
            records=0,
            failed=0,
            duration_ms=_elapsed(started),
            error_class="",
        ),
    )


def _read(records, sync_id: str) -> dict:
    row = records.get(sync_id)
    payload = row["payload"] if row else {}
    return {"modified_after_ms": payload.get("modified_after_ms") or 0}


def _write(records, sync_id: str, payload: dict) -> None:
    records.merge(sync_id, payload.get("tenant_id", ""), SYNC_STATE, payload)


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
