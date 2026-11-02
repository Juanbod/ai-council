import asyncio

import pytest

from council import app as app_mod
from council.agents import MockAgent
from council.app import CouncilApp, parse_paths


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "BASE", tmp_path)
    return CouncilApp(mock="stubborn,agree")


async def type_and_send(pilot, text):
    inp = pilot.app.query_one("#input")
    inp.value = text
    await pilot.press("enter")
    await pilot.pause()


async def wait_idle(pilot, sid, timeout=5):
    for _ in range(int(timeout / 0.05)):
        if sid not in pilot.app.running:
            return
        await asyncio.sleep(0.05)
    raise TimeoutError


@pytest.mark.asyncio
async def test_full_flow(app, tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("данные", encoding="utf-8")
    async with app.run_test(size=(140, 45)) as pilot:
        await type_and_send(pilot, "/new Архитектура | выбор БД")
        s1 = app.current
        assert s1.title == "Архитектура" and s1.topic == "выбор БД"

        await type_and_send(pilot, f'"{f}"')          # как при drag&drop
        assert s1.files() == ["doc.txt"]

        await type_and_send(pilot, "Postgres или SQLite?")
        await wait_idle(pilot, s1.id)
        await pilot.pause()
        verdict = [e for e in s1.events() if e["kind"] == "verdict"][-1]
        assert verdict["consensus"]
        assert s1.meta["status"] == "consensus"

        await type_and_send(pilot, "/new Второе")
        s2 = app.current
        assert s2.id != s1.id and len(app.sessions) == 2

        await pilot.press("ctrl+pagedown")
        await pilot.pause()
        assert app.current.id == s1.id
        assert len(app.query_one("#feed").children) > 5

        await type_and_send(pilot, "/export")
        assert (s1.root / "protocol.md").read_text(encoding="utf-8").count("Консенсус")


@pytest.mark.asyncio
async def test_chair_note_and_stop(app, monkeypatch):
    async with app.run_test(size=(140, 45)) as pilot:
        await type_and_send(pilot, "/new Спор")
        s = app.current
        monkeypatch.setattr(app_mod, "build_agents", lambda cfg, mock: [
            MockAgent("Claude", "stubborn", delay=0.4), MockAgent("Codex", "stubborn", delay=0.4)])
        await type_and_send(pilot, "вопрос")
        assert s.id in app.running
        await type_and_send(pilot, "учтите бюджет")
        assert any(e["kind"] == "chair" for e in s.events())
        await pilot.press("ctrl+s")
        await wait_idle(pilot, s.id)
        await pilot.pause()
        assert s.meta["status"] == "stopped"


@pytest.mark.asyncio
async def test_continue_after_stop_runs_again(app, monkeypatch):
    """Регрессия: /continue после /stop без перезапуска приложения сразу останавливался."""
    slow = {"delay": 0.4}
    monkeypatch.setattr(app_mod, "build_agents", lambda cfg, mock: [
        MockAgent("Claude", "stubborn", **slow), MockAgent("Codex", "agree", **slow)])
    async with app.run_test(size=(140, 45)) as pilot:
        await type_and_send(pilot, "/new Стоп-старт")
        s = app.current
        await type_and_send(pilot, "вопрос")
        await pilot.press("ctrl+s")
        await wait_idle(pilot, s.id)
        await pilot.pause()
        assert s.meta["status"] == "stopped"

        slow["delay"] = 0
        await type_and_send(pilot, "/continue 3")
        await wait_idle(pilot, s.id)
        await pilot.pause()
        verdict = [e for e in s.events() if e["kind"] == "verdict"][-1]
        assert not verdict["stopped"] and verdict["consensus"]
        assert s.meta["status"] == "consensus"


@pytest.mark.asyncio
async def test_rename_after_debate_keeps_status(app):
    async with app.run_test(size=(140, 45)) as pilot:
        await type_and_send(pilot, "/new Имя")
        s = app.current
        await type_and_send(pilot, "вопрос")
        await wait_idle(pilot, s.id)
        await pilot.pause()
        await type_and_send(pilot, "/rename Другое имя")
        meta = app_mod.Session(s.root).meta  # перечитано с диска
        assert meta["status"] == "consensus" and meta["title"] == "Другое имя"


def test_parse_paths(tmp_path):
    a = tmp_path / "a b.txt"
    a.write_text("x")
    assert parse_paths(f'"{a}"') == [a]
    assert parse_paths("просто текст") is None


@pytest.mark.asyncio
async def test_eggs_shown_in_feed_and_export(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "BASE", tmp_path)
    app = CouncilApp(mock="hothead,agree")
    async with app.run_test(size=(140, 45)) as pilot:
        await type_and_send(pilot, "/new Рада")
        s = app.current
        await type_and_send(pilot, "вопрос")
        await wait_idle(pilot, s.id)
        await pilot.pause()
        assert s.meta["status"] == "consensus"
        await type_and_send(pilot, "/export")
        protocol = (s.root / "protocol.md").read_text(encoding="utf-8")
        assert "Бросает яйцо" in protocol and "Брошено яиц: Claude — 3, Codex — 0" in protocol
