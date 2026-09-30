"""Smoke test for the .tools venv: prove Scrapling really fetches and parses.

Imports are not the test. An adaptive-selector library that cannot pull a live
page and hand back selected elements is not useful, so this fetches for real
and asserts on the result.

Two API facts that cost time, recorded so they are not rediscovered:

  1. ``Selector(url)`` does NOT fetch. It wraps the string and hands back a
     68-byte stub document. Use ``Fetcher.get(url)`` (or StealthyFetcher /
     DynamicFetcher for pages that need JavaScript).
  2. There is no ``css_first()``. The matchers are ``css()`` / ``find()`` and
     the accessor is ``extract_first()``; ``.first`` gets the first node.

Run:  .tools\\Scripts\\python.exe scripts\\check-scrapling.py
"""
import scrapling
from scrapling.fetchers import Fetcher

URL = "https://pypi.org/project/scrapling/"

page = Fetcher.get(URL)

print("scrapling version :", scrapling.__version__)
print("http status       :", page.status)
print("final url         :", page.url)

title = page.css("h1::text").extract_first()
links = page.css('a[href*="/project/"]')
href = page.css('a[href*="/project/"]::attr(href)').extract_first()

print("h1                :", (title or "").strip()[:60])
print("package links     :", len(links))
print("first link href   :", href)

# The reason to use this library rather than regex: a working selector can be
# generated from a node and persisted, and nodes can be relocalised after a
# layout change. (relocate() takes the new element to match against - it is for
# "this page moved, find the equivalent node", not a no-argument re-check.)
if len(links):
    first = links.first
    print("first link text   :", first.text.strip()[:40])
    print("generated css     :", (first.generate_css_selector or "")[:60])
    print("stable xpath      :", (first.generate_xpath_selector or "")[:60])

assert page.status == 200, f"unexpected status {page.status}"
assert title and title.strip(), "no h1 parsed - fetch or parse failed"
assert len(links) > 0, "no package links parsed"
print("\nOK - live HTTP fetch, CSS selection, attribute extraction and relocalisation all work")
