"""Fill Slack author names and merge the viewer onto the email person id.

Only the address in ``WEAVE_VIEWER_EMAIL`` receives ``level`` and ``dept``.
Everyone else keeps the Slack person id.
"""

import os

from shared.ids import person_id
from shared.schema import Label, Prop, Rel

_AUTHORED = "AUTHORED"


def enrich_slack_authors(graph, client, tenant_id: str) -> dict:
    if not hasattr(client, "user_profile"):
        return {"named": 0, "merged": False}
    viewer_email = os.environ.get("WEAVE_VIEWER_EMAIL", "").strip().lower()
    level = os.environ.get("WEAVE_VIEWER_LEVEL", "").strip()
    dept = os.environ.get("WEAVE_VIEWER_DEPT", "").strip()
    rows = graph.query(
        f"""
        MATCH (p:{Label.PERSON})
        WHERE p.{Prop.TENANT_ID} = $tenant AND p.{Prop.SLACK_ID} IS NOT NULL
        RETURN p.{Prop.ID}, p.{Prop.SLACK_ID}
        """,
        {"tenant": tenant_id},
    ).result_set
    named = 0
    merged = False
    for node_id, slack_id in rows:
        try:
            profile = client.user_profile(slack_id)
        except Exception as exc:
            if "missing_scope" in str(exc):
                return {"named": named, "merged": merged, "error": "missing_scope"}
            continue
        email = str(profile.get("email") or "").strip().lower()
        name = str(profile.get("name") or "").strip()
        if viewer_email and email == viewer_email:
            _merge_viewer(
                graph,
                tenant_id=tenant_id,
                old_id=node_id,
                slack_id=slack_id,
                email=viewer_email,
                name=name,
                level=int(level) if level else None,
                dept=dept or None,
            )
            merged = True
            continue
        if name:
            graph.query(
                f"MATCH (p:{Label.PERSON} {{{Prop.ID}: $id}}) SET p.{Prop.NAME} = $name",
                {"id": node_id, "name": name},
            )
            named += 1
    return {"named": named, "merged": merged}


def _merge_viewer(graph, *, tenant_id, old_id, slack_id, email, name, level, dept) -> None:
    viewer = person_id(tenant_id, email)
    graph.query(
        f"""
        MERGE (p:{Label.PERSON} {{{Prop.ID}: $id}})
        SET p.{Prop.TENANT_ID} = $tenant,
            p.{Prop.EMAIL} = $email,
            p.{Prop.NAME} = $name,
            p.{Prop.SLACK_ID} = $slack,
            p.{Prop.LEVEL} = $level,
            p.{Prop.DEPT} = $dept
        """,
        {
            "id": viewer,
            "tenant": tenant_id,
            "email": email,
            "name": name or None,
            "slack": slack_id,
            "level": level,
            "dept": dept,
        },
    )
    if old_id == viewer:
        return
    graph.query(
        f"""
        MATCH (old:{Label.PERSON} {{{Prop.ID}: $old}})-[r:{_AUTHORED}]->(m)
        MERGE (viewer:{Label.PERSON} {{{Prop.ID}: $viewer}})
        MERGE (viewer)-[:{_AUTHORED}]->(m)
        DELETE r
        """,
        {"old": old_id, "viewer": viewer},
    )
    graph.query(
        f"""
        MATCH (m)-[r:{Rel.AUTHORED_BY}]->(old:{Label.PERSON} {{{Prop.ID}: $old}})
        MERGE (viewer:{Label.PERSON} {{{Prop.ID}: $viewer}})
        MERGE (m)-[:{Rel.AUTHORED_BY}]->(viewer)
        DELETE r
        """,
        {"old": old_id, "viewer": viewer},
    )
    graph.query(
        f"MATCH (old:{Label.PERSON} {{{Prop.ID}: $old}}) DETACH DELETE old",
        {"old": old_id},
    )
