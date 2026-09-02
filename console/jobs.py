"""Quản lý job nền: TTS thread, crawl subprocess, player session, Drive upload."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import config
from chapter_range import ChapterRange, range_to_text
from crawler import run_tts_only
from db import get_novel_by_id
from player import PlayerSession, PlayItem

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class TtsJobInfo:
    novel_id: int
    novel_title: str
    voice: str
    rate: str


@dataclass
class CrawlJobInfo:
    url: str
    pid: int


@dataclass
class UploadJobInfo:
    novel_id: int
    novel_title: str
    chapter_numbers: frozenset[int] | None = None


class JobManager:
    """Singleton quản lý job nền trong phiên console."""

    _instance: JobManager | None = None

    def __init__(self) -> None:
        self._tts_thread: threading.Thread | None = None
        self._tts_info: TtsJobInfo | None = None
        self._crawl_process: subprocess.Popen | None = None
        self._crawl_info: CrawlJobInfo | None = None
        self._player: PlayerSession | None = None
        self._upload_thread: threading.Thread | None = None
        self._upload_info: UploadJobInfo | None = None

    @classmethod
    def get(cls) -> JobManager:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # --- TTS ---

    def is_tts_running(self) -> bool:
        return self._tts_thread is not None and self._tts_thread.is_alive()

    def tts_info(self) -> TtsJobInfo | None:
        if self.is_tts_running():
            return self._tts_info
        self._tts_info = None
        return None

    def start_tts(
        self,
        novel_id: int,
        *,
        chapter_numbers: frozenset[int] | None,
        voice: str,
        rate: str,
    ) -> bool:
        if self.is_tts_running():
            return False

        novel = get_novel_by_id(novel_id)
        if not novel:
            return False

        self._tts_info = TtsJobInfo(
            novel_id=novel_id,
            novel_title=novel.title,
            voice=voice,
            rate=rate,
        )

        def _worker() -> None:
            try:
                run_tts_only(
                    novel_id=novel_id,
                    chapter_numbers=chapter_numbers,
                    voice=voice,
                    rate=rate,
                    quiet=True,
                )
                if config.GDRIVE_ENABLED and config.GDRIVE_AUTO_AFTER_TTS:
                    _auto_upload_after_tts(novel_id)
            except Exception as exc:
                print(f"[TTS] Lỗi job nền: {exc}", flush=True)
            finally:
                print(f"[TTS] Hoàn tất job: {novel.title}", flush=True)

        self._tts_thread = threading.Thread(
            target=_worker,
            daemon=True,
            name="TtsJob",
        )
        self._tts_thread.start()
        return True

    # --- Crawl ---

    def is_crawl_running(self) -> bool:
        if self._crawl_process is None:
            return False
        code = self._crawl_process.poll()
        if code is not None:
            self._crawl_process = None
            self._crawl_info = None
            return False
        return True

    def crawl_info(self) -> CrawlJobInfo | None:
        if self.is_crawl_running():
            return self._crawl_info
        return None

    def start_crawl(self, url: str, chapter_range: ChapterRange) -> bool:
        if self.is_crawl_running():
            return False

        crawler = PROJECT_ROOT / "crawler.py"
        cmd = [
            sys.executable,
            str(crawler),
            "--crawl-only",
            "--url",
            url,
            "--chapter-range",
            range_to_text(chapter_range),
        ]

        kwargs: dict = {"cwd": str(PROJECT_ROOT)}
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        kwargs["env"] = env
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE

        proc = subprocess.Popen(cmd, **kwargs)
        pid = proc.pid

        self._crawl_process = proc
        self._crawl_info = CrawlJobInfo(url=url, pid=pid)
        print(
            f"[CÀO] Đã mở cửa sổ crawler (pid={pid}). "
            "Giải captcha trong cửa sổ đó khi được nhắc.",
            flush=True,
        )
        print(
            f"[CÀO] Nếu cửa sổ đóng sớm, xem log: {PROJECT_ROOT / 'logs' / 'crawl_latest.log'}",
            flush=True,
        )
        return True

    # --- Player ---

    def is_player_active(self) -> bool:
        return self._player is not None and self._player.is_active()

    def player_session(self) -> PlayerSession | None:
        if self._player and self._player.is_active():
            return self._player
        if self._player and not self._player.is_active():
            self._player = None
        return None

    def start_player(
        self,
        novel_id: int,
        items: list[PlayItem],
        *,
        resume: bool = False,
        start_chapter_number: int | None = None,
        start_sec: float = 0.0,
    ) -> bool:
        novel = get_novel_by_id(novel_id)
        if not novel:
            return False

        if self._player and self._player.is_active():
            self._player.stop()

        session = PlayerSession(
            novel_id=novel_id,
            novel_title=novel.title,
            items=items,
            resume=resume,
            start_chapter_number=start_chapter_number,
            start_sec=start_sec,
        )
        session.start()
        self._player = session
        return True

    def stop_player(self) -> None:
        if self._player:
            self._player.stop()
            self._player = None

    # --- Drive upload ---

    def is_upload_running(self) -> bool:
        return self._upload_thread is not None and self._upload_thread.is_alive()

    def upload_info(self) -> UploadJobInfo | None:
        if self.is_upload_running():
            return self._upload_info
        self._upload_info = None
        return None

    def start_upload(
        self,
        novel_id: int,
        *,
        chapter_numbers: frozenset[int] | None = None,
    ) -> bool:
        if self.is_upload_running():
            return False

        novel = get_novel_by_id(novel_id)
        if not novel:
            return False

        self._upload_info = UploadJobInfo(
            novel_id=novel_id,
            novel_title=novel.title,
            chapter_numbers=chapter_numbers,
        )

        def _worker() -> None:
            try:
                from drive_upload import upload_novel_mp3_by_id

                upload_novel_mp3_by_id(novel_id, chapter_numbers=chapter_numbers)
            except Exception as exc:
                print(f"[Drive] Lỗi job nền: {exc}", flush=True)
            finally:
                print(f"[Drive] Hoàn tất job: {novel.title}", flush=True)

        self._upload_thread = threading.Thread(
            target=_worker,
            daemon=True,
            name="DriveUploadJob",
        )
        self._upload_thread.start()
        print(
            f"[Drive] Đã bắt đầu upload nền: {novel.title}. "
            "Lần đầu có thể mở browser OAuth.",
            flush=True,
        )
        return True


def _auto_upload_after_tts(novel_id: int) -> None:
    try:
        from drive_upload import upload_novel_mp3_by_id

        print("[Drive] Tự động upload sau TTS...", flush=True)
        upload_novel_mp3_by_id(novel_id)
    except Exception as exc:
        print(f"[Drive] Lỗi auto-upload: {exc}", flush=True)
