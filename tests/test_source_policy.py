"""Listed sources stay allowed. A container nobody connected is not written."""

from app.graph.policy import allowed_source


def test_listed_notion_workspace_is_allowed():
    found = allowed_source("notion", "sales-deals")
    assert found is not None
    group, policy = found
    assert group == "notion:sales-deals"
    assert policy.min_level == 0
    assert policy.authority == 3


def test_unknown_container_is_closed():
    assert allowed_source("notion", "not-listed-workspace-xyz") is None
