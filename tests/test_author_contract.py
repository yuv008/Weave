"""Author edges and provisional people."""

import hashlib
from datetime import UTC, datetime

from app.graph.link_author import link_author
from app.graph.upsert_document import upsert_document
from app.gdrive.normalize import normalize_drive_file
from app.github.normalize import normalize_github_comment, normalize_github_issue
from app.notion.normalize import normalize_notion_page
from app.slack.people import enrich_slack_authors
from shared.ids import person_id
from shared.schema import Label, Prop, Rel
from shared.validate import EdgeRow, NodeRow, export_graph, validate

DOC_MIME = "application/vnd.google-apps.document"


def test_provisional_person_passes_without_level_or_dept():
    tenant = "acme"
    pid = person_id(tenant, "ada@example.com")
    nodes = [
        NodeRow((Label.PERSON,), {Prop.ID: pid, Prop.TENANT_ID: tenant, Prop.NAME: "Ada", Prop.EMAIL: "ada@example.com", Prop.PROVISIONAL: True}),
    ]
    assert validate(tenant, nodes, []) == []


def test_non_provisional_person_requires_level_and_dept():
    tenant = "acme"
    pid = person_id(tenant, "ada@example.com")
    nodes = [
        NodeRow((Label.PERSON,), {Prop.ID: pid, Prop.TENANT_ID: tenant, Prop.NAME: "Ada", Prop.EMAIL: "ada@example.com", Prop.PROVISIONAL: False}),
    ]
    errors = validate(tenant, nodes, [])
    assert any("level must be" in e for e in errors)
    assert any("dept must be" in e for e in errors)


def test_item_with_author_email_requires_authored_by():
    tenant = "acme"
    item_id = f"{tenant}:notion:document:page-1"
    nodes = [
        NodeRow(
            (Label.DOCUMENT, Label.ITEM),
            {
                Prop.ID: item_id,
                Prop.TENANT_ID: tenant,
                Prop.SOURCE: "notion",
                Prop.EXTERNAL_ID: "page-1",
                Prop.OBJECT_TYPE: "document",
                Prop.OCCURRED_AT: "2026-01-01T00:00:00Z",
                Prop.SOURCE_URL: "https://example.com",
                Prop.CONTENT_HASH: "abc",
                Prop.DELETED: False,
                Prop.ACL_GROUPS: ["notion:x"],
                Prop.MIN_LEVEL: 0,
                "author_email": "writer@example.com",
            },
        ),
    ]
    errors = validate(tenant, nodes, [])
    assert any("AUTHORED_BY" in e for e in errors)


def test_slack_profile_with_email_creates_provisional_person(graph, monkeypatch):
    monkeypatch.delenv("WEAVE_VIEWER_EMAIL", raising=False)
    graph.query(
        """
        CREATE (m:Message:Item {
          id: 't:slack:message:C1:1.0',
          tenant_id: 't',
          source: 'slack',
          author_external_id: 'U1',
          workspace_id: 'T'
        })
        """
    )

    class _Client:
        def user_profile(self, user_id: str) -> dict:
            return {"email": "ada@example.com", "name": "Ada"}

    result = enrich_slack_authors(graph, _Client(), "t")
    assert result["named"] == 1
    pid = person_id("t", "ada@example.com")
    row = graph.query(
        f"MATCH (p:{Label.PERSON} {{id: $id}}) RETURN p.{Prop.PROVISIONAL}, p.{Prop.NAME}",
        {"id": pid},
    ).result_set[0]
    assert row == [True, "Ada"]
    assert graph.query(
        f"MATCH (m {{id: 't:slack:message:C1:1.0'}})-[:{Rel.AUTHORED_BY}]->(p {{id: $id}}) RETURN count(p)",
        {"id": pid},
    ).result_set[0][0] == 1


def test_slack_profile_without_email_creates_no_person(graph):
    graph.query(
        """
        CREATE (m:Message:Item {
          id: 't:slack:message:C1:2.0',
          tenant_id: 't',
          source: 'slack',
          author_external_id: 'U9',
          workspace_id: 'T'
        })
        """
    )

    class _Client:
        def user_profile(self, user_id: str) -> dict:
            return {"email": "", "name": "Ghost"}

    enrich_slack_authors(graph, _Client(), "t")
    assert graph.query("MATCH (p:Person) RETURN count(p)").result_set[0][0] == 0


def test_notion_drive_github_authors(graph):
    page = normalize_notion_page(
        {
            "object": "page",
            "id": "page-1",
            "created_time": "2026-01-01T00:00:00.000Z",
            "last_edited_time": "2026-03-11T00:00:00.000Z",
            "url": "https://www.notion.so/page-1",
            "created_by": {"id": "user-1", "person": {"email": "n@example.com", "name": "Notion User"}},
            "properties": {"title": {"type": "title", "title": [{"plain_text": "T"}]}},
        },
        "hello",
        tenant_id="demo",
        connection_id="notion-main",
        workspace_id="sales-deals",
    )
    upsert_document(graph, page)
    pid = person_id("demo", "n@example.com")
    assert graph.query("MATCH (p:Person {id: $id}) RETURN p.provisional", {"id": pid}).result_set[0][0] is True

    page_no_email = normalize_notion_page(
        {
            "object": "page",
            "id": "page-2",
            "created_time": "2026-01-01T00:00:00.000Z",
            "last_edited_time": "2026-03-11T00:00:00.000Z",
            "url": "https://www.notion.so/page-2",
            "created_by": {"id": "user-2"},
            "properties": {"title": {"type": "title", "title": [{"plain_text": "T2"}]}},
        },
        "hello",
        tenant_id="demo",
        connection_id="notion-main",
        workspace_id="sales-deals",
    )
    upsert_document(graph, page_no_email)
    assert (
        graph.query(
            "MATCH (d:Document {id: $id}) RETURN d.author_external_id",
            {"id": page_no_email.node_id},
        ).result_set[0][0]
        == "user-2"
    )
    assert (
        graph.query(
            "MATCH (d:Document {id: $id})-[:AUTHORED_BY]->() RETURN count(*)",
            {"id": page_no_email.node_id},
        ).result_set[0][0]
        == 0
    )

    drive_doc = normalize_drive_file(
        {
            "id": "doc1",
            "mimeType": DOC_MIME,
            "modifiedTime": "2026-03-11T00:00:00.000Z",
            "name": "Doc",
            "owners": [{"emailAddress": "o@example.com", "displayName": "Owner"}],
        },
        "body",
        tenant_id="demo",
        connection_id="gdrive-main",
        folder_id="F0BUDGET",
    )
    upsert_document(graph, drive_doc)
    assert graph.query("MATCH (p:Person {id: $id}) RETURN count(p)", {"id": person_id("demo", "o@example.com")}).result_set[0][0] == 1

    issue = normalize_github_issue(
        {
            "number": 1,
            "updated_at": "2026-03-11T00:00:00Z",
            "title": "Bug",
            "body": "details",
            "user": {"login": "dev", "email": "dev@example.com"},
        },
        tenant_id="demo",
        connection_id="github-main",
        repo_id="1",
        owner="demo-org",
        repo="sso-service",
    )
    upsert_document(graph, issue)
    assert graph.query("MATCH (p:Person {id: $id}) RETURN count(p)", {"id": person_id("demo", "dev@example.com")}).result_set[0][0] == 1

    comment = normalize_github_comment(
        {
            "id": 9,
            "created_at": "2026-03-11T01:00:00Z",
            "updated_at": "2026-03-11T01:00:00Z",
            "body": "fix",
            "user": {"login": "dev"},
        },
        tenant_id="demo",
        connection_id="github-main",
        repo_id="1",
        owner="demo-org",
        repo="sso-service",
        issue_number=1,
    )
    upsert_document(graph, comment)
    assert graph.query("MATCH ()-[r:REPLIES_TO]->() RETURN count(r)").result_set[0][0] >= 1
    assert (
        graph.query(
            "MATCH (d:Document {id: $id})-[:AUTHORED_BY]->() RETURN count(*)",
            {"id": comment.node_id},
        ).result_set[0][0]
        == 0
    )


def test_link_author_on_item(graph):
    graph.query(
        """
        CREATE (i:Document:Item {
          id: 'demo:gdrive:document:x',
          tenant_id: 'demo',
          source: 'gdrive',
          external_id: 'x',
          object_type: 'document',
          occurred_at: '2026-01-01T00:00:00+00:00',
          source_url: 'https://example.com',
          content_hash: 'h',
          deleted: false,
          acl_groups: ['gdrive:f'],
          min_level: 0
        })
        """
    )
    link_author(graph, tenant_id="demo", item_id="demo:gdrive:document:x", email="a@example.com", name="A")
    graph.query(
        """
        MATCH (i:Document:Item {id: 'demo:gdrive:document:x'})
        MERGE (s:Source {id: 'demo:gdrive:source:F0BUDGET'})
        SET s.tenant_id = 'demo', s.source = 'gdrive', s.external_id = 'F0BUDGET',
            s.name = 'x', s.acl_groups = ['gdrive:F0BUDGET'], s.min_level = 0,
            s.cascade_policy = 'manual', s.authority = 1
        MERGE (i)-[:IN_SOURCE]->(s)
        """
    )
    nodes, edges = export_graph(graph)
    errors = validate("demo", nodes, edges)
    assert errors == []
