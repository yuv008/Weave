"""Write a fixture into FalkorDB and Postgres. Every write is a MERGE or an upsert."""

from collections import defaultdict
from pathlib import Path

from fixtures.acme import Fixture
from shared.indexes import ensure_contract_indexes
from shared.schema import CONTRACT_LABELS, EDGE_ENDPOINTS, SOURCE_SPECIFIC_LABELS, Prop

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "001_contract.sql"
_ALLOWED_LABELS = set(CONTRACT_LABELS) | set(SOURCE_SPECIFIC_LABELS)
BATCH = 200


def _batches(rows: list, size: int = BATCH):
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def seed_graph(graph, fx: Fixture) -> dict[str, int]:
    ensure_contract_indexes(graph)

    by_labels: dict[tuple[str, ...], list[dict]] = defaultdict(list)
    for node in fx.nodes:
        if not set(node.labels) <= _ALLOWED_LABELS:
            raise ValueError(f"unknown label in {node.labels}")
        by_labels[node.labels].append({"id": node.id, "props": node.props})
    for labels, rows in by_labels.items():
        statement = (
            f"UNWIND $rows AS row MERGE (n:{':'.join(labels)} {{{Prop.ID}: row.id}}) SET n += row.props"
        )
        for batch in _batches(rows):
            graph.query(statement, {"rows": batch})

    label_of = {node.id: node.label for node in fx.nodes}
    by_shape: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for edge in fx.edges:
        if edge.rel not in EDGE_ENDPOINTS:
            raise ValueError(f"unknown edge type {edge.rel}")
        by_shape[(edge.rel, label_of[edge.src], label_of[edge.dst])].append(
            {"src": edge.src, "dst": edge.dst, "props": edge.props}
        )
    for (rel, src_label, dst_label), rows in by_shape.items():
        statement = (
            f"UNWIND $rows AS row "
            f"MATCH (a:{src_label} {{{Prop.ID}: row.src}}) "
            f"MATCH (b:{dst_label} {{{Prop.ID}: row.dst}}) "
            f"MERGE (a)-[r:{rel}]->(b) SET r += row.props"
        )
        for batch in _batches(rows):
            graph.query(statement, {"rows": batch})
    return {"nodes": len(fx.nodes), "edges": len(fx.edges)}


def embed_texts(texts: list[str], model_name: str, dim: int) -> list[list[float]]:
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_name)
    vectors = model.encode(texts, normalize_embeddings=True)
    if vectors.shape[1] != dim:
        raise ValueError(f"{model_name} returned dimension {vectors.shape[1]}, contract says {dim}")
    return [vector.tolist() for vector in vectors]


def vector_literal(vector: list[float] | None) -> str | None:
    if vector is None:
        return None
    return "[" + ",".join(f"{value:.7f}" for value in vector) + "]"


def apply_migration(conn) -> None:
    """Run over a direct connection, never the pooled one."""

    with conn.cursor() as cur:
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
    conn.commit()


def seed_postgres(conn, fx: Fixture, embeddings: list[list[float]] | None = None) -> dict[str, int]:
    tenant = fx.tenant
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO group_membership (tenant_id, person_id, acl_group, source_system)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            [(tenant, m["person_id"], m["acl_group"], m["source_system"]) for m in fx.memberships],
        )
        cur.executemany(
            """
            INSERT INTO source_sync (tenant_id, source_id, acl_group, last_good_sync_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (tenant_id, source_id) DO UPDATE SET last_good_sync_at = now()
            """,
            [(tenant, s["source_id"], s["acl_group"]) for s in fx.source_sync],
        )
        cur.execute(
            "INSERT INTO tenant_acl (tenant_id, acl_epoch) VALUES (%s, 1) ON CONFLICT DO NOTHING",
            (tenant,),
        )
        cur.executemany(
            """
            INSERT INTO change_event (id, tenant_id, fact_id, kind, new_fact_id, created_at, processed)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            [
                (e["id"], tenant, e["fact_id"], e["kind"], e["new_fact_id"], e["created_at"], e["processed"])
                for e in fx.change_events
            ],
        )
        vectors = embeddings or [None] * len(fx.chunks)
        cur.executemany(
            """
            INSERT INTO chunk (id, tenant_id, item_id, ordinal, text, embedding, acl_groups, min_level)
            VALUES (%s, %s, %s, 0, %s, %s::vector, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
              text = EXCLUDED.text,
              embedding = coalesce(EXCLUDED.embedding, chunk.embedding),
              acl_groups = EXCLUDED.acl_groups,
              min_level = EXCLUDED.min_level
            """,
            [
                (
                    f"{c['item_id']}:chunk:0",
                    tenant,
                    c["item_id"],
                    c["text"],
                    vector_literal(vector),
                    c["acl_groups"],
                    c["min_level"],
                )
                for c, vector in zip(fx.chunks, vectors)
            ],
        )
    conn.commit()
    return {
        "memberships": len(fx.memberships),
        "sources": len(fx.source_sync),
        "change_events": len(fx.change_events),
        "chunks": len(fx.chunks),
    }
