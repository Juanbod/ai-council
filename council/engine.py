"""Протокол совета: слепой анализ, затем симметричные раунды с голосованием за зафиксированных кандидатов."""
import hashlib
import json
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from jsonschema import ValidationError, validate

from .agents import SCHEMA, Agent, AgentError, Cancelled, extract_json

RULES = """Ты — участник совета из двух ИИ ({me} и {other}). Председатель (человек) задаёт вопрос, \
вы вместе ищете лучший ответ. Цель — истина и качество решения, а не победа в споре.

Правила:
- Раунд 0 — независимый анализ: ты ещё не видел мнения оппонента. Изучи задачу и файлы заседания.
- Дальше: отвечай на вопросы оппонента, критикуй слабые места, задавай свои вопросы.
- Соглашайся только если тебя убедили аргументы. Меняя позицию, назови аргумент, который убедил.
- Итоговый ответ оформляется как кандидат (new_proposal) — полный самодостаточный текст ответа \
председателю. Кандидаты неизменяемы и имеют id. Чтобы поправить кандидата, предложи новый.
- Голосуй: vote=ACCEPT и vote_id=<id> — ты согласен с этим кандидатом дословно; \
REJECT — ни один кандидат пока не годится; ABSTAIN — в раунде 0.
- Консенсус — когда оба в одном раунде приняли один и тот же id.
- Ты работаешь только на чтение: ничего не изменяй в файлах.
- Комментарии председателя имеют приоритет.

Ответ — строго JSON по схеме: position (твоя текущая позиция), critique (критика оппонента и ответы \
на его вопросы), questions (вопросы оппоненту), vote, vote_id, new_proposal ("" если нет)."""


def proposal_id(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:8]


class Debate:
    """Одно обсуждение вопроса председателя. emit(event) вызывается из рабочего потока."""

    def __init__(self, agents: list[Agent], session, question: str, emit,
                 max_rounds: int = 5, history_full_rounds: int = 2):
        assert len(agents) == 2, "протокол рассчитан на двух участников"
        self.agents, self.session, self.question = agents, session, question
        self.emit, self.max_rounds = emit, max_rounds
        self.history_full_rounds = history_full_rounds
        self.cancel = threading.Event()
        self.chair_notes: queue.Queue[str] = queue.Queue()
        self.candidates: list[dict] = []
        self.log: list[dict] = []
        self.round = 0
        self.notes: list[dict] = []

    @classmethod
    def from_events(cls, agents, session, emit, **kw):
        """Восстанавливает последнее обсуждение заседания из протокола (для /continue после перезапуска)."""
        events = session.events()
        starts = [i for i, e in enumerate(events) if e.get("kind") == "question"]
        if not starts:
            return None
        tail = events[starts[-1]:]
        d = cls(agents, session, tail[0]["text"], emit, **kw)
        names = {a.name for a in agents}
        # Каждое событие "round" открывает попытку раунда. Принимается только попытка, где
        # записаны ответы обоих участников; оборванные попытки (авария между двумя записями,
        # /stop посреди раунда) остаются лишь в архивном протоколе и переигрываются.
        attempts: list[dict] = []
        for e in tail:
            kind = e.get("kind")
            if kind == "round":
                attempts.append({"round": e["round"], "replies": {}, "notes": []})
            elif kind == "reply" and attempts and e["round"] == attempts[-1]["round"]:
                attempts[-1]["replies"].setdefault(e["agent"], e)
            elif kind == "chair":
                if attempts:
                    attempts[-1]["notes"].append(e["text"])
                else:
                    d.notes.append({"round": 0, "text": e["text"]})
        next_round = 0
        for att in attempts:
            complete = names <= att["replies"].keys() and att["round"] == next_round
            if complete:
                for name in (a.name for a in agents):
                    e = att["replies"][name]
                    d.log.append({k: e[k] for k in ("round", "agent", "position", "critique",
                                                     "questions", "vote", "vote_id", "proposed")})
                    if e.get("proposed") and e["proposed"] not in {c["id"] for c in d.candidates}:
                        d.candidates.append({"id": e["proposed"], "author": e["agent"],
                                             "round": e["round"], "text": e["proposal_text"]})
                next_round += 1
            # реплика председателя, сделанная во время попытки, учитывается со следующего раунда
            d.notes.extend({"round": next_round, "text": t} for t in att["notes"])
        d.round = next_round
        return d

    # --- контекст ---
    def _prior_decisions(self) -> list[dict]:
        out = []
        for e in self.session.events():
            if e.get("kind") == "verdict":
                out.append({"question": e.get("question", ""), "consensus": e.get("consensus"),
                            "answer": e.get("answer", "")[:3000]})
        return out[-5:]

    def _compact_log(self) -> list[dict]:
        if not self.log:
            return []
        last = self.log[-1]["round"]
        out = []
        for entry in self.log:
            if entry["round"] > last - self.history_full_rounds:
                out.append(entry)
            else:  # старые раунды сжимаем
                out.append({**entry, "position": entry["position"][:600],
                            "critique": entry["critique"][:400]})
        return out

    def _snapshot(self) -> tuple[dict, list[Path]]:
        """Общее состояние раунда: читается с диска один раз и одинаково для обоих участников.
        Изменения председателя (тема, вложения) попадут только в следующий снимок."""
        files = self.session.files()
        shared = {
            "round": self.round, "max_rounds": self.max_rounds,
            "session_title": self.session.title, "session_topic": self.session.topic,
            "question": self.question,
            "files": [f"files/{f}" for f in files],
            "prior_decisions_in_session": self._prior_decisions(),
            "chair_notes": list(self.notes),
            "debate_log": self._compact_log(),
            "candidates": list(self.candidates),
        }
        attachments = [Path(self.session.files_dir) / f.rstrip("/") for f in files]
        return json.loads(json.dumps(shared, ensure_ascii=False)), attachments

    def _prompt(self, me: Agent, other: Agent, shared: dict) -> str:
        state = {"you": me.name, "opponent": other.name, **shared}
        intro = RULES.format(me=me.name, other=other.name)
        if shared["files"]:
            intro += "\n\nФайлы заседания лежат в рабочей папке в каталоге files/."
        return (intro + "\n\nСостояние заседания (JSON) после метки:\n<<STATE>>"
                + json.dumps(state, ensure_ascii=False, indent=1))

    # --- вызов с проверкой схемы ---
    def _ask(self, agent: Agent, prompt: str, attachments: list[Path]) -> dict:
        last_err = ""
        for attempt in range(2):
            p = prompt if not last_err else (
                prompt + f"\n\nТвой предыдущий ответ отклонён модератором: {last_err}\n"
                         "Верни ТОЛЬКО один JSON-объект по схеме.")
            try:
                raw = agent.ask(p, self.session.root, attachments, self.cancel)
                if not raw or not raw.strip():
                    raise ValueError("пустой ответ")
                data = extract_json(raw)
                validate(data, SCHEMA)
                return {"ok": True, **data}
            except Cancelled:
                raise
            except (AgentError, ValueError, ValidationError, OSError) as e:
                last_err = (e.message if isinstance(e, ValidationError) else str(e))[:500]
                self.emit({"kind": "warning", "agent": agent.name,
                           "text": f"попытка {attempt + 1}: {last_err}"})
        # сбой участника = несогласие, а не молчаливое ACCEPT
        return {"ok": False, "position": "", "critique": f"[сбой: {last_err}]", "questions": [],
                "vote": "REJECT", "vote_id": "", "new_proposal": ""}

    def add_chair_note(self, text: str):
        self.chair_notes.put(text)

    def stop(self):
        self.cancel.set()

    def run(self, start_round: int = 0, extra_rounds: int | None = None) -> dict:
        a, b = self.agents
        limit = self.max_rounds if extra_rounds is None else start_round + extra_rounds - 1
        self.round = start_round
        try:
            while self.round <= limit:
                while not self.chair_notes.empty():
                    note = {"round": self.round, "text": self.chair_notes.get()}
                    self.notes.append(note)
                self.emit({"kind": "round", "round": self.round,
                           "text": "Независимый анализ" if self.round == 0 else f"Раунд {self.round}"})
                # один снимок состояния для обоих, вызовы параллельно
                shared, attachments = self._snapshot()
                prompts = {a.name: self._prompt(a, b, shared), b.name: self._prompt(b, a, shared)}
                with ThreadPoolExecutor(2) as ex:
                    futs = {ag.name: ex.submit(self._ask, ag, prompts[ag.name], attachments)
                            for ag in self.agents}
                    replies = {n: f.result() for n, f in futs.items()}
                if self.cancel.is_set():
                    raise Cancelled()

                known = {c["id"] for c in self.candidates}
                votes = [(r["vote"], r["vote_id"]) for r in replies.values()]
                consensus_id = None
                if self.round > 0 and all(v == "ACCEPT" for v, _ in votes):
                    ids = {i for _, i in votes}
                    if len(ids) == 1 and next(iter(ids)) in known:
                        consensus_id = next(iter(ids))

                # ответы добавляются в протокол только после завершения пары вызовов
                for ag in self.agents:
                    r = replies[ag.name]
                    new_id = ""
                    if r["new_proposal"].strip():
                        new_id = proposal_id(r["new_proposal"])
                        if new_id not in known:
                            self.candidates.append({"id": new_id, "author": ag.name,
                                                    "round": self.round, "text": r["new_proposal"].strip()})
                            known.add(new_id)
                    entry = {"round": self.round, "agent": ag.name, "position": r["position"],
                             "critique": r["critique"], "questions": r["questions"],
                             "vote": r["vote"], "vote_id": r["vote_id"], "proposed": new_id}
                    self.log.append(entry)
                    self.emit({"kind": "reply", "ok": r["ok"], **entry,
                               "proposal_text": r["new_proposal"].strip()})

                if consensus_id:
                    text = next(c["text"] for c in self.candidates if c["id"] == consensus_id)
                    return self._finish(True, consensus_id, text)
                self.round += 1
            return self._finish(False, "", "")
        except Cancelled:
            self.emit({"kind": "system", "text": "Обсуждение остановлено председателем."})
            return self._finish(False, "", "", stopped=True)

    def _finish(self, consensus: bool, cid: str, text: str, stopped=False) -> dict:
        verdict = {"kind": "verdict", "question": self.question, "consensus": consensus,
                   "id": cid, "answer": text, "rounds": self.round, "stopped": stopped,
                   "candidates": self.candidates,
                   "last_positions": {e["agent"]: e["position"] for e in self.log[-2:]}}
        self.emit(verdict)
        return verdict
