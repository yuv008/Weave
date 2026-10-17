"""Fill Slack author names and merge the viewer onto the email person id.

Only the address in ``WEAVE_VIEWER_EMAIL`` receives ``level`` and ``dept``.
Everyone else stays provisional until an org chart is seeded.
"""

import os

from app.graph.link_author import link_author
from app.models.canonical import person_node_id
from shared.schema import Label, Prop, Rel

_LEGACY_AUTHORED = "AUTHORED"


def enrich_slack_authors(graph, client, tenant_id: str) -> dict:
    if not hasattr(client, "user_profile"):
        return {"named": 0, "merged": False}
    viewer_email = os.environ.get("WEAVE_VIEWER_EMAIL", "").strip().lower()
    level_raw = os.environ.get("WEAVE_VIEWER_LEVEL", "").strip()
    dept = os.environ.get("WEAVE_VIEWER_DEPT", "").strip()
    viewer_level = int(level_raw) if level_raw.isdigit() else None
    rows = graph.query(
        f"""
        MATCH (m:{Label.MESSAGE}:{Label.ITEM})
        WHERE m.{Prop.TENANT_ID} = $tenant AND m.{Prop.SOURCE} = 'slack'
          AND coalesce(m.author_external_id, '') <> ''
        RETURN m.id, m.author_external_id, m.workspace_id
        """,
        {"tenant": tenant_id},
    ).result_set
    profiles: dict[str, tuple[str, str]] = {}
    named_users: set[str] = set()
    dropped_legacy: set[tuple[str, str]] = set()
    named = 0
    merged = False
    for msg_id, slack_id, workspace_id in rows:
        if not isinstance(slack_id, str) or not slack_id or not isinstance(msg_id, str):
            continue
        if slack_id not in profiles:
            try:
                profile = client.user_profile(slack_id)
            except Exception as exc:
                if "missing_scope" in str(exc):
                    return {"named": named, "merged": merged, "error": "missing_scope"}
                continue
            email = str(profile.get("email") or "").strip().lower()
            name = str(profile.get("name") or "").strip()
            profiles[slack_id] = (email, name)
        email, name = profiles[slack_id]
        if not email:
            continue
        if viewer_email and email == viewer_email and viewer_level is not None and dept:
            link_author(
                graph,
                tenant_id=tenant_id,
                item_id=msg_id,
                email=email,
                name=name,
                slack_id=slack_id,
                level=viewer_level,
                dept=dept,
            )
            merged = True
        else:
            link_author(
                graph,
                tenant_id=tenant_id,
                item_id=msg_id,
                email=email,
                name=name,
                slack_id=slack_id,
            )
            if slack_id not in named_users:
                named_users.add(slack_id)
                named += 1
        legacy_key = (workspace_id, slack_id) if isinstance(workspace_id, str) and workspace_id else None
        if legacy_key and legacy_key not in dropped_legacy:
            dropped_legacy.add(legacy_key)
            _drop_legacy_slack_person(graph, tenant_id, workspace_id, slack_id)
    return {"named": named, "merged": merged}


def _drop_legacy_slack_person(graph, tenant_id: str, workspace_id: str, slack_id: str) -> None:
    legacy_id = person_node_id(tenant_id, workspace_id, slack_id)
    graph.query(
        f"""
        MATCH (old:{Label.PERSON} {{id: $old}})
        OPTIONAL MATCH (old)-[a:{_LEGACY_AUTHORED}]->()
        DELETE a
        WITH old
        OPTIONAL MATCH ()-[ab:{Rel.AUTHORED_BY}]->(old)
        DELETE ab
        WITH old
        DETACH DELETE old
        """,
        {"old": legacy_id},
    )
