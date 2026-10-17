"""The agent server: the registry's tools over MCP (Streamable HTTP), plus the answer API, with bearer-token auth.

Every HTTP request except ``GET /ops`` resolves its own principal from
``Authorization: Bearer``. Requests under the MCP path use the MCP surface;
``/answer``, ``/actions/...`` and ``/wisp/cue`` use the web surface. These are private surfaces,
so the caller is the token's owner. The server runs stateless, so a session can
never carry an earlier caller's identity. The tenant, and therefore the graph,
always comes from the token. ``GET /ops`` is outside that auth: it checks
``OPS_TOKEN`` and reports numbers for ``WEAVE_TENANT`` only.
"""

import hashlib
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass

import anyio
import mcp_types
from mcp.server.lowlevel import Server
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from agents.gate.principal import Principal, Surface
from agents.gate.resolve import NotAuthorized, PersonInfo
from agents.gate.stores import GraphDirectory
from agents.graph import ReadOnlyGraph
from agents.mcp.ops import OpsFront
from agents.mcp.schema import tool_definition
from agents.runtime import Runtime
from agents.tools import REGISTRY, ToolInputError
from agents.tools.invoke import invoke

log = logging.getLogger("weave.mcp")

INSTRUCTIONS = (
    "Weave is the company brain: tools over decisions, owners, figures, dependencies "
    "and source messages, filtered to what the caller may see. Prefer the most specific tool. "
    "When you use a result, cite its citations (quote, author, date, link). A citation whose "
    "source is 'restricted' is an approved summary: state it without quoting or linking. "
    "If a tool finds nothing, say so instead of guessing. draft_email and notify_owner only "
    "propose: nothing is sent until the caller confirms."
)

MAX_QUESTION_CHARS = 2000
MAX_CUE_CHARS = 1500


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


_CUE_CACHE_TTL = 45.0


class BearerAuth:
    """ASGI middleware: no valid token, no access. The 401 body never says why.

    ``POST /wisp/cue`` reuses a principal for a short time so a draft can be
    checked more than once while someone is still typing. Other routes resolve
    every request.
    """

    def __init__(self, app: ASGIApp, resolve: Callable[[str, Surface], Principal], mcp_path: str = "/mcp"):
        self._app = app
        self._resolve = resolve
        self._mcp_path = mcp_path
        self._cue_cache: dict[str, tuple[float, Principal]] = {}
        self._cue_lock = threading.Lock()

    def _cue_hit(self, token: str) -> tuple[Principal | None, str]:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.monotonic()
        with self._cue_lock:
            hit = self._cue_cache.get(digest)
            if hit is not None and now - hit[0] < _CUE_CACHE_TTL:
                return hit[1], digest
            stale = [key for key, (at, _) in self._cue_cache.items() if now - at >= _CUE_CACHE_TTL]
            for key in stale:
                del self._cue_cache[key]
        return None, digest

    def surface(self, path: str) -> Surface:
        return Surface.MCP if path == self._mcp_path or path.startswith(self._mcp_path + "/") else Surface.WEB

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        if scope.get("method") == "GET" and scope.get("path") == "/health":
            await _plain(send, 200, b"ok")
            return
        token = _bearer(scope)
        path = scope.get("path", "")
        surface = self.surface(path)
        cue_digest = None
        try:
            if token is None:
                raise NotAuthorized()
            cached = None
            if path == "/wisp/cue":
                cached, cue_digest = self._cue_hit(token)
            if cached is not None:
                principal = cached
            else:
                principal = await anyio.to_thread.run_sync(self._resolve, token, surface)
                if cue_digest is not None:
                    with self._cue_lock:
                        self._cue_cache[cue_digest] = (time.monotonic(), principal)
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


def _error(text: str) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(content=[mcp_types.TextContent(type="text", text=text)], is_error=True)


def build_server(runtime: Runtime) -> Server:
    definitions = [tool_definition(spec) for spec in REGISTRY.values()]
    audit, cfg = runtime.audit, runtime.cfg

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
            tool_ctx = runtime.context(caller.principal, agent="mcp")
            result = await anyio.to_thread.run_sync(lambda: invoke(spec.name, tool_ctx, args, runtime.cache))
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


async def _json_body(request: Request) -> dict | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def answer_routes(runtime: Runtime, agent) -> list[Route]:
    """``POST /answer`` and ``POST /actions/{task_id}/confirm`` (web surface)."""

    from agents.answer.actions import ActionNotFound, confirm_action
    from agents.answer.agent import ConversationNotFound

    async def answer(request: Request):
        caller = _CALLER.get()
        if caller is None:
            return PlainTextResponse("not authorized", status_code=401)
        body = await _json_body(request)
        question = (body or {}).get("question")
        conversation_id = (body or {}).get("conversation_id")
        if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_CHARS:
            return PlainTextResponse(f"question must be 1-{MAX_QUESTION_CHARS} characters", status_code=400)
        if conversation_id is not None and not isinstance(conversation_id, str):
            return PlainTextResponse("conversation_id must be a string", status_code=400)
        try:
            card = await anyio.to_thread.run_sync(
                lambda: agent.answer(
                    caller.principal, question.strip(), conversation_id=conversation_id, request_id=caller.request_id
                )
            )
        except ConversationNotFound:
            return PlainTextResponse("conversation not found", status_code=404)
        except Exception:
            log.exception("answer failed (request %s)", caller.request_id)
            return PlainTextResponse(f"internal error (request {caller.request_id})", status_code=500)
        return JSONResponse(card.public())

    async def confirm(request: Request):
        caller = _CALLER.get()
        if caller is None:
            return PlainTextResponse("not authorized", status_code=401)
        body = await _json_body(request) or {}
        task_id = request.path_params["task_id"]
        try:
            outcome = await anyio.to_thread.run_sync(
                lambda: confirm_action(runtime, caller.principal, task_id, body.get("decision"), body.get("text"))
            )
        except ActionNotFound:
            return PlainTextResponse("action not found", status_code=404)
        except ToolInputError as exc:
            return PlainTextResponse(str(exc), status_code=400)
        except Exception:
            log.exception("confirm failed (request %s)", caller.request_id)
            return PlainTextResponse(f"internal error (request {caller.request_id})", status_code=500)
        return JSONResponse(outcome)

    return [
        Route("/answer", answer, methods=["POST"]),
        Route("/actions/{task_id}/confirm", confirm, methods=["POST"]),
    ]


def cue_routes(runtime: Runtime) -> list[Route]:
    """``POST /wisp/cue`` (web surface). The prompt is search input only."""

    from agents.wisp.cue import cue

    async def wisp_cue(request: Request):
        caller = _CALLER.get()
        if caller is None:
            return PlainTextResponse("not authorized", status_code=401)
        body = await _json_body(request)
        prompt = (body or {}).get("prompt")
        text = prompt.strip() if isinstance(prompt, str) else ""
        if not text or len(text) > MAX_CUE_CHARS:
            return PlainTextResponse(f"prompt must be 1-{MAX_CUE_CHARS} characters", status_code=400)
        try:
            payload = await anyio.to_thread.run_sync(
                lambda: cue(runtime.context(caller.principal, agent="answer"), text)
            )
        except Exception as exc:
            log.error("cue failed (request %s): %s", caller.request_id, type(exc).__name__)
            return PlainTextResponse(f"internal error (request {caller.request_id})", status_code=500)
        return JSONResponse(payload)

    return [Route("/wisp/cue", wisp_cue, methods=["POST"])]


def build_app(runtime: Runtime, *, agent=None, host: str = "127.0.0.1", path: str = "/mcp") -> ASGIApp:
    if runtime.resolver is None:
        raise ValueError("the server needs a resolver")
    server = build_server(runtime)
    routes = answer_routes(runtime, agent) if agent is not None else []
    routes = [*routes, *cue_routes(runtime)]
    inner = server.streamable_http_app(
        streamable_http_path=path, json_response=True, stateless_http=True, host=host,
        custom_starlette_routes=routes,
    )
    protected = BearerAuth(inner, runtime.resolver.for_token, mcp_path=path)
    return OpsFront(protected)
