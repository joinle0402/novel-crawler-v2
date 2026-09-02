---
name: novel-crawler
description: >-
  Crawl truyện sangtacviet.com, console menu, job nền (TTS/cào/phát/upload Drive), resume từ SQLite,
  chapter range parser, edge-tts MP3 + progress ký tự, VLC player nền, upload MP3 Google Drive OAuth.
  Dùng khi sửa main.py, console/, crawler.py, tts.py, db.py, chapter_range.py, player.py,
  drive_upload.py, config.py hoặc user nói cào truyện, captcha, TTS, nghe MP3, upload Drive, menu, job nền.
---

# novel-crawler (sangtacviet.com)

## Kiến trúc

```
main.py → console/menu.py (vòng lặp menu, không block job nền)
              ↓
    console/jobs.py (JobManager: TTS, crawl, PlayerSession, Drive upload)
              ↓
    crawler.py / tts.py / player.py / drive_upload.py
              ↓
         novels.db → output/mp3/{title}/
              ↑
    Playwright headed (captcha tay, browser_state.json)
```

| File | Vai trò |
|------|---------|
| `main.py` | Entry point console menu |
| `console/menu.py` | Vòng lặp menu chính |
| `console/actions.py` | Handlers: cào, TTS, nghe, list, tiến độ, config, job, upload Drive |
| `console/jobs.py` | **JobManager** — TTS thread (max 1), crawl subprocess (PID), player nền, upload Drive |
| `console/prompts.py` | Nhập URL, phạm vi chương, `ask_upload_chapter_range`, confirm |
| `console/views.py` | Menu, bảng truyện, tiến độ, màn job (mục 7) |
| `chapter_range.py` | Parser phạm vi chương; `range_to_text()` cho CLI spawn |
| `crawler.py` | Logic cào Playwright; CLI `--chapter-range` |
| `tts.py` | edge-tts + chunk + BGM; progress ký tự live `[TTS]` |
| `player.py` | `PlayerSession` nền + `VlcPlayer`; tốc độ `set_rate()` |
| `drive_upload.py` | OAuth Google Drive + upload MP3 → `{GDRIVE_ROOT_FOLDER}/{tên_truyện}/` |
| `db.py` | SQLite + status + `tts_chars_*` + playback_state |
| `config.py` | Defaults (TTS, BGM, Drive, selector, timeout) |

## Console menu

```bash
python main.py
```

| Phím | Chức năng |
|------|-----------|
| `1` | Cào — spawn subprocess cửa sổ riêng, về menu ngay |
| `2` | TTS — **luôn nền** (max 1 truyện); đang chạy → chỉ xem tiến độ |
| `3` | Nghe — **luôn nền** (`PlayerSession`); điều khiển phát tại màn này (`p/n/b/+/-/s`) |
| `4` | Danh sách truyện |
| `5` | Tiến độ (DB; hiện chương `processing` + % ký tự) |
| `6` | Cấu hình |
| `7` | **Job đang chạy** — crawl PID, TTS chi tiết, Drive upload, trạng thái phát |
| `8` | **Upload MP3 lên Google Drive** — nền, hỏi phạm vi chương |
| `0` | Thoát |

**Job nền:** Menu không block. Log TTS in live `[TTS] Ch.NNNN ... ký tự (%)`; Drive in `[Drive] ...`.

## JobManager (`console/jobs.py`)

| Job | Giới hạn | Cách chạy |
|-----|----------|-----------|
| TTS | 1 truyện | `threading.Thread` → `run_tts_only()`; optional auto-upload sau TTS |
| Cào | 1 truyện | `subprocess.Popen` + `CREATE_NEW_CONSOLE` (Windows); track PID |
| Phát | 1 session | `PlayerSession` thread → VLC |
| Drive upload | 1 job | `threading.Thread` → `upload_novel_mp3_by_id()` |

- Cào đang chạy → menu 1 báo busy; resume cào **thủ công** qua CLI
- TTS đang chạy → menu 2 chỉ hiện tiến độ, không spawn job mới
- Upload đang chạy → menu 8 báo busy
- Captcha cào: pause trong **cửa sổ crawler** (`_wait_for_user`), user tự xử lý browser

## Google Drive (`drive_upload.py`)

- OAuth Desktop app: `GDRIVE_CREDENTIALS` (mặc định `client_secret.json`) → lưu `GDRIVE_TOKEN` (`token.json`)
- Scope: `drive.file` (chỉ file app tạo/sửa)
- Upload vào Drive: `{GDRIVE_ROOT_FOLDER}/{tên_truyện}/` (tên folder = `_safe_filename(title)`)
- Bỏ qua file đã có trên Drive (so theo tên file)
- Lọc chương qua DB `get_chapters_with_mp3()` + `chapter_numbers`

**Menu 8 — phạm vi upload** (`prompts.ask_upload_chapter_range`):

| Input | Kết quả |
|-------|---------|
| Enter (rỗng) | Upload **tất cả** chương có MP3 |
| `10` | chương 1–10 |
| `5-10` | chương 5–10 |
| `1,2,3,6,8` | các chương liệt kê |
| `all` | tất cả có MP3 |

**Auto-upload:** `GDRIVE_AUTO_AFTER_TTS = True` → sau TTS xong upload **hết** MP3 truyện (không hỏi range).

**OAuth lỗi 403 access_denied:** app Google Cloud đang Testing → thêm email vào **OAuth consent screen → Test users**.

API chính: `upload_novel_mp3_by_id(novel_id, chapter_numbers=)`, `upload_mp3_files(...)`.

## CLI legacy (vẫn hoạt động)

```bash
python crawler.py                                    # crawl + TTS
python crawler.py --crawl-only --url URL             # chỉ cào
python crawler.py --crawl-only --url URL --chapter-range 1-10
python crawler.py --crawl-only --url URL --chapter-range all
python crawler.py --tts-only --novel-id 1
python crawler.py --url URL --max-chapters 10         # slice đầu (không dùng range parser)
```

Menu spawn cào dùng `--chapter-range` (từ `range_to_text()`).

## Chapter range parser (`chapter_range.py`)

Dùng chung Crawl, TTS, Player. Upload Drive dùng `ask_upload_chapter_range` (Enter khác Crawl/TTS).

| Input | Crawl/TTS (`ask_chapter_range`) | Upload Drive (`ask_upload_chapter_range`) |
|-------|--------------------------------|-------------------------------------------|
| Enter (rỗng) | `1..MAX_CHAPTERS` từ config | Tất cả chương **có MP3** |
| `10` | chương 1–10 | chương 1–10 |
| `5-10` | chương 5–10 | chương 5–10 |
| `1,2,3,6,8` | các chương liệt kê | các chương liệt kê |
| `all` | tất cả chương miễn phí | tất cả có MP3 |

API: `parse_chapter_range(text)` → `ChapterRange(numbers: frozenset | None)`.
CLI: `range_to_text(chapter_range)` → `"1-10"` hoặc `"all"`.

## Resume & status

**Cột DB:** `crawl_status`, `tts_status` — `pending | processing | completed | failed | skipped`

**TTS progress (chỉ hiển thị, không resume chunk):**
- `tts_chars_total`, `tts_chars_done` — cập nhật sau mỗi chunk TTS
- Bắt đầu chương: `start_tts_chapter()` → `processing`
- Ctrl+C TTS: `reset_processing_tts_chapters()` → `pending`

| Sự kiện | DB |
|---------|-----|
| Cào xong | `crawl_status=completed` (content > 50 ký tự) |
| Cào lỗi | `crawl_status=failed` |
| TTS đang chạy | `tts_status=processing` + chars done/total |
| TTS xong | `tts_status=completed` + `mp3_path` |
| TTS lỗi | `tts_status=failed` |
| Resume cào | ưu tiên `failed` → `pending`; skip `completed` |
| Resume TTS | `get_chapters_without_mp3()` — skip MP3 hợp lệ trên disk |

**Playback:** `playback_state(novel_id, chapter_number, position_sec)` — lưu định kỳ từ `PlayerSession`.

## Crawler (tóm tắt)

- API `CHAPTER_LIST_API` — chỉ `status == "1"` (miễn phí)
- Nội dung: `.contentbox[cid]`; captcha → `_wait_for_user`
- `crawl_novel(chapter_numbers=frozenset|None)` — lọc phạm vi
- Menu cào: không gọi trực tiếp — spawn CLI subprocess

## TTS (tóm tắt)

- `generate_mp3_for_novel(novel_id, title, chapter_numbers=, voice=, rate=)`
- Song song: `TTS_CONCURRENCY`; chunk: `TTS_CHUNK_*`
- Progress live: `[TTS] Ch.NNNN title — done/total ký tự (pct%)`
- BGM: `BGM_PATH` + pydub (cần ffmpeg)
- **Không** resume chunk persistent — Ctrl+C → TTS lại chương từ đầu

## Player (`player.py`)

- **Nền:** `PlayerSession` — queue auto-next, điều khiển tại menu 3
- **Legacy blocking:** `play_queue()` — hotkey trực tiếp (ít dùng từ menu)
- Tốc độ: `1.0 / 1.25 / 1.5 / 2.0` qua `set_rate()`; menu 3: `+` / `-`
- Menu 3 phát: `p` pause, `n` next, `b` prev, `s` stop, `0` về menu (phát tiếp nền)

## Config quan trọng

| Key | Mặc định | Ý nghĩa |
|-----|----------|---------|
| `MAX_CHAPTERS` | 10 | Enter mặc định range cào/TTS = 1..N |
| `TTS_VOICE`, `TTS_RATE` | vi-VN-HoaiMyNeural, +50% | Menu TTS override session |
| `TTS_CONCURRENCY` | 2 | Quá cao → `NoAudioReceived` |
| `TTS_CHUNK_SIZE` | 1000 | Ký tự/chunk; progress theo chunk |
| `BGM_PATH` | file mp3 hoặc None | Nhạc nền sau TTS |
| `HEADLESS` | False | Bắt buộc False để captcha |
| `BROWSER_STATE_PATH` | browser_state.json | Session Playwright |
| `GDRIVE_ENABLED` | True | Tắt → menu 8 báo disabled |
| `GDRIVE_CREDENTIALS` | client_secret.json | OAuth Desktop credentials |
| `GDRIVE_TOKEN` | token.json | Token sau lần đăng nhập đầu |
| `GDRIVE_ROOT_FOLDER` | novel-crawler-mp3 | Folder gốc trên Drive |
| `GDRIVE_AUTO_AFTER_TTS` | False | Tự upload hết MP3 sau TTS |

## DB schema

- `novels`: url UNIQUE, title, author, summary
- `chapters`: …, **crawl_status**, **tts_status**, **tts_chars_total**, **tts_chars_done**, **mp3_path**
- `playback_state`: novel_id PK, chapter_number, position_sec, updated_at

Helper: `start_tts_chapter()`, `increment_tts_chars_done()`, `get_processing_tts_chapters()`, `reset_processing_tts_chapters()`, `get_chapters_with_mp3()`.

## Workflow sửa code

1. Menu/UI/job → `console/`; singleton job → `console/jobs.py`
2. Parser range → `chapter_range.py`; upload range prompt → `prompts.ask_upload_chapter_range`
3. Cào → `crawler.py`; TTS → `tts.py`; phát → `player.py`; Drive → `drive_upload.py`
4. Status/progress → `db.py`
5. Test nhanh: `MAX_CHAPTERS=2`, menu range `1-2`, mục 7 xem job, mục 8 upload thử

## Không làm

- Không headless nếu cần captcha
- Không cào chương VIP / bypass captcha
- Không resume chunk TTS persistent
- Không cancel job từ menu (user Ctrl+C ở cửa sổ crawler hoặc chờ TTS xong)
- Không TTS 2 truyện song song từ menu
- Không commit `client_secret.json`, `token.json`, `output/mp3/`
- Không rewrite crawler — mở rộng tối thiểu
