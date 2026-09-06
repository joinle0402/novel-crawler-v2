"""
Crawler sangtacviet.com dùng Playwright.

Luồng:
1. Mở trình duyệt (headed)
2. Trang danh sách truyện chỉ khi CRAWL_VISIT_NOVEL_PAGE=True hoặc DB thiếu list chương
3. Người dùng giải captcha trên trang chương nếu có, nhấn Enter
4. Cào nội dung từng chương (chờ load xong)
5. Lưu SQLite, sau đó tạo MP3
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from typing import Callable, TypeVar

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, sync_playwright

from browser_stealth import (
    apply_stealth,
    build_stealth_context_kwargs,
    build_stealth_launch_kwargs,
)
from config import (
    BROWSER_CHANNEL,
    BROWSER_LOCALE,
    BROWSER_MAXIMIZED,
    BROWSER_STATE_PATH,
    BROWSER_STEALTH,
    BROWSER_TIMEZONE,
    BROWSER_TIMEOUT_MS,
    CHAPTER_FREE_STATUS,
    CHAPTER_LIST_API,
    CONTENT_RELOAD_AFTER_SEC,
    CRAWL_403_COOLDOWN_SEC,
    CRAWL_BATCH_EVERY,
    CRAWL_BATCH_PAUSE_SEC,
    CRAWL_VISIT_NOVEL_PAGE,
    DELAY_BETWEEN_CHAPTERS_MAX,
    DELAY_BETWEEN_CHAPTERS_MIN,
    HEADLESS,
    MAX_CHAPTERS,
    MAX_CONTENT_RELOADS,
    NOVEL_URL,
    SELECTORS,
)
from db import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    chapter_has_content,
    ensure_chapter_record,
    get_chapters_for_novel,
    get_latest_novel,
    get_novel_by_id,
    get_novel_by_url,
    init_db,
    update_crawl_status,
    upsert_chapter,
    upsert_novel,
)
from tts import generate_mp3_for_novel


@dataclass
class ChapterInfo:
    site_id: str
    title: str
    url: str


class CrawlForbiddenError(RuntimeError):
    """Site trả 403 Forbidden — dừng/retry thay vì reload loop."""


_FORBIDDEN_MARKERS = (
    "403 - forbidden",
    "403 forbidden",
    "access is denied",
)

# Thông báo lỗi máy chủ tạm thời — nội dung chưa sẵn sàng, cần reload
_SERVER_TEMP_ERROR_MARKERS = (
    "các bạn không cần báo lỗi này",
    "sẽ mất vài giờ để máy chủ tự khắc phục",
)


def _normalize_novel_url(url: str) -> str:
    url = url.strip()
    if not url.endswith("/"):
        url += "/"
    return url


def _chapter_url(novel_url: str, chapter_site_id: str) -> str:
    return urljoin(novel_url, f"{chapter_site_id}/")


def _parse_novel_url(novel_url: str) -> tuple[str, str]:
    """Trích h (nguồn) và bookid từ URL truyện."""
    match = re.match(
        r"https?://[^/]+/truyen/([^/]+)/[^/]+/(\d+)/?",
        novel_url,
    )
    if not match:
        raise ValueError(
            f"Không parse được URL truyện: {novel_url}\n"
            "Định dạng mong đợi: https://sangtacviet.com/truyen/{h}/1/{bookid}/"
        )
    return match.group(1), match.group(2)


def _save_browser_state(context: BrowserContext, path: str = BROWSER_STATE_PATH) -> None:
    context.storage_state(path=path)
    print(f"Đã lưu session vào {path}")


def _wait_for_user(message: str) -> None:
    print(message)
    input(">>> Nhấn Enter để tiếp tục...")


_MAX_NAV_RETRIES = 8
_T = TypeVar("_T")


def _is_navigation_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "execution context was destroyed" in msg or "context was destroyed" in msg


def _wait_page_settle(page: Page, timeout_ms: int = 15_000) -> None:
    """Chờ trang ổn định sau reload/navigate thủ công (F5, click)."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except PlaywrightError:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=5_000)
    except PlaywrightError:
        pass
    page.wait_for_timeout(500)


def _with_navigation_retry(
    page: Page,
    fn: Callable[[], _T],
    *,
    retries: int = _MAX_NAV_RETRIES,
) -> _T:
    """Gọi fn; nếu trang đang navigate thì chờ rồi thử lại thay vì crash."""
    last_exc: PlaywrightError | None = None
    for attempt in range(retries):
        try:
            return fn()
        except PlaywrightError as exc:
            if not _is_navigation_error(exc):
                raise
            last_exc = exc
            if attempt == 0:
                print("  Trang đang reload — chờ ổn định...")
            _wait_page_settle(page)
    raise last_exc  # type: ignore[misc]


def _text_or_empty(page: Page, selector: str) -> str:
    def _read() -> str:
        el = page.query_selector(selector)
        if not el:
            return ""
        return el.inner_text().strip()

    return _with_navigation_retry(page, _read)


def _select_vietnamese(page: Page) -> None:
    """Tự chọn Tiếng Việt nếu modal ngôn ngữ xuất hiện."""

    def _select() -> None:
        option = page.query_selector(SELECTORS["language_option"])
        if option and option.is_visible():
            print("  Chọn ngôn ngữ: Tiếng Việt")
            option.click()
            page.wait_for_timeout(1000)

    _with_navigation_retry(page, _select)


def _get_content_box(page: Page):
    """Lấy .contentbox chứa nội dung chương (bỏ placeholder)."""

    def _find():
        box = page.query_selector(SELECTORS["content_box"])
        if box:
            return box

        for el in page.query_selector_all(".contentbox"):
            text = el.inner_text().strip()
            if SELECTORS["placeholder_text"] in text:
                continue
            if el.get_attribute("cid") or (el.get_attribute("id") or "").startswith("cld-"):
                return el
        return None

    return _with_navigation_retry(page, _find)


def _has_server_temp_error(text: str) -> bool:
    """True nếu nội dung chứa thông báo lỗi máy chủ tạm thời."""
    lower = text.lower()
    return any(marker in lower for marker in _SERVER_TEMP_ERROR_MARKERS)


def _content_ready(page: Page) -> bool:
    """True nếu contentbox đã có nội dung thật (không loading/placeholder/lỗi server tạm)."""
    if _is_forbidden_page(page):
        return False
    loading = SELECTORS["loading_text"]
    placeholder = SELECTORS["placeholder_text"]
    box = _get_content_box(page)
    if not box:
        return False
    text = box.inner_text().strip()
    if _has_server_temp_error(text):
        return False
    return bool(
        text
        and loading not in text
        and placeholder not in text
        and len(text) > 50
    )


def _is_forbidden_page(page: Page) -> bool:
    """True nếu trang là 403 Forbidden (IIS/WAF)."""

    def _check() -> bool:
        try:
            title = (page.title() or "").lower()
            if "403" in title and "forbidden" in title:
                return True
        except PlaywrightError:
            pass
        try:
            body = (page.inner_text("body") or "")[:3000].lower()
            if any(marker in body for marker in _FORBIDDEN_MARKERS):
                return True
        except PlaywrightError:
            pass
        return False

    return _with_navigation_retry(page, _check)


def _raise_if_forbidden(page: Page) -> None:
    if _is_forbidden_page(page):
        raise CrawlForbiddenError(
            "403 Forbidden — site từ chối truy cập (rate-limit / session bị flag)."
        )


def _recover_from_forbidden(page: Page, context: BrowserContext, url: str) -> None:
    """Chờ cooldown + user, thử lại — không reload loop."""
    print(f"\n  [403] Site chặn truy cập — chờ {CRAWL_403_COOLDOWN_SEC}s (không F5 tự động)...")
    time.sleep(CRAWL_403_COOLDOWN_SEC)
    _wait_for_user(
        "403 Forbidden. Trên trình duyệt: thử mở lại trang, giải captcha nếu có,\n"
        "hoặc đợi vài phút rồi quay lại terminal."
    )
    _save_browser_state(context)
    page.goto(url, wait_until="domcontentloaded")
    _wait_page_settle(page)
    _select_vietnamese(page)
    if _is_forbidden_page(page):
        raise CrawlForbiddenError(
            "Vẫn 403 sau khi thử lại — dừng cào. Chạy lại sau vài giờ hoặc --chapter-range nhỏ hơn."
        )


def _goto_chapter(page: Page, context: BrowserContext, url: str) -> None:
    """Mở URL chương; phát hiện 403 và recovery một lần."""
    if page.url.rstrip("/") == url.rstrip("/"):
        try:
            _raise_if_forbidden(page)
        except CrawlForbiddenError:
            _recover_from_forbidden(page, context, url)
        return

    page.goto(url, wait_until="domcontentloaded")
    _wait_page_settle(page)
    _select_vietnamese(page)
    if _is_forbidden_page(page):
        _recover_from_forbidden(page, context, url)


def _sleep_between_chapters(chapters_saved: int) -> None:
    """Delay ngẫu nhiên + nghỉ batch định kỳ."""
    lo = min(DELAY_BETWEEN_CHAPTERS_MIN, DELAY_BETWEEN_CHAPTERS_MAX)
    hi = max(DELAY_BETWEEN_CHAPTERS_MIN, DELAY_BETWEEN_CHAPTERS_MAX)
    delay = random.uniform(lo, hi)
    print(f"  Nghỉ {delay:.1f}s trước chương tiếp...")
    time.sleep(delay)
    if (
        CRAWL_BATCH_EVERY > 0
        and chapters_saved > 0
        and chapters_saved % CRAWL_BATCH_EVERY == 0
    ):
        print(
            f"  Nghỉ batch {CRAWL_BATCH_PAUSE_SEC}s sau {chapters_saved} chương đã cào..."
        )
        time.sleep(CRAWL_BATCH_PAUSE_SEC)


def _wait_for_content(page: Page) -> None:
    """Chờ nội dung chương; sau ~45s vẫn loading thì F5 reload, tối đa MAX_CONTENT_RELOADS lần."""
    loading = SELECTORS["loading_text"]

    for reload_attempt in range(MAX_CONTENT_RELOADS + 1):
        deadline = time.time() + CONTENT_RELOAD_AFTER_SEC
        while time.time() < deadline:
            try:
                if _is_forbidden_page(page):
                    raise CrawlForbiddenError("403 Forbidden khi chờ nội dung chương.")
                if _content_ready(page):
                    return
                box = _get_content_box(page)
                if box:
                    box_text = box.inner_text()
                    if _has_server_temp_error(box_text):
                        pass  # lỗi máy chủ tạm thời — chờ reload
                    elif loading in box_text:
                        pass  # vẫn đang tải — tiếp tục poll
            except PlaywrightError as exc:
                if not _is_navigation_error(exc):
                    raise
                print("  Trang đang reload (F5/thao tác tay) — chờ...")
                _wait_page_settle(page)
            time.sleep(0.5)

        if _is_forbidden_page(page):
            raise CrawlForbiddenError("403 Forbidden — bỏ qua reload.")

        if reload_attempt < MAX_CONTENT_RELOADS:
            # Kiểm tra lý do reload để log phù hợp
            _box = _get_content_box(page)
            _box_text = _box.inner_text() if _box else ""
            if _has_server_temp_error(_box_text):
                print(f"  Máy chủ đang lỗi tạm thời — chờ 3s rồi reload (lần {reload_attempt + 1})...")
                time.sleep(3)
            else:
                print(f"  Vẫn thấy '{loading}' — reload trang (lần {reload_attempt + 1})...")
            page.reload(wait_until="domcontentloaded")
            _wait_page_settle(page)
            _select_vietnamese(page)
        else:
            raise TimeoutError(
                f"Hết thời gian chờ nội dung chương sau {MAX_CONTENT_RELOADS} lần reload."
            )


def _progress_pct(current: int, total: int) -> int:
    if total <= 0:
        return 100
    return int(current * 100 / total)


def _chapters_from_db(novel_url: str, db_chapters) -> list[ChapterInfo]:
    return [
        ChapterInfo(
            site_id=ch.chapter_site_id,
            title=ch.title,
            url=_chapter_url(novel_url, ch.chapter_site_id),
        )
        for ch in db_chapters
    ]


def _needs_chapter_list_api(
    novel_id: int,
    required_numbers: set[int] | None,
) -> bool:
    """Gọi API khi DB thiếu record cho bất kỳ chương nào trong phạm vi."""
    existing = get_chapters_for_novel(novel_id)
    if not existing:
        return True
    if required_numbers is None:
        return False
    have = {ch.chapter_number for ch in existing}
    return not required_numbers.issubset(have)


def _resolve_chapter_numbers(
    max_chapters: int | None,
    chapter_numbers: frozenset[int] | None,
) -> set[int] | None:
    """None = tất cả chương miễn phí."""
    if chapter_numbers is not None:
        return set(chapter_numbers)
    if max_chapters is not None:
        return set(range(1, max_chapters + 1))
    return None


def _extract_chapter_content(page: Page) -> str:
    """Lấy nội dung tiếng Việt từ .contentbox[cid], bỏ dòng hệ thống màu xám."""

    def _extract() -> str:
        return page.evaluate(
        """() => {
            const placeholder = 'Đọc trên web';
            const boxes = [...document.querySelectorAll('.contentbox')];
            const box = boxes.find(b => b.hasAttribute('cid'))
                || boxes.find(b => b.id && b.id.startsWith('cld-'))
                || boxes.find(b => !b.innerText.includes(placeholder));
            if (!box) return '';

            const clone = box.cloneNode(true);
            clone.querySelectorAll('span[style*="color:gray"], span[style*="color: grey"]')
                .forEach(el => el.remove());
            clone.querySelectorAll('i[id^="exran"]').forEach(el => el.remove());

            const parts = [];
            const walk = (node) => {
                if (node.nodeType === Node.TEXT_NODE) {
                    const t = node.textContent.trim();
                    if (t) parts.push(t);
                } else if (node.nodeName === 'I') {
                    const t = node.textContent.trim();
                    if (t) parts.push(t);
                } else if (node.nodeName === 'BR') {
                    parts.push('\\n');
                } else {
                    node.childNodes.forEach(walk);
                }
            };
            walk(clone);

            let text = parts.join(' ');
            text = text.replace(/\\s+\\n/g, '\\n').replace(/\\n\\s+/g, '\\n');
            text = text.replace(/ {2,}/g, ' ');
            return text.trim();
        }"""
        )

    result = _with_navigation_retry(page, _extract)
    if _has_server_temp_error(result):
        raise RuntimeError(
            "Nội dung chương chứa thông báo lỗi máy chủ tạm thời — cần reload lại."
        )
    return result


def _parse_chapter_list_data(raw: str, novel_url: str) -> tuple[list[ChapterInfo], int]:
    """
    Parse chuỗi API: '1-/-34902840-/- Thứ 1 chương ... -//-1-/-34902841-/- ...'
    Chỉ lấy chương có status = CHAPTER_FREE_STATUS (miễn phí).
    """
    chapters: list[ChapterInfo] = []
    vip_count = 0

    for entry in raw.split("-//-"):
        entry = entry.strip()
        if not entry:
            continue

        parts = entry.split("-/-", 2)
        if len(parts) < 3:
            continue

        status, site_id, title = parts[0].strip(), parts[1].strip(), parts[2].strip()
        title = re.sub(r"\s+", " ", title)

        if status != CHAPTER_FREE_STATUS:
            vip_count += 1
            continue

        chapters.append(
            ChapterInfo(
                site_id=site_id,
                title=title,
                url=_chapter_url(novel_url, site_id),
            )
        )

    return chapters, vip_count


def _get_chapter_list(page: Page, novel_url: str) -> tuple[list[ChapterInfo], int]:
    """Lấy danh sách chương miễn phí qua API getchapterlist (trong context trình duyệt)."""
    h, bookid = _parse_novel_url(novel_url)
    api_url = CHAPTER_LIST_API.format(h=h, bookid=bookid)

    print(f"Gọi API danh sách chương: h={h}, bookid={bookid}")

    # Gọi fetch trong trang để dùng đúng cookie/session sau captcha
    result = page.evaluate(
        """async (url) => {
            const resp = await fetch(url, {
                method: 'GET',
                credentials: 'include',
                headers: {
                    'X-Requested-With': 'XMLHttpRequest',
                },
            });
            return { status: resp.status, text: await resp.text() };
        }""",
        api_url,
    )

    status = result.get("status", 0)
    body = (result.get("text") or "").strip()
    if status != 200:
        raise RuntimeError(f"API chapter list HTTP {status}: {api_url}")

    if not body:
        raise RuntimeError(
            "API chapter list trả về rỗng. Có thể cần giải captcha lại trên trình duyệt."
        )

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        preview = body[:300].replace("\n", " ")
        raise RuntimeError(
            f"API không trả JSON (có thể bị chặn/captcha). "
            f"Preview: {preview!r}"
        ) from exc

    if payload.get("code") != 1:
        raise RuntimeError(f"API chapter list lỗi: {payload}")

    raw = payload.get("data", "")
    if not raw:
        raise RuntimeError("API chapter list trả về data rỗng.")

    return _parse_chapter_list_data(raw, novel_url)


def _get_chapter_list_with_retry(
    page: Page,
    context: BrowserContext,
    novel_url: str,
) -> tuple[list[ChapterInfo], int]:
    """Gọi API chapter list; nếu session hết hạn thì nhờ user giải captcha lại."""
    try:
        return _get_chapter_list(page, novel_url)
    except RuntimeError as exc:
        print(f"  API lỗi: {exc}")
        _wait_for_user(
            "Session có thể đã hết hạn. Giải captcha trên trình duyệt nếu có,\n"
            "đợi trang load xong, rồi quay lại terminal."
        )
        _save_browser_state(context)
        return _get_chapter_list(page, novel_url)


def _wait_for_content_with_captcha(
    page: Page,
    context: BrowserContext,
    chapter_url: str,
) -> None:
    """Chờ nội dung; 403 → recovery; hết retry reload → nhờ user giải captcha."""
    try:
        _wait_for_content(page)
    except CrawlForbiddenError:
        _recover_from_forbidden(page, context, chapter_url)
        _wait_for_content(page)
    except TimeoutError:
        _select_vietnamese(page)
        _wait_for_user(
            "Không load được nội dung sau khi reload. "
            "Giải captcha trên trình duyệt nếu có, rồi quay lại terminal."
        )
        _save_browser_state(context)
        _wait_for_content(page)


def _status_sort_key(status: str) -> int:
    if status == STATUS_FAILED:
        return 0
    if status == STATUS_PENDING:
        return 1
    return 2


def sort_chapters_for_crawl_keyed(
    items: list[tuple[int, ChapterInfo, object | None]],
) -> list[tuple[int, ChapterInfo, object | None]]:
    def key(item: tuple[int, ChapterInfo, object | None]) -> tuple[int, int]:
        _idx, _ch, existing = item
        status = getattr(existing, "crawl_status", STATUS_PENDING) if existing else STATUS_PENDING
        return (_status_sort_key(status), _idx)

    return sorted(items, key=key)


def crawl_novel(
    novel_url: str | None = None,
    max_chapters: int | None = None,
    chapter_numbers: frozenset[int] | None = None,
    run_tts: bool = True,
    novel_id: int | None = None,
    tts_chapter_numbers: frozenset[int] | None = None,
) -> int:
    if novel_id is not None:
        novel = get_novel_by_id(novel_id)
        if not novel:
            raise ValueError(f"Không tìm thấy novel id={novel_id} trong DB.")
        db_url = _normalize_novel_url(novel.url)
        if novel_url:
            if _normalize_novel_url(novel_url) != db_url:
                raise ValueError("URL không khớp với --novel-id.")
        else:
            novel_url = novel.url

    novel_url = _normalize_novel_url(novel_url or NOVEL_URL)
    if "sangtacviet.com" not in novel_url:
        raise ValueError("URL phải thuộc sangtacviet.com")

    if chapter_numbers is None and max_chapters is None:
        max_chapters = MAX_CHAPTERS

    required_numbers = _resolve_chapter_numbers(max_chapters, chapter_numbers)

    init_db()
    print(f"URL truyện: {novel_url}")
    if chapter_numbers is not None:
        nums = sorted(chapter_numbers)
        print(f"Phạm vi chương: {nums[0]}-{nums[-1]} ({len(nums)} chương)")
    else:
        print(f"Giới hạn chương: {max_chapters or 'tất cả chương miễn phí'}")

    state_path = Path(BROWSER_STATE_PATH)
    context_kwargs: dict = {}
    if state_path.is_file():
        print(f"Đang load session từ {BROWSER_STATE_PATH}...")
        context_kwargs["storage_state"] = str(state_path)

    with sync_playwright() as p:
        launch_args = build_stealth_launch_kwargs(
            {"headless": HEADLESS},
            channel=BROWSER_CHANNEL,
        )
        if BROWSER_MAXIMIZED:
            launch_args["args"] = ["--start-maximized"]
        browser = p.chromium.launch(**launch_args)
        context_kwargs_browser = build_stealth_context_kwargs(
            dict(context_kwargs),
            locale=BROWSER_LOCALE,
            timezone=BROWSER_TIMEZONE,
        )
        if BROWSER_MAXIMIZED:
            context_kwargs_browser["no_viewport"] = True
        context = browser.new_context(**context_kwargs_browser)
        page = context.new_page()
        if BROWSER_STEALTH:
            print("Đang bật stealth (anti-bot)...")
        apply_stealth(
            context,
            page,
            enabled=BROWSER_STEALTH,
            locale=BROWSER_LOCALE,
            headless=HEADLESS,
            channel=BROWSER_CHANNEL,
        )
        page.set_default_timeout(BROWSER_TIMEOUT_MS)

        # Metadata từ DB nếu đã có (tránh mở trang danh sách khi không cần)
        title = ""
        author = ""
        summary = ""
        if novel_id is None:
            existing_novel = get_novel_by_url(novel_url)
            if existing_novel:
                novel_id = existing_novel.id
                title = existing_novel.title or ""
                author = existing_novel.author or ""
                summary = existing_novel.summary or ""
        else:
            existing_novel = get_novel_by_id(novel_id)
            if existing_novel:
                title = existing_novel.title or ""
                author = existing_novel.author or ""
                summary = existing_novel.summary or ""

        need_chapter_list_api = (
            novel_id is None
            or _needs_chapter_list_api(novel_id, required_numbers)
        )
        # False = thẳng trang chương nếu DB đủ list; vẫn mở danh sách khi thiếu list
        visit_novel_page = CRAWL_VISIT_NOVEL_PAGE or need_chapter_list_api

        if visit_novel_page:
            reason = (
                "thiếu danh sách chương trong DB"
                if need_chapter_list_api
                else "CRAWL_VISIT_NOVEL_PAGE=True"
            )
            print(f"Đang mở trang truyện (đọc metadata) — {reason}...")
            page.goto(novel_url, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)

            title = _text_or_empty(page, SELECTORS["book_title"]) or title
            author = _text_or_empty(page, SELECTORS["book_author"]) or author
            summary = _text_or_empty(page, SELECTORS["book_summary"]) or summary

            novel_id = upsert_novel(
                novel_url,
                title or "Đang tải...",
                author,
                summary,
            )
        else:
            assert novel_id is not None
            print(
                "Bỏ qua trang danh sách (CRAWL_VISIT_NOVEL_PAGE=False) "
                "— dùng metadata + list chương từ DB."
            )
            novel_id = upsert_novel(
                novel_url,
                title or "Đang tải...",
                author,
                summary,
            )

        vip_count = 0
        if need_chapter_list_api:
            chapters, vip_count = _get_chapter_list_with_retry(page, context, novel_url)
            if not chapters:
                raise RuntimeError(
                    "Không tìm thấy chương miễn phí từ API. "
                    "Kiểm tra captcha hoặc URL truyện."
                )
            print(
                f"Tìm thấy {len(chapters)} chương miễn phí, bỏ qua {vip_count} chương VIP."
            )
            for idx, ch in enumerate(chapters, start=1):
                ensure_chapter_record(novel_id, ch.site_id, idx, ch.title)
        else:
            db_chapters = get_chapters_for_novel(novel_id)
            chapters = _chapters_from_db(novel_url, db_chapters)
            print(f"Dùng danh sách chương từ DB ({len(chapters)} chương), bỏ qua API.")

        if max_chapters is not None and chapter_numbers is None:
            chapters = chapters[:max_chapters]
            print(f"Chỉ cào {len(chapters)} chương đầu (max_chapters={max_chapters}).")

        if required_numbers is not None:
            chapters = [ch for idx, ch in enumerate(chapters, start=1) if idx in required_numbers]
            if not chapters:
                raise RuntimeError("Không có chương nào trong phạm vi đã chọn.")
            print(f"Lọc {len(chapters)} chương trong phạm vi.")

        if not chapters:
            raise RuntimeError("Không có chương để cào.")

        first_ch = chapters[0]
        print(f"Đang mở trang nội dung: {first_ch.title}")
        page.goto(first_ch.url, wait_until="domcontentloaded")
        _wait_page_settle(page)
        _select_vietnamese(page)

        _wait_for_user(
            "Trình duyệt đã mở trang nội dung chương. Giải captcha nếu có,\n"
            "đợi nội dung load xong, rồi quay lại terminal."
        )
        _save_browser_state(context)

        if not title or title == "Đang tải...":
            print("Đang mở trang truyện để bổ sung tên / metadata...")
            page.goto(novel_url, wait_until="domcontentloaded")
            _wait_page_settle(page)
            title = _text_or_empty(page, SELECTORS["book_title"])
            author = _text_or_empty(page, SELECTORS["book_author"])
            summary = _text_or_empty(page, SELECTORS["book_summary"])
            if title:
                novel_id = upsert_novel(novel_url, title, author, summary)

        if not title:
            _wait_for_user(
                "Không đọc được tên truyện. Giải captcha nếu cần, rồi quay lại terminal."
            )
            _save_browser_state(context)
            page.goto(novel_url, wait_until="domcontentloaded")
            title = _text_or_empty(page, SELECTORS["book_title"])
            if not title:
                raise RuntimeError("Không đọc được tên truyện. Kiểm tra captcha hoặc selector.")

        print(f"Truyện: {title}")
        print(f"Tác giả: {author or '(không rõ)'}")
        novel_id = upsert_novel(novel_url, title, author, summary)

        existing_by_site_id = {ch.chapter_site_id: ch for ch in get_chapters_for_novel(novel_id)}

        work_items: list[tuple[int, ChapterInfo, object | None]] = []
        for idx, ch in enumerate(chapters, start=1):
            existing = existing_by_site_id.get(ch.site_id)
            work_items.append((idx, ch, existing))

        work_items = sort_chapters_for_crawl_keyed(work_items)
        total = len(work_items)
        skip_count = 0
        interrupted = False
        chapters_saved = 0

        try:
            for pos, (idx, ch, existing) in enumerate(work_items, start=1):
                pct = _progress_pct(pos, total)

                if existing and existing.crawl_status == STATUS_COMPLETED:
                    skip_count += 1
                    print(f"\n[{pos}/{total}] ({pct}%) {ch.title} — đã có, skip")
                    continue

                if existing and chapter_has_content(existing.content):
                    skip_count += 1
                    print(f"\n[{pos}/{total}] ({pct}%) {ch.title} — đã có, skip")
                    continue

                print(f"\n[{pos}/{total}] ({pct}%) {ch.title}")
                print(f"  -> {ch.url}")

                _goto_chapter(page, context, ch.url)

                _wait_for_content_with_captcha(page, context, ch.url)

                content = _extract_chapter_content(page)
                if not content or SELECTORS["loading_text"] in content or _has_server_temp_error(content):
                    if _has_server_temp_error(content):
                        print("  Nội dung lỗi máy chủ tạm thời — chờ reload lại...")
                        _wait_for_content_with_captcha(page, context, ch.url)
                    else:
                        _wait_for_user("Nội dung chưa load. Giải captcha nếu cần.")
                        _save_browser_state(context)
                        _wait_for_content_with_captcha(page, context, ch.url)
                    content = _extract_chapter_content(page)

                if not content:
                    print("  Bỏ qua: không có nội dung.")
                    rec = existing_by_site_id.get(ch.site_id)
                    if rec:
                        update_crawl_status(rec.id, STATUS_FAILED)
                    continue

                upsert_chapter(novel_id, ch.site_id, idx, ch.title, content)
                chapters_saved += 1
                print(f"  Đã lưu ({len(content)} ký tự)")

                if pos < total:
                    _sleep_between_chapters(chapters_saved)
        except CrawlForbiddenError as exc:
            print(f"\n[LỖI 403] {exc}")
            print("Tiến độ đã lưu — chạy lại sau vài giờ hoặc dùng --chapter-range nhỏ hơn.")
        except KeyboardInterrupt:
            interrupted = True
            print("\n\nĐã dừng cào (Ctrl+C). Tiến độ đã lưu — chạy lại để resume.")
        finally:
            browser.close()

        if skip_count:
            print(f"\nĐã bỏ qua {skip_count}/{total} chương (đã có nội dung trong DB).")
        if interrupted:
            return novel_id

    if run_tts:
        print("\n=== Cào xong. Bắt đầu tạo MP3 ===")
        tts_range = tts_chapter_numbers if tts_chapter_numbers is not None else chapter_numbers
        mp3_files = generate_mp3_for_novel(
            novel_id,
            title,
            chapter_numbers=tts_range,
        )
        if mp3_files:
            print(f"Đã tạo {len(mp3_files)} file MP3 trong thư mục output/mp3/")
    else:
        print("\n=== Cào xong (bỏ qua TTS) ===")

    print("Hoàn tất!")
    return novel_id


def run_tts_only(
    novel_id: int | None = None,
    chapter_numbers: frozenset[int] | None = None,
    voice: str | None = None,
    rate: str | None = None,
    *,
    quiet: bool = False,
) -> None:
    """TTS từ DB, không mở browser."""
    init_db()
    if novel_id is not None:
        novel = get_novel_by_id(novel_id)
        if not novel:
            raise ValueError(f"Không tìm thấy novel id={novel_id} trong DB.")
    else:
        novel = get_latest_novel()
        if not novel:
            raise RuntimeError("DB trống — chưa có truyện nào.")

    if not quiet:
        print(f"Truyện: {novel.title} (id={novel.id})")
    mp3_files = generate_mp3_for_novel(
        novel.id,
        novel.title,
        chapter_numbers=chapter_numbers,
        voice=voice,
        rate=rate,
        quiet=quiet,
    )
    if mp3_files and not quiet:
        print(f"Đã tạo {len(mp3_files)} file MP3 trong thư mục output/mp3/")
    if not quiet:
        print("Hoàn tất!")


def _configure_console_utf8() -> None:
    """Tránh UnicodeEncodeError khi in tiếng Việt trên Windows (cp1252/cp1258)."""
    if sys.platform != "win32":
        return
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _pause_crawl_window() -> None:
    """Giữ cửa sổ crawl mở khi spawn từ menu (CREATE_NEW_CONSOLE trên Windows)."""
    if sys.platform != "win32":
        return
    if "--crawl-only" not in sys.argv:
        return
    try:
        input("\n>>> Nhấn Enter để đóng cửa sổ...")
    except (EOFError, KeyboardInterrupt):
        pass


def _write_crawl_error_log(exc: BaseException) -> Path:
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / "crawl_latest.log"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n--- {datetime.now(timezone.utc).isoformat()} ---\n")
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
    return log_path


def main() -> None:
    _configure_console_utf8()

    parser = argparse.ArgumentParser(
        description="Crawler truyện sangtacviet.com — cào nội dung + TTS MP3.",
    )
    parser.add_argument("--url", help="URL truyện (override NOVEL_URL trong config)")
    parser.add_argument("--crawl-only", action="store_true", help="Chỉ cào, không gọi TTS")
    parser.add_argument("--tts-only", action="store_true", help="Chỉ TTS từ DB, không mở browser")
    parser.add_argument("--novel-id", type=int, help="ID truyện trong DB")
    parser.add_argument(
        "--max-chapters",
        type=int,
        default=None,
        help="Giới hạn số chương (override MAX_CHAPTERS; None = hết miễn phí)",
    )
    parser.add_argument(
        "--chapter-range",
        type=str,
        default=None,
        help='Phạm vi chương: "1-10", "all", "1,2,3" (override --max-chapters)',
    )
    args = parser.parse_args()

    chapter_numbers: frozenset[int] | None = None
    max_chapters = args.max_chapters
    if args.chapter_range:
        from chapter_range import parse_chapter_range

        chapter_numbers = parse_chapter_range(args.chapter_range).numbers
        max_chapters = None

    if args.crawl_only and args.tts_only:
        parser.error("Không dùng đồng thời --crawl-only và --tts-only.")

    if args.tts_only:
        print("=== Mode: TTS only ===")
        run_tts_only(args.novel_id)
        return

    if args.crawl_only:
        print("=== Mode: Crawl only ===")
    else:
        print("=== Mode: Crawl + TTS ===")

    exit_code = 0
    try:
        crawl_novel(
            novel_url=args.url,
            max_chapters=max_chapters,
            chapter_numbers=chapter_numbers,
            run_tts=not args.crawl_only,
            novel_id=args.novel_id,
        )
    except KeyboardInterrupt:
        print("\n\nĐã dừng (Ctrl+C).")
        exit_code = 130
    except Exception as exc:
        print(f"\n[LỖI CÀO] {exc}", flush=True)
        traceback.print_exc()
        log_path = _write_crawl_error_log(exc)
        print(f"\nChi tiết lỗi cũng ghi vào: {log_path}", flush=True)
        exit_code = 1
    finally:
        _pause_crawl_window()

    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
