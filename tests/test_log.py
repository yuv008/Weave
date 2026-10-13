from app.ingest.log import format_page_log


def test_page_log_drops_message_body_and_token():
    body = "unique-message-body"
    token = "xoxb-unique-token"
    line = format_page_log(
        tenant_id="tenant1",
        workspace_id="T1",
        channel_id="C1",
        records=2,
        failed=0,
        duration_ms=15,
        error_class="",
        text=body,
        token=token,
    )

    assert line == "tenant_id=tenant1 workspace_id=T1 channel_id=C1 records=2 failed=0 duration_ms=15 error_class="
    assert body not in line
    assert token not in line
