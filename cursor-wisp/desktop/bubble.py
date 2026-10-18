"""The @bubble wake word shared by the desktop pet and the submit hook."""

import re
import unicodedata

# The whole whitespace-delimited token, any case. @bubblegum and foo@bubble do not count.
# The @ is optional: a Cursor mention chip can expose the label "bubble" without it.
_TOKEN = re.compile(r"(?<!\S)@?bubble(?!\S)", re.IGNORECASE)


def _normalize(text):
    """Drop characters a mention chip inserts around the label."""

    kept = []
    for char in text:
        if char == "\ufffc" or unicodedata.category(char) == "Cf":
            continue
        kept.append(char)
    return "".join(kept)


def bubble_question(text):
    """Question text with the wake word removed, or None when the pet was not asked."""

    if not isinstance(text, str) or not text:
        return None
    cleaned = _normalize(text)
    if _TOKEN.search(cleaned) is None:
        return None
    question = " ".join(_TOKEN.sub(" ", cleaned).split())
    return question or None


def replace_bubble_sentence(text, answer):
    """The draft with the @bubble token swapped for the answer.

    The rest of the sentence stays. None when the wake word is missing or the
    answer is empty. The draft is not changed in place.
    """

    if not isinstance(text, str) or not text or not isinstance(answer, str):
        return None
    replacement = " ".join(answer.split())
    if not replacement:
        return None
    ranges = bubble_purple_ranges(text)
    if not ranges:
        return None
    start, end = ranges[0]
    while end < len(text) and _is_format(text[end]):
        end += 1
    return text[:start] + replacement + text[end:]


# Weave purple. Only the @bubble token uses it; the rest of the prompt stays as it was.
PURPLE = "#7C3AED"
_WORD = "bubble"


def _is_format(char):
    return char == "\ufffc" or unicodedata.category(char) == "Cf"


def bubble_purple_ranges(text):
    """Half-open ranges of @bubble tokens, including the @, in any case.

    Format characters a mention chip inserts inside the token stay inside the
    range. The bare word bubble, @bubblegum, foo@bubble, and every other
    @ mention are left alone. The text itself is not changed.
    """

    if not isinstance(text, str) or not text:
        return []
    ranges = []
    index = 0
    length = len(text)
    word = len(_WORD)
    while index < length:
        if text[index] != "@" or (index > 0 and not text[index - 1].isspace()):
            index += 1
            continue
        cursor = index + 1
        while cursor < length and _is_format(text[cursor]):
            cursor += 1
        end = cursor + word
        if end <= length and text[cursor:end].casefold() == _WORD:
            after = end
            while after < length and _is_format(text[after]):
                after += 1
            if after == length or text[after].isspace():
                ranges.append((index, end))
                index = after
                continue
        index += 1
    return ranges
