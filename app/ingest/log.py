"""Structured ingest logs.

Only the fields listed here are written. Message bodies, excerpts, and tokens
passed in by mistake are dropped.
"""

import logging

_FIELDS = (
    "tenant_id",
    "workspace_id",
    "channel_id",
    "records",
    "failed",
    "duration_ms",
    "error_class",
)


def format_page_log(**fields: object) -> str:
    parts: list[str] = []
    for key in _FIELDS:
        value = fields.get(key, "")
        if value is None:
            value = ""
        parts.append(f"{key}={value}")
    return " ".join(parts)


def format_document_log(**fields: object) -> str:
    keys = ("tenant_id", "workspace_id", "folder_id", "records", "failed", "duration_ms", "error_class")
    parts: list[str] = []
    for key in keys:
        value = fields.get(key, "")
        if value is None:
            value = ""
        parts.append(f"{key}={value}")
    return " ".join(parts)


def format_github_log(**fields: object) -> str:
    keys = ("tenant_id", "repo_id", "records", "failed", "duration_ms", "error_class")
    parts: list[str] = []
    for key in keys:
        value = fields.get(key, "")
        if value is None:
            value = ""
        parts.append(f"{key}={value}")
    return " ".join(parts)


def log_page(logger: logging.Logger, **fields: object) -> None:
    logger.info("%s", format_page_log(**fields))
