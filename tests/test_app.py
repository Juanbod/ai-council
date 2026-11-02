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
    f.write_text("данные")
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


def test_parse_paths(tmp_path):
    a = tmp_path / "a b.txt"
    a.write_text("x")
    assert parse_paths(f'"{a}"') == [a]
    assert parse_paths("просто текст") is None
