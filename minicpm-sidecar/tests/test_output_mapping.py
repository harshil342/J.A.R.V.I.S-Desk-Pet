import pytest
from gateway import jarvis_soundboard

def test_catalog_has_standard_clips():
    catalog = jarvis_soundboard.load_catalog(reload=True)
    assert len(catalog) == 52
    for c in catalog:
        assert "yacham" not in c.get("text", "").lower()

def test_system_status_mapped_to_output():
    clip = jarvis_soundboard.map_output_to_clip("System status: CPU 50%", tool_name="system_status")
    assert clip is not None
    assert "nominal" in clip["text"].lower()
    assert "sir" in clip["text"].lower()

def test_reminder_mapped_to_output():
    clip = jarvis_soundboard.map_output_to_clip("Reminder set for 10 minutes", tool_name="reminder")
    assert clip is not None
    assert "reminder" in clip["text"].lower()
    assert "sir" in clip["text"].lower()

def test_todo_mapped_to_output():
    clip = jarvis_soundboard.map_output_to_clip("Added to your to-do list", tool_name="todo_list")
    assert clip is not None
    assert "to-do" in clip["text"].lower() or "todo" in clip["text"].lower()
    assert "sir" in clip["text"].lower()

def test_spoken_sentence_extractor():
    text = "**Important:** Here is the information you requested. More details below."
    sentence = jarvis_soundboard._extract_spoken_sentence(text)
    assert sentence == "Important: Here is the information you requested."

def test_stop_audio():
    jarvis_soundboard.stop_audio()
