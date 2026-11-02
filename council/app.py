"""Терминальный интерфейс совета: заседания слева, протокол справа, строка ввода снизу."""
import argparse
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
from .engine import Debate
from .store import Session, Store

BASE = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = {
    "sessions_dir": "sessions",
    "max_rounds": 5,
    "agents": [
        {"type": "claude", "name": "Claude", "exe": "claude", "model": "", "timeout": 900,
         "use_json_schema": True},
        {"type": "codex", "name": "Codex", "exe": "codex", "model": "", "timeout": 900},
    ],
}

STATUS_ICON = {"idle": "·", "running": "⏳", "consensus": "✅", "no_consensus": "⚖️", "stopped": "⏹"}

HELP = """\
**Ввод текста** — вопрос совету (или реплика председателя, если обсуждение уже идёт).
**Перетащите файл/папку** в окно терминала — путь вставится, Enter прикрепит его.

- `/new Название | тема` — новое заседание (или Ctrl+N)
- `/topic текст` — изменить тему заседания
- `/rename название` — переименовать
- `/attach путь` — прикрепить файл/папку (или Ctrl+O — выбор файла)
- `/files`, `/detach имя` — список вложений / открепить
- `/rounds N` — лимит раундов для этого заседания
- `/stop` — остановить обсуждение (Ctrl+S)
- `/continue [N]` — ещё N раундов, если консенсуса нет
- `/export` — сохранить протокол в Markdown
- `/open` — открыть папку заседания
- `/delete confirm` — удалить заседание

Ctrl+PgUp / Ctrl+PgDn — переключение заседаний, Ctrl+Q — выход."""


def load_config() -> dict:
    path = BASE / "config.json"
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg = {**DEFAULT_CONFIG, **json.loads(path.read_text(encoding="utf-8"))}
    return cfg


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
    #feed { height: 1fr; padding: 0 1; }
    #input { dock: bottom; }
    .chair { background: $primary 15%; border-left: thick $primary; padding: 0 1; margin: 1 0 0 0; }
    .round { color: $text-muted; text-style: bold; margin: 1 0 0 0; text-align: center; }
    .reply { border-left: thick $panel; padding: 0 1; margin: 1 0 0 0; }
    .warn { color: $warning; }
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

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Label("Заседания", id="sidebar-title")
                yield ListView(id="sessions")
                yield Button("+ Новое заседание", id="new", variant="primary")
            with Vertical(id="main"):
                yield Static("", id="header")
                yield Static("", id="filesbar")
                yield VerticalScroll(id="feed")
                yield Input(placeholder="Вопрос совету, /команда или путь к файлу…  (F1 — справка)",
                            id="input")
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
        if not s:
            self.query_one("#header", Static).update("Нет заседаний — Ctrl+N, чтобы создать.")
            self.query_one("#filesbar", Static).update("")
            return
        status = s.meta.get("status", "idle")
        running = " · [b]идёт обсуждение[/b]" if s.id in self.running else ""
        rounds = s.meta.get("max_rounds", self.cfg["max_rounds"])
        self.query_one("#header", Static).update(
            f"{STATUS_ICON.get(status, '')} [b]{escape(s.title)}[/b]{running}  "
            f"[dim]раундов ≤ {rounds}[/dim]\n[dim]Тема:[/dim] {escape(s.topic) or '[dim]—[/dim]'}")
        files = s.files()
        self.query_one("#filesbar", Static).update(
            "📎 " + ", ".join(escape(f) for f in files) if files else "[dim]📎 вложений нет[/dim]")

    def render_event(self, e: dict):
        k = e.get("kind")
        if k == "question":
            return Static(f"[b]🧑 Председатель:[/b] {escape(e['text'])}", classes="chair")
        if k == "chair":
            return Static(f"[b]🧑 Председатель (реплика):[/b] {escape(e['text'])}", classes="chair")
        if k == "round":
            return Static(f"── {e['text']} ──", classes="round")
        if k == "reply":
            agent = next((a for a in self._agents_info() if a[0] == e["agent"]), (e["agent"], "white"))
            vote = {"ACCEPT": f"✅ принимает `{e['vote_id']}`", "REJECT": "❌ отклоняет",
                    "ABSTAIN": "— воздерживается"}[e["vote"]]
            md = [f"**{e['agent']}** · раунд {e['round']} · {vote}"]
            if not e.get("ok", True):
                md.append("> ⚠️ ответ не получен")
            if e["position"]:
                md.append(e["position"])
            if e["critique"]:
                md.append("**Критика / ответы:** " + e["critique"])
            if e["questions"]:
                md.append("**Вопросы оппоненту:**\n" + "\n".join(f"- {q}" for q in e["questions"]))
            if e.get("proposed"):
                quoted = "\n".join("> " + line for line in e["proposal_text"].splitlines())
                md.append(f"**Предлагает итог `{e['proposed']}`:**\n{quoted}")
            w = Markdown("\n\n".join(md), classes="reply")
            w.styles.border_left = ("thick", agent[1])
            return w
        if k == "warning":
            return Static(f"⚠ {escape(e['agent'])}: {escape(e['text'])}", classes="warn")
        if k == "system":
            return Static(escape(e["text"]), classes="sys")
        if k == "verdict":
            if e["consensus"]:
                return Markdown(f"### ✅ Консенсус за {e['rounds']} р. — вариант `{e['id']}`\n\n"
                                + e["answer"], classes="verdict-ok")
            if e.get("stopped"):
                return Markdown("### ⏹ Обсуждение остановлено", classes="verdict-no")
            parts = [f"### ⚖️ Консенсус не достигнут (раундов: {e['rounds']})",
                     "`/continue N` — продолжить спор. Последние позиции:"]
            for name, pos in e.get("last_positions", {}).items():
                parts.append(f"**{name}:** {pos}")
            return Markdown("\n\n".join(parts), classes="verdict-no")
        if k == "file":
            return Static(f"📎 прикреплено: {escape(e['text'])}", classes="sys")
        return None

    def _agents_info(self):
        return [(a.name, a.color) for a in build_agents(self.cfg, self.mock)]

    async def add_to_feed(self, e: dict):
        w = self.render_event(e)
        if w is not None:
            feed = self.query_one("#feed", VerticalScroll)
            at_bottom = feed.scroll_y >= feed.max_scroll_y - 2
            await feed.mount(w)
            if at_bottom:
                feed.scroll_end(animate=False)

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
            dest = self.current.attach(path)
        except OSError as ex:
            self.notify(f"Не удалось прикрепить: {ex}", severity="error")
            return
        e = self.current.append({"kind": "file", "text": f"{path} → files/{dest.name}"})
        await self.add_to_feed(e)
        self.update_header()

    def action_stop(self):
        s = self.current
        if s and s.id in self.running:
            self.debates[s.id].stop()
            self.notify("Останавливаю…")

    async def action_help(self):
        await self.show_help()

    # ---------- обсуждение в фоне ----------
    def _emitter(self, s: Session):
        def emit(e):
            saved = s.append(e)
            self.call_from_thread(self._on_event, s.id, saved)
        return emit

    async def _on_event(self, sid: str, e: dict):
        if self.current and self.current.id == sid:
            await self.add_to_feed(e)

    async def start_debate(self, s: Session, question: str):
        e = s.append({"kind": "question", "text": question})
        await self.add_to_feed(e)
        d = Debate(build_agents(self.cfg, self.mock), s, question, self._emitter(s),
                   max_rounds=s.meta.get("max_rounds", self.cfg["max_rounds"]))
        await self._launch(s, d, lambda: d.run())

    async def continue_debate(self, s: Session, n: int):
        if s.id in self.running:
            self.notify("Обсуждение уже идёт", severity="warning")
            return
        d = self.debates.get(s.id) or Debate.from_events(
            build_agents(self.cfg, self.mock), s, self._emitter(s),
            max_rounds=s.meta.get("max_rounds", self.cfg["max_rounds"]))
        if d is None:
            self.notify("Нечего продолжать — задайте вопрос", severity="warning")
            return
        await self._launch(s, d, lambda: d.run(start_round=d.round, extra_rounds=n))

    async def _launch(self, s: Session, d: Debate, job):
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
                lines += [f"**{e['agent']}** — голос: {e['vote']} {e['vote_id']}", "", e["position"], ""]
                if e["critique"]:
                    lines += ["*Критика:* " + e["critique"], ""]
                lines += [f"- ❓ {q}" for q in e["questions"]] + [""]
                if e.get("proposed"):
                    lines += [f"> **Кандидат `{e['proposed']}`:** {e['proposal_text']}", ""]
            elif k == "verdict":
                lines += ["## " + ("✅ Консенсус" if e["consensus"] else "⚖️ Без консенсуса"), "",
                          e.get("answer", ""), ""]
        path = s.root / "protocol.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path


def main():
    ap = argparse.ArgumentParser(description="Совет ИИ: дебаты Claude и Codex")
    ap.add_argument("--mock", nargs="?", const="stubborn,agree",
                    help="фиктивные агенты, напр. --mock stubborn,agree (для проверки без затрат)")
    args = ap.parse_args()
    CouncilApp(mock=args.mock).run()


if __name__ == "__main__":
    main()
