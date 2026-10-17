"""Attach AUTHORED_BY when a source supplies an author email."""

from shared.ids import person_id
from shared.schema import Label, Prop, Rel


def link_author(
    graph,
    *,
    tenant_id: str,
    item_id: str,
    email: str,
    name: str | None = None,
    slack_id: str | None = None,
    github_login: str | None = None,
    notion_user: str | None = None,
    level: int | None = None,
    dept: str | None = None,
) -> None:
    cleaned = email.strip().lower()
    if "@" not in cleaned:
        return
    display = (name or "").strip() or cleaned
    complete = level is not None and bool((dept or "").strip())
    sets = [
        f"p.{Prop.TENANT_ID} = $tenant_id",
        f"p.{Prop.EMAIL} = $email",
        f"p.{Prop.NAME} = $name",
        f"p.{Prop.PROVISIONAL} = $provisional",
    ]
    params: dict = {
        "person_id": person_id(tenant_id, cleaned),
        "tenant_id": tenant_id,
        "email": cleaned,
        "name": display,
        "item_id": item_id,
        "provisional": not complete,
    }
    if slack_id:
        sets.append(f"p.{Prop.SLACK_ID} = $slack_id")
        params["slack_id"] = slack_id
    if github_login:
        sets.append(f"p.{Prop.GITHUB_LOGIN} = $github_login")
        params["github_login"] = github_login
    if notion_user:
        sets.append(f"p.{Prop.NOTION_USER} = $notion_user")
        params["notion_user"] = notion_user
    if complete:
        sets.append(f"p.{Prop.LEVEL} = $level")
        sets.append(f"p.{Prop.DEPT} = $dept")
        params["level"] = level
        params["dept"] = dept.strip()
    graph.query(
        f"""
MERGE (p:{Label.PERSON} {{id: $person_id}})
SET {", ".join(sets)}
MERGE (i {{id: $item_id}})
MERGE (i)-[:{Rel.AUTHORED_BY}]->(p)
""",
        params,
    )


def purge_legacy_item_edges(graph, item_id: str) -> None:
    graph.query(
        """
MATCH (n {id: $id})
OPTIONAL MATCH (n)<-[r:CONTAINS]-()
DELETE r
WITH n
OPTIONAL MATCH (n)<-[r:AUTHORED]-()
DELETE r
""",
        {"id": item_id},
    )
