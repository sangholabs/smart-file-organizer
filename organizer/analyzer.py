"""중복 / 이름충돌 / 버전이상 탐지.

catalog.db 를 읽어 세 가지 카테고리로 분류한 결과(dict)를 돌려준다.
원본 파일은 절대 수정하지 않는다 (읽기 전용).
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from . import config

# 파일명에서 버전 토큰을 인식하는 정규식
RE_VNUM = re.compile(r"(?:^|[ _\-.(\[])v\.?\s?(\d{1,3})\b", re.IGNORECASE)
RE_PAREN_NUM = re.compile(r"\((\d{1,3})\)")
RE_TRAIL_NUM = re.compile(r"[ _\-](\d{1,3})\s*$")
RE_DATE = re.compile(r"(20\d{2})[._\-]?(0[1-9]|1[0-2])[._\-]?(0[1-9]|[12]\d|3[01])")
RE_COPY = re.compile(r"복사본|사본|copy|-\s*복사본", re.IGNORECASE)


def _content_key(row: dict) -> str:
    """내용 동일성 키. 전체해시가 있으면 그것, 없으면 (크기 유일) 경로."""
    if row["full_hash"]:
        return "h:" + row["full_hash"]
    return "u:" + row["path"]  # 크기가 유일 → 내용도 유일


def _strip_version_tokens(stem: str) -> str:
    """버전/날짜/복사본 토큰을 제거해 '가족 이름'을 만든다."""
    s = stem
    s = RE_DATE.sub("", s)
    s = RE_VNUM.sub("", s)
    s = RE_PAREN_NUM.sub("", s)
    s = RE_COPY.sub("", s)
    s = RE_TRAIL_NUM.sub("", s)
    # 흔한 키워드 제거 — '구분자/문자열 경계'로 둘러싸인 토큰만 제거한다.
    # (예: 'report_final'의 final 은 제거하되, '원본자료'의 '원본'은 글자에 붙어 있어 제거하지 않음
    #  → '자료'와 '원본자료'가 한 가족으로 잘못 묶이는 것을 방지)
    sep = r"[ _\-.()\[\]]"
    for kw in ["최종", "최신", "진짜", "real", "final", "fin", "수정", "revised",
               "rev", "초안", "draft", "원본", "구버전", "old", "본", "최최종"]:
        s = re.sub(rf"(?:(?<=^)|(?<={sep}))(?:{re.escape(kw)})(?=$|{sep})", "", s, flags=re.IGNORECASE)
    s = re.sub(r"[ _\-.]+", " ", s).strip().lower()
    return s


def version_score(stem: str, keywords: list[str]) -> tuple:
    """파일명이 '얼마나 최신이라 주장하는지' 점수. 클수록 최신 주장."""
    low = stem.lower()
    # 날짜
    m = RE_DATE.search(stem)
    date_val = int(f"{m.group(1)}{m.group(2)}{m.group(3)}") if m else 0
    # 키워드 순위 (config 의 version_keywords 리스트 인덱스)
    kw_score = -1
    for i, kw in enumerate(keywords):
        if kw.lower() in low:
            kw_score = max(kw_score, i)
    # 숫자 버전
    num = 0
    for rex in (RE_VNUM, RE_PAREN_NUM, RE_TRAIL_NUM):
        for mm in rex.finditer(stem):
            num = max(num, int(mm.group(1)))
    # 복사본 개수 (보통 나중에 만든 사본)
    copies = len(RE_COPY.findall(stem))
    return (date_val, kw_score, num, copies)


def _pick_keeper(files: list[dict]) -> str:
    """완전중복 그룹에서 보관할 1개 선택 — 사본보다 원본 우선(keeper 모듈)."""
    from . import keeper
    return keeper.pick_keeper(files)


def analyze(db_path: Path = config.CATALOG_DB, cfg: dict | None = None) -> dict:
    cfg = cfg or config.load_config()
    keywords = cfg.get("version_keywords", [])
    conn = sqlite3.connect(str(db_path))
    _COLS = ("path", "name", "ext", "size", "mtime", "partial_hash", "full_hash")

    def _rows_where(where_expr, values):
        """후보 값들에 해당하는 행만 청크로 로드(메모리 절약)."""
        out = []
        vals = list(values)
        for i in range(0, len(vals), 400):
            chunk = vals[i:i + 400]
            ph = ",".join("?" * len(chunk))
            q = (f"SELECT path,name,ext,size,mtime,partial_hash,full_hash "
                 f"FROM files WHERE {where_expr} IN ({ph})")
            for row in conn.execute(q, chunk):
                out.append(dict(zip(_COLS, row)))
        return out

    try:
        total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        errors = conn.execute("SELECT path, error FROM scan_errors ORDER BY at").fetchall()

        # ---------- (A) 완전 중복 ----------  (중복 후보 해시만 로드)
        dup_hashes = [h for (h,) in conn.execute(
            "SELECT full_hash FROM files WHERE full_hash IS NOT NULL AND size>0 "
            "GROUP BY full_hash HAVING COUNT(*)>1")]
        by_hash: dict[str, list[dict]] = {}
        for r in _rows_where("full_hash", dup_hashes):
            if r["full_hash"] and r["size"] > 0:
                by_hash.setdefault(r["full_hash"], []).append(r)

        # ---------- (B) 이름 같음 (동명 후보만 로드) ----------
        dup_names = [n for (n,) in conn.execute(
            "SELECT lower(name) FROM files GROUP BY lower(name) HAVING COUNT(*)>1")]
        by_name: dict[str, list[dict]] = {}
        for r in _rows_where("lower(name)", dup_names):
            by_name.setdefault(r["name"].lower(), []).append(r)

        # ---------- (C) 버전 이상 (2-pass: 가족 후보만 로드) ----------
        fam_count: dict[str, int] = {}
        for name, ext in conn.execute("SELECT name, ext FROM files"):
            stem = os.path.splitext(name or "")[0]
            fam = _strip_version_tokens(stem) + "|" + (ext or "")
            if not fam.strip("| "):
                continue
            fam_count[fam] = fam_count.get(fam, 0) + 1
        cand_fams = {f for f, c in fam_count.items() if c >= 2}
        fam_count.clear()
        by_family: dict[str, list[dict]] = {}
        if cand_fams:
            for row in conn.execute(
                    "SELECT path,name,ext,size,mtime,partial_hash,full_hash FROM files"):
                r = dict(zip(_COLS, row))
                stem = os.path.splitext(r["name"])[0]
                fam = _strip_version_tokens(stem) + "|" + r["ext"]
                if fam in cand_fams:
                    by_family.setdefault(fam, []).append(r)
    finally:
        conn.close()

    exact_duplicates = []
    reclaimable = 0
    gid = 0
    for fh, files in by_hash.items():
        if len(files) < 2:
            continue
        gid += 1
        keeper = _pick_keeper(files)
        size = files[0]["size"]
        members = []
        for f in sorted(files, key=lambda x: x["path"]):
            is_keep = f["path"] == keeper
            if not is_keep:
                reclaimable += size
            members.append({
                "path": f["path"],
                "size": f["size"],
                "mtime": f["mtime"],
                "recommend": "keep" if is_keep else "move",
            })
        exact_duplicates.append({
            "id": f"dup{gid}",
            "hash": fh,
            "size": size,
            "count": len(files),
            "files": members,
        })
    exact_duplicates.sort(key=lambda g: g["size"] * (g["count"] - 1), reverse=True)

    # ---------- (B) 이름 같음 · 내용 다름 ----------
    name_conflicts = []
    cid = 0
    for name, files in by_name.items():
        if len(files) < 2:
            continue
        distinct = {_content_key(f) for f in files}
        if len(distinct) < 2:
            continue  # 같은 이름이지만 내용 동일 → 완전중복(A)에서 처리됨
        cid += 1
        members = [{
            "path": f["path"], "size": f["size"], "mtime": f["mtime"],
            "recommend": "review",
        } for f in sorted(files, key=lambda x: x["mtime"], reverse=True)]
        name_conflicts.append({
            "id": f"name{cid}",
            "name": files[0]["name"],
            "count": len(files),
            "distinct": len(distinct),
            "files": members,
        })
    name_conflicts.sort(key=lambda g: g["count"], reverse=True)

    # ---------- (C) 버전 이상 ----------  (by_family 는 위에서 후보만 구성됨)
    version_anomalies = []
    aid = 0
    for fam, files in by_family.items():
        if len(files) < 2:
            continue
        # 내용이 서로 다른 파일이 2개 이상이어야 의미 있음
        if len({_content_key(f) for f in files}) < 2:
            continue
        scored = []
        for f in files:
            stem = os.path.splitext(f["name"])[0]
            scored.append((version_score(stem, keywords), f))
        # 버전 주장 점수가 모두 같으면(토큰 없음) 판단 불가 → 건너뜀
        score_set = {s for s, _ in scored}
        if len(score_set) < 2:
            continue
        claimed_latest = max(scored, key=lambda x: x[0])[1]
        mtime_latest = max(files, key=lambda x: x["mtime"])
        # 이상: '최신이라 주장하는 파일'이 '실제 가장 최근 수정 파일'이 아님
        if claimed_latest["path"] == mtime_latest["path"]:
            continue
        aid += 1
        members = []
        for s, f in sorted(scored, key=lambda x: x[0], reverse=True):
            tag = []
            if f["path"] == claimed_latest["path"]:
                tag.append("이름상_최신")
            if f["path"] == mtime_latest["path"]:
                tag.append("실제_최근수정")
            members.append({
                "path": f["path"], "size": f["size"], "mtime": f["mtime"],
                "tags": tag, "recommend": "review",
            })
        version_anomalies.append({
            "id": f"ver{aid}",
            "family": fam.split("|")[0].strip() or "(이름)",
            "ext": files[0]["ext"],
            "count": len(files),
            "claimed_latest": claimed_latest["path"],
            "mtime_latest": mtime_latest["path"],
            "files": members,
        })
    version_anomalies.sort(key=lambda g: g["count"], reverse=True)

    # ---------- (D~F) 내용 지문 기반: 이미지/문서/zip ----------
    try:
        from . import fingerprint
        sim = fingerprint.similarity_groups(db_path, cfg)
    except Exception:
        sim = {"image_dups": [], "doc_dups": [], "zip_dups": [], "doc_near": [],
               "video_dups": [], "audio_dups": [],
               "counts": {"image": 0, "doc": 0, "zip": 0, "docnear": 0, "video": 0, "audio": 0}}

    # 압축 ↔ 풀린 파일 교차(내용이 이미 풀려있는 압축)
    try:
        from . import archives
        archive_loose = archives.redundant_archives(db_path, cfg)
    except Exception:
        archive_loose = []

    try:
        from . import insights
        insight = insights.compute(db_path, cfg)
    except Exception:
        insight = {}

    # ---------- (G) 시스템 찌꺼기 (맥/윈도우) ----------
    junk_files = []
    junk_bytes = 0
    try:
        jconn = sqlite3.connect(str(db_path))
        try:
            for jp, js, jm in jconn.execute(
                    "SELECT path, size, mtime FROM junk ORDER BY size DESC"):
                junk_files.append({"path": jp, "size": js, "mtime": jm, "recommend": "move"})
                junk_bytes += js or 0
        finally:
            jconn.close()
    except sqlite3.Error:
        junk_files = []

    # ---------- (H) 빈 폴더 ----------
    empty_dirs = []
    try:
        econn = sqlite3.connect(str(db_path))
        try:
            for (ep,) in econn.execute("SELECT path FROM empty_dirs ORDER BY path"):
                empty_dirs.append({"path": ep, "size": 0, "mtime": 0, "recommend": "move"})
        finally:
            econn.close()
    except sqlite3.Error:
        empty_dirs = []

    return {
        "stats": {
            "total_files": total_files,
            "exact_dup_groups": len(exact_duplicates),
            "name_conflict_groups": len(name_conflicts),
            "version_anomaly_groups": len(version_anomalies),
            "image_dup_groups": sim["counts"]["image"],
            "doc_dup_groups": sim["counts"]["doc"],
            "zip_dup_groups": sim["counts"]["zip"],
            "doc_near_groups": sim["counts"].get("docnear", 0),
            "video_dup_groups": sim["counts"].get("video", 0),
            "audio_dup_groups": sim["counts"].get("audio", 0),
            "archive_loose_count": len(archive_loose),
            "junk_count": len(junk_files),
            "junk_bytes": junk_bytes,
            "empty_dir_count": len(empty_dirs),
            "reclaimable_bytes": reclaimable,
            "error_count": len(errors),
        },
        "exact_duplicates": exact_duplicates,
        "name_conflicts": name_conflicts,
        "version_anomalies": version_anomalies,
        "image_dups": sim["image_dups"],
        "doc_dups": sim["doc_dups"],
        "zip_dups": sim["zip_dups"],
        "doc_near": sim.get("doc_near", []),
        "video_dups": sim.get("video_dups", []),
        "audio_dups": sim.get("audio_dups", []),
        "archive_loose": archive_loose,
        "junk_files": junk_files,
        "empty_dirs": empty_dirs,
        "insights": insight,
        "errors": [{"path": p, "error": e} for p, e in errors[:500]],
    }
