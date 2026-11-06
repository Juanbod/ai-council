"""Участники совета: обёртки над CLI Claude Code и Codex, плюс фиктивные агенты для тестов."""
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

# Строгая схема: все поля обязательны (этого требует structured output у OpenAI).
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["position", "critique", "questions", "vote", "vote_id", "new_proposal", "egg",
                 "convinced_by"],
    "properties": {
        "position": {"type": "string"},
        "critique": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "string"}},
        "vote": {"enum": ["ACCEPT", "REJECT", "ABSTAIN"]},
        "vote_id": {"type": "string"},
        "new_proposal": {"type": "string"},
        # яйцо в оппонента: "" — не бросать, иначе — за что (крайнее несогласие)
        "egg": {"type": "string"},
        # чем убедил оппонент, если принимаешь его вариант: уступка без довода не засчитывается
        "convinced_by": {"type": "string"},
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


def kill_tree(proc: subprocess.Popen):
    """claude/codex — обёртки .cmd, настоящий node — их дочерний процесс. proc.kill() убил бы только
    cmd.exe, а node продолжил бы работу и держал каналы вывода, так что communicate() завис бы."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.kill()
    proc.wait(timeout=30)


def run_cli(cmd: list[str], prompt: str, cwd: Path, timeout: int,
            cancel: threading.Event | None = None, on_line=None) -> str:
    """Запуск без shell=True, промпт через stdin (обходит лимит длины командной строки Windows).
    Вывод читается построчно в отдельных потоках: on_line(строка) видит события CLI по мере появления."""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", creationflags=flags, start_new_session=os.name != "nt")
    out_lines: list[str] = []
    err_lines: list[str] = []

    def pump(stream, sink, callback):
        for line in stream:
            sink.append(line)
            if callback is not None:
                try:
                    callback(line)
                except Exception:  # индикатор не должен ронять вызов
                    pass

    readers = [threading.Thread(target=pump, args=(proc.stdout, out_lines, on_line), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, err_lines, None), daemon=True)]
    for t in readers:
        t.start()
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except OSError:  # процесс завершился, не дочитав stdin, — код выхода скажет почему
        pass
    deadline = time.monotonic() + timeout
    while True:
        try:
            proc.wait(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                kill_tree(proc)
                raise Cancelled()
            if time.monotonic() > deadline:
                kill_tree(proc)
                raise AgentError(f"таймаут {timeout} с")
    for t in readers:
        t.join(timeout=30)
    out, err = "".join(out_lines), "".join(err_lines)
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


def short_path(path: str) -> str:
    """Для индикатора хватает имени файла и его папки."""
    parts = Path(str(path)).parts
    return str(Path(*parts[-2:])) if len(parts) > 1 else str(path)


def clip(text: str, n: int = 60) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


def claude_activity(event: dict) -> list[str]:
    """Что делает Claude: вызовы инструментов из события assistant потока stream-json."""
    if event.get("type") != "assistant":
        return []
    out = []
    for c in (event.get("message") or {}).get("content") or []:
        if not isinstance(c, dict) or c.get("type") != "tool_use":
            continue
        name, inp = c.get("name"), c.get("input") or {}
        if name == "Read":
            out.append("читает " + short_path(inp.get("file_path", "")))
        elif name in ("Glob", "Grep"):
            where = f" в {short_path(inp['path'])}" if inp.get("path") else ""
            out.append(f"ищет «{clip(inp.get('pattern', ''), 40)}»{where}")
        elif name == "WebSearch":
            out.append(f"ищет в сети «{clip(inp.get('query', ''), 40)}»")
        elif name == "WebFetch":
            out.append("открывает " + clip(inp.get("url", "")))
        elif name == "StructuredOutput":
            out.append("пишет ответ")
        elif name:
            out.append(str(name))
    return out


def codex_command(command: str) -> str:
    """Codex оборачивает команды в `powershell.exe -Command '…'` — показываем саму команду."""
    marker = "-Command "
    if marker in command:
        command = command.split(marker, 1)[1].strip()
        if len(command) > 1 and command[0] == command[-1] and command[0] in "'\"":
            command = command[1:-1]
    return command.replace("\\\\", "\\")  # Codex удваивает обратные слэши путей внутри команды


def codex_activity(event: dict) -> list[str]:
    """Что делает Codex: начатые команды и поиски из потока `codex exec --json`."""
    if event.get("type") != "item.started":
        return []
    item = event.get("item") or {}
    kind = item.get("type")
    if kind == "command_execution":
        return ["выполняет " + clip(codex_command(item.get("command", "")))]
    if kind == "web_search":
        return [f"ищет в сети «{clip(item.get('query', ''), 40)}»"]
    return []


def claude_result(out: str) -> dict | None:
    """Итоговое событие result потока stream-json (те же поля, что у --output-format json)."""
    found = None
    for line in out.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(e, dict) and e.get("type") == "result":
            found = e
    return found


def json_events(callback, describe):
    """on_line для run_cli: разбирает строку JSONL и сообщает действия участника."""
    if callback is None:
        return None

    def on_line(line):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            return
        if isinstance(e, dict):
            for text in describe(e):
                callback(text)
    return on_line


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
    # on_activity(текст) — что агент делает прямо сейчас («читает a.py»); только для индикатора
    on_activity = None

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
        # только чтение: белый список встроенных инструментов (без MCP, плагинов, субагентов);
        # запрет записи и команд — вторым слоем, веб — только с /web
        tools = "Read,Glob,Grep" + (",WebFetch,WebSearch" if self.web else "")
        denied = "Edit,Write,NotebookEdit,Bash" + ("" if self.web else ",WebFetch,WebSearch")
        # stream-json (требует --verbose): по ходу видно, какие файлы Claude читает
        cmd = [resolve_exe(self.exe), "-p", "--output-format", "stream-json", "--verbose",
               "--tools", tools,
               "--disallowedTools", denied, "--strict-mcp-config", "--no-session-persistence"]
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
            raw = run_cli(cmd, prompt, workdir, self.timeout, cancel,
                          json_events(self.on_activity, claude_activity))
        except AgentError as e:
            if not (self.use_json_schema and "unknown option" in str(e).lower()):
                raise
            # старая версия CLI без --json-schema: схему всё равно проверит модератор
            self.use_json_schema = False
            return self.ask(prompt, workdir, attachments, cancel)
        data = claude_result(raw)
        if data is None:
            try:  # --output-format json (через extra_args) или старый CLI
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
            out = run_cli(cmd, prompt, workdir, self.timeout, cancel,
                          json_events(self.on_activity, codex_activity))
            self.last_usage, text = codex_events(out)
            return last.read_text(encoding="utf-8") if last.exists() else text


class MockAgent(Agent):
    """Поведение: agree — принимает чужое предложение со 2-го раунда; stubborn — никогда;
    hothead — как stubborn, но с раунда 1 каждый раз бросает яйцо; flaky — первый ответ мусорный;
    slow — ждёт delay секунд; pushover — поддакивает: принимает чужое сразу и без довода."""

    def __init__(self, name, behavior="agree", delay=0.0, color="cyan"):
        self.name, self.behavior, self.delay, self.color = name, behavior, delay, color
        self.calls = 0

    def ask(self, prompt, workdir, attachments, cancel=None):
        self.calls += 1
        if self.on_activity is not None:  # как настоящие CLI: шаг виден, пока агент «думает»
            for a in attachments:
                self.on_activity(f"читает {Path(a).name}")
            self.on_activity("пишет ответ")
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
                 "vote_id": "", "new_proposal": "", "egg": "", "convinced_by": ""}
        if ctx["round"] == 0:
            reply["new_proposal"] = f"Решение от {self.name}"
        elif self.behavior in ("agree", "flaky", "slow") and theirs:
            reply.update(vote="ACCEPT", vote_id=theirs[0]["id"],
                         convinced_by=f"довод {theirs[0]['author']} о рисках моего варианта")
        elif self.behavior == "pushover" and theirs:
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
