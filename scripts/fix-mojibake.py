"""Reverse double-encoded (mojibake) runs in a source file.

A run of cp1252-zone characters is a UTF-8 sequence that was decoded as
cp1252 and re-encoded as UTF-8. Reversing it per-run keeps legitimate
accented text intact: a run that is not a valid UTF-8 reversal is left
alone.
"""
import ast
import pathlib
import re
import sys

ZONE = re.compile(
    "[\u00a0-\u00ff\u20ac\u2018\u2019\u201a\u201c\u201d\u201e"
    "\u2020-\u2026\u2030-\u203e\u20a0-\u20bf]+"
)

MARKERS = ("\u00e2\u20ac", "\u00e2\u201d\u20ac", "\ufffd")


def reverse_run(match):
    run = match.group(0)
    try:
        return run.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return run


def main(path):
    p = pathlib.Path(path)
    text = p.read_text("utf-8")
    before = sum(text.count(m) for m in MARKERS)
    new = ZONE.sub(reverse_run, text)
    after = sum(new.count(m) for m in MARKERS)
    if new != text:
        p.write_text(new, encoding="utf-8", newline="")
    if path.endswith(".py"):
        ast.parse(new)
    print(f"{path}")
    print(f"  mojibake markers before: {before}")
    print(f"  mojibake markers after:  {after}")
    return 0 if after == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
