"""Phát MP3 bằng VLC — pause/resume, queue, lưu vị trí, session nền."""

from __future__ import annotations

import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from db import Chapter, get_playback_state, save_playback_state

try:
    import vlc
except ImportError as exc:
    raise ImportError(
        "Cần cài python-vlc: pip install python-vlc\n"
        "Và cài VLC media player: winget install VideoLAN.VLC"
    ) from exc


PLAYBACK_SPEEDS = (1.0, 1.25, 1.5, 2.0)


@dataclass
class PlayItem:
    chapter: Chapter
    mp3_path: Path


_HELP = """
Điều khiển:
  p / Space  — pause / resume
  n          — chương tiếp theo
  b          — chương trước
  s          — dừng chương hiện tại
  q          — thoát player
  h          — hiện trợ giúp
"""


def _read_key() -> str:
    """Đọc một phím (Windows)."""
    if sys.platform == "win32":
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            msvcrt.getwch()
            return ""
        return ch.lower()
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ch.lower()


def _format_time(ms: int) -> str:
    sec = max(0, ms // 1000)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


class VlcPlayer:
    def __init__(self, novel_id: int) -> None:
        self.novel_id = novel_id
        self._instance = vlc.Instance("--no-video")
        self._player = self._instance.media_player_new()
        self._current: PlayItem | None = None
        self._last_save = 0.0
        self._rate = 1.5

    def _save_position(self, force: bool = False) -> None:
        if not self._current:
            return
        now = time.time()
        if not force and now - self._last_save < 2.0:
            return
        pos_ms = self._player.get_time()
        if pos_ms < 0:
            pos_ms = 0
        save_playback_state(
            self.novel_id,
            self._current.chapter.chapter_number,
            pos_ms / 1000.0,
        )
        self._last_save = now

    def play_item(self, item: PlayItem, start_sec: float = 0) -> None:
        self._save_position(force=True)
        self._current = item
        media = self._instance.media_new(str(item.mp3_path))
        self._player.set_media(media)
        self._player.play()
        time.sleep(0.3)
        self._player.set_rate(self._rate)
        if start_sec > 0:
            self._player.set_time(int(start_sec * 1000))
        print(
            f"\n▶ [{item.chapter.chapter_number:04d}] {item.chapter.title}\n"
            f"   {item.mp3_path.name}",
            flush=True,
        )

    def set_rate(self, rate: float) -> None:
        self._rate = rate
        self._player.set_rate(rate)

    def get_rate(self) -> float:
        return self._rate

    def pause_toggle(self) -> None:
        self._player.pause()

    def is_playing(self) -> bool:
        return self._player.is_playing() == 1

    def wait_until_end_or_key(self) -> str:
        """Chờ hết file hoặc phím. Trả về '' nếu hết file, hoặc ký tự phím."""
        while True:
            state = self._player.get_state()
            if state in (vlc.State.Ended, vlc.State.Stopped):
                self._save_position(force=True)
                return ""

            if msvcrt_kbhit():
                key = _read_key()
                if key:
                    return key

            self._save_position()
            time.sleep(0.25)

    def poll_end(self) -> bool:
        """True nếu file hiện tại đã phát xong."""
        state = self._player.get_state()
        if state in (vlc.State.Ended, vlc.State.Stopped):
            self._save_position(force=True)
            return True
        self._save_position()
        return False

    def stop(self) -> None:
        self._save_position(force=True)
        self._player.stop()

    def get_position_sec(self) -> float:
        t = self._player.get_time()
        return max(0.0, t / 1000.0) if t >= 0 else 0.0

    def get_duration_sec(self) -> float:
        length = self._player.get_length()
        return max(0.0, length / 1000.0) if length >= 0 else 0.0

    @property
    def current_item(self) -> PlayItem | None:
        return self._current

    def release(self) -> None:
        self._save_position(force=True)
        self._player.stop()
        self._player.release()


def msvcrt_kbhit() -> bool:
    if sys.platform == "win32":
        import msvcrt

        return msvcrt.kbhit()
    import select

    return bool(select.select([sys.stdin], [], [], 0)[0])


@dataclass
class PlayerSnapshot:
    novel_id: int
    novel_title: str
    chapter_number: int
    chapter_title: str
    position_sec: float
    duration_sec: float
    rate: float
    is_playing: bool
    queue_index: int
    queue_total: int


class PlayerSession:
    """Phát queue MP3 trong thread nền — điều khiển qua màn nghe (menu 3)."""

    def __init__(
        self,
        novel_id: int,
        novel_title: str,
        items: list[PlayItem],
        *,
        resume: bool = False,
        start_chapter_number: int | None = None,
        start_sec: float = 0.0,
    ) -> None:
        self.novel_id = novel_id
        self.novel_title = novel_title
        self.items = items
        self._resume = resume
        self._start_chapter_number = start_chapter_number
        self._start_sec = start_sec
        self._lock = threading.Lock()
        self._cmd_queue: queue.Queue[str] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._player: VlcPlayer | None = None
        self._index = 0
        self._start_sec = 0.0
        self._stopped = False

    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_active():
            return
        self._stopped = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="PlayerSession")
        self._thread.start()
        print(
            f"[PHÁT] Bắt đầu: {self.novel_title} ({len(self.items)} chương).",
            flush=True,
        )

    def _resolve_start(self) -> None:
        self._index = 0
        seek_sec = 0.0

        if self._start_chapter_number is not None:
            for i, item in enumerate(self.items):
                if item.chapter.chapter_number == self._start_chapter_number:
                    self._index = i
                    seek_sec = self._start_sec
                    break
        elif self._resume:
            state = get_playback_state(self.novel_id)
            if state:
                for i, item in enumerate(self.items):
                    if item.chapter.chapter_number == state.chapter_number:
                        self._index = i
                        seek_sec = state.position_sec
                        break

        self._start_sec = seek_sec

    def _run(self) -> None:
        self._resolve_start()
        player = VlcPlayer(self.novel_id)
        self._player = player
        index = self._index
        start_sec = self._start_sec

        try:
            while not self._stopped and 0 <= index < len(self.items):
                self._index = index
                item = self.items[index]
                seek = start_sec if index == self._index else 0.0
                start_sec = 0.0
                player.play_item(item, start_sec=seek)

                while not self._stopped:
                    new_index = self._drain_commands(player, index)
                    if new_index != index:
                        index = new_index
                        break

                    if player.poll_end():
                        index += 1
                        break

                    time.sleep(0.25)
        except Exception as exc:
            print(f"[PHÁT] Lỗi: {exc}", flush=True)
        finally:
            player.release()
            self._player = None
            print("[PHÁT] Đã dừng phát nền.", flush=True)

    def _drain_commands(self, player: VlcPlayer, index: int) -> int:
        while True:
            try:
                cmd = self._cmd_queue.get_nowait()
            except queue.Empty:
                break

            if cmd == "pause_toggle":
                player.pause_toggle()
            elif cmd == "next":
                player.stop()
                return index + 1
            elif cmd == "prev":
                player.stop()
                return max(0, index - 1)
            elif cmd == "stop":
                self._stopped = True
                player.stop()
                return index
            elif cmd.startswith("rate:"):
                try:
                    rate = float(cmd.split(":", 1)[1])
                    player.set_rate(rate)
                except ValueError:
                    pass
        return index

    def pause_toggle(self) -> None:
        self._cmd_queue.put("pause_toggle")

    def next_chapter(self) -> None:
        self._cmd_queue.put("next")

    def prev_chapter(self) -> None:
        self._cmd_queue.put("prev")

    def stop(self) -> None:
        self._cmd_queue.put("stop")

    def set_rate(self, rate: float) -> None:
        self._cmd_queue.put(f"rate:{rate}")

    def cycle_rate(self) -> float:
        player = self._player
        current = player.get_rate() if player else 1.0
        speeds = list(PLAYBACK_SPEEDS)
        try:
            idx = speeds.index(current)
            nxt = speeds[(idx + 1) % len(speeds)]
        except ValueError:
            nxt = 1.0
        self.set_rate(nxt)
        return nxt

    def cycle_rate_down(self) -> float:
        player = self._player
        current = player.get_rate() if player else 1.0
        speeds = list(PLAYBACK_SPEEDS)
        try:
            idx = speeds.index(current)
            nxt = speeds[(idx - 1) % len(speeds)]
        except ValueError:
            nxt = 1.0
        self.set_rate(nxt)
        return nxt

    def snapshot(self) -> PlayerSnapshot | None:
        player = self._player
        if not player or not player.current_item:
            if not self.is_active():
                return None
            return PlayerSnapshot(
                novel_id=self.novel_id,
                novel_title=self.novel_title,
                chapter_number=0,
                chapter_title="(đang khởi động)",
                position_sec=0.0,
                duration_sec=0.0,
                rate=1.0,
                is_playing=False,
                queue_index=0,
                queue_total=len(self.items),
            )

        item = player.current_item
        return PlayerSnapshot(
            novel_id=self.novel_id,
            novel_title=self.novel_title,
            chapter_number=item.chapter.chapter_number,
            chapter_title=item.chapter.title,
            position_sec=player.get_position_sec(),
            duration_sec=player.get_duration_sec(),
            rate=player.get_rate(),
            is_playing=player.is_playing(),
            queue_index=self._index,
            queue_total=len(self.items),
        )


def play_queue(
    novel_id: int,
    items: list[PlayItem],
    *,
    resume: bool = True,
) -> None:
    """Phát danh sách chương với điều khiển VLC (blocking — legacy)."""
    if not items:
        print("Không có chương nào để phát.")
        return

    start_index = 0
    start_sec = 0.0
    if resume:
        state = get_playback_state(novel_id)
        if state:
            for i, item in enumerate(items):
                if item.chapter.chapter_number == state.chapter_number:
                    start_index = i
                    start_sec = state.position_sec
                    print(
                        f"Tiếp tục từ chương {state.chapter_number}, "
                        f"vị trí {_format_time(int(state.position_sec * 1000))}"
                    )
                    break

    player = VlcPlayer(novel_id)
    index = start_index
    print(_HELP.strip())

    try:
        while 0 <= index < len(items):
            item = items[index]
            seek = start_sec if index == start_index else 0.0
            start_sec = 0.0
            player.play_item(item, start_sec=seek)

            while True:
                key = player.wait_until_end_or_key()
                if key == "":
                    index += 1
                    break
                if key in ("p", " "):
                    player.pause_toggle()
                    state_label = "tạm dừng" if not player.is_playing() else "tiếp tục"
                    print(f"  ({state_label})")
                elif key == "n":
                    player.stop()
                    index += 1
                    break
                elif key == "b":
                    player.stop()
                    index = max(0, index - 1)
                    break
                elif key == "s":
                    player.stop()
                    index += 1
                    break
                elif key == "q":
                    print("Thoát player.")
                    return
                elif key == "h":
                    print(_HELP.strip())
                else:
                    pos = player.get_position_sec()
                    dur = player.get_duration_sec()
                    print(
                        f"  {_format_time(int(pos * 1000))} / "
                        f"{_format_time(int(dur * 1000))} — ch {item.chapter.chapter_number}"
                    )
    except KeyboardInterrupt:
        print("\nDừng phát (Ctrl+C).")
    finally:
        player.release()

    print("Hết danh sách phát.")
