import hashlib
from datetime import UTC, datetime

import pytest

from app.slack.normalize import format_slack_version, normalize_slack_message

TENANT = "tenant1"
CONNECTION = "conn1"
WORKSPACE = "T1"
CHANNEL = "C1"


def normalize(raw, *, channel_type="public_channel", domain="acme.slack.com"):
    return normalize_slack_message(
        raw,
        tenant_id=TENANT,
        connection_id=CONNECTION,
        workspace_id=WORKSPACE,
        channel_id=CHANNEL,
        channel_type=channel_type,
        workspace_domain=domain,
    )


def test_version_padding_matches_chronological_order():
    assert format_slack_version("9.0") < format_slack_version("10.0")
    assert format_slack_version("123.6") == format_slack_version("123.60")
    assert format_slack_version("123.6") < format_slack_version("123.70")


def test_public_message_uses_workspace_in_person_and_conversation_ids():
    message = normalize(
        {
            "user": "U1",
            "text": "Ship the annual plan",
            "ts": "1403051575.000407",
            "client_msg_id": "abc",
        }
    )

    assert message is not None
    assert message.external_id == "C1:1403051575.000407"
    assert message.node_id == "tenant1:slack:message:C1:1403051575.000407"
    assert message.conversation_node_id == "tenant1:slack:conversation:T1:C1"
    assert message.person_node_id == "tenant1:slack:person:T1:U1"
    assert message.version == format_slack_version("1403051575.000407")
    assert message.occurred_at == datetime(2014, 6, 18, 0, 32, 55, 407, tzinfo=UTC)
    assert message.source_url == "https://acme.slack.com/archives/C1/p1403051575000407"
    assert message.visibility == "demo_single_tenant"
    assert message.metadata == {"client_msg_id": "abc"}
    assert message.content_hash == hashlib.sha256(b"Ship the annual plan").hexdigest()


def test_excerpt_is_a_prefix_and_the_tail_is_not_retained():
    text = ("A" * 280) + "TAIL_MUST_NOT_BE_STORED"
    message = normalize({"user": "U1", "text": text, "ts": "1403051575.000407"})

    assert message is not None
    assert message.excerpt == "A" * 280
    assert "TAIL_MUST_NOT_BE_STORED" not in message.model_dump_json()
    assert message.content_hash == hashlib.sha256(text.encode()).hexdigest()


def test_thread_parent_is_set_only_for_a_reply():
    parent = normalize({"user": "U1", "text": "top", "ts": "10.1", "thread_ts": "10.1"})
    reply = normalize({"user": "U2", "text": "reply", "ts": "10.2", "thread_ts": "10.1"})

    assert parent is not None and parent.thread_parent_external_id is None
    assert reply is not None
    assert reply.thread_parent_external_id == "C1:10.1"


def test_edit_version_is_the_edited_timestamp():
    message = normalize(
        {
            "user": "U1",
            "text": "revised",
            "ts": "10.1",
            "edited": {"user": "U1", "ts": "10.2"},
        }
    )

    assert message is not None
    assert message.version == format_slack_version("10.2")
    assert message.edited_at is not None
    assert message.occurred_at < message.edited_at


def test_message_changed_reads_the_inner_message():
    message = normalize(
        {
            "subtype": "message_changed",
            "ts": "10.2",
            "message": {"user": "U1", "text": "inner", "ts": "10.1", "edited": {"ts": "10.2"}},
        }
    )

    assert message is not None
    assert message.external_id == "C1:10.1"
    assert message.excerpt == "inner"
    assert message.version == format_slack_version("10.2")


def test_delete_drops_the_previous_body():
    secret = "do not keep this body"
    message = normalize(
        {
            "subtype": "message_deleted",
            "ts": "10.3",
            "deleted_ts": "10.1",
            "previous_message": {
                "user": "U1",
                "text": secret,
                "ts": "10.1",
                "thread_ts": "10.0",
            },
        }
    )

    assert message is not None
    assert message.deleted is True
    assert message.excerpt == ""
    assert message.author_external_id == "U1"
    assert message.thread_parent_external_id == "C1:10.0"
    assert message.external_id == "C1:10.1"
    assert message.version == format_slack_version("10.3")
    assert secret not in message.model_dump_json()
    assert message.content_hash == hashlib.sha256(b"").hexdigest()


def test_non_public_channels_are_skipped():
    raw = {"user": "U1", "text": "secret", "ts": "10.1"}
    for channel_type in ("private_channel", "mpim", "im"):
        assert normalize(raw, channel_type=channel_type) is None


def test_bot_message_without_a_user_has_no_author():
    message = normalize(
        {"subtype": "bot_message", "bot_id": "B1", "text": "ping", "ts": "10.1"}
    )

    assert message is not None
    assert message.author_external_id is None
    assert message.person_node_id is None
    assert message.metadata["bot_id"] == "B1"


def test_malformed_public_payload_raises():
    with pytest.raises(ValueError):
        normalize({"user": "U1", "text": "x"})


def test_tenant_id_cannot_contain_the_id_separator():
    with pytest.raises(ValueError):
        normalize_slack_message(
            {"user": "U1", "text": "x", "ts": "10.1"},
            tenant_id="a:b",
            connection_id=CONNECTION,
            workspace_id=WORKSPACE,
            channel_id=CHANNEL,
            channel_type="public_channel",
        )
