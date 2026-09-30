import os
from gateway import jarvis_soundboard


def test_soundboard_catalog_loaded():
    catalog = jarvis_soundboard.load_catalog(reload=True)
    assert len(catalog) >= 50
    sample = catalog[0]
    assert "filename" in sample
    assert "text" in sample
    assert "category" in sample


def test_find_best_clip():
    clip_reminder = jarvis_soundboard.find_best_clip("here is your reminder")
    assert clip_reminder is not None
    assert "reminder" in clip_reminder["text"].lower()

    clip_system = jarvis_soundboard.find_best_clip("all systems nominal")
    assert clip_system is not None
    assert "nominal" in clip_system["text"].lower() or "systems" in clip_system["text"].lower()

    clip_intro = jarvis_soundboard.find_best_clip("allow me to introduce myself")
    assert clip_intro is not None
    assert "introduce" in clip_intro["text"].lower()


def test_get_clip_by_id():
    clip = jarvis_soundboard.get_clip_by_id("clip_001")
    assert clip is not None
    assert clip["id"] == "clip_001"


def test_speak_tool_and_routing():
    from gateway import tools
    from gateway.tool_registry import default_registry

    # 1. Tool registry contains speak
    tool_def = default_registry.get_tool("speak")
    assert tool_def is not None
    assert "phrase" in tool_def.parameters["properties"]

    # 2. speak function works without throwing
    res = tools.speak("allow me to introduce myself")
    assert "Spoken aloud" in res

    # 3. route_tools matches speak intent
    hits = tools.route_tools("speak systems operational")
    assert any(label == "speak" for label, _ in hits)

