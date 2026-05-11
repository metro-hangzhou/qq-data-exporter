from __future__ import annotations

from pathlib import Path


def test_preprocess_docs_exist_and_have_distinct_purposes() -> None:
    major = Path("major_AGENTs.md")
    process = Path("process_AGENTs.md")
    todo = Path("TODOs.preprocess.md")
    rag = Path("TODOs.rag.md")

    assert major.exists()
    assert process.exists()
    assert todo.exists()
    assert rag.exists()

    major_text = major.read_text(encoding="utf-8")
    process_text = process.read_text(encoding="utf-8")
    todo_text = todo.read_text(encoding="utf-8")
    rag_text = rag.read_text(encoding="utf-8")

    assert "repository-wide orchestration" in major_text
    assert "preprocessing subsystem" in process_text
    assert "Preprocess TODOs" in todo_text
    assert "RAG TODOs" in rag_text


def test_qq_data_process_does_not_import_cli_modules() -> None:
    process_root = Path("src/qq_data_process")
    for path in process_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "qq_data_cli" not in text
        assert "prompt_toolkit" not in text
