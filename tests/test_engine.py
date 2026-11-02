import json
import threading
import time

import pytest

from council.agents import MockAgent, extract_json
from council.engine import Debate, proposal_id
from council.store import Session, Store


@pytest.fixture
def session(tmp_path):
    return Store(tmp_path).create("Тест", "тема")


def run(session, a, b, rounds=4):
    events = []
    def emit(e):
        events.append(session.append(e))
    session.append({"kind": "question", "text": "вопрос?"})
    d = Debate([a, b], session, "вопрос?", lambda e: emit(e), max_rounds=rounds)
    return d, d.run(), events


def test_agree_reaches_consensus_on_same_id(session):
    d, v, _ = run(session, MockAgent("Claude", "stubborn"), MockAgent("Codex", "agree"))
    assert v["consensus"] and v["answer"] == "Решение от Claude"
    assert v["rounds"] == 1


def test_two_accepts_of_different_ids_are_not_consensus(session):
    # оба agree: каждый принимает вариант другого -> разные id -> консенсуса нет
    d, v, _ = run(session, MockAgent("Claude", "agree"), MockAgent("Codex", "agree"), rounds=3)
    assert not v["consensus"]
    assert len(d.candidates) == 2


def test_stubborn_pair_hits_round_limit(session):
    d, v, _ = run(session, MockAgent("Claude", "stubborn"), MockAgent("Codex", "stubborn"), rounds=3)
    assert not v["consensus"] and v["rounds"] == 4


def test_flaky_agent_is_retried(session):
    flaky = MockAgent("Codex", "flaky")
    d, v, events = run(session, MockAgent("Claude", "stubborn"), flaky)
    assert v["consensus"]
    assert any(e["kind"] == "warning" for e in events)


def test_symmetric_snapshot(session):
    """В раунде 0 ни один участник не видит реплик другого."""
    seen = {}
    class Spy(MockAgent):
        def ask(self, prompt, *a, **k):
            state = json.loads(prompt.rsplit("<<STATE>>", 1)[1])
            if state["round"] == 0:
                seen[self.name] = state["debate_log"] == [] and state["candidates"] == []
            return super().ask(prompt, *a, **k)
    run(session, Spy("Claude", "stubborn"), Spy("Codex", "agree"))
    assert seen == {"Claude": True, "Codex": True}


def test_parallel_calls(session):
    t = time.monotonic()
    run(session, MockAgent("Claude", "stubborn", delay=0.5), MockAgent("Codex", "slow", delay=0.5))
    assert time.monotonic() - t < 1.8  # 2 раунда по 0.5 с параллельно, а не 2 с


def test_stop(session):
    d = Debate([MockAgent("Claude", "stubborn", delay=5), MockAgent("Codex", "stubborn", delay=5)],
               session, "q", session.append)
    threading.Timer(0.3, d.stop).start()
    t = time.monotonic()
    v = d.run()
    assert v["stopped"] and time.monotonic() - t < 2


def test_resume_from_events(session):
    d, v, _ = run(session, MockAgent("Claude", "stubborn"), MockAgent("Codex", "stubborn"), rounds=1)
    d2 = Debate.from_events([MockAgent("Claude", "stubborn"), MockAgent("Codex", "agree")],
                            session, session.append, max_rounds=1)
    assert len(d2.candidates) == 2 and d2.round == 2
    v2 = d2.run(start_round=d2.round, extra_rounds=2)
    assert v2["consensus"] and v2["answer"] == "Решение от Claude"


def test_extract_json_not_greedy():
    text = 'код: {"a": 1} и ответ {"position": "p", "critique": "", "questions": [], ' \
           '"vote": "ABSTAIN", "vote_id": "", "new_proposal": "x {y}"} конец {"z": 2}'
    assert extract_json(text)["new_proposal"] == "x {y}"


def test_attach_and_detach(session, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hi")
    session.attach(f)
    session.attach(f)
    assert session.files() == ["a.txt", "a_1.txt"]
    assert session.detach("a.txt") and session.files() == ["a_1.txt"]
    assert not session.detach("../meta.json")


# ---------- регрессии по ревью Codex (2026-11-02) ----------

def test_snapshot_is_shared_even_if_session_changes_mid_round(session, tmp_path):
    """Оба участника получают одинаковое общее состояние, даже если председатель меняет тему
    и прикрепляет файл, пока идёт раунд. Изменения видны только со следующего раунда."""
    extra = tmp_path / "late.txt"
    extra.write_text("x")
    states = {}

    class Mutator(MockAgent):
        def ask(self, prompt, workdir, attachments, cancel=None):
            st = json.loads(prompt.rsplit("<<STATE>>", 1)[1])
            states.setdefault(st["round"], {})[self.name] = (st, [p.name for p in attachments])
            if st["round"] == 0 and self.name == "Claude":
                session.set(topic="новая тема")
                session.attach(extra)
            return super().ask(prompt, workdir, attachments, cancel)

    run(session, Mutator("Claude", "stubborn", delay=0.1), Mutator("Codex", "agree", delay=0.1))

    def shared(st):
        return {k: v for k, v in st.items() if k not in ("you", "opponent")}

    (c0, ca0), (x0, xa0) = states[0]["Claude"], states[0]["Codex"]
    assert shared(c0) == shared(x0) and ca0 == xa0 == []
    assert c0["session_topic"] == "тема"
    assert states[1]["Codex"][0]["session_topic"] == "новая тема"
    assert states[1]["Codex"][1] == ["late.txt"]


def test_session_state_is_read_once_per_round(session, monkeypatch):
    """Детерминированно: если каждое чтение вложений с диска даёт новый результат,
    оба участника всё равно видят один и тот же список."""
    calls = iter(range(1000))
    monkeypatch.setattr(session, "files", lambda: [f"f{next(calls)}.txt"])
    seen = {}

    class Spy(MockAgent):
        def ask(self, prompt, workdir, attachments, cancel=None):
            st = json.loads(prompt.rsplit("<<STATE>>", 1)[1])
            seen.setdefault(st["round"], []).append((tuple(st["files"]), tuple(a.name for a in attachments)))
            return super().ask(prompt, workdir, attachments, cancel)

    run(session, Spy("Claude", "stubborn"), Spy("Codex", "agree"))
    for rnd, views in seen.items():
        assert len(set(views)) == 1, f"раунд {rnd}: участники видели разные вложения {views}"


def test_accept_of_id_proposed_in_same_round_is_not_consensus(session):
    """Оба в одном раунде предлагают одинаковый текст и сразу голосуют за его id —
    в снимке этого кандидата ещё не было, поэтому консенсус наступает только раундом позже."""
    text = "Общий ответ"

    class Eager(MockAgent):
        def ask(self, prompt, workdir, attachments, cancel=None):
            return json.dumps({"position": "p", "critique": "", "questions": [], "vote": "ACCEPT",
                               "vote_id": proposal_id(text), "new_proposal": text})

    session.append({"kind": "question", "text": "q"})
    d = Debate([Eager("Claude"), Eager("Codex")], session, "q", session.append, max_rounds=3)
    v = d.run()
    assert v["consensus"] and v["rounds"] == 1


def test_two_failures_count_as_reject(session):
    class Broken(MockAgent):
        def ask(self, *a, **k):
            return "совсем не json"

    events = []
    session.append({"kind": "question", "text": "q"})
    d = Debate([Broken("Claude"), MockAgent("Codex", "agree")], session, "q",
               lambda e: events.append(session.append(e)), max_rounds=2)
    v = d.run()
    assert not v["consensus"]
    replies = [e for e in events if e["kind"] == "reply" and e["agent"] == "Claude"]
    assert replies and all(not r["ok"] and r["vote"] == "REJECT" for r in replies)
    assert sum(e["kind"] == "warning" for e in events) == 2 * len(replies)


def test_resume_drops_half_written_round(session):
    """Аварийный выход между двумя записями пары: оборванный раунд переигрывается целиком."""
    run(session, MockAgent("Claude", "stubborn"), MockAgent("Codex", "stubborn"), rounds=1)
    session.append({"kind": "reply", "ok": True, "round": 2, "agent": "Claude", "position": "p",
                    "critique": "", "questions": [], "vote": "REJECT", "vote_id": "",
                    "proposed": "deadbeef", "proposal_text": "из оборванного раунда"})
    d2 = Debate.from_events([MockAgent("Claude", "stubborn"), MockAgent("Codex", "agree")],
                            session, session.append)
    assert d2.round == 2
    assert all(e["round"] < 2 for e in d2.log)
    assert "deadbeef" not in {c["id"] for c in d2.candidates}


def test_meta_updates_from_stale_instance_do_not_revert(tmp_path):
    store = Store(tmp_path)
    s = store.create("Имя", "тема")
    stale = Session(s.root)  # независимая копия со старыми meta
    s.set(status="consensus")
    stale.set(title="Новое имя")
    fresh = Session(s.root).meta
    assert fresh["status"] == "consensus" and fresh["title"] == "Новое имя"


def test_store_returns_one_instance_per_id(tmp_path):
    store = Store(tmp_path)
    s = store.create("A")
    assert store.list()[0] is s
