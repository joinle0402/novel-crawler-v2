"""Parser vị trí bắt đầu nghe — tách khỏi chapter_range (cào/TTS)."""

from __future__ import annotations

import re
from dataclasses import dataclass


class PlayStartError(ValueError):
    """Input vị trí nghe không hợp lệ."""


@dataclass(frozen=True)
class PlayStart:
    """Chương và giây bắt đầu; use_saved=True nghĩa là lấy từ playback_state."""

    chapter_number: int
    position_sec: float = 0.0
    use_saved: bool = False


def _parse_time_token(token: str) -> float:
    """Parse 170, 170s, 2:50, 2:50s, 0s → giây."""
    token = token.strip().lower()
    if not token:
        return 0.0

    if token.endswith("s") and token[:-1].isdigit():
        return float(int(token[:-1]))

    if token.isdigit():
        return float(int(token))

    m = re.fullmatch(r"(\d+):(\d{1,2})(?:s)?", token)
    if m:
        minutes, seconds = int(m.group(1)), int(m.group(2))
        if seconds >= 60:
            raise PlayStartError(f"Giây không hợp lệ: {token!r}")
        return float(minutes * 60 + seconds)

    raise PlayStartError(
        f"Thời gian không hợp lệ: {token!r}. Dùng: 0s | 170 | 2:50 | 2:50s"
    )


def parse_play_start(
    text: str,
    *,
    saved_chapter: int | None = None,
    saved_sec: float = 0.0,
    min_chapter: int = 1,
    max_chapter: int,
) -> PlayStart:
    """
    Parse ô nhập nghe.

    - Enter (rỗng) + có saved → tiếp tục đã lưu
    - Enter (rỗng) + không saved → chương min_chapter @ 0s
    - ``50`` → chương 50 @ 0s
    - ``50 2:50`` / ``50 0s`` / ``50 170`` → chương 50 @ thời điểm tương ứng
    """
    raw = text.strip()
    if not raw:
        if saved_chapter is not None:
            return PlayStart(
                chapter_number=saved_chapter,
                position_sec=saved_sec,
                use_saved=True,
            )
        return PlayStart(chapter_number=min_chapter, position_sec=0.0)

    parts = raw.split(None, 1)
    chapter_token = parts[0]
    if not chapter_token.isdigit():
        raise PlayStartError(
            f"Chương không hợp lệ: {chapter_token!r}. Ví dụ: 50 | 50 2:50"
        )

    chapter = int(chapter_token)
    if chapter <= 0:
        raise PlayStartError(f"Số chương phải > 0: {chapter}")
    if chapter < min_chapter:
        raise PlayStartError(f"Chương nhỏ hơn chương có MP3 đầu tiên ({min_chapter})")
    if chapter > max_chapter:
        raise PlayStartError(f"Chương vượt quá số chương có MP3 ({max_chapter})")

    position_sec = 0.0
    if len(parts) > 1:
        position_sec = _parse_time_token(parts[1])
        if position_sec < 0:
            raise PlayStartError("Thời gian không được âm.")

    return PlayStart(chapter_number=chapter, position_sec=position_sec)
