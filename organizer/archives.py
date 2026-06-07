"""압축 ↔ 풀린 파일 교차 탐지.

압축파일(zip/7z) 안의 항목이 '전부' 디스크에 이미 풀려 있으면(이름+크기 일치),
그 압축파일은 사실상 중복이므로 정리 후보로 표시한다(휴리스틱 → 기본 미체크/검토).
원본은 읽기만 한다(헤더/목록만 열어봄).
"""

from __future__ import annotations

import os
import sqlite3
import zipfile

from .hashing import long_path

ZIP_EXTS = {".zip", ".jar", ".war", ".apk", ".epub"}
SEVENZIP_EXTS = {".7z"}


def _zip_entries(path: str):
    try:
        with zipfile.ZipFile(long_path(path)) as z:
            return [(os.path.basename(i.filename).lower(), i.file_size)
                    for i in z.infolist() if not i.is_dir()]
    except Exception:
        return None


def _7z_entries(path: str):
    try:
        import py7zr
        with py7zr.SevenZipFile(long_path(path), "r") as z:
            return [(os.path.basename(f.filename).lower(), f.uncompressed or 0)
                    for f in z.list() if not f.is_directory]
    except Exception:
        return None


def redundant_archives(db_path, cfg=None) -> list[dict]:
    """내용이 이미 밖에 다 풀려있는 압축파일 목록."""
    out: list[dict] = []
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return out
    try:
        rows = conn.execute("SELECT path, size, mtime, ext FROM files").fetchall()
    except sqlite3.Error:
        conn.close()
        return out
    conn.close()

    loose = set()
    archives = []
    for path, size, mtime, ext in rows:
        ext = (ext or "").lower()
        loose.add((os.path.basename(path).lower(), size))
        if ext in ZIP_EXTS or ext in SEVENZIP_EXTS:
            archives.append((path, size, mtime, ext))

    for path, size, mtime, ext in archives:
        entries = _7z_entries(path) if ext in SEVENZIP_EXTS else _zip_entries(path)
        if not entries:
            continue
        if all(e in loose for e in entries):
            out.append({"path": path, "size": size, "mtime": mtime,
                        "recommend": "move", "entries": len(entries)})
    out.sort(key=lambda x: x["size"], reverse=True)
    return out
