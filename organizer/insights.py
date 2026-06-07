"""용량 분석 인사이트 — 색인(catalog)을 읽어 '어디가 용량을 먹는지' 요약한다.

- 상위 폴더(하위 포함 롤업), 가장 큰 파일, 오래된 대용량 파일, 확장자별 용량.
원본은 읽지 않고 색인 메타데이터(path/size/mtime/ext)만 사용한다(빠름, 안전).
주의: 색인에는 min_size_bytes 이상 파일만 들어 있어, 아주 작은 파일은 합계에서 빠질 수 있다.
"""

from __future__ import annotations

import os
import sqlite3
import time


def compute(db_path, cfg: dict | None = None,
            top_files: int = 60, top_folders: int = 40,
            old_days: int = 730) -> dict:
    empty = {
        "total_files": 0, "total_size": 0,
        "largest_files": [], "old_files": [], "by_ext": [], "top_folders": [],
    }
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return empty
    try:
        rows = conn.execute("SELECT path, size, mtime, ext FROM files").fetchall()
    except sqlite3.Error:
        conn.close()
        return empty
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass

    if not rows:
        return empty

    total_size = sum(r[1] for r in rows)

    # 가장 큰 파일
    largest = sorted(rows, key=lambda r: r[1], reverse=True)[:top_files]
    largest_files = [{"path": p, "size": s, "mtime": m} for p, s, m, _ in largest]

    # 오래된 대용량 파일 (old_days 보다 오래 수정 안 됨)
    cutoff = time.time() - old_days * 86400
    old = [r for r in rows if r[2] and r[2] < cutoff]
    old.sort(key=lambda r: r[1], reverse=True)
    old_files = [{"path": p, "size": s, "mtime": m} for p, s, m, _ in old[:top_files]]

    # 확장자별 용량
    ext_size: dict[str, list] = {}
    for p, s, m, e in rows:
        e = (e or "(없음)").lower()
        if e not in ext_size:
            ext_size[e] = [0, 0]
        ext_size[e][0] += s
        ext_size[e][1] += 1
    by_ext = sorted(
        ({"ext": e, "size": v[0], "count": v[1]} for e, v in ext_size.items()),
        key=lambda x: x["size"], reverse=True,
    )

    # 상위 폴더(하위 포함 롤업): 각 파일 크기를 모든 상위 폴더에 누적
    folder_size: dict[str, int] = {}
    for p, s, m, e in rows:
        d = os.path.dirname(p)
        seen_drive = False
        while d and not seen_drive:
            folder_size[d] = folder_size.get(d, 0) + s
            parent = os.path.dirname(d)
            if parent == d:  # 드라이브 루트 도달
                seen_drive = True
            d = parent
    top_folder = sorted(folder_size.items(), key=lambda kv: kv[1], reverse=True)[:top_folders]
    top_folders = [{"path": d, "size": s} for d, s in top_folder]

    return {
        "total_files": len(rows),
        "total_size": total_size,
        "largest_files": largest_files,
        "old_files": old_files,
        "by_ext": by_ext,
        "top_folders": top_folders,
    }
