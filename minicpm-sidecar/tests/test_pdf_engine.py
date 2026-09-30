import os
from pathlib import Path
from gateway import pdf_engine, tools, tool_registry


def test_synthesize_research_structure():
    data = pdf_engine.synthesize_research("Autonomous Vehicles", "Autonomous vehicles use sensors and AI to navigate.")
    assert "title" in data
    assert "executive_summary" in data
    assert "sections" in data
    assert len(data["sections"]) >= 3
    assert "comparison_table" in data
    assert "recommendations" in data


def test_compile_pdf_creates_file(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_engine, "OUTPUT_DIR", tmp_path)
    data = pdf_engine.synthesize_research("Robotics", "Robotics is an interdisciplinary branch of engineering.")
    filepath = pdf_engine.compile_pdf("Robotics", data)
    assert os.path.exists(filepath)
    assert filepath.endswith(".pdf")
    assert os.path.getsize(filepath) > 1000


def test_route_tools_pdf_briefing():
    routes = tools.route_tools("create an executive pdf report on cybersecurity")
    assert len(routes) == 1
    label, response = routes[0]
    assert label == "research_and_generate_pdf"
    assert "cybersecurity" in response.lower()
    assert "executive pdf briefing" in response.lower()


def test_tool_registry_contains_pdf_engine():
    reg = tool_registry.ToolRegistry()
    assert "research_and_generate_pdf" in reg._tools
    schema = reg.get_openai_schemas()
    names = [s["function"]["name"] for s in schema]
    assert "research_and_generate_pdf" in names
