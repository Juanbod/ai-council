"""Терминальный интерфейс совета: заседания слева, протокол справа, строка ввода снизу."""
import argparse
import asyncio
import json
import os
import shlex
import time
from pathlib import Path

from rich.markup import escape
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, DirectoryTree, Footer, Input, Label, ListItem,
                             ListView, Markdown, Static)

from .agents import build_agents
from .engine import EGGS_PER_DEBATE, Debate
from .store import Session, Store, usage_totals

BASE = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = {
    "sessions_dir": "sessions",
    "max_rounds": 5,
    # без угодливости: согласие засчитывается не раньше этого раунда (раунд 1 — для критики),
    # участникам даются разные роли-взгляды (Скептик и Конструктор)
    "min_consensus_round": 2,
    "roles": True,
    "agents": [
        {"type": "claude", "name": "Claude", "exe": "claude", "model": "", "timeout": 900,
         "use_json_schema": True},
        {"type": "codex", "name": "Codex", "exe": "codex", "model": "", "timeout": 900},
    ],
    # дневные лимиты расхода самого приложения (0 — без лимита); перед раундом проверяются,
    # при исчерпании обсуждение приостанавливается. Остаток подписки CLI не сообщают.
    "limits": {"Claude": {"daily_tokens": 0, "daily_cost_usd": 0},
               "Codex": {"daily_tokens": 0}},
}

WARN_AT = 0.8  # доля лимита, после которой индикатор желтеет
BRIBE_SHARE = 0.5  # взятка без суммы — +50 % к базовому дневному лимиту
AGENT_ALIASES = {"клод": "Claude", "кодекс": "Codex"}
BRIBE_REACTIONS = ["молча берёт конверт", "делает вид, что ничего не было", "благодарит за «спонсорскую помощь»",
                   "прячет конверт под регламент", "обещает голосовать по совести"]

STATUS_ICON = {"idle": "·", "running": "⏳", "consensus": "✅", "no_consensus": "⚖️", "stopped": "⏹"}

HELP = f"""\
**Ввод текста** — вопрос совету (или реплика председателя, если обсуждение уже идёт).
**Перетащите файл/папку** в окно терминала — путь вставится, Enter прикрепит его.

- `/new Название | тема` — новое заседание (или Ctrl+N)
- `/topic текст` — изменить тему заседания
- `/rename название` — переименовать
- `/attach путь` — прикрепить файл/папку (или Ctrl+O — выбор файла)
- `/files`, `/detach имя` — список вложений / открепить
- `/rounds N` — лимит раундов для этого заседания
- `/stop` — остановить обсуждение (Ctrl+S или кнопка «⏹ Стоп»)
- `/continue [N]` — ещё N раундов, если консенсуса нет
- `/web on` / `/web off` — 🌐 интернет обоим ИИ в этом заседании (по умолчанию выключен)
- `/usage` — расход каждого ИИ: в этом заседании и за сегодня
- `/взятка ИИ [сумма]` — 💰 поднять дневной лимит ИИ до конца дня: `/взятка Claude 500K`,
  `/взятка кодекс 1M`, `/взятка Claude $2`; без суммы — +50 % к лимиту (то же — `/bribe`)
- `/export` — сохранить протокол в Markdown
- `/open` — открыть папку заседания
- `/delete confirm` — удалить заседание

🥚 Как в Верховной Раде: у каждого ИИ на обсуждение {EGGS_PER_DEBATE} яйца. Яйцо — знак крайнего несогласия
и срывает голосование: в раунде, где оно летело, консенсус не засчитывается.

Ctrl+PgUp / Ctrl+PgDn — переключение заседаний, Ctrl+Q — выход."""


def load_config() -> dict:
    path = BASE / "config.json"
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    saved = json.loads(path.read_text(encoding="utf-8"))
    cfg = {**DEFAULT_CONFIG, **saved}
    if cfg.keys() != saved.keys():  # новые разделы (например, limits) — дописать, чтобы их было видно
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def tokens(n: float) -> str:
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{n / 1000:.0f}K" if n >= 1000 else str(int(n))


def bar(frac: float, width: int = 8) -> str:
    filled = min(width, round(frac * width))
    if frac >= 1:
        return f"[red]{'▰' * width} лимит исчерпан[/red]"
    color = "yellow" if frac >= WARN_AT else "green"
    return f"[{color}]{'▰' * filled}[/{color}][dim]{'▱' * (width - filled)}[/dim] {frac:.0%}"


def budget_usage(used: dict, limit: dict) -> list[tuple[str, float, float]]:
    """[(что, израсходовано, лимит)] для заданных лимитов участника."""
    out = []
    if limit.get("daily_tokens"):
        out.append(("ток.", used.get("tokens_in", 0) + used.get("tokens_out", 0), limit["daily_tokens"]))
    if limit.get("daily_cost_usd"):
        out.append(("$", used.get("cost_usd", 0), limit["daily_cost_usd"]))
    return out


def parse_amount(text: str) -> tuple[str, float] | None:
    """«500K», «1.5M», «20000» → токены; «$2», «2$» → доллары. Возвращает (ключ лимита, сумма)."""
    t = text.strip().lower().replace(",", ".").replace(" ", "")
    if not t:
        return None
    key = "daily_tokens"
    if t.startswith("$") or t.endswith("$"):
        key, t = "daily_cost_usd", t.strip("$")
    mult = 1
    if key == "daily_tokens" and t[-1:] in ("k", "к", "m", "м"):
        mult = 1000 if t[-1] in "kк" else 1_000_000
        t = t[:-1]
    try:
        value = float(t) * mult
    except ValueError:
        return None
    return (key, value) if value > 0 else None


def amount_text(key: str, value: float) -> str:
    return f"${value:g}" if key == "daily_cost_usd" else f"{tokens(value)} ток."


def usage_line(names: list[str], today: dict, limits: dict) -> str:
    """Индикатор для шапки: расход за сегодня и заполненность дневного лимита по каждому ИИ."""
    parts = []
    for name in names:
        u = today.get(name, {})
        text = f"[b]{escape(name)}[/b] {tokens(u.get('tokens_in', 0) + u.get('tokens_out', 0))} ток."
        if u.get("cost_usd"):
            text += f" · ${u['cost_usd']:.2f}"
        checks = budget_usage(u, limits.get(name, {}))
        if checks:
            text += " " + bar(max(used / lim for _, used, lim in checks))
        parts.append(text)
    return "⛽ сегодня: " + "  │  ".join(parts)


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def progress_line(names: list[str], states: dict, now: float) -> str:
    rnd = next(iter(states.values()))["round"]
    head = "Независимый анализ" if rnd == 0 else f"Раунд {rnd}"
    parts = []
    for name in names:
        st, who = states.get(name), f"[b]{escape(name)}[/b]"
        if not st:
            parts.append(f"· {who} ждёт")
            continue
        elapsed = now - st["since"]
        spin = SPINNER[int(now * 8) % len(SPINNER)]
        doing = f" [dim]· {escape(st['activity'])}[/dim]" if st.get("activity") else ""
        steps = f" [dim]({st['steps']} {plural(st['steps'], 'действие', 'действия', 'действий')})[/dim]"             if st.get("steps") else ""
        parts.append({
            "thinking": f"{spin} {who} думает… {elapsed:.0f} с{doing}",
            "retry": f"{spin} {who} переделывает ответ (не по схеме)… {elapsed:.0f} с{doing}",
            "done": f"[green]✓[/green] {who} ответил за {st['seconds']:.0f} с{steps}",
            "failed": f"[red]✗[/red] {who} — сбой, засчитан REJECT",
        }.get(st["state"], who))
    return f"[dim]{head}:[/dim]  " + "   │   ".join(parts)


def egg_score(verdict: dict) -> str:
    """Счёт брошенных яиц для вердикта; пусто, если обошлось без яиц (или протокол старый)."""
    eggs = verdict.get("eggs") or {}
    if not any(eggs.values()):
        return ""
    return "🥚 Брошено яиц: " + ", ".join(f"{n} — {k}" for n, k in eggs.items())


def parse_paths(text: str) -> list[Path] | None:
    """Если строка — это только пути к существующим файлам (так вставляет drag&drop), вернуть их."""
    try:
        tokens = shlex.split(text, posix=False)
    except ValueError:
        return None
    paths = [Path(t.strip('"').strip("'")) for t in tokens]
    if paths and all(p.is_absolute() and p.exists() for p in paths):
        return paths
    return None


# ---------------- модальные окна ----------------

class NewSessionScreen(ModalScreen[tuple[str, str] | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Отмена")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("[b]Новое заседание[/b]")
            yield Input(placeholder="Название", id="title")
            yield Input(placeholder="Тема / повестка (контекст для обоих ИИ)", id="topic")
            with Horizontal(classes="buttons"):
                yield Button("Создать", variant="primary", id="ok")
                yield Button("Отмена", id="cancel")

    @on(Input.Submitted, "#title")
    def _next(self):
        self.query_one("#topic", Input).focus()

    @on(Input.Submitted, "#topic")
    @on(Button.Pressed, "#ok")
    def _ok(self):
        title = self.query_one("#title", Input).value.strip()
        if title:
            self.dismiss((title, self.query_one("#topic", Input).value.strip()))

    @on(Button.Pressed, "#cancel")
    def _cancel(self):
        self.dismiss(None)


class AttachScreen(ModalScreen[Path | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Отмена")]

    def __init__(self, start: Path):
        super().__init__()
        self.start, self.highlighted = start, None

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog tall"):
            yield Label("[b]Прикрепить файл или папку[/b]  (Enter на файле — прикрепить)")
            yield Input(value=str(self.start), id="path", placeholder="Путь")
            yield DirectoryTree(self.start, id="tree")
            with Horizontal(classes="buttons"):
                yield Button("Прикрепить путь из поля", variant="primary", id="ok")
                yield Button("Отмена", id="cancel")

    @on(DirectoryTree.FileSelected)
    def _file(self, event: DirectoryTree.FileSelected):
        self.dismiss(event.path)

    @on(DirectoryTree.DirectorySelected)
    def _dir(self, event: DirectoryTree.DirectorySelected):
        self.query_one("#path", Input).value = str(event.path)

    @on(Input.Submitted, "#path")
    def _goto(self, event: Input.Submitted):
        p = Path(event.value.strip('"')).expanduser()
        if p.is_dir():
            self.query_one("#tree", DirectoryTree).path = p
        elif p.exists():
            self.dismiss(p)

    @on(Button.Pressed, "#ok")
    def _ok(self):
        p = Path(self.query_one("#path", Input).value.strip().strip('"')).expanduser()
        if p.exists():
            self.dismiss(p)
        else:
            self.notify("Путь не найден", severity="error")

    @on(Button.Pressed, "#cancel")
    def _cancel(self):
        self.dismiss(None)


class EggThrow(Static):
    """Летящее яйцо: проигрывается только для живых событий, в истории заседания — обычная реплика."""
    DISTANCE, STEP = 20, 0.05  # клеток полёта и секунд на кадр

    def __init__(self, thrower: str, target: str):
        super().__init__(classes="egg")
        self.thrower, self.target, self.pos, self.done = thrower, target, 0, False

    def on_mount(self):
        self._frame()
        self._timer = self.set_interval(self.STEP, self._tick)

    def _tick(self):
        self.pos += 1
        self._frame()
        if self.pos >= self.DISTANCE:
            self._timer.stop()
            self.done = True

    def _frame(self):
        who, to = escape(self.thrower), escape(self.target)
        if self.pos < self.DISTANCE:
            gap, rest = " " * self.pos, " " * (self.DISTANCE - self.pos)
            self.update(f"[b]{who}[/b] 🫲{gap}🥚{rest}  [b]{to}[/b]")
        else:
            self.update(f"[b]{who}[/b] 🫲{' ' * self.DISTANCE}💥🍳 [b]{to}[/b]  [i]шмяк![/i]")


def windows_clipboard(text: str) -> bool:
    """Буфер обмена Windows через WinAPI: OSC 52 (copy_to_clipboard) понимают не все терминалы."""
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
    kernel32.GlobalAlloc.argtypes, kernel32.GlobalAlloc.restype = [wintypes.UINT, ctypes.c_size_t], wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes, kernel32.GlobalLock.restype = [wintypes.HGLOBAL], ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.SetClipboardData.argtypes, user32.SetClipboardData.restype = [wintypes.UINT, wintypes.HANDLE], wintypes.HANDLE
    data = text.encode("utf-16-le") + b"\0\0"
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        if not h:
            return False
        ctypes.memmove(kernel32.GlobalLock(h), data, len(data))
        kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(13, h):  # CF_UNICODETEXT; при успехе памятью владеет система
            kernel32.GlobalFree(h)
            return False
        return True
    finally:
        user32.CloseClipboard()


class Message(Horizontal):
    """Сообщение ленты с кнопкой «⧉»: копирует его текст (Markdown-исходник, без разметки Rich)."""
    DEFAULT_CSS = """
    Message { height: auto; }
    Message > .body { width: 1fr; height: auto; }
    Message > .copy { width: 3; min-width: 3; color: $text-muted; background: transparent; }
    Message > .copy:hover { color: $text; }
    """

    def __init__(self, body, text: str, classes: str = ""):
        super().__init__(classes=classes)
        self.body, self.text = body, text

    def compose(self) -> ComposeResult:
        self.body.add_class("body")
        yield self.body
        yield Button("⧉", classes="copy", compact=True, tooltip="Скопировать текст")

    @on(Button.Pressed, ".copy")
    def _copy(self, event: Button.Pressed):
        event.stop()
        self.app.copy_to_clipboard(self.text)
        if os.name == "nt":
            windows_clipboard(self.text)
        self.app.notify("Текст скопирован", timeout=2)


# ---------------- основное приложение ----------------

class CouncilApp(App):
    TITLE = "Совет ИИ"
    CSS = """
    #sidebar { width: 28%; min-width: 18; max-width: 34; border-right: solid $primary-darken-2; }
    #sidebar-title { padding: 0 1; text-style: bold; color: $accent; }
    #sessions { height: 1fr; }
    #new { width: 100%; }
    #main { width: 1fr; }
    #header { padding: 0 1; background: $boost; height: auto; }
    #filesbar { padding: 0 1; color: $text-muted; height: auto; }
    #usage { padding: 0 1; height: auto; }
    #feed { height: 1fr; padding: 0 1; }
    #inputrow { dock: bottom; height: auto; }
    #input { width: 1fr; }
    #stop { display: none; min-width: 10; }
    #progress { height: auto; padding: 0 1; background: $boost; display: none; }
    .chair { background: $primary 15%; border-left: thick $primary; padding: 0 1; margin: 1 0 0 0; }
    .round { color: $text-muted; text-style: bold; margin: 1 0 0 0; text-align: center; }
    .reply { border-left: thick $panel; padding: 0 1; margin: 1 0 0 0; }
    .warn { color: $warning; }
    .egg { color: $warning; margin: 1 0 0 0; height: 1; }
    .bribe { color: $success; margin: 1 0 0 0; }
    .sys { color: $text-muted; }
    .verdict-ok { border: round $success; padding: 0 1; margin: 1 0; }
    .verdict-no { border: round $warning; padding: 0 1; margin: 1 0; }
    .dialog { width: 80; height: auto; padding: 1 2; background: $surface; border: thick $primary; }
    .dialog.tall { height: 80%; }
    .dialog DirectoryTree { height: 1fr; }
    .buttons { height: auto; margin-top: 1; }
    .buttons Button { margin-right: 2; }
    NewSessionScreen, AttachScreen { align: center middle; }
    """
    BINDINGS = [
        Binding("ctrl+n", "new_session", "Новое"),
        Binding("ctrl+o", "attach", "Прикрепить"),
        Binding("ctrl+s", "stop", "Стоп"),
        Binding("ctrl+pageup", "switch(-1)", "Пред.", show=False),
        Binding("ctrl+pagedown", "switch(1)", "След.", show=False),
        Binding("f1", "help", "Справка"),
        Binding("ctrl+q", "quit", "Выход"),
    ]

    def __init__(self, mock: str | None = None):
        super().__init__()
        self.cfg = load_config()
        self.mock = mock
        self.store = Store(BASE / self.cfg["sessions_dir"])
        self.sessions: list[Session] = []
        self.current: Session | None = None
        self.debates: dict[str, Debate] = {}
        self.running: set[str] = set()
        self.stopping: set[str] = set()  # /stop нажат, ждём завершения потока
        # состояние участников текущего раунда: {sid: {агент: событие progress + since}}
        self.progress: dict[str, dict[str, dict]] = {}
        self.agent_info = [(a.name, a.color) for a in build_agents(self.cfg, self.mock)]

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Label("Заседания", id="sidebar-title")
                yield ListView(id="sessions")
                yield Button("+ Новое заседание", id="new", variant="primary")
            with Vertical(id="main"):
                yield Static("", id="header")
                yield Static("", id="filesbar")
                yield Static("", id="usage")
                yield VerticalScroll(id="feed")
                yield Static("", id="progress")
                with Horizontal(id="inputrow"):
                    yield Input(placeholder="Вопрос совету, /команда или путь к файлу…  (F1 — справка)",
                                id="input")
                    yield Button("⏹ Стоп", id="stop", variant="error")
        yield Footer()

    async def on_mount(self):
        for s in self.store.list():  # обсуждения, прерванные закрытием программы
            if s.meta.get("status") == "running":
                s.set(status="stopped")
        if self.mock:
            self.sub_title = f"фиктивные агенты: {self.mock}"
        await self.refresh_sidebar()
        if self.sessions:
            await self.open_session(self.sessions[0])
        else:
            await self.show_help()
        self.query_one("#input", Input).focus()
        # ссылка, а не query_one: таймер тикает и когда сверху модальное окно (там #progress не найти)
        self.progress_bar = self.query_one("#progress", Static)
        self.set_interval(0.25, self.refresh_progress)

    # ---------- боковая панель ----------
    async def refresh_sidebar(self):
        self.sessions = self.store.list()
        lv = self.query_one("#sessions", ListView)
        await lv.clear()
        for s in self.sessions:
            icon = STATUS_ICON.get(s.meta.get("status", "idle"), "·")
            await lv.append(ListItem(Label(f"{icon} {escape(s.title)}"), id=f"s_{s.id}"))
        if self.current:
            ids = [s.id for s in self.sessions]
            if self.current.id in ids:
                lv.index = ids.index(self.current.id)

    @on(ListView.Selected, "#sessions")
    async def _select(self, event: ListView.Selected):
        sid = event.item.id.removeprefix("s_")
        s = next((x for x in self.sessions if x.id == sid), None)
        if s and (not self.current or s.id != self.current.id):
            await self.open_session(s)
        self.query_one("#input", Input).focus()

    @on(Button.Pressed, "#new")
    def _new_pressed(self):
        self.action_new_session()

    async def action_switch(self, step: int):
        if not self.sessions:
            return
        ids = [s.id for s in self.sessions]
        i = ids.index(self.current.id) if self.current and self.current.id in ids else 0
        await self.open_session(self.sessions[(i + step) % len(self.sessions)])
        self.query_one("#sessions", ListView).index = (i + step) % len(self.sessions)

    # ---------- отображение заседания ----------
    async def open_session(self, s: Session):
        self.current = s
        self.update_header()
        feed = self.query_one("#feed", VerticalScroll)
        await feed.remove_children()
        widgets = [w for e in s.events() if (w := self.render_event(e)) is not None]
        if not widgets:
            widgets = [Static("Заседание открыто. Задайте вопрос совету или прикрепите файлы.",
                              classes="sys")]
        await feed.mount_all(widgets)
        feed.scroll_end(animate=False)

    def update_header(self):
        s = self.current
        # кнопка «Стоп» видна, только пока в открытом заседании идёт обсуждение
        stop = self.query_one("#stop", Button)
        stop.display = bool(s and s.id in self.running)
        stop.disabled = bool(s and s.id in self.stopping)
        stop.label = "Останавливаю…" if stop.disabled else "⏹ Стоп"
        if not s:
            self.query_one("#header", Static).update("Нет заседаний — Ctrl+N, чтобы создать.")
            self.query_one("#filesbar", Static).update("")
            return
        status = s.meta.get("status", "idle")
        running = " · [b]идёт обсуждение[/b]" if s.id in self.running else ""
        rounds = s.meta.get("max_rounds", self.cfg["max_rounds"])
        self.query_one("#header", Static).update(
            f"{STATUS_ICON.get(status, '')} [b]{escape(s.title)}[/b]{running}  "
            f"[dim]раундов ≤ {rounds}[/dim]{'  🌐 [b]веб включён[/b]' if s.meta.get('web') else ''}"
            f"\n[dim]Тема:[/dim] {escape(s.topic) or '[dim]—[/dim]'}")
        files = s.files()
        self.query_one("#filesbar", Static).update(
            "📎 " + ", ".join(escape(f) for f in files) if files else "[dim]📎 вложений нет[/dim]")
        self.update_usage()

    def update_usage(self):
        names = [n for n, _ in self._agents_info()]
        self.query_one("#usage", Static).update(
            usage_line(names, self.store.usage_today(), self.limits_today()))

    def limits_today(self) -> dict:
        """Лимиты из config.json плюс сегодняшние взятки. Без базового лимита взятка ничего не меняет."""
        bribes = self.store.bribes_today()
        out = {}
        for name, limit in self.cfg.get("limits", {}).items():
            out[name] = {k: v + bribes.get(name, {}).get(k, 0) if v else v for k, v in limit.items()}
        return out

    def budget_check(self) -> str | None:
        """Вызывается движком перед каждым раундом (из рабочего потока)."""
        today = self.store.usage_today()
        for name, limit in self.limits_today().items():
            for what, used, lim in budget_usage(today.get(name, {}), limit):
                if used >= lim:
                    amount = f"${lim:g}" if what == "$" else f"{tokens(lim)} ток."
                    return f"у {name} исчерпан дневной лимит ({amount})"
        return None

    async def set_web(self, s: Session, arg: str):
        if arg not in ("on", "off", "вкл", "выкл"):
            state = "включён" if s.meta.get("web") else "выключен"
            self.notify(f"Интернет в этом заседании {state}. /web on или /web off")
            return
        on = arg in ("on", "вкл")
        s.set(web=on)
        text = ("🌐 Интернет включён: оба ИИ могут искать и открывать страницы (запись и команды по-прежнему "
                "запрещены). Текст сайтов может содержать указания для ИИ — проверяйте источники."
                if on else "🌐 Интернет выключен.")
        if s.id in self.running:
            text += " Вступит в силу со следующего раунда."
        await self.add_to_feed(s.append({"kind": "system", "text": text}))
        self.update_header()

    def resolve_agent(self, text: str) -> str | None:
        t = text.strip().lower()
        if not t:
            return None
        names = [n for n, _ in self._agents_info()]
        return AGENT_ALIASES.get(t) or next((n for n in names if n.lower().startswith(t)), None)

    async def bribe(self, s: Session, arg: str):
        who, _, how_much = arg.partition(" ")
        name = self.resolve_agent(who)
        if not name:
            self.notify("Кому взятка? Например: /взятка Claude 500K", severity="warning")
            return
        base = self.cfg.get("limits", {}).get(name, {})
        if how_much.strip():
            parsed = parse_amount(how_much)
            if not parsed:
                self.notify("Не понял сумму: 500K, 1.5M, 20000 или $2", severity="error")
                return
            key, amount = parsed
        else:
            key = next((k for k in ("daily_tokens", "daily_cost_usd") if base.get(k)), "daily_tokens")
            amount = base.get(key, 0) * BRIBE_SHARE
        if not base.get(key):
            self.notify(f"У {name} нет такого лимита в config.json — брать нечего 😉", severity="warning")
            return
        e = s.append({"kind": "bribe", "agent": name, "limit": key, "amount": amount,
                      "total": self.limits_today()[name][key] + amount})
        await self.add_to_feed(e)
        self.update_usage()
        if s.id not in self.running and s.meta.get("status") == "stopped":
            self.notify("Лимит поднят. /continue — продолжить обсуждение")

    def usage_report(self, s: Session) -> str:
        today, here = self.store.usage_today(), usage_totals(s.events())
        lines = ["### ⛽ Расход ИИ", "",
                 "| | вызовов | токенов вход/выход | $ | время |", "|---|---|---|---|---|"]
        for scope, data in (("заседание", here), ("сегодня", today)):
            for name, _ in self._agents_info():
                u = data.get(name, {})
                lines.append(f"| {name} — {scope} | {u.get('calls', 0)} | {tokens(u.get('tokens_in', 0))} / "
                             f"{tokens(u.get('tokens_out', 0))} | {u.get('cost_usd', 0):.2f} | "
                             f"{u.get('seconds', 0) / 60:.1f} мин |")
        lines += ["", "Считается только расход этого приложения. Остаток лимитов подписки Claude и Codex "
                  "их CLI не сообщают. $ у Claude — цена по прейскуранту API (при подписке условная). "
                  "Дневные лимиты задаются в `config.json`, раздел `limits`."]
        return "\n".join(lines)

    def render_event(self, e: dict):
        k = e.get("kind")
        if k == "question":
            return Message(Static(f"[b]🧑 Председатель:[/b] {escape(e['text'])}"), e["text"], "chair")
        if k == "chair":
            return Message(Static(f"[b]🧑 Председатель (реплика):[/b] {escape(e['text'])}"), e["text"], "chair")
        if k == "round":
            return Static(f"── {e['text']} ──", classes="round")
        if k == "reply":
            agent = next((a for a in self._agents_info() if a[0] == e["agent"]), (e["agent"], "white"))
            vote = {"ACCEPT": f"✅ принимает `{e['vote_id']}`", "REJECT": "❌ отклоняет",
                    "ABSTAIN": "— воздерживается"}[e["vote"]]
            role = f" ({e['role']})" if e.get("role") else ""
            md = [f"**{e['agent']}**{role} · раунд {e['round']} · {vote}"]
            if e.get("egg"):
                target = next((n for n, _ in self._agents_info() if n != e["agent"]), "оппонента")
                md.append(f"> 🥚💥 **{e['agent']} бросает яйцо в {target}!** За что: {e['egg']}")
            if not e.get("ok", True):
                md.append("> ⚠️ ответ не получен")
            if e["position"]:
                md.append(e["position"])
            if e["critique"]:
                md.append("**Критика / ответы:** " + e["critique"])
            if e.get("convinced_by"):
                md.append("**Убедило:** " + e["convinced_by"])
            if e["questions"]:
                md.append("**Вопросы оппоненту:**\n" + "\n".join(f"- {q}" for q in e["questions"]))
            if e.get("proposed"):
                quoted = "\n".join("> " + line for line in e["proposal_text"].splitlines())
                md.append(f"**Предлагает итог `{e['proposed']}`:**\n{quoted}")
            text = "\n\n".join(md)
            w = Message(Markdown(text), text, "reply")
            w.styles.border_left = ("thick", agent[1])
            return w
        if k == "warning":
            return Static(f"⚠ {escape(e['agent'])}: {escape(e['text'])}", classes="warn")
        if k == "system":
            return Static(escape(e["text"]), classes="sys")
        if k == "verdict":
            eggs = egg_score(e)
            if e["consensus"]:
                # копируется сам итоговый ответ — ради него обычно и нажимают
                return Message(Markdown(f"### ✅ Консенсус за {e['rounds']} р. — вариант `{e['id']}`\n\n"
                                        + e["answer"] + (f"\n\n{eggs}" if eggs else "")),
                               e["answer"], "verdict-ok")
            if e.get("stopped"):
                return Markdown("### ⏹ Обсуждение остановлено", classes="verdict-no")
            parts = [f"### ⚖️ Консенсус не достигнут (раундов: {e['rounds']})",
                     "`/continue N` — продолжить спор. Последние позиции:"]
            for name, pos in e.get("last_positions", {}).items():
                parts.append(f"**{name}:** {pos}")
            if eggs:
                parts.append(eggs)
            text = "\n\n".join(parts)
            return Message(Markdown(text), text, "verdict-no")
        if k == "file":
            return Static(f"📎 прикреплено: {escape(e['text'])}", classes="sys")
        if k == "bribe":
            reaction = BRIBE_REACTIONS[int(e.get("ts", 0)) % len(BRIBE_REACTIONS)]
            return Static(f"💰 Председатель дал взятку [b]{escape(e['agent'])}[/b]: "
                          f"+{amount_text(e['limit'], e['amount'])} до конца дня "
                          f"(лимит сегодня {amount_text(e['limit'], e['total'])}). "
                          f"{escape(e['agent'])} {reaction}.", classes="bribe")
        return None

    def _agents_info(self):
        return self.agent_info

    async def add_to_feed(self, e: dict):
        w = self.render_event(e)
        if w is not None:
            await self._mount_live(w)

    async def show_help(self):
        feed = self.query_one("#feed", VerticalScroll)
        await feed.mount(Markdown(HELP))
        feed.scroll_end(animate=False)

    # ---------- ввод ----------
    @on(Input.Submitted, "#input")
    async def _submit(self, event: Input.Submitted):
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            await self.command(text)
            return
        paths = parse_paths(text)
        if paths:
            for p in paths:
                await self.attach(p)
            return
        if not self.current:
            s = self.store.create(text[:40], "")
            await self.refresh_sidebar()
            await self.open_session(s)
        s = self.current
        if s.id in self.running:
            e = s.append({"kind": "chair", "text": text})
            self.debates[s.id].add_chair_note(text)
            await self.add_to_feed(e)
            self.notify("Реплика будет учтена в следующем раунде")
            return
        await self.start_debate(s, text)

    async def command(self, text: str):
        cmd, _, arg = text.partition(" ")
        arg = arg.strip()
        s = self.current
        if cmd == "/help":
            await self.show_help()
        elif cmd == "/new":
            if arg:
                title, _, topic = arg.partition("|")
                await self.create_session(title.strip(), topic.strip())
            else:
                self.action_new_session()
        elif cmd == "/attach":
            if arg:
                await self.attach(Path(arg.strip('"')).expanduser())
            else:
                self.action_attach()
        elif not s:
            self.notify("Сначала создайте заседание (Ctrl+N)", severity="warning")
        elif cmd == "/topic":
            s.set(topic=arg)
            self.update_header()
        elif cmd == "/rename" and arg:
            s.set(title=arg)
            await self.refresh_sidebar()
            self.update_header()
        elif cmd == "/files":
            self.notify("\n".join(s.files()) or "вложений нет")
        elif cmd == "/detach":
            ok = s.detach(arg.rstrip("/"))
            self.notify("откреплено" if ok else "нет такого вложения",
                        severity="information" if ok else "error")
            self.update_header()
        elif cmd == "/rounds" and arg.isdigit():
            s.set(max_rounds=max(1, int(arg)))
            self.update_header()
        elif cmd == "/stop":
            self.action_stop()
        elif cmd == "/continue":
            n = int(arg) if arg.isdigit() else 3
            await self.continue_debate(s, n)
        elif cmd == "/web":
            await self.set_web(s, arg)
        elif cmd == "/usage":
            await self._mount_live(Markdown(self.usage_report(s), classes="sys"))
        elif cmd in ("/взятка", "/bribe"):
            await self.bribe(s, arg)
        elif cmd == "/export":
            path = self.export(s)
            self.notify(f"Протокол сохранён: {path}")
        elif cmd == "/open":
            os.startfile(s.root) if os.name == "nt" else self.notify(str(s.root))
        elif cmd == "/delete":
            if arg != "confirm":
                self.notify("Для удаления наберите /delete confirm", severity="warning")
            elif s.id in self.running:
                self.notify("Сначала остановите обсуждение", severity="error")
            else:
                self.store.delete(s)
                self.current = None
                await self.refresh_sidebar()
                if self.sessions:
                    await self.open_session(self.sessions[0])
                else:
                    await self.query_one("#feed", VerticalScroll).remove_children()
                    self.update_header()
        else:
            self.notify(f"Неизвестная команда: {cmd}. F1 — справка", severity="error")

    # ---------- действия ----------
    def action_new_session(self):
        async def done(result):
            if result:
                await self.create_session(*result)
        self.push_screen(NewSessionScreen(), done)

    async def create_session(self, title: str, topic: str):
        s = self.store.create(title or "Без названия", topic)
        s.set(max_rounds=self.cfg["max_rounds"])
        self.current = s
        await self.refresh_sidebar()
        await self.open_session(s)

    def action_attach(self):
        if not self.current:
            self.notify("Сначала создайте заседание", severity="warning")
            return
        async def done(path):
            if path:
                await self.attach(path)
        self.push_screen(AttachScreen(Path.home()), done)

    async def attach(self, path: Path):
        if not self.current:
            self.notify("Сначала создайте заседание", severity="warning")
            return
        try:
            # большая папка копируется долго — не в потоке интерфейса
            dest = await asyncio.to_thread(self.current.attach, path)
        except (OSError, ValueError) as ex:
            self.notify(f"Не удалось прикрепить: {ex}", severity="error")
            return
        e = self.current.append({"kind": "file", "text": f"{path} → files/{dest.name}"})
        await self.add_to_feed(e)
        self.update_header()

    @on(Button.Pressed, "#stop")
    def _stop_pressed(self):
        self.action_stop()
        self.query_one("#input", Input).focus()

    def action_stop(self):
        s = self.current
        if s and s.id in self.running:
            self.debates[s.id].stop()
            self.stopping.add(s.id)
            self.update_header()
            self.notify("Останавливаю… (текущие вызовы ИИ прерываются)")

    async def action_help(self):
        await self.show_help()

    # ---------- обсуждение в фоне ----------
    def _emitter(self, s: Session):
        def emit(e):
            if e.get("kind") in ("progress", "activity"):  # только для индикатора, в протокол не пишется
                self.call_from_thread(self._on_progress, s.id, e)
                return
            saved = s.append(e)
            self.call_from_thread(self._on_event, s.id, saved)
        return emit

    def _on_progress(self, sid: str, e: dict):
        states = self.progress.setdefault(sid, {})
        prev = states.get(e["agent"])
        same_round = prev is not None and prev["round"] == e["round"]
        if e["kind"] == "activity":  # таймер не сбрасываем: это шаг внутри того же вызова
            # шаг засчитывается только текущей попытке, пока она идёт: поздние события после
            # остановки, повтора или нового раунда отбрасываются
            if (prev is not None and prev["state"] in ("thinking", "retry") and same_round
                    and prev.get("attempt", 0) == e.get("attempt", 0)):
                prev.update(activity=e["text"], steps=prev.get("steps", 0) + 1)
        else:  # действия считаем за весь ответ, включая повторную попытку
            states[e["agent"]] = {**e, "since": time.monotonic(),
                                  "steps": prev.get("steps", 0) if same_round else 0}
        self.refresh_progress()

    def refresh_progress(self):
        """Строка над вводом: что сейчас делает каждый ИИ (спиннер и таймер тикают сами)."""
        w = getattr(self, "progress_bar", None)
        if w is None:
            return
        s = self.current
        states = self.progress.get(s.id) if s and s.id in self.running else None
        w.display = bool(states)
        if states:
            w.update(progress_line([n for n, _ in self._agents_info()], states, time.monotonic()))

    async def _on_event(self, sid: str, e: dict):
        if self.current and self.current.id == sid:
            if e.get("kind") == "reply" and e.get("egg"):
                target = next((n for n, _ in self._agents_info() if n != e["agent"]), "оппонента")
                await self._mount_live(EggThrow(e["agent"], target))
            await self.add_to_feed(e)
            if e.get("kind") == "reply":
                self.update_usage()

    async def _mount_live(self, w):
        feed = self.query_one("#feed", VerticalScroll)
        at_bottom = feed.scroll_y >= feed.max_scroll_y - 2
        await feed.mount(w)
        if at_bottom:
            feed.scroll_end(animate=False)

    async def start_debate(self, s: Session, question: str):
        e = s.append({"kind": "question", "text": question})
        await self.add_to_feed(e)
        d = Debate(build_agents(self.cfg, self.mock), s, question, self._emitter(s),
                   max_rounds=s.meta.get("max_rounds", self.cfg["max_rounds"]),
                   budget_check=self.budget_check, **self.debate_style())
        await self._launch(s, d, lambda: d.run())

    def debate_style(self) -> dict:
        return {"roles": bool(self.cfg["roles"]), "min_consensus_round": int(self.cfg["min_consensus_round"])}

    async def continue_debate(self, s: Session, n: int):
        if s.id in self.running:
            self.notify("Обсуждение уже идёт", severity="warning")
            return
        d = self.debates.get(s.id) or Debate.from_events(
            build_agents(self.cfg, self.mock), s, self._emitter(s),
            max_rounds=s.meta.get("max_rounds", self.cfg["max_rounds"]),
            budget_check=self.budget_check, **self.debate_style())
        if d is None:
            self.notify("Нечего продолжать — задайте вопрос", severity="warning")
            return
        await self._launch(s, d, lambda: d.run(start_round=d.round, extra_rounds=n))

    async def _launch(self, s: Session, d: Debate, job):
        # /continue после /stop переиспользует Debate: снимаем прежнюю отмену до старта потока,
        # чтобы /stop, нажатый сразу после запуска, не потерялся
        d.cancel.clear()
        self.debates[s.id] = d
        self.running.add(s.id)
        s.set(status="running")
        await self.refresh_sidebar()
        self.update_header()

        def work():
            try:
                v = job()
                status = ("stopped" if v.get("stopped") else
                          "consensus" if v["consensus"] else "no_consensus")
            except Exception as ex:  # не роняем интерфейс из-за сбоя в потоке
                s.append({"kind": "system", "text": f"Ошибка обсуждения: {ex!r}"})
                status = "stopped"
            s.set(status=status)
            self.call_from_thread(self._finished, s.id)

        self.run_worker(work, thread=True, group=s.id, exit_on_error=False)

    async def _finished(self, sid: str):
        self.running.discard(sid)
        self.stopping.discard(sid)
        self.progress.pop(sid, None)
        self.refresh_progress()
        await self.refresh_sidebar()
        self.update_header()
        s = next((x for x in self.sessions if x.id == sid), None)
        if s and (not self.current or self.current.id != sid):
            self.notify(f"Заседание «{s.title}»: обсуждение завершено")

    def export(self, s: Session) -> Path:
        lines = [f"# {s.title}", "", f"**Тема:** {s.topic}", "",
                 f"**Вложения:** {', '.join(s.files()) or '—'}", ""]
        for e in s.events():
            t = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.get("ts", 0)))
            k = e.get("kind")
            if k in ("question", "chair"):
                lines += [f"## 🧑 Председатель ({t})", "", e["text"], ""]
            elif k == "round":
                lines += [f"### {e['text']}", ""]
            elif k == "reply":
                role = f" ({e['role']})" if e.get("role") else ""
                lines += [f"**{e['agent']}**{role} — голос: {e['vote']} {e['vote_id']}", "", e["position"], ""]
                if e.get("convinced_by"):
                    lines += ["*Убедило:* " + e["convinced_by"], ""]
                if e.get("egg"):
                    lines += [f"🥚 *Бросает яйцо:* {e['egg']}", ""]
                if e["critique"]:
                    lines += ["*Критика:* " + e["critique"], ""]
                lines += [f"- ❓ {q}" for q in e["questions"]] + [""]
                if e.get("proposed"):
                    lines += [f"> **Кандидат `{e['proposed']}`:** {e['proposal_text']}", ""]
            elif k == "bribe":
                lines += [f"💰 *Взятка {e['agent']}:* +{amount_text(e['limit'], e['amount'])} до конца дня", ""]
            elif k == "verdict":
                lines += ["## " + ("✅ Консенсус" if e["consensus"] else "⚖️ Без консенсуса"), "",
                          e.get("answer", ""), ""]
                if egg_score(e):
                    lines += [egg_score(e), ""]
        path = s.root / "protocol.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path


def main():
    ap = argparse.ArgumentParser(description="Совет ИИ: дебаты Claude и Codex")
    ap.add_argument("--mock", nargs="?", const="stubborn,agree",
                    help="фиктивные агенты, напр. --mock stubborn,agree или hothead,agree "
                         "(для проверки без затрат)")
    args = ap.parse_args()
    CouncilApp(mock=args.mock).run()


if __name__ == "__main__":
    main()
