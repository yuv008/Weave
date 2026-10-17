"""Source allow-list from ``config/weave.yaml``.

A container that is not listed is not written. Nothing is defaulted.
"""

from shared.acl import acl_group
from shared.config import SourcePolicy, load_config
from shared.ids import source_id
from shared.schema import Label


def allowed_source(system: str, container: str) -> tuple[str, SourcePolicy] | None:
    if not container:
        return None
    group = acl_group(system, container)
    policy = load_config().sources.get(group)
    if policy is None:
        return None
    return group, policy


def slack_source_id(tenant_id: str, workspace_id: str, channel_id: str) -> str:
    from shared.ids import slack_channel_id

    return slack_channel_id(tenant_id, workspace_id, channel_id)


def notion_source_id(tenant_id: str, workspace_id: str) -> str:
    return source_id(tenant_id, "notion", "teamspace", workspace_id)


def drive_source_id(tenant_id: str, folder_id: str) -> str:
    return source_id(tenant_id, "gdrive", "folder", folder_id)


def github_source_id(tenant_id: str, repo_slug: str) -> str:
    return source_id(tenant_id, "github", "repo", repo_slug)


def gmail_source_id(tenant_id: str, mailbox: str) -> str:
    return source_id(tenant_id, "gmail", "mailbox", mailbox)


def gcal_source_id(tenant_id: str, calendar_id: str) -> str:
    return source_id(tenant_id, "gcal", "calendar", calendar_id)


def hubspot_source_id(tenant_id: str, portal_id: str) -> str:
    return source_id(tenant_id, "hubspot", "portal", portal_id)


SOURCE_LABELS = {
    "slack": (Label.CONVERSATION, Label.SOURCE),
    "notion": (Label.TEAMSPACE, Label.SOURCE),
    "gdrive": (Label.SOURCE,),
    "github": (Label.REPOSITORY, Label.SOURCE),
    "gmail": (Label.SOURCE,),
    "gcal": (Label.SOURCE,),
    "hubspot": (Label.SOURCE,),
}
