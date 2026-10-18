"""Seed a fake API key the desktop pet can answer in a demo video.

The value is not a real provider key. Re-running updates the same message and chunk.
"""

import hashlib
import json
import os
import urllib.request
from datetime import UTC, datetime

from app.graph.client import connect_graph
from shared.env import load_env
from shared.pg import connect_pg
from shared.schema import Label, Prop, Rel
from shared.timefmt import to_ts

DEMO_KEY = "sk-weave-demo-4k7Q"
ITEM_ID = "real:slack:message:demo:api-key"
EXCERPT = f"The API key to change the OpenAI API key to is {DEMO_KEY}."
CHUNK = (
    "We are using the OpenAI API key. "
    f"Change the OpenAI API key to {DEMO_KEY}. "
    f"The API key to change it to is {DEMO_KEY}. "
    "openai api key change using open."
)
QUESTION = "rn we are using the open ai api key can you change it to"


def main() -> None:
    load_env()
    load_env(".env.tokens")
    os.environ["WEAVE_TENANT"] = "real"
    tenant = "real"
    graph = connect_graph()
    found = graph.query(
        f"""
        MATCH (m:{Label.MESSAGE}:{Label.ITEM})-[:{Rel.IN_SOURCE}]->(s:{Label.SOURCE})
        WHERE m.{Prop.TENANT_ID} = $tenant
          AND coalesce(m.{Prop.DELETED}, false) = false
          AND toLower(coalesce(s.{Prop.NAME}, '')) = 'social'
        RETURN s.{Prop.ID}, m.{Prop.ACL_GROUPS}, m.{Prop.MIN_LEVEL}
        LIMIT 1
        """,
        {"tenant": tenant},
    ).result_set
    if not found:
        found = graph.query(
            f"""
            MATCH (m:{Label.MESSAGE}:{Label.ITEM})-[:{Rel.IN_SOURCE}]->(s:{Label.SOURCE})
            WHERE m.{Prop.TENANT_ID} = $tenant
              AND coalesce(m.{Prop.DELETED}, false) = false
            RETURN s.{Prop.ID}, m.{Prop.ACL_GROUPS}, m.{Prop.MIN_LEVEL}
            LIMIT 1
            """,
            {"tenant": tenant},
        ).result_set
    if not found:
        raise SystemExit("no visible message to copy access from")
    source_id, acl_groups, min_level = found[0]
    acl_groups = _groups(acl_groups)
    min_level = int(min_level or 0)
    email = os.environ.get("WEAVE_VIEWER_EMAIL", "").strip().lower()
    person_id = ""
    if email:
        people = graph.query(
            f"""
            MATCH (p:{Label.PERSON})
            WHERE p.{Prop.TENANT_ID} = $tenant AND toLower(p.{Prop.EMAIL}) = $email
            RETURN p.{Prop.ID}
            LIMIT 1
            """,
            {"tenant": tenant, "email": email},
        ).result_set
        if people:
            person_id = people[0][0]
    graph.query(
        f"""
        MERGE (m:{Label.MESSAGE}:{Label.ITEM} {{id: $item_id}})
        SET
          m.{Prop.TENANT_ID} = $tenant,
          m.{Prop.SOURCE} = 'slack',
          m.{Prop.OBJECT_TYPE} = 'message',
          m.{Prop.EXTERNAL_ID} = 'demo:api-key',
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
            "item_id": ITEM_ID,
            "tenant": tenant,
            "occurred_at": to_ts(datetime.now(UTC)),
            "excerpt": EXCERPT,
            "content_hash": hashlib.sha256(EXCERPT.encode()).hexdigest(),
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
            {"item_id": ITEM_ID, "person_id": person_id},
        )
    conn = connect_pg()
    try:
        with conn.cursor() as cur:
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
                (f"{ITEM_ID}:chunk:0", tenant, ITEM_ID, CHUNK, acl_groups, min_level),
            )
        conn.commit()
    finally:
        conn.close()
    print("seeded", ITEM_ID)
    print("source", source_id)
    print("groups", ",".join(acl_groups))
    print("level", min_level)
    _ask()


def _redact(text: str) -> str:
    import re

    return re.sub(r"\b[A-Za-z0-9_\-]{20,}\b", "[redacted]", text)


def _groups(value) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(part) for part in value or []]


def _ask() -> None:
    token = os.environ.get("WEAVE_VIEWER_TOKEN") or os.environ.get("WEAVE_TOKEN_SAM") or ""
    if not token:
        print("ask skipped: no viewer token")
        return
    request = urllib.request.Request(
        "http://127.0.0.1:8765/answer",
        data=json.dumps({"question": QUESTION}).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        card = json.loads(response.read().decode())
    answer = " ".join(str(card.get("answer") or "").split())
    print("item_cited", ITEM_ID in json.dumps(card))
    print("answer_has_demo_key", DEMO_KEY in answer)
    print("answer", answer if DEMO_KEY in answer else _redact(answer))


if __name__ == "__main__":
    main()
