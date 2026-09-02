"""Parser phạm vi chương dùng chung cho Crawler, TTS và Player."""

from __future__ import annotations

import re
from dataclasses import dataclass

from config import MAX_CHAPTERS


class ChapterRangeError(ValueError):
    """Input phạm vi chương không hợp lệ."""


@dataclass(frozen=True)
class ChapterRange:
    """Phạm vi chương đã parse — numbers=None nghĩa là tất cả."""

    numbers: frozenset[int] | None

    @property
    def is_all(self) -> bool:
        return self.numbers is None

    def filter_numbers(self, available: list[int]) -> list[int]:
        """Lọc theo chương có sẵn, giữ thứ tự."""
        if self.is_all:
            return sorted(available)
        return sorted(n for n in available if n in self.numbers)

    def preview(self, max_available: int | None = None) -> str:
        if self.is_all:
            if max_available is not None:
                return f"all (1-{max_available}, {max_available} chương)"
            return "all (tất cả chương miễn phí)"

        nums = sorted(self.numbers)
        if not nums:
            return "(rỗng)"
        return f"{_format_compact(nums)} ({len(nums)} chương)"


def _format_compact(numbers: list[int]) -> str:
    """1,2,3,6,8 hoặc 1-3,6,8."""
    if not numbers:
        return ""
    parts: list[str] = []
    start = prev = numbers[0]
    for n in numbers[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = n
    parts.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(parts)


def default_range_text() -> str:
    """Chuỗi mặc định khi người dùng nhấn Enter."""
    if MAX_CHAPTERS is None:
        return "all"
    return f"1-{MAX_CHAPTERS}"


def range_to_text(chapter_range: ChapterRange) -> str:
    """Chuyển ChapterRange thành chuỗi CLI."""
    if chapter_range.is_all:
        return "all"
    return _format_compact(sorted(chapter_range.numbers))


def parse_chapter_range(
    text: str,
    *,
    max_available: int | None = None,
    use_default_on_empty: bool = True,
) -> ChapterRange:
    """
  Parse phạm vi chương.

  - ``10`` → chương 1-10
  - ``5-10`` → chương 5-10
  - ``1,2,3,6,8`` → các chương tương ứng
  - ``1-3,6,8`` → chương 1,2,3,6,8
  - ``all`` → tất cả
  - Enter (rỗng) → 1..MAX_CHAPTERS từ config
    """
    raw = text.strip()
    if not raw:
        if not use_default_on_empty:
            raise ChapterRangeError("Phạm vi chương không được để trống.")
        raw = default_range_text()

    if raw.lower() == "all":
        return ChapterRange(numbers=None)

    numbers: set[int] = set()
    segments = [s.strip() for s in raw.split(",") if s.strip()]
    if not segments:
        raise ChapterRangeError(f"Phạm vi không hợp lệ: {text!r}")

    for segment in segments:
        if re.fullmatch(r"\d+", segment):
            n = int(segment)
            if n <= 0:
                raise ChapterRangeError(f"Số chương phải > 0: {segment}")
            if n == int(segment) and "," not in raw and "-" not in raw and len(segments) == 1:
                # "10" nghĩa là 1-10
                numbers.update(range(1, n + 1))
            else:
                numbers.add(n)
        elif re.fullmatch(r"\d+-\d+", segment):
            a_str, b_str = segment.split("-", 1)
            a, b = int(a_str), int(b_str)
            if a <= 0 or b <= 0:
                raise ChapterRangeError(f"Số chương phải > 0: {segment}")
            if a > b:
                raise ChapterRangeError(f"Range không hợp lệ (đầu > cuối): {segment}")
            numbers.update(range(a, b + 1))
        else:
            raise ChapterRangeError(
                f"Đoạn không hợp lệ: {segment!r}. "
                "Dùng: 10 | 5-10 | 1,2,3 | 1-3,6 | all"
            )

    if not numbers:
        raise ChapterRangeError(f"Không có chương nào trong phạm vi: {text!r}")

    if max_available is not None:
        out_of_range = [n for n in numbers if n > max_available]
        if out_of_range:
            raise ChapterRangeError(
                f"Chương vượt quá số chương có sẵn ({max_available}): "
                f"{_format_compact(sorted(out_of_range))}"
            )

    return ChapterRange(numbers=frozenset(numbers))
