"""Console menu chính."""

from __future__ import annotations

from console import actions, views
from db import init_db


def run() -> None:
    init_db()
    print("Chào mừng đến Novel Crawler & TTS!")

    handlers = {
        "1": actions.action_crawl,
        "2": actions.action_tts,
        "3": actions.action_listen,
        "4": actions.action_novel_list,
        "5": actions.action_progress,
        "6": actions.action_config,
        "7": actions.action_jobs,
        "8": actions.action_upload_drive,
    }

    while True:
        views.print_main_menu()
        choice = input("Chọn: ").strip()

        if choice == "0":
            print("Tạm biệt!")
            break

        handler = handlers.get(choice)
        if handler is None:
            print("Lựa chọn không hợp lệ.")
            continue

        try:
            handler()
        except KeyboardInterrupt:
            print("\nĐã hủy thao tác.")
        except Exception as exc:
            print(f"\nLỗi: {exc}")

        print()
