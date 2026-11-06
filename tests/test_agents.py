"""Обёртки CLI: без shell, промпт через stdin, флаги только-чтения, отмена и таймаут."""
import ast
import inspect
import json
import sys
import threading
import time

import pytest

from council import agents
from council.agents import AgentError, Cancelled, ClaudeAgent, CodexAgent, run_cli

READ_STDIN = "import sys; sys.stdin.reconfigure(encoding='utf-8'); print(len(sys.stdin.read()))"


def test_run_cli_passes_long_prompt_via_stdin(tmp_path):
    prompt = "я" * 100_000  # больше лимита командной строки Windows (~32К)
    out = run_cli([sys.executable, "-c", READ_STDIN], prompt, tmp_path, timeout=30)
    assert out.strip() == str(len(prompt))


def test_run_cli_error_and_timeout(tmp_path):
    with pytest.raises(AgentError, match="код выхода 3"):
        run_cli([sys.executable, "-c", "import sys; sys.exit(3)"], "", tmp_path, timeout=30)
    with pytest.raises(AgentError, match="таймаут"):
        run_cli([sys.executable, "-c", "import time; time.sleep(10)"], "", tmp_path, timeout=1)


def test_run_cli_cancel_kills_process(tmp_path):
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    t = time.monotonic()
    with pytest.raises(Cancelled):
        run_cli([sys.executable, "-c", "import time; time.sleep(10)"], "", tmp_path, 30, cancel)
    assert time.monotonic() - t < 3


# Как обёртка claude.cmd: настоящая работа — во внуке, который унаследовал stdout
WRAPPER = ("import subprocess, sys, time; "
           "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
           "open(sys.argv[1], 'w').write(str(p.pid)); time.sleep(60)")


def test_cancel_kills_the_whole_process_tree(tmp_path):
    pid_file = tmp_path / "child.pid"
    cancel = threading.Event()
    threading.Timer(1.5, cancel.set).start()
    t = time.monotonic()
    with pytest.raises(Cancelled):
        run_cli([sys.executable, "-c", WRAPPER, str(pid_file)], "", tmp_path, 120, cancel)
    assert time.monotonic() - t < 20  # без убийства дерева communicate() ждал бы внука 60 с
    pid = int(pid_file.read_text())
    time.sleep(0.5)
    assert not pid_alive(pid)


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import subprocess
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    import os
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@pytest.fixture
def captured(monkeypatch):
    calls = []

    def fake(cmd, prompt, cwd, timeout, cancel=None, on_line=None):
        calls.append({"cmd": cmd, "prompt": prompt, "cwd": cwd})
        return '{"result": "{}"}'

    monkeypatch.setattr(agents, "run_cli", fake)
    monkeypatch.setattr(agents, "resolve_exe", lambda name: name)
    return calls


def test_claude_is_read_only_and_uses_stdin(captured, tmp_path):
    ClaudeAgent().ask("ПРОМПТ", tmp_path, [])
    cmd = captured[0]["cmd"]
    assert "-p" in cmd and "ПРОМПТ" not in " ".join(cmd)
    denied = set(cmd[cmd.index("--disallowedTools") + 1].split(","))
    assert {"Edit", "Write", "NotebookEdit", "Bash"} <= denied
    assert captured[0]["prompt"] == "ПРОМПТ" and captured[0]["cwd"] == tmp_path


def test_codex_is_read_only_and_uses_stdin(captured, tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"")
    CodexAgent().ask("ПРОМПТ", tmp_path, [img, tmp_path / "b.txt"])
    cmd = captured[0]["cmd"]
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert cmd[-1] == "-" and "ПРОМПТ" not in " ".join(cmd)
    assert cmd.count("-i") == 1 and cmd[cmd.index("-i") + 1] == str(img)


def test_no_shell_true():
    tree = ast.parse(inspect.getsource(agents))
    shell_kwargs = [k for node in ast.walk(tree) if isinstance(node, ast.Call)
                    for k in node.keywords if k.arg == "shell"]
    assert shell_kwargs == []


def test_codex_reports_usage_from_json_events(monkeypatch, tmp_path):
    jsonl = "\n".join([
        '{"type": "thread.started", "thread_id": "x"}',
        '{"type": "item.completed", "item": {"type": "agent_message", "text": "{\\"a\\": 1}"}}',
        '{"type": "turn.completed", "usage": {"input_tokens": 16862, "cached_input_tokens": 0, '
        '"output_tokens": 5, "reasoning_output_tokens": 0}}'])
    calls = []
    monkeypatch.setattr(agents, "run_cli", lambda cmd, *a, **k: calls.append(cmd) or jsonl)
    monkeypatch.setattr(agents, "resolve_exe", lambda name: name)
    ag = CodexAgent()
    assert ag.ask("p", tmp_path, []) == '{"a": 1}'  # без файла -o берём текст из событий
    assert "--json" in calls[0]
    assert ag.last_usage == {"tokens_in": 16862, "tokens_out": 5, "cost_usd": 0.0}


def test_claude_reports_usage_and_cost(monkeypatch, tmp_path):
    out = ('{"type": "result", "is_error": false, "result": "{}", "total_cost_usd": 0.25, '
           '"usage": {"input_tokens": 2, "cache_creation_input_tokens": 30845, '
           '"cache_read_input_tokens": 100, "output_tokens": 4}}')
    monkeypatch.setattr(agents, "run_cli", lambda *a, **k: out)
    monkeypatch.setattr(agents, "resolve_exe", lambda name: name)
    ag = ClaudeAgent()
    ag.ask("p", tmp_path, [])
    assert ag.last_usage == {"tokens_in": 30947, "tokens_out": 4, "cost_usd": 0.25}


def test_claude_gets_add_dir_for_outside_paths(captured, tmp_path):
    workdir, outside = tmp_path / "session", tmp_path / "project"
    (workdir / "files").mkdir(parents=True)
    outside.mkdir()
    inner = workdir / "files" / "a.txt"
    inner.write_text("x")
    ClaudeAgent().ask("p", workdir, [inner, outside, outside / "b.txt"])
    cmd = captured[0]["cmd"]
    dirs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--add-dir"]
    assert dirs == [str(outside.resolve())]  # вложения внутри рабочей папки доступны и так


def test_claude_web_flags(captured, tmp_path):
    def denied_and_allowed(web):
        ag = ClaudeAgent()
        ag.web = web
        ag.ask("p", tmp_path, [])
        cmd = captured[-1]["cmd"]
        denied = set(cmd[cmd.index("--disallowedTools") + 1].split(","))
        allowed = cmd[cmd.index("--allowedTools") + 1] if "--allowedTools" in cmd else ""
        return denied, allowed

    denied, allowed = denied_and_allowed(False)
    assert {"WebFetch", "WebSearch"} <= denied and allowed == ""
    denied, allowed = denied_and_allowed(True)
    assert "WebFetch" not in denied and allowed == "WebFetch,WebSearch"
    assert {"Edit", "Write", "NotebookEdit", "Bash"} <= denied  # запись и команды — никогда


def test_codex_search_flag_goes_before_exec(captured, tmp_path):
    ag = CodexAgent()
    ag.ask("p", tmp_path, [])
    assert "--search" not in captured[-1]["cmd"]
    ag.web = True
    ag.ask("p", tmp_path, [])
    cmd = captured[-1]["cmd"]
    assert cmd[1:3] == ["--search", "exec"]
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"


def test_claude_gets_only_read_tools_without_mcp(captured, tmp_path):
    ag = ClaudeAgent()
    ag.ask("p", tmp_path, [])
    cmd = captured[-1]["cmd"]
    assert cmd[cmd.index("--tools") + 1] == "Read,Glob,Grep"  # белый список, а не только запреты
    assert "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
    ag.web = True
    ag.ask("p", tmp_path, [])
    cmd = captured[-1]["cmd"]
    assert cmd[cmd.index("--tools") + 1] == "Read,Glob,Grep,WebFetch,WebSearch"


def test_run_cli_streams_lines_while_process_runs(tmp_path):
    # процесс ждёт файл-флажок, который создаёт только on_line: без построчного чтения — таймаут
    flag = tmp_path / "seen"
    script = ("import sys, time, pathlib\n"
              "print('first', flush=True)\n"
              "p = pathlib.Path(sys.argv[1])\n"
              "for _ in range(200):\n"
              "    if p.exists(): break\n"
              "    time.sleep(0.05)\n"
              "print('found' if p.exists() else 'missing')\n")
    seen = []

    def on_line(line):
        seen.append(line.strip())
        flag.touch()

    out = run_cli([sys.executable, "-c", script, str(flag)], "", tmp_path, 30, on_line=on_line)
    assert seen == ["first", "found"] and out.split() == ["first", "found"]


# события в том виде, в каком их печатают настоящие CLI (сняты 2026-11-06)
CLAUDE_STREAM = [
    {"type": "system", "subtype": "init"},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": r"C:\tmp\proj\note.txt"}}]}},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Grep", "input": {"pattern": "def main", "path": r"C:\tmp\proj"}}]}},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "StructuredOutput", "input": {"position": "x"}}]}},
    {"type": "result", "is_error": False, "result": "", "structured_output": {"position": "x"},
     "total_cost_usd": 0.05, "usage": {"input_tokens": 10, "output_tokens": 2}},
]
CODEX_STREAM = [
    {"type": "thread.started", "thread_id": "x"},
    {"type": "item.started", "item": {"type": "command_execution", "status": "in_progress",
     "command": "\"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe\" "
                "-Command 'Get-Content -LiteralPath C:\\\\tmp\\\\note.txt'"}},
    {"type": "item.completed", "item": {"type": "command_execution", "status": "completed",
     "command": "powershell.exe -Command 'Get-Content -LiteralPath note.txt'"}},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "{\"a\": 1}"}},
    {"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}},
]


def streaming(monkeypatch, events):
    calls = []

    def fake(cmd, prompt, cwd, timeout, cancel=None, on_line=None):
        calls.append(cmd)
        lines = [json.dumps(e, ensure_ascii=False) for e in events]
        for line in lines:
            if on_line:
                on_line(line + "\n")
        return "\n".join(lines)

    monkeypatch.setattr(agents, "run_cli", fake)
    monkeypatch.setattr(agents, "resolve_exe", lambda name: name)
    return calls


def test_claude_streams_activity_and_still_returns_answer(monkeypatch, tmp_path):
    calls = streaming(monkeypatch, CLAUDE_STREAM)
    seen = []
    ag = ClaudeAgent()
    ag.on_activity = seen.append
    assert json.loads(ag.ask("p", tmp_path, [])) == {"position": "x"}
    cmd = calls[0]
    assert cmd[cmd.index("--output-format") + 1] == "stream-json" and "--verbose" in cmd
    assert len(seen) == 3 and seen[2] == "пишет ответ"
    assert seen[0].startswith("читает ") and seen[0].endswith("note.txt")
    assert seen[1].startswith("ищет «def main»")
    assert ag.last_usage == {"tokens_in": 10, "tokens_out": 2, "cost_usd": 0.05}


def test_codex_streams_commands_without_powershell_wrapper(monkeypatch, tmp_path):
    streaming(monkeypatch, CODEX_STREAM)
    seen = []
    ag = CodexAgent()
    ag.on_activity = seen.append
    assert ag.ask("p", tmp_path, []) == '{"a": 1}'
    # только начатые команды, без обёртки powershell и удвоенных слэшей
    assert seen == ["выполняет Get-Content -LiteralPath C:\\tmp\\note.txt"]


def test_no_activity_callback_means_no_parsing(monkeypatch, tmp_path):
    streaming(monkeypatch, CLAUDE_STREAM)
    assert json.loads(ClaudeAgent().ask("p", tmp_path, [])) == {"position": "x"}
