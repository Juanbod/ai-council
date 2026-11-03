"""Хранение заседаний: каждое — папка с meta.json, events.jsonl и files/."""
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path


USAGE_KEYS = ("tokens_in", "tokens_out", "cost_usd", "seconds", "calls")


def usage_totals(events, since: float = 0.0) -> dict[str, dict]:
    """Суммарный расход по участникам из реплик протокола (старые реплики без usage пропускаются)."""
    out: dict[str, dict] = {}
    for e in events:
        u = e.get("usage")
        if e.get("kind") != "reply" or not isinstance(u, dict) or e.get("ts", 0) < since:
            continue
        t = out.setdefault(e["agent"], dict.fromkeys(USAGE_KEYS, 0))
        for k in USAGE_KEYS:
            t[k] += u.get(k, 0)
    return out


def bribe_totals(events, since: float = 0.0) -> dict[str, dict]:
    """Прибавки к дневным лимитам («взятки» председателя): {участник: {ключ лимита: сумма}}."""
    out: dict[str, dict] = {}
    for e in events:
        if e.get("kind") == "bribe" and e.get("ts", 0) >= since:
            t = out.setdefault(e["agent"], {})
            t[e["limit"]] = t.get(e["limit"], 0) + e["amount"]
    return out


def start_of_today() -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


class Session:
    def __init__(self, root: Path):
        self.root = root
        self.files_dir = root / "files"
        self._lock = threading.Lock()
        self.meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))

    @property
    def id(self) -> str:
        return self.meta["id"]

    @property
    def title(self) -> str:
        return self.meta["title"]

    @property
    def topic(self) -> str:
        return self.meta.get("topic", "")

    @property
    def workdir(self) -> Path:
        """Пустая рабочая папка участников — вне проекта и вне папки заседания: иначе они видят
        events.jsonl/meta.json (расход, переигранный ответ оппонента) и подхватывают AGENTS.md/CLAUDE.md
        репозитория, в котором лежит sessions/. Вложения передаются им абсолютными путями."""
        d = Path(tempfile.gettempdir()) / "ai-council" / self.id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def set(self, **fields):
        """Чтение-изменение-запись под замком: меняются только переданные поля, остальные
        берутся с диска, поэтому устаревшая копия meta не затрёт чужие изменения."""
        with self._lock:
            path = self.root / "meta.json"
            meta = json.loads(path.read_text(encoding="utf-8"))
            meta.update(fields)
            tmp = self.root / "meta.json.tmp"
            tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
            self.meta = meta

    # --- события (протокол заседания) ---
    def append(self, event: dict) -> dict:
        event = {"ts": time.time(), **event}
        line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            with open(self.root / "events.jsonl", "a+b") as f:
                # после аварии последняя строка может быть оборвана — иначе новое событие склеится с ней
                if f.seek(0, os.SEEK_END):
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        line = b"\n" + line
                f.write(line)
        return event

    def events(self) -> list[dict]:
        path = self.root / "events.jsonl"
        if not path.exists():
            return []
        out = []
        # обрыв посреди многобайтного символа не должен ронять чтение всего протокола
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # оборванная строка после аварийного выхода
        return out

    # --- вложения ---
    def attach(self, src: Path) -> Path:
        src = Path(src).expanduser().resolve()
        if not src.exists():
            raise FileNotFoundError(src)
        self.files_dir.mkdir(exist_ok=True)
        dest = self.files_dir / src.name
        n = 1
        while dest.exists():
            dest = self.files_dir / f"{src.stem}_{n}{src.suffix}"
            n += 1
        if src.is_dir():
            sessions_root = self.root.parent.resolve()
            if self.files_dir.resolve().is_relative_to(src) and src.is_relative_to(sessions_root):
                raise ValueError("нельзя прикрепить папку заседаний или её часть")
            skip = shutil.ignore_patterns(".git", "node_modules", "__pycache__", ".venv", "venv")

            def ignore(d, names):
                # папка заседаний внутри вложения (например, весь проект): иначе копия копирует саму себя
                return set(skip(d, names)) | {n for n in names if Path(d, n).resolve() == sessions_root}
            shutil.copytree(src, dest, ignore=ignore)
        else:
            shutil.copy2(src, dest)
        return dest

    def detach(self, name: str) -> bool:
        target = self.files_dir / name
        if not target.exists() or target.parent != self.files_dir:
            return False
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return True

    def files(self) -> list[str]:
        if not self.files_dir.exists():
            return []
        return sorted(p.name + ("/" if p.is_dir() else "") for p in self.files_dir.iterdir())


class Store:
    """Один экземпляр Session на id: интерфейс и рабочие потоки работают с одним объектом и одним замком."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def _get(self, root: Path) -> Session:
        with self._lock:
            s = self._sessions.get(root.name)
            if s is None:
                s = self._sessions[root.name] = Session(root)
            return s

    def list(self) -> list[Session]:
        sessions = [self._get(p) for p in self.root.iterdir() if (p / "meta.json").exists()]
        return sorted(sessions, key=lambda s: s.meta.get("created", 0), reverse=True)

    def create(self, title: str, topic: str = "") -> Session:
        sid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        root = self.root / sid
        (root / "files").mkdir(parents=True)
        meta = {"id": sid, "title": title, "topic": topic,
                "created": time.time(), "status": "idle"}
        (root / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return self._get(root)

    def delete(self, session: Session):
        with self._lock:
            self._sessions.pop(session.id, None)
        shutil.rmtree(session.root)

    def usage_today(self) -> dict[str, dict]:
        """Расход за сегодня (по местному времени) по всем заседаниям."""
        return usage_totals(self._events_today(), start_of_today())

    def bribes_today(self) -> dict[str, dict]:
        """Взятки действуют до конца дня, в каком бы заседании их ни дали."""
        return bribe_totals(self._events_today(), start_of_today())

    def _events_today(self):
        return [e for s in self.list() for e in s.events()]
