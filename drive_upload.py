"""Upload thư mục MP3 lên Google Drive."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from config import (
    GDRIVE_CREDENTIALS,
    GDRIVE_ROOT_FOLDER,
    GDRIVE_TOKEN,
)
from db import get_chapters_with_mp3, get_novel_by_id
from tts import _safe_filename

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


@dataclass
class UploadResult:
    uploaded: list[str]
    skipped: list[str]
    failed: list[tuple[str, str]]


def _q_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _get_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds_path = Path(GDRIVE_CREDENTIALS)
    if not creds_path.is_file():
        raise FileNotFoundError(
            f"Không tìm thấy file OAuth: {creds_path}. "
            "Tải credentials Desktop app từ Google Cloud Console."
        )

    token_path = Path(GDRIVE_TOKEN)
    creds = None
    if token_path.is_file():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("drive", "v3", credentials=creds)


def _find_or_create_folder(service, name: str, parent_id: str | None = None) -> str:
    q = (
        f"name='{_q_escape(name)}' and "
        "mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    if parent_id:
        q += f" and '{parent_id}' in parents"

    res = (
        service.files()
        .list(q=q, fields="files(id)", spaces="drive")
        .execute()
    )
    files = res.get("files", [])
    if files:
        return files[0]["id"]

    meta: dict = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]
    return service.files().create(body=meta, fields="id").execute()["id"]


def _file_exists_in_folder(service, filename: str, parent_id: str) -> bool:
    q = (
        f"name='{_q_escape(filename)}' and "
        f"'{parent_id}' in parents and trashed=false"
    )
    res = (
        service.files()
        .list(q=q, fields="files(id)", spaces="drive")
        .execute()
    )
    return bool(res.get("files"))


def _chapter_number_from_mp3(path: Path) -> int | None:
    """Đọc số chương từ tên file dạng 0001_....mp3."""
    stem = path.stem
    if len(stem) >= 4 and stem[:4].isdigit():
        return int(stem[:4])
    return None


def _filter_mp3_by_chapters(
    mp3_files: list[Path],
    chapter_numbers: frozenset[int] | None,
) -> list[Path]:
    if chapter_numbers is None:
        return mp3_files
    return [
        mp3
        for mp3 in mp3_files
        if (n := _chapter_number_from_mp3(mp3)) is not None and n in chapter_numbers
    ]


def upload_mp3_files(
    mp3_files: list[Path],
    *,
    novel_folder_name: str,
    root_folder_name: str | None = None,
    skip_existing: bool = True,
) -> UploadResult:
    """Upload danh sách file MP3 lên Drive/novel-crawler-mp3/{novel_folder_name}/."""
    if not mp3_files:
        raise FileNotFoundError("Không có file MP3 nào để upload.")

    from googleapiclient.http import MediaFileUpload

    service = _get_service()
    root_name = root_folder_name or GDRIVE_ROOT_FOLDER
    root_id = _find_or_create_folder(service, root_name)
    novel_folder_id = _find_or_create_folder(service, novel_folder_name, root_id)

    uploaded: list[str] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []

    print(f"[Drive] Thư mục đích: {root_name}/{novel_folder_name}", flush=True)

    for mp3 in sorted(mp3_files):
        if skip_existing and _file_exists_in_folder(service, mp3.name, novel_folder_id):
            skipped.append(mp3.name)
            print(f"[Drive] Bỏ qua (đã có): {mp3.name}", flush=True)
            continue

        try:
            media = MediaFileUpload(str(mp3), mimetype="audio/mpeg", resumable=True)
            meta = {"name": mp3.name, "parents": [novel_folder_id]}
            created = (
                service.files()
                .create(body=meta, media_body=media, fields="id")
                .execute()
            )
            uploaded.append(created["id"])
            print(f"[Drive] Đã upload: {mp3.name}", flush=True)
        except Exception as exc:
            failed.append((mp3.name, str(exc)))
            print(f"[Drive] Lỗi {mp3.name}: {exc}", flush=True)

    print(
        f"[Drive] Xong: {len(uploaded)} upload, "
        f"{len(skipped)} bỏ qua, {len(failed)} lỗi",
        flush=True,
    )
    return UploadResult(uploaded=uploaded, skipped=skipped, failed=failed)


def upload_novel_mp3_folder(
    local_dir: Path,
    *,
    chapter_numbers: frozenset[int] | None = None,
    root_folder_name: str | None = None,
    skip_existing: bool = True,
) -> UploadResult:
    """Upload *.mp3 trong local_dir lên Drive (có thể lọc theo số chương)."""
    if not local_dir.is_dir():
        raise FileNotFoundError(f"Không tìm thấy thư mục MP3: {local_dir}")

    mp3_files = _filter_mp3_by_chapters(
        sorted(local_dir.glob("*.mp3")),
        chapter_numbers,
    )
    if not mp3_files:
        if chapter_numbers is not None:
            raise FileNotFoundError(
                f"Không có file MP3 trong phạm vi chương đã chọn: {local_dir}"
            )
        raise FileNotFoundError(f"Không có file MP3 trong: {local_dir}")

    return upload_mp3_files(
        mp3_files,
        novel_folder_name=local_dir.name,
        root_folder_name=root_folder_name,
        skip_existing=skip_existing,
    )


def upload_novel_mp3_by_id(
    novel_id: int,
    chapter_numbers: frozenset[int] | None = None,
) -> UploadResult:
    """Upload MP3 của truyện theo novel_id (có thể lọc theo số chương)."""
    novel = get_novel_by_id(novel_id)
    if not novel:
        raise ValueError(f"Không tìm thấy novel id={novel_id}")

    chapters = get_chapters_with_mp3(novel_id)
    if chapter_numbers is not None:
        chapters = [ch for ch in chapters if ch.chapter_number in chapter_numbers]

    mp3_files = [
        Path(ch.mp3_path)
        for ch in chapters
        if ch.mp3_path and Path(ch.mp3_path).is_file()
    ]

    range_hint = ""
    if chapter_numbers is not None:
        range_hint = f" ({len(mp3_files)} chương trong phạm vi)"
    print(f"[Drive] Upload: {novel.title}{range_hint}", flush=True)

    novel_dir_name = _safe_filename(novel.title)
    return upload_mp3_files(
        mp3_files,
        novel_folder_name=novel_dir_name,
    )
