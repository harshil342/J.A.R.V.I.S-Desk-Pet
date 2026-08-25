"""Cross-chunk safety of the leaked tool-call markup scrubber.

The 1B model streams ``<function name="...">...`` one SSE delta at a time,
so a per-chunk regex never sees a whole tag. ``_TagScrubber`` must strip
the markup no matter where the chunk boundaries fall.
"""

from __future__ import annotations

from gateway.server import _RE_FUNCTION_TAG, _TagScrubber


def _fed(chunks: list[str]) -> str:
    scrub = _TagScrubber()
    out = "".join(scrub.feed(c) for c in chunks)
    return out + scrub.flush()


def test_whole_tag_in_one_chunk_is_stripped() -> None:
    assert (
        _fed(['<function name="get_weather"><param name="city">New York</param></function>'])
        == ""
    )


def test_tag_split_at_every_boundary_is_stripped() -> None:
    text = (
        'No problem, Sir. <function name="get_weather">'
        '<param name="city">New York</param></function>'
    )
    # one character per chunk — worst case
    assert _fed(list(text)) == "No problem, Sir. "


def test_opener_without_closer_swallows_rest_of_stream() -> None:
    chunks = ["Sure. ", '<function name="x">', "<param name=", 'a">v</param>', " tail"]
    assert _fed(chunks) == "Sure. "


def test_stray_closer_alone_is_stripped() -> None:
    assert _fed(["</function>", "hello"]) == "hello"


def test_stray_param_fragments_are_stripped() -> None:
    assert _fed(['<param name="city">x</param>', "ok"]) == "ok"


def test_tool_call_block_split_across_chunks_is_stripped() -> None:
    chunks = ['<tool_call>{"name": "get', '_weather"}</tool_call>', "done"]
    assert _fed(chunks) == "done"


def test_plain_angle_bracket_text_passes_through() -> None:
    assert _fed(["a < b and c > d"]) == "a < b and c > d"
    # a trailing partial opener is held, then released once harmless
    assert _fed(["look <fun", " times"]) == "look <fun times"


def test_flush_releases_held_fragment() -> None:
    scrub = _TagScrubber()
    assert scrub.feed("hello <") == "hello "
    assert scrub.flush() == "<"


def test_unterminated_block_is_dropped_not_leaked() -> None:
    scrub = _TagScrubber()
    assert scrub.feed('<function name="x">' + "y" * 9000) == ""
    assert scrub.flush() == ""


def test_master_regex_still_matches_bare_and_full_forms() -> None:
    assert _RE_FUNCTION_TAG.search('<function name="x"></function>')
    assert not _RE_FUNCTION_TAG.search("no markup here")
