"""Участники совета: обёртки над CLI Claude Code и Codex, плюс фиктивные агенты для тестов."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

# Строгая схема: все поля обязательны (этого требует structured output у OpenAI).
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["position", "critique", "questions", "vote", "vote_id", "new_proposal", "egg"],
    "properties": {
        "position": {"type": "string"},
        "critique": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "string"}},
        "vote": {"enum": ["ACCEPT", "REJECT", "ABSTAIN"]},
        "vote_id": {"type": "string"},
        "new_proposal": {"type": "string"},
        # яйцо в оппонента: "" — не бросать, иначе — за что (крайнее несогласие)
        "egg": {"type": "string"},
    },
}

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


class AgentError(Exception):
    pass


class Cancelled(Exception):
    pass


def resolve_exe(name_or_path: str) -> str:
    found = shutil.which(name_or_path)
    if found:
        return found
    if Path(name_or_path).exists():
        return name_or_path
    raise AgentError(f"не найден исполняемый файл: {name_or_path}")


def run_cli(cmd: list[str], prompt: str, cwd: Path, timeout: int,
            cancel: threading.Event | None = None) -> str:
    """Запуск без shell=True, промпт через stdin (обходит лимит длины командной строки Windows)."""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", creationflags=flags)
    deadline = time.monotonic() + timeout
    pending_input = prompt
    while True:
        try:
            out, err = proc.communicate(input=pending_input, timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            pending_input = None  # stdin уже передан
            if cancel is not None and cancel.is_set():
                proc.kill()
                proc.communicate()
                raise Cancelled()
            if time.monotonic() > deadline:
                proc.kill()
                proc.communicate()
                raise AgentError(f"таймаут {timeout} с")
    if proc.returncode != 0:
        tail = (err or out or "").strip()[-800:]
        raise AgentError(f"код выхода {proc.returncode}: {tail}")
    return out


def extract_json(text: str):
    """Ищет JSON-объекты без жадных регулярок: пробует весь текст, затем каждый '{' через raw_decode.
    Возвращает последний найденный объект со всеми полями схемы (или последний объект вообще)."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    dec = json.JSONDecoder()
    found, i = [], 0
    while (i := text.find("{", i)) != -1:
        try:
            obj, end = dec.raw_decode(text, i)
            if isinstance(obj, dict):
                found.append(obj)
            i = end
        except json.JSONDecodeError:
            i += 1
    full = [o for o in found if set(SCHEMA["required"]) <= o.keys()]
    if full:
        return full[-1]
    if found:
        return found[-1]
    raise ValueError("в ответе нет JSON-объекта")


def usage(tokens_in=0, tokens_out=0, cost_usd=0.0) -> dict:
    """Расход одного вызова в общем виде для обоих CLI."""
    return {"tokens_in": int(tokens_in), "tokens_out": int(tokens_out), "cost_usd": float(cost_usd)}


def claude_usage(data: dict) -> dict:
    u = data.get("usage") or {}
    return usage(u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                 + u.get("cache_read_input_tokens", 0), u.get("output_tokens", 0),
                 data.get("total_cost_usd") or 0)


def codex_events(out: str) -> tuple[dict | None, str]:
    """Разбор JSONL `codex exec --json`: расход из turn.completed и текст последнего ответа."""
    found, text = None, ""
    for line in out.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(e, dict):
            continue
        if e.get("type") == "turn.completed" and isinstance(e.get("usage"), dict):
            u = e["usage"]
            found = usage(u.get("input_tokens", 0), u.get("output_tokens", 0))
        item = e.get("item") or {}
        if e.get("type") == "item.completed" and item.get("type") == "agent_message":
            text = item.get("text", "")
    return found, text


def outside_dirs(attachments: list[Path], workdir: Path) -> list[Path]:
    """Каталоги вложений вне рабочей папки (для файла — его папка)."""
    root = Path(workdir).resolve()
    out: list[Path] = []
    for a in attachments:
        d = Path(a).resolve()
        d = d if d.is_dir() else d.parent
        if not d.is_relative_to(root) and d not in out:
            out.append(d)
    return out


class Agent:
    name = "Agent"
    color = "white"
    # расход последнего вызова ask() (None — CLI не сообщил); каждый агент работает в своём потоке
    last_usage: dict | None = None
    # доступ в интернет на этот вызов; движок выставляет его из снимка раунда (/web в заседании)
    web = False

    def ask(self, prompt: str, workdir: Path, attachments: list[Path],
            cancel: threading.Event | None = None) -> str:
        raise NotImplementedError


class ClaudeAgent(Agent):
    def __init__(self, name="Claude", exe="claude", model="", timeout=900,
                 use_json_schema=True, extra_args=None, color="#d97757"):
        self.name, self.exe, self.model, self.timeout = name, exe, model, timeout
        self.use_json_schema, self.extra_args, self.color = use_json_schema, extra_args or [], color

    def ask(self, prompt, workdir, attachments, cancel=None):
        self.last_usage = None
        # только чтение: инструменты записи и выполнения команд запрещены всегда, веб — без /web
        denied = "Edit,Write,NotebookEdit,Bash" + ("" if self.web else ",WebFetch,WebSearch")
        cmd = [resolve_exe(self.exe), "-p", "--output-format", "json", "--disallowedTools", denied]
        if self.web:  # в режиме -p без явного разрешения WebFetch получает отказ
            cmd += ["--allowedTools", "WebFetch,WebSearch"]
        if self.model:
            cmd += ["--model", self.model]
        # без --add-dir Claude в режиме -p не может читать вне рабочей папки (Codex с read-only — может)
        for d in outside_dirs(attachments, workdir):
            cmd += ["--add-dir", str(d)]
        if self.use_json_schema:
            cmd += ["--json-schema", json.dumps(SCHEMA)]
        cmd += self.extra_args
        try:
            raw = run_cli(cmd, prompt, workdir, self.timeout, cancel)
        except AgentError as e:
            if not (self.use_json_schema and "unknown option" in str(e).lower()):
                raise
            # старая версия CLI без --json-schema: схему всё равно проверит модератор
            self.use_json_schema = False
            return self.ask(prompt, workdir, attachments, cancel)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if isinstance(data, dict):
            self.last_usage = claude_usage(data)
            if data.get("is_error"):
                raise AgentError(str(data.get("result") or data)[:800])
            if isinstance(data.get("structured_output"), dict):
                return json.dumps(data["structured_output"], ensure_ascii=False)
            return str(data.get("result", ""))
        return raw


class CodexAgent(Agent):
    def __init__(self, name="Codex", exe="codex", model="", timeout=900,
                 extra_args=None, color="#10a37f"):
        self.name, self.exe, self.model, self.timeout = name, exe, model, timeout
        self.extra_args, self.color = extra_args or [], color

    def ask(self, prompt, workdir, attachments, cancel=None):
        self.last_usage = None
        with tempfile.TemporaryDirectory() as d:
            schema, last = Path(d) / "schema.json", Path(d) / "last.txt"
            schema.write_text(json.dumps(SCHEMA), encoding="utf-8")
            # --search — флаг самого codex, не exec: `codex exec --search` не принимается
            cmd = [resolve_exe(self.exe)] + (["--search"] if self.web else []) + ["exec", "--sandbox", "read-only",
                   "--skip-git-repo-check", "--ephemeral", "--color", "never", "--json",
                   "-C", str(workdir), "--output-schema", str(schema), "-o", str(last)]
            if self.model:
                cmd += ["-m", self.model]
            for a in attachments:
                if a.suffix.lower() in IMAGE_EXT:
                    cmd += ["-i", str(a)]
            cmd += self.extra_args + ["-"]
            out = run_cli(cmd, prompt, workdir, self.timeout, cancel)
            self.last_usage, text = codex_events(out)
            return last.read_text(encoding="utf-8") if last.exists() else text


class MockAgent(Agent):
    """Поведение: agree — принимает чужое предложение со 2-го раунда; stubborn — никогда;
    hothead — как stubborn, но с раунда 1 каждый раз бросает яйцо; flaky — первый ответ мусорный;
    slow — ждёт delay секунд."""

    def __init__(self, name, behavior="agree", delay=0.0, color="cyan"):
        self.name, self.behavior, self.delay, self.color = name, behavior, delay, color
        self.calls = 0

    def ask(self, prompt, workdir, attachments, cancel=None):
        self.calls += 1
        end = time.monotonic() + self.delay
        while time.monotonic() < end:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            time.sleep(0.05)
        self.last_usage = usage(len(prompt) // 4, 50)  # грубая оценка: ~4 символа на токен
        if self.behavior == "flaky" and self.calls == 1:
            return "извините, вот {сломанный: json"
        # после состояния может идти замечание модератора (повторная попытка) — читаем только JSON
        ctx, _ = json.JSONDecoder().raw_decode(prompt.rsplit("<<STATE>>", 1)[1].lstrip())
        cands = ctx["candidates"]
        theirs = [c for c in cands if c["author"] != self.name]
        mine = [c for c in cands if c["author"] == self.name]
        reply = {"position": f"{self.name}: позиция в раунде {ctx['round']}",
                 "critique": "", "questions": [], "vote": "ABSTAIN",
                 "vote_id": "", "new_proposal": "", "egg": ""}
        if ctx["round"] == 0:
            reply["new_proposal"] = f"Решение от {self.name}"
        elif self.behavior in ("agree", "flaky", "slow") and theirs:
            reply.update(vote="ACCEPT", vote_id=theirs[0]["id"])
        elif mine:
            reply.update(vote="ACCEPT", vote_id=mine[0]["id"],
                         questions=["Почему вы не принимаете мой вариант?"])
        if self.behavior == "hothead" and ctx["round"] > 0:
            reply["egg"] = "за игнорирование моих аргументов"
        return "Вот мой ответ:\n```json\n" + json.dumps(reply, ensure_ascii=False) + "\n```"


def build_agents(cfg: dict, mock: str | None = None) -> list[Agent]:
    if mock:
        a, b = (mock.split(",") + ["agree"])[:2]
        return [MockAgent("Claude", a, color="#d97757"), MockAgent("Codex", b, color="#10a37f")]
    out = []
    for spec in cfg["agents"]:
        spec = dict(spec)
        kind = spec.pop("type")
        out.append({"claude": ClaudeAgent, "codex": CodexAgent}[kind](**spec))
    return out
