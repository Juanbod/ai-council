import json
import threading
import time

import pytest

from council.agents import MockAgent, extract_json
from council.engine import Debate
from council.store import Store


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
