# DeskPet — Agent Guidance

## graphify knowledge graph (always-on)

A queryable knowledge graph of this repo lives at `graphify-out/graph.json`
(9k+ nodes: clawd-on-desk Electron app + minicpm-sidecar Python gateway).
Before grepping raw files for architecture questions, prefer:

```
graphify query "<question>"        # scoped subgraph for a plain-language question
graphify path "A" "B"              # shortest connection between two symbols
graphify explain "<symbol>"        # one node + its neighbors, with file:line refs
graphify god-nodes                 # architectural hubs
```

- Read `graphify-out/GRAPH_REPORT.md` before broad architecture work.
- `graph.json` is committed-friendly; rebuild after code changes with
  `graphify update .` (local AST only, no API cost). Full rebuild:
  `graphify extract . --code-only`.
- `.graphifyignore` excludes vendored `llama.cpp/`, `models/`, media, and
  build dirs — keep it updated when adding large subtrees.
