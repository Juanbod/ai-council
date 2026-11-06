import asyncio
import threading

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


@pytest.mark.asyncio
async def test_new_session_button_opens_dialog(app):
    """Регрессия: кнопка «+ Новое заседание» не реагировала, работал только Ctrl+N."""
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.click("#new")
        await pilot.pause()
        assert isinstance(app.screen, app_mod.NewSessionScreen)
        app.screen.query_one("#title").value = "Через кнопку"
        await pilot.click("#ok")
        await pilot.pause()
        assert app.current and app.current.title == "Через кнопку"


@pytest.mark.asyncio
async def test_egg_throw_animation_plays_for_live_eggs(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "BASE", tmp_path)
    monkeypatch.setattr(app_mod.EggThrow, "STEP", 0.01)
    app = CouncilApp(mock="hothead,agree")
    async with app.run_test(size=(140, 45)) as pilot:
        await type_and_send(pilot, "/new Рада")
        s = app.current
        await type_and_send(pilot, "вопрос")
        await wait_idle(pilot, s.id)
        await asyncio.sleep(0.5)
        throws = list(app.query(app_mod.EggThrow))
        assert len(throws) == 3 and all(t.done for t in throws)
        assert "шмяк" in str(throws[0].render())

        await type_and_send(pilot, "/new Другое")
        await pilot.press("ctrl+pagedown")  # повторное открытие: история без анимации
        await pilot.pause()
        assert app.current.id == s.id and not list(app.query(app_mod.EggThrow))


@pytest.mark.asyncio
async def test_usage_indicator_and_daily_limit(app):
    async with app.run_test(size=(140, 45)) as pilot:
        await type_and_send(pilot, "/new Расход")
        s = app.current
        await type_and_send(pilot, "вопрос")
        await wait_idle(pilot, s.id)
        await pilot.pause()
        line = str(app.query_one("#usage").render())
        assert "Claude" in line and "Codex" in line and "ток." in line

        app.cfg["limits"] = {"Claude": {"daily_tokens": 1}}  # уже превышен
        await pilot.pause()
        app.update_usage()
        assert "лимит исчерпан" in str(app.query_one("#usage").render())
        await type_and_send(pilot, "второй вопрос")
        await wait_idle(pilot, s.id)
        await pilot.pause()
        assert s.meta["status"] == "stopped"
        assert any(e["kind"] == "system" and "лимит" in e["text"] for e in s.events())

        await type_and_send(pilot, "/usage")
        assert "Расход ИИ" in app.query(app_mod.Markdown).last().source


def test_old_config_gets_limits_section(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "BASE", tmp_path)
    (tmp_path / "config.json").write_text('{"max_rounds": 7}', encoding="utf-8")
    cfg = app_mod.load_config()
    assert cfg["max_rounds"] == 7 and "limits" in cfg
    saved = (tmp_path / "config.json").read_text(encoding="utf-8")
    assert '"limits"' in saved and '"max_rounds": 7' in saved


def test_parse_bribe_amount():
    from council.app import parse_amount
    assert parse_amount("500K") == ("daily_tokens", 500_000)
    assert parse_amount("1,5м") == ("daily_tokens", 1_500_000)
    assert parse_amount("20000") == ("daily_tokens", 20_000)
    assert parse_amount("$2") == ("daily_cost_usd", 2) == parse_amount("2$")
    assert parse_amount("много") is None and parse_amount("-5") is None and parse_amount("") is None


def test_bribes_count_only_today():
    from council.store import bribe_totals
    events = [{"kind": "bribe", "agent": "Claude", "limit": "daily_tokens", "amount": 100, "ts": 5},
              {"kind": "bribe", "agent": "Claude", "limit": "daily_tokens", "amount": 30, "ts": 20},
              {"kind": "bribe", "agent": "Claude", "limit": "daily_cost_usd", "amount": 2, "ts": 20}]
    assert bribe_totals(events, since=10) == {"Claude": {"daily_tokens": 30, "daily_cost_usd": 2}}


@pytest.mark.asyncio
async def test_bribe_raises_limit_and_debate_continues(app):
    async with app.run_test(size=(140, 45)) as pilot:
        await type_and_send(pilot, "/new Взятка")
        s = app.current
        app.cfg["limits"] = {"Claude": {"daily_tokens": 1}, "Codex": {"daily_tokens": 0}}
        await type_and_send(pilot, "вопрос")
        await wait_idle(pilot, s.id)
        await pilot.pause()
        assert s.meta["status"] == "stopped"  # лимит исчерпан ещё до первого раунда

        await type_and_send(pilot, "/взятка кодекс 1M")  # у Codex лимита нет — взятка не принимается
        assert not any(e["kind"] == "bribe" for e in s.events())

        await type_and_send(pilot, "/взятка клод")  # без суммы: +50 % от лимита в 1 токен
        await type_and_send(pilot, "/bribe Claude 10M")
        bribes = [e for e in s.events() if e["kind"] == "bribe"]
        assert [b["amount"] for b in bribes] == [0.5, 10_000_000]
        assert bribes[-1]["total"] == 10_000_001.5
        assert "исчерпан" not in str(app.query_one("#usage").render())

        await type_and_send(pilot, "/continue 3")
        await wait_idle(pilot, s.id)
        await pilot.pause()
        assert s.meta["status"] == "consensus"
        await type_and_send(pilot, "/export")
        assert "Взятка Claude" in (s.root / "protocol.md").read_text(encoding="utf-8")


def test_progress_line_states():
    from council.app import progress_line
    states = {"Claude": {"state": "thinking", "round": 1, "since": 100.0, "seconds": 0},
              "Codex": {"state": "done", "round": 1, "since": 90.0, "seconds": 8.4}}
    line = progress_line(["Claude", "Codex"], states, now=112.0)
    assert "Раунд 1" in line and "Claude[/b] думает… 12 с" in line and "Codex[/b] ответил за 8 с" in line
    assert "ждёт" in progress_line(["Claude", "Codex"], {"Claude": states["Claude"]}, now=100.0)


def test_progress_line_shows_current_action_and_step_count():
    from council.app import progress_line
    states = {"Claude": {"state": "thinking", "round": 0, "since": 100.0, "seconds": 0,
                         "activity": "читает proj/a.py", "steps": 1},
              "Codex": {"state": "done", "round": 0, "since": 90.0, "seconds": 8.4, "steps": 3,
                        "activity": "выполняет Get-Content a.py"}}
    line = progress_line(["Claude", "Codex"], states, now=112.0)
    assert "думает… 12 с [dim]· читает proj/a.py" in line
    assert "ответил за 8 с [dim](3 действия)" in line and "Get-Content" not in line
    states["Codex"]["steps"] = 1
    assert "(1 действие)" in progress_line(["Claude", "Codex"], states, now=112.0)



def test_late_activity_is_dropped(app):
    # ревью совета PR #7: событие чужой попытки (повтор, «Стоп» → «Продолжить» в том же раунде),
    # прошлого раунда или пришедшее после ответа не засчитывается
    def ev(kind, **kw):
        app._on_progress("sid", {"kind": kind, "agent": "Claude", "round": 1, "call": "c1", **kw})

    def state():
        return app.progress["sid"]["Claude"]

    ev("progress", state="thinking", seconds=0)
    ev("activity", text="читает a.py")
    assert state()["steps"] == 1 and state()["activity"] == "читает a.py"
    ev("progress", state="retry", call="c2", seconds=0)
    ev("activity", text="старое", call="c1")  # от прошлой попытки того же раунда
    ev("activity", text="из прошлого раунда", round=0, call="c2")  # пока новая попытка идёт
    ev("activity", text="читает b.py", call="c2")
    assert state()["steps"] == 2 and state()["activity"] == "читает b.py"
    ev("progress", state="done", call="c2", seconds=3)
    ev("activity", text="после ответа", call="c2")
    assert state()["steps"] == 2 and "activity" not in state()  # у ответившего — только число шагов
    ev("progress", state="thinking", call="c3", seconds=0)  # тот же раунд снова после «Продолжить»
    ev("activity", text="опоздавшее от c2", call="c2")
    assert state().get("activity") is None and state()["steps"] == 2


@pytest.mark.asyncio
async def test_progress_delivery_does_not_block_the_reader(app):
    # поток чтения вывода CLI не ждёт интерфейс: emit индикатора возвращается сразу, даже когда UI занят
    async with app.run_test(size=(140, 45)) as pilot:
        await type_and_send(pilot, "/new Неблокирующая доставка")
        s = app.current
        emit = app._emitter(s)
        done = threading.Event()

        def worker():
            for i in range(50):
                emit({"kind": "activity", "agent": "Claude", "round": 0, "call": "c", "text": f"шаг {i}"})
            done.set()

        t = threading.Thread(target=worker, daemon=True)  # при регрессии поток повиснет — не держим им pytest
        t.start()
        # UI-цикл в это время стоит на done.wait — call_from_thread завис бы до его освобождения
        assert done.wait(1), "emit индикатора ждёт интерфейс"
        t.join(1)
        await pilot.pause()
        assert not any(e["kind"] == "activity" for e in s.events())


@pytest.mark.asyncio
async def test_progress_bar_shows_while_running_and_is_not_saved(app, monkeypatch):
    monkeypatch.setattr(app_mod, "build_agents", lambda cfg, mock: [
        MockAgent("Claude", "stubborn", delay=0.6), MockAgent("Codex", "agree", delay=0.1)])
    async with app.run_test(size=(140, 45)) as pilot:
        await type_and_send(pilot, "/new Индикатор")
        s = app.current
        await type_and_send(pilot, "вопрос")
        await asyncio.sleep(0.35)
        await pilot.pause()
        bar = app.query_one("#progress")
        text = str(bar.render())
        assert bar.display and "Claude" in text and "думает" in text and "ответил" in text
        assert "· пишет ответ" in text and "(1 действие)" in text  # шаги внутри вызова видны
        await wait_idle(pilot, s.id)
        await pilot.pause()
        assert not bar.display
        assert not any(e["kind"] in ("progress", "activity") for e in s.events())


@pytest.mark.asyncio
async def test_stop_button_stops_debate(app, monkeypatch):
    monkeypatch.setattr(app_mod, "build_agents", lambda cfg, mock: [
        MockAgent("Claude", "stubborn", delay=2), MockAgent("Codex", "stubborn", delay=2)])
    async with app.run_test(size=(140, 45)) as pilot:
        await type_and_send(pilot, "/new Стоп")
        s = app.current
        stop = app.query_one("#stop")
        assert not stop.display
        await type_and_send(pilot, "вопрос")
        assert stop.display
        t = asyncio.get_running_loop().time()
        await pilot.click("#stop")
        await wait_idle(pilot, s.id)
        assert asyncio.get_running_loop().time() - t < 1.5  # прервано, а не дождались вызовов по 2 с
        await pilot.pause()
        assert s.meta["status"] == "stopped" and not stop.display


@pytest.mark.asyncio
async def test_web_command_toggles_session(app):
    async with app.run_test(size=(140, 45)) as pilot:
        await type_and_send(pilot, "/new Веб")
        s = app.current
        await type_and_send(pilot, "/web on")
        assert s.meta["web"] is True and "веб включён" in str(app.query_one("#header").render())
        await type_and_send(pilot, "/web off")
        assert s.meta["web"] is False and "веб" not in str(app.query_one("#header").render())
        assert sum("Интернет" in e.get("text", "") for e in s.events() if e["kind"] == "system") == 2


@pytest.mark.asyncio
async def test_copy_button_copies_message_text(app, monkeypatch):
    copied = []
    monkeypatch.setattr(app_mod, "windows_clipboard", copied.append)  # не трогаем настоящий буфер
    async with app.run_test(size=(140, 45)) as pilot:
        await type_and_send(pilot, "/new Копирование")
        s = app.current
        await type_and_send(pilot, "Какой [b]формат[/b] выбрать?")
        await wait_idle(pilot, s.id)
        await pilot.pause()
        messages = list(app.query(app_mod.Message))
        kinds = [m.classes for m in messages]
        assert {"chair"} <= kinds[0] and {"verdict-ok"} <= kinds[-1]
        assert sum("reply" in k for k in kinds) >= 2

        async def copy(message):
            # immediate: без него прокрутка ждёт следующего обновления и клик изредка мимо экрана
            message.scroll_visible(animate=False, immediate=True)
            await pilot.pause()
            await pilot.pause()
            await pilot.click(message.query_one(".copy"))
            await pilot.pause()
            return app.clipboard

        # разметка Rich в тексте председателя копируется как есть, а не съедается
        assert await copy(messages[0]) == "Какой [b]формат[/b] выбрать?"
        verdict = [e for e in s.events() if e["kind"] == "verdict"][-1]
        assert await copy(messages[-1]) == verdict["answer"]  # из вердикта — только итоговый ответ
        reply = next(m for m in messages if "reply" in m.classes)
        assert (await copy(reply)).startswith("**Claude** (")  # с ролью: Скептик или Конструктор
        if app_mod.os.name == "nt":
            assert copied == [messages[0].text, verdict["answer"], reply.text]


@pytest.mark.asyncio
async def test_app_debate_has_roles_and_critique_round(app):
    async with app.run_test(size=(140, 45)) as pilot:
        await type_and_send(pilot, "/new Без поддакивания")
        s = app.current
        await type_and_send(pilot, "Монолит или микросервисы?")
        await wait_idle(pilot, s.id)
        await pilot.pause()
        events = s.events()
        verdict = [e for e in events if e["kind"] == "verdict"][-1]
        assert verdict["consensus"] and verdict["rounds"] == 2  # в раунде 1 — только критика
        assert sorted(verdict["roles"].values()) == ["Конструктор", "Скептик"]
        feed = "\n".join(m.text for m in app.query(app_mod.Message))
        assert "(Скептик)" in feed and "(Конструктор)" in feed and "**Убедило:**" in feed
        await type_and_send(pilot, "/export")
        assert "*Убедило:*" in (s.root / "protocol.md").read_text(encoding="utf-8")
