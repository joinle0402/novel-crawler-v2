"""Prompts nhập liệu cho console menu."""

from __future__ import annotations

from chapter_range import ChapterRange, ChapterRangeError, parse_chapter_range
from config import NOVEL_URL


def pause(message: str = "Nhấn Enter để tiếp tục") -> None:
    input(f"\n{message}...")


def ask_url(default: str = NOVEL_URL) -> str:
    raw = input(f"URL truyện [{default}]: ").strip()
    return raw or default


def ask_chapter_range(
    *,
    max_available: int | None = None,
    prompt: str = "Phạm vi chương (Enter = mặc định)",
) -> ChapterRange:
    while True:
        raw = input(f"{prompt}: ").strip()
        try:
            return parse_chapter_range(raw, max_available=max_available)
        except ChapterRangeError as exc:
            print(f"  Lỗi: {exc}")


def ask_upload_chapter_range(
    *,
    max_available: int | None = None,
) -> ChapterRange:
    """Enter = upload tất cả chương có MP3; hoặc 5-10, 1,2,3, all..."""
    while True:
        raw = input("Phạm vi upload (Enter = tất cả có MP3): ").strip()
        try:
            if not raw:
                return ChapterRange(numbers=None)
            return parse_chapter_range(
                raw,
                max_available=max_available,
                use_default_on_empty=False,
            )
        except ChapterRangeError as exc:
            print(f"  Lỗi: {exc}")


def confirm(message: str, default_yes: bool = True) -> bool:
    hint = "Y/n" if default_yes else "y/N"
    raw = input(f"{message} [{hint}]: ").strip().lower()
    if not raw:
        return default_yes
    return raw in ("y", "yes", "có", "co")


def ask_int(prompt: str, min_val: int = 1, max_val: int | None = None) -> int:
    while True:
        raw = input(f"{prompt}: ").strip()
        if not raw.isdigit():
            print("  Nhập số nguyên dương.")
            continue
        val = int(raw)
        if val < min_val:
            print(f"  Số phải >= {min_val}.")
            continue
        if max_val is not None and val > max_val:
            print(f"  Số phải <= {max_val}.")
            continue
        return val
