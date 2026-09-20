import os
import pytest
from pathlib import Path

@pytest.fixture(autouse=True)
def isolate_deskpet_environment(tmp_path, monkeypatch):
    test_docs = tmp_path / "DeskPet"
    test_docs.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DESKPET_DOCS_DIR", str(test_docs))
    try:
        from gateway.semantic_memory import default_memory_store
        default_memory_store._path = test_docs / "memory_store.json"
        default_memory_store._items.clear()
    except Exception:
        pass
    yield test_docs
