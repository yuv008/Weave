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
}


def plain_text_from_blocks(blocks: list) -> str:
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type not in _TEXT_TYPES:
            continue
        typed = block.get(block_type)
        if not isinstance(typed, dict):
            continue
        rich = typed.get("rich_text") or []
        text = "".join(
            item.get("plain_text", "")
            for item in rich
            if isinstance(item, dict) and isinstance(item.get("plain_text"), str)
        )
        if text:
            parts.append(text)
    return "\n".join(parts)
