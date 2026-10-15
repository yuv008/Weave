"""Run a registered tool the same way everywhere (MCP server, answer agent): check arguments, then call.

Read-only results can be cached briefly. The key starts with the caller's
visibility hash, which includes the tenant's ``acl_epoch``, so a permission
change always misses the cache.
"""

import copy
import json
import threading
import time

from agents.mcp.schema import input_schema
from agents.tools.base import REGISTRY, ToolContext, ToolInputError, ToolResult, ToolSpec

_JSON_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v),
}


def check_args(spec: ToolSpec, args: dict) -> None:
    schema = input_schema(spec)
    props = schema["properties"]
    unknown = sorted(set(args) - set(props))
    if unknown:
        raise ToolInputError(f"unknown arguments {unknown}; allowed {sorted(props)}")
    missing = [key for key in schema.get("required", []) if args.get(key) is None]
    if missing:
        raise ToolInputError(f"missing arguments {missing}")
    for key, value in args.items():
        if value is None:
            continue
        kind = props[key]["type"]
        if not _JSON_CHECKS[kind](value):
            raise ToolInputError(f"{key} must be {'a list of strings' if kind == 'array' else 'a ' + kind}")
        if "enum" in props[key] and value not in props[key]["enum"]:
            raise ToolInputError(f"{key} must be one of {props[key]['enum']}")


class ToolCache:
    def __init__(self, ttl_seconds: float, clock=time.monotonic):
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[tuple, tuple[float, ToolResult]] = {}

    @staticmethod
    def key(ctx: ToolContext, name: str, args: dict) -> tuple:
        return (ctx.principal.visibility_hash, ctx.graph.name, name, json.dumps(args, sort_keys=True, default=str))

    def get(self, key: tuple) -> ToolResult | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or self._clock() - entry[0] > self._ttl:
                self._entries.pop(key, None)
                return None
            return copy.deepcopy(entry[1])

    def put(self, key: tuple, result: ToolResult) -> None:
        if self._ttl <= 0:
            return
        with self._lock:
            self._entries[key] = (self._clock(), copy.deepcopy(result))


def invoke(name: str, ctx: ToolContext, args: dict | None = None, cache: ToolCache | None = None) -> ToolResult:
    spec = REGISTRY.get(name)
    if spec is None:
        raise ToolInputError(f"unknown tool {name!r}")
    args = dict(args or {})
    check_args(spec, args)
    key = ToolCache.key(ctx, name, args) if cache is not None and spec.mode == "read" else None
    if key is not None and (hit := cache.get(key)) is not None:
        return hit
    result = spec.fn(ctx, **args)
    if key is not None:
        cache.put(key, result)
    return result
