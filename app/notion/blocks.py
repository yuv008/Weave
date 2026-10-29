"""Plain text from a Notion block list. The block tree is not stored."""

_TEXT_TYPES = {
    "paragraph",
    "heading_1",
    "heading_2",
    "heading_3",
    "bulleted_list_item",
    "numbered_list_item",
    "to_do",
    "quote",
    "callout",
    "toggle",
    "code",
}


def plain_text_from_blocks(blocks: list) -> str:
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        typed = block.get(block_type) if isinstance(block_type, str) else None
        if not isinstance(typed, dict):
            continue
        if block_type == "table_row":
            cells = typed.get("cells") or []
            text = " | ".join(_rich(cell) for cell in cells if isinstance(cell, list))
        elif block_type in _TEXT_TYPES:
            text = _rich(typed.get("rich_text") or [])
        else:
            continue
        if text.strip(" |"):
            parts.append(text)
    return "\n".join(parts)


def _rich(rich: list) -> str:
    return "".join(
        item.get("plain_text", "")
        for item in rich
        if isinstance(item, dict) and isinstance(item.get("plain_text"), str)
    )
