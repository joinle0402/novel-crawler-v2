"""Cấu hình crawler sangtacviet.com — sửa các giá trị dưới đây rồi chạy crawler.py."""

# Dán URL truyện vào đây, ví dụ:
# https://sangtacviet.com/truyen/69shu/1/53962/
NOVEL_URL = "https://sangtacviet.com/truyen/fanqie/1/7481496454264015897/"

# Giới hạn mặc định khi Enter (phiên nhỏ = an toàn hơn). Đặt None = hết miễn phí.
# 50 chương: chia range 1-10, 11-20... nhiều phiên, nghỉ vài giờ giữa phiên.
MAX_CHAPTERS = 10

# Delay giữa các chương (giây) — random [MIN, MAX]; chậm hơn = ít rate-limit
DELAY_BETWEEN_CHAPTERS_MIN = 10
DELAY_BETWEEN_CHAPTERS_MAX = 20
# Nghỉ dài sau mỗi N chương đã cào thành công trong phiên
CRAWL_BATCH_EVERY = 5
CRAWL_BATCH_PAUSE_SEC = 180  # 3 phút
# Chờ trước khi thử lại sau 403 (ban tạm có thể vài phút–vài giờ)
CRAWL_403_COOLDOWN_SEC = 300  # 5 phút
# True = luôn mở trang danh sách truyện trước; False = bỏ qua nếu DB đã có list chương
# (vẫn mở trang danh sách khi DB chưa có novel/list chương)
CRAWL_VISIT_NOVEL_PAGE = False

# SQLite
DB_PATH = "novels.db"

# Thư mục lưu file MP3
MP3_OUTPUT_DIR = "output/mp3"

# Google Drive — upload folder MP3
GDRIVE_ENABLED = True
GDRIVE_CREDENTIALS = "client_secret.json"  # OAuth Desktop app từ Google Cloud
GDRIVE_TOKEN = "token.json"
GDRIVE_ROOT_FOLDER = "novel-crawler-mp3"
GDRIVE_AUTO_AFTER_TTS = False  # True = tự upload sau khi TTS xong

# Nhạc nền MP3 — đặt None để tắt
BGM_PATH = "Just_Stay_Aakash_Gandhi.mp3"
BGM_VOLUME_DB = -18  # âm lượng nhạc nền (dB, số âm = nhỏ hơn giọng)
BGM_LOOP = True  # lặp nhạc nếu chương dài hơn track
BGM_FADE_IN_MS = 2000
BGM_FADE_OUT_MS = 3000
BGM_EXPORT_BITRATE = "128k"

# TTS — edge-tts (miễn phí, giọng neural tiếng Việt)
TTS_VOICE = "vi-VN-HoaiMyNeural"  # giọng nữ
TTS_RATE = "+50%"  # ~1.5x tốc độ

# Playwright
HEADLESS = False  # False để người dùng giải captcha
BROWSER_STEALTH = False  # playwright-stealth: ẩn navigator.webdriver, v.v.
BROWSER_CHANNEL = 'chrome'  # "chrome" = dùng Google Chrome đã cài (nếu có)
BROWSER_LOCALE = "vi-VN"
BROWSER_TIMEZONE = "Asia/Ho_Chi_Minh"
BROWSER_MAXIMIZED = True  # mở Chrome full màn hình khi cào
BROWSER_TIMEOUT_MS = 60_000
CONTENT_LOAD_TIMEOUT_MS = 120_000
# Chờ nội dung chương tối đa bao lâu trước khi F5 reload (ít F5 = ít bị flag)
CONTENT_RELOAD_AFTER_SEC = 60
MAX_CONTENT_RELOADS = 1

# Số chương TTS chạy song song (edge-tts) — quá cao dễ bị rate-limit
TTS_CONCURRENCY = 1

# Retry TTS khi edge-tts lỗi (exponential backoff: 2s, 4s, ...)
TTS_MAX_RETRIES = 3

# Nghỉ giữa các request edge-tts (giây) — tránh NoAudioReceived
TTS_REQUEST_DELAY_SEC = 1.0

# Chia nội dung dài thành chunk trước khi TTS (ffmpeg tùy chọn cho ghép)
ENABLE_TTS_CHUNK = True
TTS_CHUNK_SIZE = 1000  # ký tự mỗi chunk
TTS_CHUNK_CONCURRENCY = 1  # số chunk TTS song song trong 1 chương

# Lưu session Playwright sau captcha (tái sử dụng lần sau)
BROWSER_STATE_PATH = "browser_state.json"

# API danh sách chương
CHAPTER_LIST_API = (
    "https://sangtacviet.com/index.php"
    "?ngmar=chapterlist&h={h}&bookid={bookid}&sajax=getchapterlist"
)
# Trong response API: field đầu = 1 là chương miễn phí, khác 1 là VIP
CHAPTER_FREE_STATUS = "1"

# Selector (từ selector.txt)
SELECTORS = {
    "book_title": "#book_name2",
    "book_author": ".cap h2",
    "book_summary": "#book-sumary",
    # Nội dung chương thật có attr cid; .contentbox đầu tiên thường là placeholder
    "content_box": ".contentbox[cid]",
    "language_option": '.seloption[value="vi"]',
    "loading_text": "Đang tải nội dung chương",
    "placeholder_text": "Đọc trên web",
}
