"""ACL group strings. One builder for both sides of the gate.

Format ``{system}:{container}``:
- ``slack:C0123ABC`` for a channel
- ``github:org/repo`` for a repository
- ``notion:{teamspace_or_page}``
- ``gdrive:{folder_id}``
- ``org:all`` for org-wide

Items and sources store these in ``acl_groups``; the caller's groups come
from the ``group_membership`` table and use the same strings.
"""

SYSTEMS = ("slack", "github", "notion", "gdrive", "gmail", "gcal", "hubspot")
ORG_WIDE = "org:all"


def acl_group(system: str, container: str) -> str:
    value = f"{system}:{container}"
    parse_acl(value)
    return value


def parse_acl(value: str) -> tuple[str, str]:
    if value == ORG_WIDE:
        return ("org", "all")
    system, sep, container = value.partition(":")
    if not sep or system not in SYSTEMS:
        raise ValueError(f"unknown acl system in {value!r}")
    if not container or container != container.strip() or any(c.isspace() for c in container):
        raise ValueError(f"bad acl container in {value!r}")
    return (system, container)


def is_acl(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parse_acl(value)
    except ValueError:
        return False
    return True
