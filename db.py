"""SQLite helpers cho novel crawler."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal

from config import DB_PATH

ChapterStatus = Literal["pending", "processing", "completed", "failed", "skipped"]

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


@dataclass
class Novel:
    id: int
    url: str
    title: str
    author: str
    summary: str


@dataclass
class Chapter:
    id: int
    novel_id: int
    chapter_site_id: str
    chapter_number: int
    title: str
    content: str
    mp3_path: str | None
    crawl_status: str = STATUS_PENDING
    tts_status: str = STATUS_PENDING
    tts_chars_total: int = 0
    tts_chars_done: int = 0


@dataclass
class NovelStats:
    novel_id: int
    title: str
    total: int
    crawled: int
    tts_done: int
    crawl_failed: int
    tts_failed: int


@dataclass
class PlaybackState:
    novel_id: int
    chapter_number: int
    position_sec: float
    updated_at: str


_DB_TIMEOUT_SEC = 30.0


def _connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=_DB_TIMEOUT_SEC)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def get_db(db_path: str = DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _backfill_status_columns(conn: sqlite3.Connection) -> None:
    """Đồng bộ status từ content/mp3 cho DB cũ."""
    rows = conn.execute(
        """
        SELECT id, content, mp3_path, crawl_status, tts_status,
               tts_chars_total, tts_chars_done
        FROM chapters
        """
    ).fetchall()
    for row in rows:
        crawl_status = row["crawl_status"] or STATUS_PENDING
        tts_status = row["tts_status"] or STATUS_PENDING
        content = row["content"] or ""

        if crawl_status == STATUS_PENDING and chapter_has_content(content):
            crawl_status = STATUS_COMPLETED

        mp3_path = row["mp3_path"]
        if tts_status == STATUS_PENDING and not _mp3_needs_generation(mp3_path):
            tts_status = STATUS_COMPLETED

        if crawl_status != row["crawl_status"] or tts_status != row["tts_status"]:
            conn.execute(
                "UPDATE chapters SET crawl_status=?, tts_status=? WHERE id=?",
                (crawl_status, tts_status, row["id"]),
            )

        _repair_tts_chars_total(conn, int(row["id"]), content, row, tts_status)


def init_db(db_path: str = DB_PATH) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with get_db(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS novels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                author TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chapters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                novel_id INTEGER NOT NULL,
                chapter_site_id TEXT NOT NULL,
                chapter_number INTEGER NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                mp3_path TEXT,
                crawled_at TEXT NOT NULL,
                crawl_status TEXT DEFAULT 'pending',
                tts_status TEXT DEFAULT 'pending',
                UNIQUE(novel_id, chapter_site_id),
                FOREIGN KEY (novel_id) REFERENCES novels(id)
            );

            CREATE TABLE IF NOT EXISTS playback_state (
                novel_id INTEGER PRIMARY KEY,
                chapter_number INTEGER NOT NULL,
                position_sec REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (novel_id) REFERENCES novels(id)
            );
            """
        )

        if not _column_exists(conn, "chapters", "crawl_status"):
            conn.execute(
                "ALTER TABLE chapters ADD COLUMN crawl_status TEXT DEFAULT 'pending'"
            )
        if not _column_exists(conn, "chapters", "tts_status"):
            conn.execute(
                "ALTER TABLE chapters ADD COLUMN tts_status TEXT DEFAULT 'pending'"
            )
        if not _column_exists(conn, "chapters", "tts_chars_total"):
            conn.execute(
                "ALTER TABLE chapters ADD COLUMN tts_chars_total INTEGER DEFAULT 0"
            )
        if not _column_exists(conn, "chapters", "tts_chars_done"):
            conn.execute(
                "ALTER TABLE chapters ADD COLUMN tts_chars_done INTEGER DEFAULT 0"
            )
        if not _table_exists(conn, "playback_state"):
            conn.execute(
                """
                CREATE TABLE playback_state (
                    novel_id INTEGER PRIMARY KEY,
                    chapter_number INTEGER NOT NULL,
                    position_sec REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (novel_id) REFERENCES novels(id)
                )
                """
            )

        _backfill_status_columns(conn)


def upsert_novel(url: str, title: str, author: str, summary: str, db_path: str = DB_PATH) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with get_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO novels (url, title, author, summary, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                title = excluded.title,
                author = excluded.author,
                summary = excluded.summary
            """,
            (url, title, author, summary, now),
        )
        row = conn.execute("SELECT id FROM novels WHERE url = ?", (url,)).fetchone()
        return int(row["id"])


def upsert_chapter(
    novel_id: int,
    chapter_site_id: str,
    chapter_number: int,
    title: str,
    content: str,
    db_path: str = DB_PATH,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    crawl_status = STATUS_COMPLETED if chapter_has_content(content) else STATUS_PENDING
    with get_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO chapters
                (novel_id, chapter_site_id, chapter_number, title, content,
                 crawled_at, crawl_status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(novel_id, chapter_site_id) DO UPDATE SET
                title = excluded.title,
                content = excluded.content,
                crawled_at = excluded.crawled_at,
                crawl_status = excluded.crawl_status
            """,
            (novel_id, chapter_site_id, chapter_number, title, content, now, crawl_status),
        )
        row = conn.execute(
            "SELECT id FROM chapters WHERE novel_id = ? AND chapter_site_id = ?",
            (novel_id, chapter_site_id),
        ).fetchone()
        chapter_id = int(row["id"])
        _sync_tts_chars_total_on_conn(conn, chapter_id, content)
        return chapter_id


def update_chapter_mp3(chapter_id: int, mp3_path: str, db_path: str = DB_PATH) -> None:
    with get_db(db_path) as conn:
        conn.execute(
            """
            UPDATE chapters
            SET mp3_path=?, tts_status=?,
                tts_chars_done=tts_chars_total
            WHERE id=?
            """,
            (mp3_path, STATUS_COMPLETED, chapter_id),
        )


def start_tts_chapter(chapter_id: int, chars_total: int, db_path: str = DB_PATH) -> None:
    """Đánh dấu chương đang TTS và reset progress ký tự."""
    with get_db(db_path) as conn:
        conn.execute(
            """
            UPDATE chapters
            SET tts_status=?, tts_chars_total=?, tts_chars_done=0
            WHERE id=?
            """,
            (STATUS_PROCESSING, chars_total, chapter_id),
        )


def increment_tts_chars_done(
    chapter_id: int,
    delta: int,
    db_path: str = DB_PATH,
) -> tuple[int, int]:
    """Cộng ký tự đã TTS; trả về (done, total)."""
    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE chapters SET tts_chars_done = tts_chars_done + ? WHERE id=?",
            (delta, chapter_id),
        )
        row = conn.execute(
            "SELECT tts_chars_done, tts_chars_total FROM chapters WHERE id=?",
            (chapter_id,),
        ).fetchone()
    if not row:
        return 0, 0
    return int(row["tts_chars_done"]), int(row["tts_chars_total"])


def reset_processing_tts_chapters(novel_id: int, db_path: str = DB_PATH) -> None:
    """Reset chương processing về pending (sau Ctrl+C)."""
    with get_db(db_path) as conn:
        conn.execute(
            """
            UPDATE chapters
            SET tts_status=?, tts_chars_done=0, tts_chars_total=0
            WHERE novel_id=? AND tts_status=?
            """,
            (STATUS_PENDING, novel_id, STATUS_PROCESSING),
        )


def update_crawl_status(
    chapter_id: int,
    status: ChapterStatus,
    db_path: str = DB_PATH,
) -> None:
    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE chapters SET crawl_status=? WHERE id=?",
            (status, chapter_id),
        )


def update_tts_status(
    chapter_id: int,
    status: ChapterStatus,
    db_path: str = DB_PATH,
) -> None:
    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE chapters SET tts_status=? WHERE id=?",
            (status, chapter_id),
        )


def ensure_chapter_record(
    novel_id: int,
    chapter_site_id: str,
    chapter_number: int,
    title: str,
    db_path: str = DB_PATH,
) -> None:
    """Tạo record chương nếu chưa có (không ghi đè content đã cào)."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM chapters WHERE novel_id = ? AND chapter_site_id = ?",
            (novel_id, chapter_site_id),
        ).fetchone()
        if row:
            return
        conn.execute(
            """
            INSERT INTO chapters
                (novel_id, chapter_site_id, chapter_number, title, content,
                 crawled_at, crawl_status, tts_status)
            VALUES (?, ?, ?, ?, '', ?, ?, ?)
            """,
            (
                novel_id,
                chapter_site_id,
                chapter_number,
                title,
                now,
                STATUS_PENDING,
                STATUS_PENDING,
            ),
        )


def chapter_has_content(content: str, min_len: int = 50) -> bool:
    """True nếu content đủ dài để coi là đã cào xong."""
    return bool(content and len(content.strip()) > min_len)


def _repair_tts_chars_total(
    conn: sqlite3.Connection,
    chapter_id: int,
    content: str,
    row: sqlite3.Row,
    tts_status: str | None = None,
) -> None:
    """Khôi phục tts_chars_total từ content nếu bị về 0."""
    if int(row["tts_chars_total"] or 0) > 0:
        return
    if not chapter_has_content(content):
        return
    chars = len(content)
    status = tts_status or row["tts_status"] or STATUS_PENDING
    done = chars if status == STATUS_COMPLETED else int(row["tts_chars_done"] or 0)
    conn.execute(
        "UPDATE chapters SET tts_chars_total=?, tts_chars_done=? WHERE id=?",
        (chars, done, chapter_id),
    )


def _sync_tts_chars_total_on_conn(
    conn: sqlite3.Connection,
    chapter_id: int,
    content: str,
) -> None:
    """Cập nhật tts_chars_total trên connection hiện có (tránh nested get_db)."""
    if not chapter_has_content(content):
        return
    row = conn.execute(
        """
        SELECT tts_chars_total, tts_chars_done, tts_status
        FROM chapters WHERE id=?
        """,
        (chapter_id,),
    ).fetchone()
    if not row:
        return
    _repair_tts_chars_total(conn, chapter_id, content, row)


def sync_tts_chars_total_from_content(
    chapter_id: int,
    content: str,
    db_path: str = DB_PATH,
) -> None:
    """Cập nhật tts_chars_total từ content nếu đang 0 và có nội dung."""
    if not chapter_has_content(content):
        return
    with get_db(db_path) as conn:
        _sync_tts_chars_total_on_conn(conn, chapter_id, content)


def _row_to_chapter(row: sqlite3.Row) -> Chapter:
    return Chapter(
        id=row["id"],
        novel_id=row["novel_id"],
        chapter_site_id=row["chapter_site_id"],
        chapter_number=row["chapter_number"],
        title=row["title"],
        content=row["content"],
        mp3_path=row["mp3_path"],
        crawl_status=row["crawl_status"] or STATUS_PENDING,
        tts_status=row["tts_status"] or STATUS_PENDING,
        tts_chars_total=int(row["tts_chars_total"] or 0),
        tts_chars_done=int(row["tts_chars_done"] or 0),
    )


def get_chapters_for_novel(novel_id: int, db_path: str = DB_PATH) -> list[Chapter]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, novel_id, chapter_site_id, chapter_number, title, content,
                   mp3_path, crawl_status, tts_status,
                   tts_chars_total, tts_chars_done
            FROM chapters
            WHERE novel_id = ?
            ORDER BY chapter_number
            """,
            (novel_id,),
        ).fetchall()
    return [_row_to_chapter(row) for row in rows]


def _status_sort_key(status: str) -> int:
    """failed=0, processing=1, pending=2, completed/skipped=3."""
    if status == STATUS_FAILED:
        return 0
    if status == STATUS_PROCESSING:
        return 1
    if status == STATUS_PENDING:
        return 2
    return 3


def get_processing_tts_chapters(
    novel_id: int,
    db_path: str = DB_PATH,
) -> list[Chapter]:
    """Chương đang TTS (processing) của một truyện."""
    chapters = get_chapters_for_novel(novel_id, db_path)
    return [ch for ch in chapters if ch.tts_status == STATUS_PROCESSING]


def sort_chapters_for_crawl(chapters: list[Chapter]) -> list[Chapter]:
    return sorted(
        chapters,
        key=lambda ch: (_status_sort_key(ch.crawl_status), ch.chapter_number),
    )


def sort_chapters_for_tts(chapters: list[Chapter]) -> list[Chapter]:
    return sorted(
        chapters,
        key=lambda ch: (_status_sort_key(ch.tts_status), ch.chapter_number),
    )


def get_novel_by_id(novel_id: int, db_path: str = DB_PATH) -> Novel | None:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT id, url, title, author, summary FROM novels WHERE id = ?",
            (novel_id,),
        ).fetchone()
    if not row:
        return None
    return Novel(
        id=row["id"],
        url=row["url"],
        title=row["title"],
        author=row["author"],
        summary=row["summary"],
    )


def list_novels(db_path: str = DB_PATH) -> list[Novel]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT id, url, title, author, summary FROM novels ORDER BY id"
        ).fetchall()
    return [
        Novel(
            id=row["id"],
            url=row["url"],
            title=row["title"],
            author=row["author"],
            summary=row["summary"],
        )
        for row in rows
    ]


def get_latest_novel(db_path: str = DB_PATH) -> Novel | None:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT id, url, title, author, summary FROM novels ORDER BY id DESC LIMIT 1",
        ).fetchone()
    if not row:
        return None
    return Novel(
        id=row["id"],
        url=row["url"],
        title=row["title"],
        author=row["author"],
        summary=row["summary"],
    )


def get_novel_stats(novel_id: int, db_path: str = DB_PATH) -> NovelStats | None:
    novel = get_novel_by_id(novel_id, db_path)
    if not novel:
        return None
    chapters = get_chapters_for_novel(novel_id, db_path)
    crawled = sum(1 for ch in chapters if ch.crawl_status == STATUS_COMPLETED)
    tts_done = sum(1 for ch in chapters if ch.tts_status == STATUS_COMPLETED)
    crawl_failed = sum(1 for ch in chapters if ch.crawl_status == STATUS_FAILED)
    tts_failed = sum(1 for ch in chapters if ch.tts_status == STATUS_FAILED)
    return NovelStats(
        novel_id=novel.id,
        title=novel.title,
        total=len(chapters),
        crawled=crawled,
        tts_done=tts_done,
        crawl_failed=crawl_failed,
        tts_failed=tts_failed,
    )


def _mp3_needs_generation(mp3_path: str | None) -> bool:
    """True nếu chưa có MP3 hoặc file trên disk không tồn tại."""
    if not mp3_path or not mp3_path.strip():
        return True
    return not Path(mp3_path).is_file()


def get_chapters_without_mp3(novel_id: int, db_path: str = DB_PATH) -> list[Chapter]:
    chapters = get_chapters_for_novel(novel_id, db_path)
    return [ch for ch in chapters if _mp3_needs_generation(ch.mp3_path)]


def get_chapters_with_mp3(novel_id: int, db_path: str = DB_PATH) -> list[Chapter]:
    chapters = get_chapters_for_novel(novel_id, db_path)
    return [
        ch
        for ch in chapters
        if not _mp3_needs_generation(ch.mp3_path)
    ]


def save_playback_state(
    novel_id: int,
    chapter_number: int,
    position_sec: float,
    db_path: str = DB_PATH,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO playback_state (novel_id, chapter_number, position_sec, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(novel_id) DO UPDATE SET
                chapter_number = excluded.chapter_number,
                position_sec = excluded.position_sec,
                updated_at = excluded.updated_at
            """,
            (novel_id, chapter_number, position_sec, now),
        )


def get_playback_state(novel_id: int, db_path: str = DB_PATH) -> PlaybackState | None:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT novel_id, chapter_number, position_sec, updated_at "
            "FROM playback_state WHERE novel_id = ?",
            (novel_id,),
        ).fetchone()
    if not row:
        return None
    return PlaybackState(
        novel_id=row["novel_id"],
        chapter_number=row["chapter_number"],
        position_sec=row["position_sec"],
        updated_at=row["updated_at"],
    )
