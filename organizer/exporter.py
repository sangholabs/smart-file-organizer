"""분석 결과를 CSV / JSON 으로 내보내기.

리포트(HTML)와 별개로, 외부 검토·기록·엑셀 분석용 표를 만든다.
모든 처리는 로컬에서만 이루어지며 파일 내용은 포함하지 않는다(경로·크기·날짜 등 메타만).
"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime
from pathlib import Path

# (gui_key, 사람이 읽는 제목, analyzer 결과 키, 종류)
#  - group: g["files"] 안에 파일 목록이 있는 카테고리
#  - flat : 결과 리스트 자체가 파일/폴더 목록인 카테고리
CATEGORIES = [
    ("dup", "완전중복", "exact_duplicates", "group"),
    ("name", "이름충돌", "name_conflicts", "group"),
    ("ver", "버전이상", "version_anomalies", "group"),
    ("image", "비슷한 이미지", "image_dups", "group"),
    ("video", "비슷한 영상", "video_dups", "group"),
    ("audio", "비슷한 오디오", "audio_dups", "group"),
    ("doc", "같은 내용 문서", "doc_dups", "group"),
    ("docnear", "비슷한 문서", "doc_near", "group"),
    ("zip", "같은 내용 압축", "zip_dups", "group"),
    ("archloose", "풀린 압축", "archive_loose", "flat"),
    ("junk", "시스템 찌꺼기", "junk_files", "flat"),
    ("empty", "빈 폴더", "empty_dirs", "flat"),
]

CSV_HEADER = ["category", "category_title", "group", "role",
              "path", "name", "folder", "size_bytes", "size_human", "mtime_iso"]


def _human(n: int) -> str:
    f = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.0f}{unit}" if unit == "B" else f"{f:.1f}{unit}"
        f /= 1024
    return f"{f:.1f}TB"


def _mtime_iso(mtime) -> str:
    try:
        return datetime.fromtimestamp(float(mtime)).isoformat(timespec="seconds")
    except (ValueError, OSError, TypeError):
        return ""


def _role(f: dict, flat: bool) -> str:
    if flat:
        return "item"
    r = f.get("recommend")
    if r == "keep":
        return "keeper"
    if r == "move":
        return "move"
    return "review"


def _file_row(f: dict, cat_key, cat_title, group_idx, flat) -> dict:
    path = f.get("path", "")
    size = int(f.get("size", 0) or 0)
    return {
        "category": cat_key,
        "category_title": cat_title,
        "group": group_idx,
        "role": _role(f, flat),
        "path": path,
        "name": os.path.basename(path.rstrip("\\/")) or path,
        "folder": os.path.dirname(path),
        "size_bytes": size,
        "size_human": _human(size),
        "mtime_iso": _mtime_iso(f.get("mtime", 0)),
    }


def iter_rows(result: dict):
    """결과를 파일당 1행(dict)으로 평탄화해 순회."""
    for cat_key, cat_title, result_key, kind in CATEGORIES:
        items = result.get(result_key) or []
        if kind == "group":
            for gi, g in enumerate(items):
                for f in g.get("files", []):
                    yield _file_row(f, cat_key, cat_title, gi, flat=False)
        else:  # flat
            for fi, f in enumerate(items):
                yield _file_row(f, cat_key, cat_title, fi, flat=True)


def export_csv(result: dict, path) -> Path:
    """파일당 1행 CSV. Excel 한글 호환 위해 utf-8-sig(BOM)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=CSV_HEADER)
        w.writeheader()
        for row in iter_rows(result):
            w.writerow(row)
    return path


def export_json(result: dict, path) -> Path:
    """카테고리별 그룹/항목 구조의 JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    st = result.get("stats", {})
    out = {
        "schema": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "stats": {
            "total_files": st.get("total_files"),
            "reclaimable_bytes": st.get("reclaimable_bytes"),
        },
        "categories": [],
    }
    for cat_key, cat_title, result_key, kind in CATEGORIES:
        items = result.get(result_key) or []
        groups = []
        if kind == "group":
            for gi, g in enumerate(items):
                groups.append({
                    "group": gi,
                    "files": [_file_row(f, cat_key, cat_title, gi, flat=False)
                              for f in g.get("files", [])],
                })
        else:
            if items:
                groups.append({
                    "group": 0,
                    "files": [_file_row(f, cat_key, cat_title, 0, flat=True)
                              for f in items],
                })
        out["categories"].append({
            "key": cat_key, "title": cat_title,
            "count": len(items), "groups": groups,
        })
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=2)
    return path


def export(result: dict, path, fmt: str | None = None) -> Path:
    """확장자(.csv/.json) 또는 fmt 로 분기."""
    path = Path(path)
    f = (fmt or path.suffix.lstrip(".")).lower()
    if f == "csv":
        return export_csv(result, path)
    if f == "json":
        return export_json(result, path)
    raise ValueError(f"지원하지 않는 형식: {f} (csv 또는 json)")
