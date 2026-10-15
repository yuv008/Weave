"""The MCP server: the registry's read-only tools over Streamable HTTP, with bearer-token auth.

Every HTTP request resolves its own principal from ``Authorization: Bearer``
on the MCP surface (a private surface, so the caller is the token's owner).
The server runs stateless, so a session can never carry an earlier caller's
identity. The tenant, and therefore the graph, always comes from the token.
"""

import json
import logging
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime

import anyio
import mcp_types
from mcp.server.lowlevel import Server
from starlette.types import ASGIApp, Receive, Scope, Send

from agents.gate.principal import Principal, Surface
from agents.gate.resolve import NotAuthorized, PersonInfo, Resolver
from agents.gate.stores import GraphDirectory
from agents.graph import ReadOnlyGraph
from agents.mcp.audit import Audit
from agents.mcp.schema import input_schema, tool_definition
from agents.tools import REGISTRY, ToolContext, ToolInputError
from shared.config import WeaveConfig

log = logging.getLogger("weave.mcp")

INSTRUCTIONS = (
    "Weave is the company brain: read-only tools over decisions, owners, figures, dependencies "
    "and source messages, filtered to what the caller may see. Prefer the most specific tool. "
    "When you use a result, cite its citations (quote, author, date, link). A citation whose "
    "source is 'restricted' is an approved summary: state it without quoting or linking. "
    "If a tool finds nothing, say so instead of guessing."
)

_JSON_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


@dataclass(frozen=True)
class Caller:
    principal: Principal
    request_id: str


_CALLER: ContextVar[Caller | None] = ContextVar("weave_mcp_caller", default=None)


class TenantDirectory:
    """People are looked up in the token's own tenant graph."""

    def __init__(self, open_graph: Callable[[str], ReadOnlyGraph]):
        self._open = open_graph

    def person(self, tenant: str, person_id: str) -> PersonInfo | None:
        return GraphDirectory(self._open(tenant)).person(tenant, person_id)

    def levels(self, tenant: str, person_ids) -> dict[str, int]:
        return GraphDirectory(self._open(tenant)).levels(tenant, person_ids)


def _bearer(scope: Scope) -> str | None:
    for key, value in scope.get("headers", []):
        if key == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            token = token.strip()
            return token if scheme.lower() == "bearer" and token else None
    return None


async def _plain(send: Send, status: int, body: bytes, headers: list[tuple[bytes, bytes]] = ()) -> None:
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"text/plain; charset=utf-8"), *headers],
    })
    await send({"type": "http.response.body", "body": body})


class BearerAuth:
    """ASGI middleware: no valid token, no MCP. The 401 body never says why."""

    def __init__(self, app: ASGIApp, resolve: Callable[[str], Principal]):
        self._app = app
        self._resolve = resolve

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        token = _bearer(scope)
        try:
            if token is None:
                raise NotAuthorized()
            principal = await anyio.to_thread.run_sync(self._resolve, token)
        except NotAuthorized:
            await _plain(send, 401, b"not authorized", [(b"www-authenticate", b"Bearer")])
            return
        except Exception:
            log.exception("token resolution failed")
            await _plain(send, 503, b"unavailable")
            return
        reset = _CALLER.set(Caller(principal, uuid.uuid4().hex[:16]))
        try:
            await self._app(scope, receive, send)
        finally:
            _CALLER.reset(reset)


def _check_args(name: str, args: dict) -> None:
    schema = input_schema(REGISTRY[name])
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
        if not _JSON_CHECKS[props[key]["type"]](value):
            raise ToolInputError(f"{key} must be a {props[key]['type']}")
        if "enum" in props[key] and value not in props[key]["enum"]:
            raise ToolInputError(f"{key} must be one of {props[key]['enum']}")


def _error(text: str) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(content=[mcp_types.TextContent(type="text", text=text)], is_error=True)


def build_server(
    open_graph: Callable[[str], ReadOnlyGraph],
    cfg: WeaveConfig,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    pg_connect: Callable | None = None,
    audit: Audit | None = None,
) -> Server:
    definitions = [tool_definition(spec) for spec in REGISTRY.values()]

    async def list_tools(ctx, params) -> mcp_types.ListToolsResult:
        return mcp_types.ListToolsResult(tools=definitions)

    async def call_tool(ctx, params: mcp_types.CallToolRequestParams) -> mcp_types.CallToolResult:
        caller = _CALLER.get()
        if caller is None:
            return _error("not authorized")
        spec = REGISTRY.get(params.name)
        if spec is None:
            return _error(f"unknown tool {params.name!r}; call tools/list")
        args = dict(params.arguments or {})
        started = time.monotonic()
        outcome, node_ids, hidden = "ok", [], 0
        try:
            _check_args(spec.name, args)
            tool_ctx = ToolContext(
                caller.principal, open_graph(caller.principal.tenant), cfg, clock=clock, pg_connect=pg_connect
            )
            result = await anyio.to_thread.run_sync(lambda: spec.fn(tool_ctx, **args))
            node_ids, hidden = result.node_ids, result.hidden
            body = result.public(cfg)
            reply = mcp_types.CallToolResult(
                content=[mcp_types.TextContent(type="text", text=json.dumps(body, ensure_ascii=False))],
                structured_content=body,
            )
        except ToolInputError as exc:
            outcome, reply = "bad_input", _error(str(exc))
        except Exception:
            log.exception("tool %s failed (request %s)", spec.name, caller.request_id)
            outcome, reply = "error", _error(f"internal error (request {caller.request_id})")
        if audit is not None:
            entry = dict(
                request_id=caller.request_id,
                principal=caller.principal,
                tool=spec.name,
                args=args,
                node_ids=node_ids,
                hidden=hidden,
                outcome=outcome,
                ms=int((time.monotonic() - started) * 1000),
            )
            try:
                await anyio.to_thread.run_sync(lambda: audit.record(**entry))
            except Exception:
                log.warning("audit write failed (request %s)", caller.request_id, exc_info=True)
        return reply

    return Server(
        "weave",
        version="0.1.0",
        instructions=INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def build_app(
    resolver: Resolver,
    open_graph: Callable[[str], ReadOnlyGraph],
    cfg: WeaveConfig,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    pg_connect: Callable | None = None,
    audit: Audit | None = None,
    host: str = "127.0.0.1",
    path: str = "/mcp",
) -> ASGIApp:
    server = build_server(open_graph, cfg, clock=clock, pg_connect=pg_connect, audit=audit)
    inner = server.streamable_http_app(
        streamable_http_path=path, json_response=True, stateless_http=True, host=host
    )
    return BearerAuth(inner, lambda token: resolver.for_token(token, Surface.MCP))
