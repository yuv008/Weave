"""Seed fake profit, budget, and sales figures for the Gmail @bubble demo.

The amounts are not real company numbers. Re-running updates the same messages
and chunks. Keyword search is enough; this script does not embed.
"""

import hashlib
import json
import os
import re
import urllib.request
from datetime import UTC, datetime

from app.graph.client import connect_graph
from shared.env import load_env
from shared.pg import connect_pg
from shared.schema import Label, Prop, Rel
from shared.timefmt import to_ts

ACL_ITEM = "real:slack:message:demo:api-key"
FALLBACK_SOURCE = "real:slack:conversation:T0C8H9PPXU7:C0C90JZ4YMP"
FILL_BODY = "hey, as per discussed the profit is @bubble it has grown from @bubble"
DRAFT_QUESTION = "can you draft email about the budget and sales"
DRAFT_BODY = "@bubble can you draft email about the budget and sales"

FACTS = (
    {
        "item_id": "real:slack:message:demo:profit-current",
        "external_id": "demo:profit-current",
        "excerpt": "The current profit discussed is $2.4M.",
        "text": (
            "The current profit discussed is $2.4M. "
            "Current profit discussed is $2.4M. "
            "discussed profit current profit is $2.4M."
        ),
    },
    {
        "item_id": "real:slack:message:demo:profit-prior",
        "external_id": "demo:profit-prior",
        "excerpt": "The earlier amount grown from is $1.8M.",
        "text": (
            "The earlier amount grown from is $1.8M. "
            "grown from $1.8M. grown grown from earlier amount."
        ),
    },
    {
        "item_id": "real:slack:message:demo:budget",
        "external_id": "demo:budget",
        "excerpt": "The budget is $900K.",
        "text": "The budget is $900K. budget budget budget figure is $900K.",
    },
    {
        "item_id": "real:slack:message:demo:sales",
        "external_id": "demo:sales",
        "excerpt": "The sales are $3.1M.",
        "text": "The sales are $3.1M. sales sales sales figure is $3.1M.",
    },
)


def main() -> None:
    load_env()
    load_env(".env.tokens")
    os.environ["WEAVE_TENANT"] = "real"
    tenant = "real"
    graph = connect_graph()
    source_id, acl_groups, min_level = _access(graph, tenant)
    person_id = _person(graph, tenant)
    for fact in FACTS:
        _upsert(graph, tenant, source_id, acl_groups, min_level, person_id, fact)
    _chunks(tenant, acl_groups, min_level)
    print("seeded", ",".join(fact["item_id"] for fact in FACTS))
    print("source", source_id)
    print("level", min_level)
    _note_playbook(graph, tenant)
    _ask()


def _access(graph, tenant: str):
    found = graph.query(
        f"""
        MATCH (m:{Label.MESSAGE}:{Label.ITEM} {{id: $item}})
        WHERE m.{Prop.TENANT_ID} = $tenant AND coalesce(m.{Prop.DELETED}, false) = false
        OPTIONAL MATCH (m)-[:{Rel.IN_SOURCE}]->(s:{Label.SOURCE})
        RETURN s.{Prop.ID}, m.{Prop.ACL_GROUPS}, m.{Prop.MIN_LEVEL}
        """,
        {"tenant": tenant, "item": ACL_ITEM},
    ).result_set
    if not found or not found[0][0]:
        found = graph.query(
            f"""
            MATCH (m:{Label.MESSAGE}:{Label.ITEM})-[:{Rel.IN_SOURCE}]->(s:{Label.SOURCE})
            WHERE m.{Prop.TENANT_ID} = $tenant
              AND coalesce(m.{Prop.DELETED}, false) = false
              AND s.{Prop.ID} = $source
            RETURN s.{Prop.ID}, m.{Prop.ACL_GROUPS}, m.{Prop.MIN_LEVEL}
            LIMIT 1
            """,
            {"tenant": tenant, "source": FALLBACK_SOURCE},
        ).result_set
    if not found or not found[0][0]:
        raise SystemExit("no visible message to copy access from")
    source_id, acl_groups, min_level = found[0]
    return source_id, _groups(acl_groups), int(min_level or 0)


def _person(graph, tenant: str) -> str:
    email = os.environ.get("WEAVE_VIEWER_EMAIL", "").strip().lower()
    if not email:
        return ""
    people = graph.query(
        f"""
        MATCH (p:{Label.PERSON})
        WHERE p.{Prop.TENANT_ID} = $tenant AND toLower(p.{Prop.EMAIL}) = $email
        RETURN p.{Prop.ID}
        LIMIT 1
        """,
        {"tenant": tenant, "email": email},
    ).result_set
    return people[0][0] if people else ""


def _upsert(graph, tenant, source_id, acl_groups, min_level, person_id, fact) -> None:
    graph.query(
        f"""
        MERGE (m:{Label.MESSAGE}:{Label.ITEM} {{id: $item_id}})
        SET
          m.{Prop.TENANT_ID} = $tenant,
          m.{Prop.SOURCE} = 'slack',
          m.{Prop.OBJECT_TYPE} = 'message',
          m.{Prop.EXTERNAL_ID} = $external_id,
          m.{Prop.OCCURRED_AT} = $occurred_at,
          m.{Prop.VERSION} = '9',
          m.{Prop.DELETED} = false,
          m.{Prop.EXCERPT} = $excerpt,
          m.{Prop.CONTENT_HASH} = $content_hash,
          m.{Prop.SOURCE_URL} = '',
          m.{Prop.ACL_GROUPS} = $acl_groups,
          m.{Prop.MIN_LEVEL} = $min_level,
          m.placeholder = false
        WITH m
        MATCH (s:{Label.SOURCE} {{id: $source_id}})
        MERGE (m)-[:{Rel.IN_SOURCE}]->(s)
        """,
        {
            "item_id": fact["item_id"],
            "tenant": tenant,
            "external_id": fact["external_id"],
            "occurred_at": to_ts(datetime.now(UTC)),
            "excerpt": fact["excerpt"],
            "content_hash": hashlib.sha256(fact["excerpt"].encode()).hexdigest(),
            "acl_groups": acl_groups,
            "min_level": min_level,
            "source_id": source_id,
        },
    )
    if person_id:
        graph.query(
            f"""
            MATCH (m:{Label.MESSAGE} {{id: $item_id}})
            MATCH (p:{Label.PERSON} {{id: $person_id}})
            MERGE (m)-[:{Rel.AUTHORED_BY}]->(p)
            """,
            {"item_id": fact["item_id"], "person_id": person_id},
        )


def _chunks(tenant, acl_groups, min_level) -> None:
    conn = connect_pg()
    try:
        with conn.cursor() as cur:
            for fact in FACTS:
                cur.execute(
                    """
                    INSERT INTO chunk (id, tenant_id, item_id, ordinal, text, embedding, acl_groups, min_level)
                    VALUES (%s, %s, %s, 0, %s, NULL, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                      text = EXCLUDED.text,
                      embedding = NULL,
                      acl_groups = EXCLUDED.acl_groups,
                      min_level = EXCLUDED.min_level
                    """,
                    (f"{fact['item_id']}:chunk:0", tenant, fact["item_id"], fact["text"], acl_groups, min_level),
                )
        conn.commit()
    finally:
        conn.close()


def _groups(value) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(part) for part in value or []]


def _redact(text: str) -> str:
    return re.sub(r"\b[A-Za-z0-9_\-]{20,}\b", "[redacted]", " ".join(str(text or "").split()))


def _post(token: str, path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        "http://127.0.0.1:8765" + path,
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read().decode())


def _show(text: str, needles: tuple[str, ...]) -> None:
    flat = " ".join(str(text or "").split())
    if needles and not all(needle in flat for needle in needles):
        print("MISSING", _redact(flat)[:400])
        return
    print(_redact(flat)[:400])


def _note_playbook(graph, tenant: str) -> None:
    """Print the viewer's draft playbook. Update it when budget and sales would skip search."""

    email = os.environ.get("WEAVE_VIEWER_EMAIL", "").strip().lower()
    if not email:
        print("viewer_role unknown")
        return
    rows = graph.query(
        f"""
        MATCH (p:{Label.PERSON})
        WHERE p.{Prop.TENANT_ID} = $tenant AND toLower(p.{Prop.EMAIL}) = $email
        RETURN p.{Prop.DEPT}
        LIMIT 1
        """,
        {"tenant": tenant, "email": email},
    ).result_set
    dept = rows[0][0] if rows and rows[0][0] else "default"
    role = str(dept).strip().lower() or "default"
    from agents.memory.store import PostgresMemoryStore

    store = PostgresMemoryStore(connect_pg)
    steps = store.playbook(tenant, role, "draft") or store.playbook(tenant, "default", "draft")
    print("viewer_role", role)
    print("stored_draft_playbook", steps)
    if steps and "search_figures" not in list(steps):
        updated = list(steps)
        if "propose" in updated:
            updated.insert(updated.index("propose"), "search_figures")
        else:
            updated.append("search_figures")
        version = store.put_playbook(None, role, "draft", updated, "demo")
        print("updated_draft_playbook", version)


def _ask() -> None:
    from agents.wisp.mail import bubble_blanks

    token = os.environ.get("WEAVE_VIEWER_TOKEN") or os.environ.get("WEAVE_TOKEN_SAM") or ""
    if not token:
        print("ask skipped: no viewer token")
        return
    questions = bubble_blanks(FILL_BODY)
    print("blank_questions", " | ".join(questions))
    expected = (("$2.4M",), ("$1.8M",))
    for question, needles in zip(questions, expected):
        card = _post(token, "/answer", {"question": question})
        print("answer_needles", ",".join(needles))
        _show(card.get("answer"), needles)
    card = _post(token, "/answer", {"question": DRAFT_QUESTION})
    proposal = (card.get("proposal") or {}).get("draft") or ""
    print("draft_answer")
    _show(card.get("answer"), ())
    print("draft_figures", "$900K" in proposal and "$3.1M" in proposal)
    for line in proposal.splitlines():
        if "$900K" in line or "$3.1M" in line:
            print("draft_line", _redact(line))
    filled = _post(token, "/wisp/mail", {"to": "", "subject": "", "body": FILL_BODY})
    print("mail_fill_mode", filled.get("mode"), filled.get("state"))
    print("mail_fill_insert")
    _show(filled.get("insert"), ("$2.4M", "$1.8M"))
    drafted = _post(token, "/wisp/mail", {"to": "", "subject": "", "body": DRAFT_BODY})
    print("mail_draft_mode", drafted.get("mode"), drafted.get("state"))
    print("mail_draft_insert")
    _show(drafted.get("insert"), ("$900K", "$3.1M"))


if __name__ == "__main__":
    main()
