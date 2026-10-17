"""Ask one question from co:{WEAVE_TENANT}. Does not print the token or the key."""

import os
import secrets

from agents.gate.resolve import hash_token
from app.graph.client import connect_graph
from scripts.ask import ask_local, render
from shared.env import load_env
from shared.pg import connect_pg
from shared.schema import Label, Prop


def main() -> None:
    load_env()
    wanted = {"GROQ_API_KEY", "WEAVE_TENANT", "WEAVE_VIEWER_EMAIL", "FALKORDB_URL", "DATABASE_URL", "DATABASE_URL_UNPOOLED"}
    for line in open(".env", encoding="utf-8"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() in wanted and value.strip():
            os.environ[name.strip()] = value.strip().strip('"').strip("'")
    tenant = os.environ["WEAVE_TENANT"].strip()
    email = os.environ.get("WEAVE_VIEWER_EMAIL", "").strip().lower()
    if not email:
        raise SystemExit("WEAVE_VIEWER_EMAIL is not set")
    graph = connect_graph()
    people = graph.query(
        f"""
        MATCH (p:{Label.PERSON})
        WHERE p.{Prop.TENANT_ID} = $tenant AND toLower(p.{Prop.EMAIL}) = $email
        RETURN p.{Prop.ID}
        LIMIT 1
        """,
        {"tenant": tenant, "email": email},
    ).result_set
    if not people:
        raise SystemExit("viewer email is not a Person in the company graph")
    person_id = people[0][0]
    rows = graph.query(
        f"""
        MATCH (m:{Label.MESSAGE})
        WHERE m.{Prop.TENANT_ID} = $tenant AND m.excerpt <> ''
        RETURN m.excerpt
        LIMIT 1
        """,
        {"tenant": tenant},
    ).result_set
    if not rows or not rows[0][0]:
        raise SystemExit("no message excerpt in the company graph")
    excerpt = str(rows[0][0]).strip().splitlines()[0][:180]
    question = f"What was said in this message: {excerpt}"
    token = "wv_" + secrets.token_urlsafe(24)
    conn = connect_pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_token (token_sha256, tenant_id, person_id, label) VALUES (%s, %s, %s, %s)",
                (hash_token(token), tenant, person_id, "live-check"),
            )
        conn.commit()
    finally:
        conn.close()
    print("question:", question)
    print(render(ask_local(token, question, None)))


if __name__ == "__main__":
    main()
