"""Source allow-list from ``config/weave.yaml``.

A container that is not listed is not written. A connector grant can add
one through ``ensure_connected_source``; that row is the only default.
"""

from shared.acl import acl_group
from shared.config import SourcePolicy, load_config
from shared.ids import source_id
from shared.schema import Label

_connected: dict[str, SourcePolicy] | None = None


def allowed_source(system: str, container: str) -> tuple[str, SourcePolicy] | None:
    if not container:
        return None
    group = acl_group(system, container)
    policy = load_config().sources.get(group)
    if policy is None:
        policy = _connected_policy(group)
    if policy is None:
        return None
    return group, policy


def ensure_connected_source(tenant_id: str, system: str, container: str) -> None:
    """Remember a source a tenant just connected, so the next poll can write it."""

    if not tenant_id or not container:
        return
    group = acl_group(system, container)
    if load_config().sources.get(group) is not None or _connected_policy(group) is not None:
        return
    authority = load_config().authority.get(system)
    if authority is None:
        return
    policy = SourcePolicy(
        name=container[:80],
        min_level=0,
        cascade_policy="manual",
        authority=authority,
        org_wide=True,
    )
    if not _store_connected(tenant_id, group, policy):
        return
    _remember(group, policy)


def _connected_policy(group: str) -> SourcePolicy | None:
    return _load_connected().get(group)


def _remember(group: str, policy: SourcePolicy) -> None:
    _load_connected()[group] = policy


def _load_connected() -> dict[str, SourcePolicy]:
    global _connected
    if _connected is not None:
        return _connected
    _connected = {}
    try:
        from shared.pg import connect_pg

        with connect_pg() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT acl_group, name, min_level, cascade_policy, authority, org_wide
                    FROM connected_source
                    """
                )
                for group, name, min_level, cascade, authority, org_wide in cur.fetchall():
                    _connected[group] = SourcePolicy(
                        name=name,
                        min_level=int(min_level),
                        cascade_policy=cascade,
                        authority=int(authority),
                        org_wide=bool(org_wide),
                    )
            conn.commit()
    except Exception:
        return _connected
    return _connected


def _store_connected(tenant_id: str, group: str, policy: SourcePolicy) -> bool:
    try:
        from shared.pg import connect_pg

        with connect_pg() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO connected_source
                        (tenant_id, acl_group, name, min_level, cascade_policy, authority, org_wide)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, acl_group) DO NOTHING
                    """,
                    (
                        tenant_id,
                        group,
                        policy.name,
                        policy.min_level,
                        policy.cascade_policy,
                        policy.authority,
                        policy.org_wide,
                    ),
                )
            conn.commit()
    except Exception:
        return False
    return True


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
