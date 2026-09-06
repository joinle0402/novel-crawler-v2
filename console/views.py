"""Hiển thị danh sách, tiến độ, cấu hình, job nền."""



from __future__ import annotations



import config

from db import (

    STATUS_PROCESSING,

    Chapter,

    Novel,

    get_chapters_for_novel,

    get_novel_by_id,

    get_novel_stats,

    get_processing_tts_chapters,

    list_novels,

)

from player import _format_time





MAIN_MENU = """

╔══════════════════════════════════════╗

║       NOVEL CRAWLER & TTS            ║

╠══════════════════════════════════════╣

║ 1. Cào dữ liệu truyện                ║

║ 2. Tạo TTS                           ║

║ 3. Nghe truyện                       ║

║ 4. Danh sách truyện                  ║

║ 5. Tiến độ                           ║

║ 6. Cấu hình                          ║

║ 7. Job đang chạy                     ║

║ 8. Upload MP3 lên Google Drive       ║

║ 0. Thoát                             ║

╚══════════════════════════════════════╝

"""





def print_main_menu() -> None:

    print(MAIN_MENU)





def _tts_progress_line(ch: Chapter) -> str:

    if ch.tts_status == STATUS_PROCESSING and ch.tts_chars_total > 0:

        pct = int(ch.tts_chars_done * 100 / ch.tts_chars_total)

        return (

            f"{ch.tts_chars_done:,}/{ch.tts_chars_total:,} ký tự ({pct}%)"

        )

    return ch.tts_status





def print_novel_list() -> list[Novel]:

    novels = list_novels()

    if not novels:

        print("\nChưa có truyện nào trong database.")

        return novels



    print(f"\n{'STT':>3} | {'ID':>3} | Tên truyện")

    print("-" * 60)

    for i, novel in enumerate(novels, start=1):

        print(f"{i:>3} | {novel.id:>3} | {novel.title}")

    return novels





def print_novel_table() -> list[Novel]:

    novels = list_novels()

    if not novels:

        print("\nChưa có truyện nào trong database.")

        return novels



    print(

        f"\n{'STT':>3} | {'Tên truyện':<30} | {'Tổng':>5} | {'Đã cào':>6} | {'Đã TTS':>6}"

    )

    print("-" * 70)

    for i, novel in enumerate(novels, start=1):

        stats = get_novel_stats(novel.id)

        if not stats:

            continue

        title = novel.title[:30]

        print(

            f"{i:>3} | {title:<30} | {stats.total:>5} | {stats.crawled:>6} | {stats.tts_done:>6}"

        )

    return novels





def print_novel_detail(novel: Novel) -> None:

    stats = get_novel_stats(novel.id)

    chapters = get_chapters_for_novel(novel.id)

    print(f"\n=== {novel.title} ===")

    print(f"ID: {novel.id}")

    print(f"URL: {novel.url}")

    print(f"Tác giả: {novel.author or '(không rõ)'}")

    if stats:

        print(f"Tổng chương: {stats.total}")

        print(f"Đã cào: {stats.crawled}")

        print(f"Đã TTS: {stats.tts_done}")

        print(f"Crawl failed: {stats.crawl_failed}")

        print(f"TTS failed: {stats.tts_failed}")



    print(f"\n{'#':>4} | {'Crawl':^10} | {'TTS':^28} | Tiêu đề")

    print("-" * 85)

    for ch in chapters[:50]:

        tts_col = _tts_progress_line(ch)

        if len(tts_col) > 28:

            tts_col = tts_col[:25] + "..."

        print(

            f"{ch.chapter_number:>4} | {ch.crawl_status:^10} | {tts_col:^28} | {ch.title[:30]}"

        )

    if len(chapters) > 50:

        print(f"... và {len(chapters) - 50} chương nữa")





def print_tts_job_detail(novel_id: int) -> None:

    novel = get_novel_by_id(novel_id)

    if not novel:

        return



    stats = get_novel_stats(novel_id)

    processing = get_processing_tts_chapters(novel_id)

    chapters = get_chapters_for_novel(novel_id)



    print(f"\n--- TTS: {novel.title} ---")

    if stats:

        print(f"Tổng: {stats.tts_done}/{stats.crawled} chương đã TTS")



    if processing:

        print("Chương đang xử lý:")

        for ch in processing:

            print(

                f"  Ch.{ch.chapter_number:>4}  {_tts_progress_line(ch)}  | {ch.title[:35]}"

            )



    pending_tts = [

        ch for ch in chapters

        if ch.tts_status in ("pending", "failed")

        and ch.crawl_status == "completed"

    ]

    if pending_tts[:5]:

        print("Chương chờ TTS (5 tiếp theo):")

        for ch in pending_tts[:5]:

            print(f"  Ch.{ch.chapter_number:>4}  pending  | {ch.title[:35]}")





def print_progress() -> None:

    novels = list_novels()

    if not novels:

        print("\nChưa có truyện nào.")

        return



    print()

    for novel in novels:

        stats = get_novel_stats(novel.id)

        if not stats:

            continue

        failed = stats.crawl_failed + stats.tts_failed

        print(f"{novel.title}")

        print(f"  Crawl: {stats.crawled}/{stats.total}")

        print(f"  TTS:   {stats.tts_done}/{stats.crawled}")



        processing = get_processing_tts_chapters(novel.id)

        for ch in processing:

            print(f"    → Ch.{ch.chapter_number}: {_tts_progress_line(ch)}")



        if failed:

            print(f"  Failed: {failed}")

        print()





def print_jobs(jobs) -> None:

    """Hiển thị job nền đang chạy."""

    print("\n=== JOB ĐANG CHẠY ===")



    crawl = jobs.crawl_info() if jobs.is_crawl_running() else None

    if crawl:

        print(f"\n[CÀO] pid={crawl.pid}")

        print(f"  URL: {crawl.url}")

        print("  Tiến độ: xem DB (menu 5) — cào ghi crawl_status từng chương")

    else:

        print("\n[CÀO] (không chạy)")



    tts = jobs.tts_info() if jobs.is_tts_running() else None

    if tts:

        print(f"\n[TTS] {tts.novel_title} (id={tts.novel_id})")

        print(f"  voice={tts.voice}, rate={tts.rate}")

        print_tts_job_detail(tts.novel_id)

    else:

        print("\n[TTS] (không chạy)")



    player = jobs.player_session()

    if player:

        snap = player.snapshot()

        if snap:

            pos = _format_time(int(snap.position_sec * 1000))

            dur = _format_time(int(snap.duration_sec * 1000))

            state = "đang phát" if snap.is_playing else "tạm dừng"

            print(f"\n[PHÁT] {snap.novel_title} — điều khiển tại menu 3")

            print(

                f"  Ch.{snap.chapter_number} {snap.chapter_title[:35]} — "

                f"{pos}/{dur} — {snap.rate}x — {state}"

            )

    else:

        print("\n[PHÁT] (không chạy — điều khiển tại menu 3)")



    upload = jobs.upload_info() if jobs.is_upload_running() else None

    if upload:

        print(f"\n[DRIVE] Đang upload: {upload.novel_title} (id={upload.novel_id})")

        if upload.chapter_numbers is not None:

            from chapter_range import ChapterRange

            preview = ChapterRange(numbers=upload.chapter_numbers).preview()

            print(f"  Phạm vi: {preview}")

    else:

        print("\n[DRIVE] (không chạy)")





def print_config() -> None:

    print("\n=== Cấu hình hiện tại ===")

    fields = [

        ("NOVEL_URL", config.NOVEL_URL),

        ("MAX_CHAPTERS", config.MAX_CHAPTERS),

        ("DELAY_BETWEEN_CHAPTERS_MIN", config.DELAY_BETWEEN_CHAPTERS_MIN),
        ("DELAY_BETWEEN_CHAPTERS_MAX", config.DELAY_BETWEEN_CHAPTERS_MAX),
        ("CRAWL_BATCH_EVERY", config.CRAWL_BATCH_EVERY),
        ("CRAWL_BATCH_PAUSE_SEC", config.CRAWL_BATCH_PAUSE_SEC),
        ("CRAWL_403_COOLDOWN_SEC", config.CRAWL_403_COOLDOWN_SEC),
        ("CRAWL_VISIT_NOVEL_PAGE", config.CRAWL_VISIT_NOVEL_PAGE),

        ("TTS_VOICE", config.TTS_VOICE),

        ("TTS_RATE", config.TTS_RATE),

        ("TTS_CONCURRENCY", config.TTS_CONCURRENCY),

        ("BGM_PATH", config.BGM_PATH),

        ("HEADLESS", config.HEADLESS),

        ("DB_PATH", config.DB_PATH),

        ("MP3_OUTPUT_DIR", config.MP3_OUTPUT_DIR),

        ("GDRIVE_ENABLED", config.GDRIVE_ENABLED),

        ("GDRIVE_CREDENTIALS", config.GDRIVE_CREDENTIALS),

        ("GDRIVE_ROOT_FOLDER", config.GDRIVE_ROOT_FOLDER),

        ("GDRIVE_AUTO_AFTER_TTS", config.GDRIVE_AUTO_AFTER_TTS),

    ]

    for name, value in fields:

        print(f"  {name}: {value}")

    print("\nNhấn Enter để dùng giá trị mặc định khi được hỏi trong menu.")





def print_chapters_with_mp3(chapters: list[Chapter]) -> None:

    playable = [ch for ch in chapters if ch.mp3_path]

    if not playable:

        print("Chưa có chương nào có MP3.")

        return

    print(f"\n{'STT':>3} | {'#':>4} | Tiêu đề")

    print("-" * 50)

    for i, ch in enumerate(playable, start=1):

        print(f"{i:>3} | {ch.chapter_number:>4} | {ch.title[:40]}")

