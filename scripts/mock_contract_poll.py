"""One mock record per connector into a temp graph; run contract validate.

Usage: python -m scripts.mock_contract_poll
"""

import hashlib
import uuid
from datetime import UTC, datetime

from app.gcal.normalize import normalize_calendar_event
from app.gdrive.normalize import normalize_drive_file
from app.github.normalize import normalize_github_comment, normalize_github_issue
from app.graph.upsert import ensure_indexes, upsert_message
from app.graph.upsert_document import upsert_document
from app.graph.upsert_gmail import upsert_gmail_message
from app.graph.upsert_hubspot import upsert_hubspot_company
from app.hubspot.normalize import normalize_company
from app.models.canonical import CanonicalMessage
from app.models.canonical_gmail import CanonicalGmailMessage
from app.notion.normalize import normalize_notion_page
from app.slack.normalize import format_slack_version
from app.slack.people import enrich_slack_authors
from shared.env import load_env
from shared.graphdb import connect
from shared.validate import export_graph, validate

TENANT = "mockpoll"
DOC_MIME = "application/vnd.google-apps.document"


class _SlackProfiles:
    def user_profile(self, user_id: str) -> dict:
        return {"email": "slack-author@example.com", "name": "Slack Author"}


def main() -> int:
    load_env()
    client = connect()
    graph_name = f"weave_mock_{uuid.uuid4().hex}"
    graph = client.select_graph(graph_name)
    try:
        ensure_indexes(graph)
        _poll(graph)
        nodes, edges = export_graph(graph)
        errors = validate(TENANT, nodes, edges)
        print(f"tenant={TENANT} nodes={len(nodes)} edges={len(edges)} errors={len(errors)}")
        for err in errors[:30]:
            print(err)
        return 1 if errors else 0
    finally:
        graph.delete()


def _poll(graph) -> None:
    upsert_message(
        graph,
        CanonicalMessage(
            tenant_id=TENANT,
            connection_id="conn-mock",
            workspace_id="TMOCK",
            external_id="C0ENGSSO:1403051575.000407",
            conversation_external_id="C0ENGSSO",
            author_external_id="U1",
            occurred_at=datetime(2014, 6, 18, 1, 39, 35, 407, tzinfo=UTC),
            version=format_slack_version("1403051575.000407"),
            excerpt="mock slack",
            content_hash=hashlib.sha256(b"mock slack").hexdigest(),
            source_url="https://example.com/slack",
        ),
    )
    enrich_slack_authors(graph, _SlackProfiles(), TENANT)

    upsert_document(
        graph,
        normalize_notion_page(
            {
                "object": "page",
                "id": "page-mock",
                "created_time": "2026-01-01T00:00:00.000Z",
                "last_edited_time": "2026-03-11T00:00:00.000Z",
                "url": "https://www.notion.so/page-mock",
                "created_by": {
                    "id": "notion-user-1",
                    "person": {"email": "notion@example.com", "name": "Notion Author"},
                },
                "properties": {"title": {"type": "title", "title": [{"plain_text": "Mock"}]}},
            },
            "notion body",
            tenant_id=TENANT,
            connection_id="notion-main",
            workspace_id="sales-deals",
        ),
    )

    upsert_document(
        graph,
        normalize_drive_file(
            {
                "id": "doc-mock",
                "mimeType": DOC_MIME,
                "modifiedTime": "2026-03-11T00:00:00.000Z",
                "name": "Mock Doc",
                "owners": [{"emailAddress": "drive@example.com", "displayName": "Drive Owner"}],
            },
            "drive body",
            tenant_id=TENANT,
            connection_id="gdrive-main",
            folder_id="F0BUDGET",
        ),
    )

    issue = normalize_github_issue(
        {
            "number": 1,
            "updated_at": "2026-03-11T00:00:00Z",
            "created_at": "2026-03-11T00:00:00Z",
            "title": "Mock issue",
            "body": "details",
            "html_url": "https://github.com/demo-org/sso-service/issues/1",
            "user": {"login": "dev", "email": "dev@example.com"},
        },
        tenant_id=TENANT,
        connection_id="github-main",
        repo_id="99",
        owner="demo-org",
        repo="sso-service",
    )
    upsert_document(graph, issue)
    upsert_document(
        graph,
        normalize_github_comment(
            {
                "id": 1,
                "created_at": "2026-03-11T01:00:00Z",
                "updated_at": "2026-03-11T01:00:00Z",
                "body": "comment",
                "user": {"login": "dev"},
            },
            tenant_id=TENANT,
            connection_id="github-main",
            repo_id="99",
            owner="demo-org",
            repo="sso-service",
            issue_number=1,
        ),
    )

    upsert_gmail_message(
        graph,
        CanonicalGmailMessage(
            tenant_id=TENANT,
            connection_id="gmail-main",
            mailbox="sales@example.com",
            external_id="msg-mock",
            author_email="gmail@example.com",
            occurred_at=datetime(2026, 3, 11, tzinfo=UTC),
            version="20260311T000000Z",
            excerpt="mock gmail",
            content_hash=hashlib.sha256(b"mock gmail").hexdigest(),
        ),
    )

    upsert_document(
        graph,
        normalize_calendar_event(
            {
                "id": "evt-mock",
                "updated": "2026-03-11T00:00:00Z",
                "summary": "Standup",
                "start": {"dateTime": "2026-03-11T09:00:00Z"},
                "end": {"dateTime": "2026-03-11T09:30:00Z"},
                "organizer": {"email": "organizer@example.com"},
                "htmlLink": "https://calendar.google.com/event/mock",
            },
            tenant_id=TENANT,
            connection_id="gcal-main",
            calendar_id="primary",
        ),
    )

    company = normalize_company(
        {
            "id": "co-mock",
            "properties": {
                "name": "Mock Corp",
                "hs_lastmodifieddate": "1700000000000",
                "hubspot_owner_id": "42",
            },
        },
        tenant_id=TENANT,
        connection_id="hubspot-main",
        portal_id="demo-portal",
        owner_emails={"42": "hubspot@example.com"},
    )
    assert company is not None
    upsert_hubspot_company(graph, company)


if __name__ == "__main__":
    raise SystemExit(main())
