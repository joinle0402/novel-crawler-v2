"""Handlers cho từng chức năng menu."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import config
from console import prompts, views
from console.jobs import JobManager
from db import (
    get_chapters_for_novel,
    get_chapters_with_mp3,
    get_novel_by_id,
    get_playback_state,
    init_db,
)
from playback_start import PlayStartError, parse_play_start
from player import PlayItem, _format_time


def _select_novel() -> int | None:
    novels = views.print_novel_list()
    if not novels:
        return None
    idx = prompts.ask_int("Chọn STT truyện", max_val=len(novels))
    return novels[idx - 1].id


def _build_play_queue_from(
    chapters: list,
    start_chapter: int,
) -> list[PlayItem]:
    """Queue từ chương start đến hết các chương có MP3."""
    items: list[PlayItem] = []
    for ch in sorted(chapters, key=lambda c: c.chapter_number):
        if ch.chapter_number < start_chapter:
            continue
        if ch.mp3_path and Path(ch.mp3_path).is_file():
            items.append(PlayItem(chapter=ch, mp3_path=Path(ch.mp3_path)))
    return items


def _print_player_status(jobs: JobManager) -> None:
    player = jobs.player_session()
    if not player:
        print("\n(Không có player đang chạy.)")
        return
    snap = player.snapshot()
    if not snap:
        print("\n(Đang khởi động player...)")
        return
    pos = _format_time(int(snap.position_sec * 1000))
    dur = _format_time(int(snap.duration_sec * 1000))
    state = "đang phát" if snap.is_playing else "tạm dừng"
    print(f"\n▶ {snap.novel_title}")
    print(
        f"  Ch.{snap.chapter_number} {snap.chapter_title[:40]} — "
        f"{pos}/{dur} — {snap.rate}x — {state} "
        f"({snap.queue_index + 1}/{snap.queue_total})"
    )


def _player_control_loop(jobs: JobManager) -> None:
    """Màn điều khiển phát (blocking). 0 = về menu, phát tiếp nền."""
    print("\n=== Điều khiển phát ===")
    print("p pause/resume | n next | b prev | +/- tốc độ | s stop | 0 về menu")

    while True:
        _print_player_status(jobs)
        choice = input("Chọn: ").strip().lower()

        if choice in ("0", "q"):
            if jobs.is_player_active():
                print("Về menu — phát tiếp nền.")
            break

        player = jobs.player_session()
        if not player:
            print("Player đã dừng.")
            break

        if choice == "p":
            player.pause_toggle()
            print("  Đã gửi lệnh pause/resume.")
        elif choice == "n":
            player.next_chapter()
            print("  Chương tiếp theo.")
        elif choice == "b":
            player.prev_chapter()
            print("  Chương trước.")
        elif choice in ("+", "="):
            rate = player.cycle_rate()
            print(f"  Tốc độ: {rate}x")
        elif choice == "-":
            rate = player.cycle_rate_down()
            print(f"  Tốc độ: {rate}x")
        elif choice == "s":
            jobs.stop_player()
            print("  Đã dừng phát.")
            break
        else:
            print("  Lựa chọn không hợp lệ.")


def _tts_live_monitor(jobs: JobManager, novel_id: int) -> None:
    """Poll DB và hiển thị tiến độ TTS. Enter = về menu (TTS vẫn chạy)."""
    stop = threading.Event()

    def _wait_enter() -> None:
        input()
        stop.set()

    print("\n--- Tiến độ TTS (live) — nhấn Enter để về menu ---")
    waiter = threading.Thread(target=_wait_enter, daemon=True)
    waiter.start()

    while jobs.is_tts_running() and not stop.is_set():
        print("\n" + "=" * 50)
        views.print_tts_job_detail(novel_id)
        print("=" * 50)
        time.sleep(1.5)

    if not jobs.is_tts_running():
        print("\n" + "=" * 50)
        views.print_tts_job_detail(novel_id)
        print("=" * 50)
        print("TTS hoàn tất.")


def action_crawl() -> None:
    jobs = JobManager.get()
    if jobs.is_crawl_running():
        info = jobs.crawl_info()
        print(f"\nĐang cào (pid={info.pid if info else '?'}): {info.url if info else ''}")
        print("Xem tiến độ tại menu 5 hoặc 7. Resume thủ công: python crawler.py --crawl-only ...")
        return

    prompts.pause()
    url = prompts.ask_url()
    chapter_range = prompts.ask_chapter_range()
    print(f"\nPreview phạm vi: {chapter_range.preview()}")
    if not prompts.confirm("Bắt đầu cào?"):
        print("Đã hủy.")
        return

    init_db()
    if jobs.start_crawl(url, chapter_range):
        print("Crawler chạy nền — quay lại menu tự do.")


def action_tts() -> None:
    jobs = JobManager.get()

    if jobs.is_tts_running():
        info = jobs.tts_info()
        print(f"\nĐang TTS nền: {info.novel_title if info else '(không rõ)'}")
        if info:
            views.print_tts_job_detail(info.novel_id)
        if info and prompts.confirm("Xem tiến độ live?", default_yes=True):
            _tts_live_monitor(jobs, info.novel_id)
        else:
            print("Chờ job hiện tại xong để TTS truyện khác.")
        return

    novel_id = _select_novel()
    if novel_id is None:
        return

    novel = get_novel_by_id(novel_id)
    if not novel:
        print("Không tìm thấy truyện.")
        return

    chapters = get_chapters_for_novel(novel_id)
    max_available = max((ch.chapter_number for ch in chapters), default=None)

    chapter_range = prompts.ask_chapter_range(max_available=max_available)
    print(f"\nPreview phạm vi: {chapter_range.preview(max_available)}")

    voice = input(f"Giọng TTS [{config.TTS_VOICE}]: ").strip() or config.TTS_VOICE
    rate = input(f"Tốc độ TTS [{config.TTS_RATE}]: ").strip() or config.TTS_RATE

    if not prompts.confirm("Bắt đầu TTS?"):
        print("Đã hủy.")
        return

    init_db()
    if not jobs.start_tts(
        novel_id,
        chapter_numbers=chapter_range.numbers,
        voice=voice,
        rate=rate,
    ):
        print("Không thể bắt đầu TTS.")
        return

    print(f"[TTS] Đã bắt đầu nền: {novel.title}.")
    if prompts.confirm("Xem tiến độ live?", default_yes=True):
        _tts_live_monitor(jobs, novel_id)
    else:
        print("TTS chạy nền — quay lại menu tự do.")


def action_listen() -> None:
    jobs = JobManager.get()

    if jobs.is_player_active():
        snap = jobs.player_session().snapshot() if jobs.player_session() else None
        if snap:
            print(
                f"\nĐang phát: {snap.novel_title} — "
                f"Ch.{snap.chapter_number} ({snap.rate}x)"
            )
        if prompts.confirm("Vào màn điều khiển?", default_yes=True):
            _player_control_loop(jobs)
        return

    novel_id = _select_novel()
    if novel_id is None:
        return

    novel = get_novel_by_id(novel_id)
    if not novel:
        return

    with_mp3 = get_chapters_with_mp3(novel_id)
    if not with_mp3:
        print("Chưa có chương nào có MP3.")
        return

    views.print_chapters_with_mp3(with_mp3)
    min_chapter = min(ch.chapter_number for ch in with_mp3)
    max_chapter = max(ch.chapter_number for ch in with_mp3)

    state = get_playback_state(novel_id)
    if state:
        print(
            f"\nLần nghe gần nhất: chương {state.chapter_number}, "
            f"{state.position_sec:.0f}s"
        )

    hint = "Enter=tiếp tục đã lưu" if state else f"Enter=ch.{min_chapter} từ đầu"
    prompt = f"Chương bắt đầu [{hint} | 50 | 50 2:50]: "

    while True:
        raw = input(prompt).strip()
        try:
            play_start = parse_play_start(
                raw,
                saved_chapter=state.chapter_number if state else None,
                saved_sec=state.position_sec if state else 0.0,
                min_chapter=min_chapter,
                max_chapter=max_chapter,
            )
            break
        except PlayStartError as exc:
            print(f"  Lỗi: {exc}")

    items = _build_play_queue_from(with_mp3, play_start.chapter_number)
    if not items:
        print("Không có chương phù hợp.")
        return

    if play_start.use_saved:
        print(
            f"Sẽ phát {len(items)} chương — tiếp tục Ch.{play_start.chapter_number} "
            f"@ {_format_time(int(play_start.position_sec * 1000))}"
        )
        started = jobs.start_player(
            novel_id,
            items,
            resume=True,
        )
    else:
        print(
            f"Sẽ phát {len(items)} chương — bắt đầu Ch.{play_start.chapter_number} "
            f"@ {_format_time(int(play_start.position_sec * 1000))}"
        )
        started = jobs.start_player(
            novel_id,
            items,
            resume=False,
            start_chapter_number=play_start.chapter_number,
            start_sec=play_start.position_sec,
        )

    if started:
        _player_control_loop(jobs)


def action_novel_list() -> None:
    novels = views.print_novel_table()
    if not novels:
        return
    if not prompts.confirm("Xem chi tiết một truyện?", default_yes=False):
        return
    idx = prompts.ask_int("Chọn STT", max_val=len(novels))
    novel = novels[idx - 1]
    views.print_novel_detail(novel)


def action_progress() -> None:
    views.print_progress()


def action_config() -> None:
    views.print_config()
    prompts.pause()


def action_jobs() -> None:
    """Menu 7 — trạng thái job nền (cào, TTS, Drive). Điều khiển phát tại menu 3."""
    jobs = JobManager.get()

    while True:
        views.print_jobs(jobs)
        print("\nr refresh | 0 về menu")
        choice = input("Chọn: ").strip().lower()

        if choice in ("0", "q", ""):
            break
        if choice == "r":
            continue
        print("  Lựa chọn không hợp lệ. Điều khiển phát tại menu 3.")


def action_upload_drive() -> None:
    jobs = JobManager.get()

    if not config.GDRIVE_ENABLED:
        print("\nGoogle Drive đang tắt (GDRIVE_ENABLED = False trong config.py).")
        return

    if jobs.is_upload_running():
        info = jobs.upload_info()
        print(
            f"\nĐang upload nền: {info.novel_title if info else '(không rõ)'}"
        )
        print("Chờ job hiện tại xong để upload truyện khác.")
        return

    novel_id = _select_novel()
    if novel_id is None:
        return

    novel = get_novel_by_id(novel_id)
    if not novel:
        print("Không tìm thấy truyện.")
        return

    with_mp3 = get_chapters_with_mp3(novel_id)
    if not with_mp3:
        print("Chưa có chương nào có MP3 để upload.")
        return

    max_chapter = max(ch.chapter_number for ch in with_mp3)
    chapter_range = prompts.ask_upload_chapter_range(max_available=max_chapter)
    available_numbers = [ch.chapter_number for ch in with_mp3]
    selected_numbers = chapter_range.filter_numbers(available_numbers)
    if not selected_numbers:
        print("Không có chương MP3 nào trong phạm vi đã chọn.")
        return

    print(f"\nPreview phạm vi: {chapter_range.preview(max_chapter)}")
    print(f"Sẽ upload {len(selected_numbers)} file MP3 của: {novel.title}")
    print(f"Thư mục Drive: {config.GDRIVE_ROOT_FOLDER}/{novel.title[:40]}")
    if not prompts.confirm("Bắt đầu upload?"):
        print("Đã hủy.")
        return

    if not jobs.start_upload(
        novel_id,
        chapter_numbers=chapter_range.numbers,
    ):
        print("Không thể bắt đầu upload (có thể đang chạy job khác).")
        return

    print("Upload chạy nền — quay lại menu tự do. Xem log [Drive] hoặc menu 7.")
