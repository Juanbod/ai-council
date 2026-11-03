"""Хранилище заседаний: протокол переживает аварийную запись, вложения не копируют сами себя."""
import pytest

from council.store import Store


@pytest.fixture
def session(tmp_path):
    return Store(tmp_path / "sessions").create("Тест")


def test_append_after_truncated_line_keeps_new_event(session):
    session.append({"kind": "question", "text": "первый"})
    log = session.root / "events.jsonl"
    with open(log, "ab") as f:  # авария посреди записи: строка оборвана внутри кириллической буквы
        f.write('{"kind": "chair", "text": "об'.encode("utf-8")[:-1])
    session.append({"kind": "question", "text": "второй"})
    assert [e["text"] for e in session.events()] == ["первый", "второй"]


def test_attach_project_folder_skips_sessions(tmp_path):
    project = tmp_path / "project"
    (project / "code").mkdir(parents=True)
    (project / "code" / "a.py").write_text("x = 1", encoding="utf-8")
    s = Store(project / "sessions").create("Ревью")  # заседания лежат внутри прикрепляемой папки
    dest = s.attach(project)
    assert (dest / "code" / "a.py").exists()
    assert not (dest / "sessions").exists()


def test_attach_sessions_folder_is_refused(session):
    with pytest.raises(ValueError):
        session.attach(session.root)
    with pytest.raises(ValueError):
        session.attach(session.root.parent)
