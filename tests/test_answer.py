from datetime import UTC, datetime

from app.answer.channel_question import answer_channel_question
from app.graph.upsert import upsert_message
from app.models.canonical import CanonicalMessage
from app.slack.normalize import format_slack_version

TENANT = "tenant1"
CHANNEL = "C1"


def _message(excerpt: str, ts: str, *, deleted: bool = False) -> CanonicalMessage:
    return CanonicalMessage(
        tenant_id=TENANT,
        connection_id="conn1",
        workspace_id="T1",
        external_id=f"{CHANNEL}:{ts}",
        conversation_external_id=CHANNEL,
        author_external_id="U1",
        occurred_at=datetime.fromtimestamp(int(ts), tz=UTC),
        version=format_slack_version(ts),
        excerpt="" if deleted else excerpt,
        content_hash="ab" * 32,
        source_url="" if deleted else f"https://acme.slack.com/archives/C1/p{ts}",
        deleted=deleted,
    )


def test_deleted_excerpt_is_absent_and_the_live_hit_keeps_its_url(graph):
    live = _message("annual plan stays", "20")
    deleted = _message("annual plan secret", "10", deleted=True)
    upsert_message(graph, live)
    upsert_message(graph, deleted)
    graph.query(
        "MATCH (m:Message {id: $id}) SET m.excerpt = $excerpt, m.deleted = true",
        {"id": deleted.node_id, "excerpt": "annual plan secret"},
    )

    answer = answer_channel_question(graph, TENANT, CHANNEL, "annual plan")

    assert len(answer.citations) == 1
    assert answer.citations[0].source_url == "https://acme.slack.com/archives/C1/p20"
    assert "annual plan stays" in answer.text
    assert "annual plan secret" not in answer.text
    assert answer.citations[0].source_url in answer.text


def test_missing_phrase_says_the_graph_has_no_match(graph):
    upsert_message(graph, _message("something else", "20"))

    answer = answer_channel_question(graph, TENANT, CHANNEL, "annual plan")

    assert answer.citations == ()
    assert answer.text == "The graph has no matching public message."
