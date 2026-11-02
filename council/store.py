"""Хранение заседаний: каждое — папка с meta.json, events.jsonl и files/."""
import json
import shutil
import threading
import time
import uuid
from pathlib import Path


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

    def save_meta(self):
        with self._lock:
            tmp = self.root / "meta.json.tmp"
            tmp.write_text(json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.root / "meta.json")

    def set(self, **fields):
        self.meta.update(fields)
        self.save_meta()

    # --- события (протокол заседания) ---
    def append(self, event: dict) -> dict:
        event = {"ts": time.time(), **event}
        with self._lock:
            with open(self.root / "events.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def events(self) -> list[dict]:
        path = self.root / "events.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
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
            shutil.copytree(src, dest, ignore=shutil.ignore_patterns(
                ".git", "node_modules", "__pycache__", ".venv", "venv"))
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
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[Session]:
        sessions = [Session(p) for p in self.root.iterdir() if (p / "meta.json").exists()]
        return sorted(sessions, key=lambda s: s.meta.get("created", 0), reverse=True)

    def create(self, title: str, topic: str = "") -> Session:
        sid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        root = self.root / sid
        (root / "files").mkdir(parents=True)
        meta = {"id": sid, "title": title, "topic": topic,
                "created": time.time(), "status": "idle"}
        (root / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return Session(root)

    def delete(self, session: Session):
        shutil.rmtree(session.root)
