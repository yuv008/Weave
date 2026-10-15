"""Gate rules on plain data. No I/O, so every rule is testable offline.

``item_is_visible`` and ``projection_is_visible`` are the Python twins of
the Cypher in ``agents.gate.visible``; tests check both agree.
"""

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta

from shared.acl import ORG_WIDE
from shared.schema import ProjectionStatus, Prop

OVERSIGHT_LEVEL = 2


def person_groups(
    person_id: str,
    level: int,
    memberships: Mapping[str, Iterable[str]],
    subordinates: Iterable[str] = (),
) -> set[str]:
    """Own groups plus org-wide; level 2 and above also get everyone below them."""

    groups = set(memberships.get(person_id, ())) | {ORG_WIDE}
    if level >= OVERSIGHT_LEVEL:
        for subordinate in subordinates:
            groups |= set(memberships.get(subordinate, ()))
    return groups


def channel_level(member_levels: Iterable[int]) -> int:
    levels = list(member_levels)
    if not levels:
        raise ValueError("a channel principal needs at least one member")
    return min(levels)


def channel_groups(channel_group: str) -> set[str]:
    return {channel_group, ORG_WIDE}


def fresh_groups(last_sync: Mapping[str, datetime], now: datetime, max_minutes: int) -> set[str]:
    """Source groups with a good sync within ``max_minutes``. Sources with no sync row are absent."""

    cutoff = now - timedelta(minutes=max_minutes)
    return {group for group, synced in last_sync.items() if synced >= cutoff}


def drop_stale(
    groups: Iterable[str],
    last_sync: Mapping[str, datetime],
    now: datetime,
    max_minutes: int,
) -> set[str]:
    """Remove groups whose source has no good sync within ``max_minutes``. Unknown sources fail closed."""

    fresh = fresh_groups(last_sync, now, max_minutes)
    return {group for group in groups if group == ORG_WIDE or group in fresh}


def item_is_visible(item: Mapping, level: int, groups: Iterable[str], fresh: Iterable[str]) -> bool:
    """Source rule. Every source group on the item must be fresh, so a stale org-wide source hides too."""

    if item.get(Prop.DELETED):
        return False
    if item.get(Prop.MIN_LEVEL, 99) > level:
        return False
    acl = set(item.get(Prop.ACL_GROUPS) or ())
    fresh = set(fresh)
    if any(group != ORG_WIDE and group not in fresh for group in acl):
        return False
    return bool(acl & set(groups))


def projection_is_visible(projection: Mapping, level: int, groups: Iterable[str]) -> bool:
    if projection.get(Prop.STATUS) != ProjectionStatus.APPROVED:
        return False
    if projection.get(Prop.LEVEL_FLOOR, 99) > level:
        return False
    return bool(set(projection.get(Prop.AUDIENCE) or ()) & set(groups))
