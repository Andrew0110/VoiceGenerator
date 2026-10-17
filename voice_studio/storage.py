import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def identifier():
    return uuid4().hex


def now():
    return datetime.now(timezone.utc).isoformat()


class Storage:
    def __init__(self, root: Path):
        self.root = root
        for name in ("voices", "outputs", "editor_sources"):
            (root / name).mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS voices (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, transcript TEXT NOT NULL,
                    language TEXT NOT NULL, duration REAL NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, text TEXT NOT NULL, voice_id TEXT,
                    voice_name TEXT NOT NULL, language TEXT NOT NULL, steps INTEGER NOT NULL,
                    status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '', duration REAL,
                    created_at TEXT NOT NULL, finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS editor_sources (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    duration REAL NOT NULL, created_at TEXT NOT NULL
                );
            """)
            # Existing recordings retain their original WAV format and normal tempo.
            columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            if "output_format" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN output_format TEXT NOT NULL DEFAULT 'wav'")
            if "speed" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN speed REAL NOT NULL DEFAULT 1.0")
            if "mp3_bitrate" not in columns:
                # Every existing MP3 was encoded at 128 kbps; new requests explicitly set their choice.
                db.execute("ALTER TABLE jobs ADD COLUMN mp3_bitrate INTEGER NOT NULL DEFAULT 128")
            if "kind" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'generation'")
            if "name" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN name TEXT")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / "studio.sqlite3", timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def all(self, table):
        assert table in ("voices", "jobs", "editor_sources")
        with self.connect() as db:
            return [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY created_at DESC")]

    def get(self, table, key):
        assert table in ("voices", "jobs", "editor_sources")
        with self.connect() as db:
            row = db.execute(f"SELECT * FROM {table} WHERE id = ?", (key,)).fetchone()
        return dict(row) if row else None

    def insert(self, table, values):
        assert table in ("voices", "jobs", "editor_sources")
        keys = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        with self.connect() as db:
            db.execute(f"INSERT INTO {table} ({keys}) VALUES ({marks})", tuple(values.values()))

    def update_job(self, key, **values):
        assert set(values) <= {"status", "progress", "message", "duration", "finished_at"}
        with self.connect() as db:
            db.execute("UPDATE jobs SET " + ", ".join(f"{k} = ?" for k in values) + " WHERE id = ?",
                       (*values.values(), key))

    def interrupt_jobs(self):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='failed', message=?, finished_at=? WHERE status IN ('queued', 'running')",
                       ("Генерацію перервано закриттям застосунку. Запустіть її повторно.", now()))
