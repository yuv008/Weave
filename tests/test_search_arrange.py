"""Search ranks an item by its best chunk, folds duplicate text into the newest copy, and breaks near-ties by authority."""

from agents.tools.search import arrange, best_chunks

HASH_A = "a" * 64
HASH_B = "b" * 64


def test_an_item_with_many_chunks_appears_once_with_its_best_chunk():
    order, text = best_chunks([("doc", 0.9, "best part"), ("msg", 0.8, "hello"), ("doc", 0.5, "weaker part")])

    assert order == ["doc", "msg"]
    assert text == {"doc": "best part", "msg": "hello"}


def test_duplicates_collapse_into_the_newest_copy():
    ranked = [("mail-1", 0.050), ("mail-2", 0.040), ("doc", 0.030)]
    meta = {
        "mail-1": (HASH_A, "2026-10-01T00:00:00+00:00", 2),
        "mail-2": (HASH_A, "2026-10-20T00:00:00+00:00", 2),
        "doc": (HASH_B, "2026-09-01T00:00:00+00:00", 3),
    }

    hits = arrange(ranked, meta, {})

    assert [hit["item"] for hit in hits] == ["mail-2", "doc"]
    assert hits[0]["similar"] == ["mail-1"]
    assert hits[0]["score"] == 0.050


def test_same_text_without_a_hash_still_collapses():
    ranked = [("a", 0.02), ("b", 0.01)]
    meta = {"a": ("", "2026-01-01", 1), "b": ("", "2026-02-01", 1)}

    hits = arrange(ranked, meta, {"a": "Weekly  digest", "b": "weekly digest"})

    assert [hit["item"] for hit in hits] == ["b"] and hits[0]["similar"] == ["a"]


def test_a_near_tie_goes_to_the_more_authoritative_then_newer_source():
    # Equal fused scores: the same rank in the keyword list and in the vector list.
    ranked = [("slack", 0.016393), ("notion", 0.016393), ("drive-old", 0.016393), ("next", 0.016129)]
    meta = {
        "slack": ("1" * 64, "2026-10-28", 1),
        "notion": ("2" * 64, "2026-10-01", 3),
        "drive-old": ("3" * 64, "2026-01-01", 3),
        "next": ("4" * 64, "2026-10-29", 3),
    }

    hits = arrange(ranked, meta, {})

    # A lower-ranked hit stays below, however authoritative or new it is.
    assert [hit["item"] for hit in hits] == ["notion", "drive-old", "slack", "next"]


def test_a_snippet_carries_a_whole_chunk():
    from agents.tools.search import SNIPPET_CHARS
    from app.ingest.chunks import CHUNK_CHARS

    assert SNIPPET_CHARS >= CHUNK_CHARS
