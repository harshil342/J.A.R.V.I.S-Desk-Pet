"""Cross-chunk safe stripper for leaked tool-call markup.

Extracted verbatim from server.py so the stream filter can be tested
and evolved without touching the FastAPI app. gateway.server re-exports
``_TagScrubber`` / ``_RE_FUNCTION_TAG`` for compatibility.
"""

from __future__ import annotations

import re

from .log_setup import get_logger

# 1B models parrot what they see. The router's [label] tags stay in the logs
# but are stripped before injection; this net catches any tag that still
# leaks into the reply stream.
_RE_FUNCTION_TAG = re.compile(
    r"<function\b[^>]*>(?:.*?</function\s*>|</function\s*>)?"
    r"|</function\s*>"
    r"|<param\b[^>]*>(?:.*?</param\s*>|</param\s*>)?"
    r"|</param\s*>"
    r"|<tool_call\s*>[\s\S]*?</tool_call\s*>"
    r"|</tool_call\s*>",
    re.IGNORECASE | re.DOTALL,
)
_RE_FN_OPENER = re.compile(r"<function\b[^>]*>", re.IGNORECASE)
_RE_FN_CLOSER = re.compile(r"</function\s*>", re.IGNORECASE)
_RE_LEAK_CLOSER = re.compile(r"</(?:function|tool_call)\s*>", re.IGNORECASE)
# Openers a partial trailing fragment might still grow into. Used to decide
# how much text must be held back across chunk boundaries.
_TAG_OPENERS = (
    "<function",
    "</function",
    "<param",
    "</param",
    "<tool_call",
    "</tool_call",
)


class _TagScrubber:
    """Cross-chunk safe stripper for leaked tool-call markup.

    ``_RE_FUNCTION_TAG`` alone cannot do the job on a token stream: the
    model emits ``<function name="get_weather">`` one delta at a time, so a
    per-chunk ``sub()`` never sees a whole tag. This filter keeps a tiny
    trailing buffer that could still grow into a tag opener, and once a
    ``<function ...>`` opener is seen it swallows everything until the
    matching ``</function>`` (or the stream ends), so nested ``<param>``
    payloads never reach the bubble either.
    """

    _SWALLOW_CAP = 8192  # runaway guard: unterminated block, drop it all

    def __init__(self) -> None:
        self._pending = ""
        self._inside = False  # saw a bare <function ...> opener; swallowing

    def feed(self, piece: str) -> str:
        buf = self._pending + piece
        self._pending = ""
        out: list[str] = []
        while True:
            if self._inside:
                m = _RE_LEAK_CLOSER.search(buf)
                if m:
                    buf = buf[m.end():]
                    self._inside = False
                    continue
                if len(buf) > self._SWALLOW_CAP:
                    get_logger().warning(
                        "tag scrubber: unterminated <function> block "
                        "dropped (%d buffered chars)",
                        len(buf),
                    )
                    buf = ""
                    self._inside = False
                    continue
                self._pending = buf  # stay swallowed, emit nothing yet
                break
            m = _RE_FUNCTION_TAG.search(buf)
            if m:
                out.append(buf[: m.start()])
                matched = m.group(0)
                buf = buf[m.end():]
                self._inside = matched[:9].lower() == "<function" and (
                    _RE_FN_CLOSER.search(matched) is None
                )
                continue
            # No complete tag left. Hold back a trailing fragment that could
            # still grow into something the net catches: either a partial
            # opener ("</fun") or an opener whose ">" has not arrived yet
            # (attributes span several deltas).
            hold_from = -1
            idx = buf.rfind("<")
            if idx >= 0:
                frag = buf[idx:].lower()
                for op in _TAG_OPENERS:
                    if frag.startswith(op):
                        # Opener started; hold until its ">" (or closer for
                        # block forms) arrives via the regex above.
                        if op == "<tool_call" or ">" not in frag[len(op):]:
                            hold_from = idx
                            if op == "<tool_call":
                                # Block form only becomes matchable once its
                                # closer arrives; swallow until then.
                                self._inside = True
                        break
                    if op.startswith(frag):
                        hold_from = idx
                        break
            if hold_from < 0:
                out.append(buf)
            else:
                out.append(buf[:hold_from])
                self._pending = buf[hold_from:]
            break
        return "".join(out)

    def flush(self) -> str:
        buf, self._pending = self._pending, ""
        if self._inside:
            get_logger().warning(
                "tag scrubber: stream ended inside a <function> block; "
                "dropping %d buffered chars",
                len(buf),
            )
            self._inside = False
            return ""
        return _RE_FUNCTION_TAG.sub("", buf)
