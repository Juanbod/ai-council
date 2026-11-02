"""Обёртки CLI: без shell, промпт через stdin, флаги только-чтения, отмена и таймаут."""
import ast
import inspect
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


@pytest.fixture
def captured(monkeypatch):
    calls = []

    def fake(cmd, prompt, cwd, timeout, cancel=None):
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
