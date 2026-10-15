"""MCP tool definitions built from the tool registry.

Descriptions read "Use when / Do not use when" so a client such as Cursor
picks the right tool, and input schemas come from the function signature, so
they can never offer a user id, tenant or surface (tools do not take one).
"""

import inspect
import types
import typing

import mcp_types

from agents.tools.base import ToolSpec

_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


def description(spec: ToolSpec) -> str:
    summary = inspect.getdoc(spec.fn) or ""
    first = summary.split("\n\n", 1)[0].replace("\n", " ").replace("``", "`").strip()
    return f"{first}\n\nUse when: {spec.use_when}\nDo not use when: {spec.dont_use_when}"


def _json_type(annotation) -> tuple[str, bool]:
    """JSON type for an annotation, and whether ``None`` is allowed."""

    args = typing.get_args(annotation)
    if typing.get_origin(annotation) is list and args == (str,):
        return "array", False
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        inner = [a for a in args if a is not type(None)]
        if len(inner) != 1:
            raise TypeError(f"unsupported union {annotation!r}")
        return _json_type(inner[0])[0], True
    if annotation not in _JSON_TYPES:
        raise TypeError(f"unsupported argument type {annotation!r}")
    return _JSON_TYPES[annotation], False


def input_schema(spec: ToolSpec) -> dict:
    hints = typing.get_type_hints(spec.fn)
    properties, required = {}, []
    for param in spec.params:
        json_type, _ = _json_type(hints[param.name])
        prop: dict = {"type": json_type, "description": spec.args[param.name]}
        if json_type == "array":
            prop["items"] = {"type": "string"}
        if param.name in spec.choices:
            prop["enum"] = list(spec.choices[param.name])
        if param.default is inspect.Parameter.empty:
            required.append(param.name)
        elif param.default is not None:
            prop["default"] = param.default
        properties[param.name] = prop
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


def tool_definition(spec: ToolSpec) -> mcp_types.Tool:
    return mcp_types.Tool(
        name=spec.name,
        description=description(spec),
        input_schema=input_schema(spec),
        annotations=mcp_types.ToolAnnotations(
            read_only_hint=spec.mode == "read",
            destructive_hint=False,
            idempotent_hint=spec.mode == "read",
            open_world_hint=False,
        ),
    )
