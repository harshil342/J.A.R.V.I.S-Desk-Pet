"""Semantic Episodic Memory Store for DeskPet Jarvis.

Provides long-term categorical memory with hybrid semantic search (TF-IDF &
character/word n-gram cosine similarity) and automatic persistence.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .log_setup import get_logger

log = get_logger("semantic_memory")


@dataclass
class MemoryItem:
    id: str
    text: str
    category: str = "general"
    created_at: str = field(default_factory=lambda: datetime.datetime.now().isoformat())
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> MemoryItem:
        return cls(
            id=data.get("id", str(uuid.uuid4())[:8]),
            text=data.get("text", ""),
            category=data.get("category", "general"),
            created_at=data.get("created_at", datetime.datetime.now().isoformat()),
            tags=data.get("tags", []),
        )


def _tokenize(text: str) -> List[str]:
    """Tokenize text into lowercase alpha-numeric words."""
    return re.findall(r"\b[a-z0-9_]+\b", (text or "").lower())


def _get_ngrams(tokens: List[str], n: int = 2) -> Set[str]:
    """Extract character or word n-grams for semantic fuzzy overlap."""
    ngrams = set()
    for tok in tokens:
        # 3-char sub-grams for typo & stem tolerance
        if len(tok) >= 3:
            for i in range(len(tok) - 2):
                ngrams.add(tok[i : i + 3])
        else:
            ngrams.add(tok)
    # Word pairs
    for i in range(len(tokens) - n + 1):
        ngrams.add(" ".join(tokens[i : i + n]))
    return ngrams


def _compute_similarity(query_tokens: List[str], query_ngrams: Set[str], doc_tokens: List[str], doc_ngrams: Set[str]) -> float:
    """Compute hybrid semantic cosine similarity between query and memory item."""
    if not query_tokens or not doc_tokens:
        return 0.0

    # 1. Word token Jaccard & overlap
    q_set = set(query_tokens)
    d_set = set(doc_tokens)
    common_words = q_set & d_set
    word_score = len(common_words) / math.sqrt(len(q_set) * len(d_set))

    # 2. N-gram fuzzy character similarity
    if query_ngrams and doc_ngrams:
        common_ngrams = query_ngrams & doc_ngrams
        ngram_score = len(common_ngrams) / math.sqrt(len(query_ngrams) * len(doc_ngrams))
    else:
        ngram_score = 0.0

    # Combined score (weighted toward exact word overlap with n-gram fuzzy backing)
    return float(0.65 * word_score + 0.35 * ngram_score)


class SemanticMemoryStore:
    """Categorical semantic memory store with local JSON persistence."""

    def __init__(self, storage_path: Optional[Path] = None):
        self._items: Dict[str, MemoryItem] = {}
        self._path = storage_path or self._resolve_default_path()
        self._load()

    def _resolve_default_path(self) -> Path:
        docs_env = os.environ.get("DESKPET_DOCS_DIR")
        if docs_env:
            base = Path(docs_env).expanduser().resolve()
        else:
            base = Path.home() / "Documents" / "DeskPet"
        return base / "memory_store.json"

    def _load(self) -> None:
        if not self._path.exists():
            # Attempt to migrate legacy memory.txt if present
            legacy_txt = self._path.parent / "memory.txt"
            if legacy_txt.exists():
                self._migrate_legacy_txt(legacy_txt)
            return

        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, list):
                for item_dict in data:
                    item = MemoryItem.from_dict(item_dict)
                    self._items[item.id] = item
            log.info("Loaded %d memory item(s) from %s", len(self._items), self._path)
        except Exception as exc:
            log.warning("Failed to load memory store from %s: %s", self._path, exc)

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            serializable = [item.to_dict() for item in self._items.values()]
            self._path.write_text(json.dumps(serializable, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            log.warning("Failed to persist memory store: %s", exc)

    def _migrate_legacy_txt(self, legacy_file: Path) -> None:
        try:
            lines = legacy_file.read_text(encoding="utf-8").splitlines()
            for line in lines:
                clean = line.strip()
                if clean:
                    self.add(clean, category="migrated")
            log.info("Migrated %d legacy memories into semantic store", len(lines))
        except Exception as exc:
            log.warning("Legacy memory migration failed: %s", exc)

    def add(self, text: str, category: str = "general", tags: Optional[List[str]] = None) -> MemoryItem:
        """Add or update a fact in the semantic memory store."""
        clean_text = (text or "").strip()
        if not clean_text:
            raise ValueError("Memory text cannot be empty.")

        # Check for near-duplicates
        for item in self._items.values():
            if item.text.lower() == clean_text.lower():
                item.category = category
                item.tags = tags or item.tags
                item.created_at = datetime.datetime.now().isoformat()
                self._save()
                return item

        item_id = str(uuid.uuid4())[:8]
        item = MemoryItem(
            id=item_id,
            text=clean_text,
            category=category or "general",
            tags=tags or [],
        )
        self._items[item_id] = item
        self._save()
        return item

    def delete(self, memory_id: str) -> bool:
        """Delete a memory item by ID."""
        if memory_id in self._items:
            del self._items[memory_id]
            self._save()
            return True
        return False

    def list_all(self, category: Optional[str] = None) -> List[MemoryItem]:
        """List all stored memory items, optionally filtered by category."""
        items = list(self._items.values())
        if category:
            items = [i for i in items if i.category.lower() == category.lower()]
        return sorted(items, key=lambda x: x.created_at, reverse=True)

    def search(self, query: str, limit: int = 5, min_score: float = 0.12) -> List[Tuple[MemoryItem, float]]:
        """Search memory using hybrid semantic matching."""
        query_clean = (query or "").strip().lower()
        if not query_clean or not self._items:
            return []

        q_tokens = _tokenize(query_clean)
        q_ngrams = _get_ngrams(q_tokens)

        scored: List[Tuple[MemoryItem, float]] = []
        for item in self._items.values():
            d_tokens = _tokenize(item.text)
            d_ngrams = _get_ngrams(d_tokens)

            # Substring exact boost
            score = _compute_similarity(q_tokens, q_ngrams, d_tokens, d_ngrams)
            if query_clean in item.text.lower():
                score = max(score, 0.85)

            if score >= min_score:
                scored.append((item, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    def clear(self) -> None:
        """Clear all memory items."""
        self._items.clear()
        self._save()


# Global default semantic memory instance
default_memory_store = SemanticMemoryStore()


def remember_fact(fact: str) -> str:
    """Tool wrapper: store a fact in semantic memory."""
    clean = (fact or "").strip()
    if not clean:
        return "Nothing to remember."
    item = default_memory_store.add(clean)
    return f"remembered: {item.text}"


def recall_fact(query: str) -> str:
    """Tool wrapper: recall facts matching a semantic query."""
    clean = (query or "").strip()
    if not clean:
        items = default_memory_store.list_all()
        if not items:
            return "No memories stored yet, sir."
        summary = "\n".join(f"• {i.text}" for i in items[:8])
        return f"Here is what I remember:\n{summary}"

    matches = default_memory_store.search(clean, limit=4)
    if not matches:
        return f"I have no memory matching '{clean}', sir."

    lines = [f"• {item.text}" for item, score in matches]
    return "Here is what I recall:\n" + "\n".join(lines)
