"""디렉터리 워크 + 메타데이터 색인 + 2단계 해시.

SQLite(catalog.db)에 파일 메타데이터와 해시를 저장한다.
재실행 시 크기·수정시간이 같은 파일은 기존 해시를 재사용한다(증분).
"""

from __future__ import annotations

import fnmatch
import os
import sqlite3
import stat
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import config
from .hashing import full_hash, partial_hash

# Windows 파일 속성 플래그
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000


def connect(db_path: Path = config.CATALOG_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            name TEXT,
            ext TEXT,
            size INTEGER,
            mtime REAL,
            partial_hash TEXT,
            full_hash TEXT,
            seen INTEGER DEFAULT 1
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_size ON files(size)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_full ON files(full_hash)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scan_errors (
            path TEXT,
            error TEXT,
            at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS junk (
            path TEXT PRIMARY KEY, name TEXT, size INTEGER, mtime REAL
        )
        """
    )
    conn.execute("CREATE TABLE IF NOT EXISTS empty_dirs (path TEXT PRIMARY KEY)")
    return conn


def _is_excluded_dir(name: str, exclude_names: set[str]) -> bool:
    return name.lower() in exclude_names


def _is_excluded_file(name: str, globs: list[str]) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatch(low, g.lower()) for g in globs)


def _is_junk(name: str, globs: list[str]) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatch(low, g.lower()) for g in globs)


def _is_skippable_attr(entry: os.DirEntry, cfg: dict) -> bool:
    """심볼릭/정션/오프라인(클라우드) 파일 여부."""
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    if not cfg.get("follow_symlinks", False):
        if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
            return True
    if cfg.get("skip_offline_files", True):
        if attrs & (FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS):
            return True
    return False


class Cancelled(Exception):
    """사용자가 스캔을 멈춤(취소)했을 때 내부적으로 던지는 신호."""


def _cancelled(cancel) -> bool:
    return cancel is not None and cancel.is_set()


def walk(cfg: dict, conn: sqlite3.Connection, progress=None, cancel=None) -> dict:
    """scan_roots 를 워크하며 메타데이터를 upsert. 통계 dict 반환.

    progress(stage, done, total, msg): 선택적 진행 콜백
    cancel: 선택적 threading.Event — set 되면 깔끔히 중단(부분 색인 보존).
    """
    exclude_names = {n.lower() for n in cfg.get("exclude_dir_names", [])}
    exclude_globs = cfg.get("exclude_file_globs", [])
    junk_globs = cfg.get("junk_globs", [])
    min_size = int(cfg.get("min_size_bytes", 0))
    quarantine = os.path.normcase(os.path.abspath(cfg.get("quarantine_dir", "")))

    conn.execute("UPDATE files SET seen=0")  # 이번 스캔에서 본 파일만 1로 표시
    conn.execute("DELETE FROM junk")          # 찌꺼기는 매 스캔 재수집(작고 빠름)
    stats = {"files": 0, "dirs": 0, "skipped": 0, "errors": 0, "reused": 0,
             "new_meta": 0, "junk": 0}
    junk_batch = []
    seen_inodes: set = set()   # 하드링크(같은 실체) 한 번만 색인 (가능한 OS에서만)

    # 기존 (path -> (size, mtime)) 캐시 로드: 변경 여부 판단용
    existing: dict[str, tuple[int, float]] = {}
    for path, size, mtime in conn.execute("SELECT path, size, mtime FROM files"):
        existing[path] = (size, mtime)

    batch = []

    def flush():
        if batch:
            conn.executemany(
                """INSERT INTO files(path,name,ext,size,mtime,partial_hash,full_hash,seen)
                   VALUES(?,?,?,?,?,?,?,1)
                   ON CONFLICT(path) DO UPDATE SET
                     name=excluded.name, ext=excluded.ext, size=excluded.size,
                     mtime=excluded.mtime, partial_hash=excluded.partial_hash,
                     full_hash=excluded.full_hash, seen=1""",
                batch,
            )
            conn.commit()
            batch.clear()

    def log_error(path: str, err: str):
        stats["errors"] += 1
        conn.execute(
            "INSERT INTO scan_errors(path,error,at) VALUES(?,?,?)",
            (path, err, time.time()),
        )

    def recurse(dir_path: str):
        try:
            it = os.scandir(dir_path)
        except OSError as e:
            log_error(dir_path, f"폴더 열기 실패: {e}")
            return
        stats["dirs"] += 1
        if progress is not None:
            progress("collect", stats["files"], None, dir_path)
        with it:
            for entry in it:
                if _cancelled(cancel):
                    raise Cancelled()
                try:
                    name = entry.name
                    if entry.is_dir(follow_symlinks=False):
                        if _is_excluded_dir(name, exclude_names):
                            continue
                        full = os.path.normcase(os.path.abspath(entry.path))
                        if quarantine and full.startswith(quarantine):
                            continue
                        if not cfg.get("follow_symlinks", False) and _is_skippable_attr(entry, cfg):
                            stats["skipped"] += 1
                            continue
                        recurse(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        # 시스템 찌꺼기는 크기/제외 필터보다 먼저, 무관하게 수집
                        if junk_globs and _is_junk(name, junk_globs):
                            try:
                                jst = entry.stat(follow_symlinks=False)
                                junk_batch.append((entry.path, name, jst.st_size, jst.st_mtime))
                                stats["junk"] += 1
                                if len(junk_batch) >= 500:
                                    conn.executemany(
                                        "INSERT OR REPLACE INTO junk VALUES(?,?,?,?)", junk_batch)
                                    junk_batch.clear()
                            except OSError:
                                pass
                            continue
                        if _is_excluded_file(name, exclude_globs):
                            stats["skipped"] += 1
                            continue
                        if _is_skippable_attr(entry, cfg):
                            stats["skipped"] += 1
                            continue
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError as e:
                            log_error(entry.path, f"stat 실패: {e}")
                            continue
                        size = st.st_size
                        if size < min_size:
                            continue
                        ino = getattr(st, "st_ino", 0)
                        if ino:
                            key = (getattr(st, "st_dev", 0), ino)
                            if key in seen_inodes:
                                continue  # 하드링크: 같은 실체는 한 번만
                            seen_inodes.add(key)
                        mtime = st.st_mtime
                        path = entry.path
                        ext = os.path.splitext(name)[1].lower()
                        prev = existing.get(path)
                        if prev and prev[0] == size and abs(prev[1] - mtime) < 1e-6:
                            # 변경 없음 → 기존 해시 유지 (seen=1 만 갱신)
                            conn.execute("UPDATE files SET seen=1 WHERE path=?", (path,))
                            stats["reused"] += 1
                        else:
                            batch.append((path, name, ext, size, mtime, None, None))
                            stats["new_meta"] += 1
                        stats["files"] += 1
                        if len(batch) >= 500:
                            flush()
                except OSError as e:
                    log_error(getattr(entry, "path", dir_path), str(e))

    def flush_junk():
        if junk_batch:
            conn.executemany("INSERT OR REPLACE INTO junk VALUES(?,?,?,?)", junk_batch)
            junk_batch.clear()

    conn.execute("DELETE FROM scan_errors")
    cancelled = False
    try:
        for root in cfg.get("scan_roots", []):
            if os.path.isdir(root):
                print(f"[스캔] {root}")
                recurse(root)
            else:
                print(f"[스캔] 건너뜀 (폴더 없음): {root}")
    except Cancelled:
        cancelled = True
    flush()
    flush_junk()
    if cancelled:
        # 취소: 부분 색인은 보존하되, 이번에 못 본 파일을 지우지 않음(seen=0 유지)
        conn.commit()
        stats["cancelled"] = True
        stats["removed"] = 0
        return stats
    # 이번 스캔에서 사라진 파일 제거
    removed = conn.execute("DELETE FROM files WHERE seen=0").rowcount
    conn.commit()
    stats["removed"] = removed
    return stats


def _hash_workers(cfg) -> int:
    if cfg and cfg.get("hash_workers"):
        try:
            return max(1, int(cfg["hash_workers"]))
        except (TypeError, ValueError):
            pass
    return min(8, (os.cpu_count() or 4) * 2)


def _parallel_hash(conn, targets, hashfn, col, stage, err_msg,
                   progress, cancel, workers) -> int:
    """targets=[(path, extra)] 를 스레드풀로 해시하고 col 컬럼에 기록. 성공 수 반환.

    해시 계산(파일 읽기)은 병렬, DB 쓰기는 메인 스레드에서만 수행한다.
    """
    total = len(targets)
    if total == 0:
        return 0
    ok = done = 0
    updates, errors = [], []

    def _flush():
        if updates:
            conn.executemany(f"UPDATE files SET {col}=? WHERE path=?", updates)
            updates.clear()
        if errors:
            conn.executemany("INSERT INTO scan_errors(path,error,at) VALUES(?,?,?)", errors)
            errors.clear()
        conn.commit()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futmap = {ex.submit(hashfn, p, extra): p for p, extra in targets}
        for fut in as_completed(futmap):
            if _cancelled(cancel):
                ex.shutdown(wait=False, cancel_futures=True)
                _flush()
                raise Cancelled()
            path = futmap[fut]
            try:
                h = fut.result()
            except Exception:
                h = None
            done += 1
            if h is None:
                errors.append((path, err_msg, time.time()))
            else:
                updates.append((h, path))
                ok += 1
            if progress is not None and done % 50 == 0:
                progress(stage, done, total, path)
            if len(updates) >= 500:
                _flush()
    _flush()
    return ok


def compute_hashes(conn: sqlite3.Connection, progress=None, cancel=None,
                   cfg: dict | None = None) -> dict:
    """크기 중복 → 부분해시 → 전체해시 단계적 계산 (멀티코어 병렬)."""
    workers = _hash_workers(cfg)
    stats = {"partial": 0, "full": 0, "workers": workers}

    # 1) 크기가 2개 이상 겹치는 파일만 부분해시 대상
    partial_targets = conn.execute(
        """SELECT path, size FROM files
           WHERE partial_hash IS NULL
             AND size IN (SELECT size FROM files GROUP BY size HAVING COUNT(*) > 1)"""
    ).fetchall()
    stats["partial"] = _parallel_hash(
        conn, partial_targets, lambda p, s: partial_hash(p, s),
        "partial_hash", "hash", "부분해시 실패(잠김/권한)", progress, cancel, workers)

    # 2) (size, partial_hash) 가 겹치는 파일만 전체해시 대상
    full_targets = conn.execute(
        """SELECT path FROM files
           WHERE full_hash IS NULL AND partial_hash IS NOT NULL
             AND (size, partial_hash) IN (
                 SELECT size, partial_hash FROM files WHERE partial_hash IS NOT NULL
                 GROUP BY size, partial_hash HAVING COUNT(*) > 1)"""
    ).fetchall()
    full_targets = [(row[0], None) for row in full_targets]
    stats["full"] = _parallel_hash(
        conn, full_targets, lambda p, _: full_hash(p),
        "full_hash", "hash", "전체해시 실패(잠김/권한)", progress, cancel, workers)

    return stats


def find_empty_dirs(cfg: dict, conn: sqlite3.Connection, cancel=None) -> int:
    """엔트리가 0이거나 찌꺼기/빈 하위폴더만 남은 폴더를 수집(최대 빈 폴더만).

    scan_root 자체는 이동 대상에서 제외한다.
    """
    conn.execute("DELETE FROM empty_dirs")
    exclude_names = {n.lower() for n in cfg.get("exclude_dir_names", [])}
    junk_globs = cfg.get("junk_globs", [])
    quarantine = os.path.normcase(os.path.abspath(cfg.get("quarantine_dir", "")))
    roots = [os.path.abspath(r) for r in cfg.get("scan_roots", []) if os.path.isdir(r)]
    root_set = set(roots)
    empty_set: set[str] = set()

    def is_empty(d: str) -> bool:
        if _cancelled(cancel):
            raise Cancelled()
        try:
            entries = list(os.scandir(d))
        except OSError:
            return False
        only_ignorable = True
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    full = os.path.normcase(os.path.abspath(e.path))
                    if _is_excluded_dir(e.name, exclude_names):
                        only_ignorable = False
                        continue
                    if quarantine and full.startswith(quarantine):
                        continue
                    if _is_skippable_attr(e, cfg):
                        continue
                    if is_empty(e.path):
                        empty_set.add(os.path.abspath(e.path))
                    else:
                        only_ignorable = False
                elif e.is_file(follow_symlinks=False):
                    if junk_globs and _is_junk(e.name, junk_globs):
                        continue  # 찌꺼기만 있으면 사실상 빈 것
                    only_ignorable = False
            except OSError:
                only_ignorable = False
        return only_ignorable

    for root in roots:
        if is_empty(root):
            empty_set.add(root)

    # 최대 빈 폴더만(부모가 빈 폴더면 부모가 대표) + 스캔루트 제외
    maximal = []
    for d in empty_set:
        if d in root_set:
            continue
        parent = os.path.dirname(d)
        if parent in empty_set and parent not in root_set:
            continue
        maximal.append(d)
    conn.executemany("INSERT OR REPLACE INTO empty_dirs(path) VALUES(?)",
                     [(d,) for d in sorted(maximal)])
    conn.commit()
    return len(maximal)


def run_scan(cfg: dict, progress=None, cancel=None) -> dict:
    conn = connect()
    try:
        print("[1/3] 파일 목록 수집 중...")
        meta = walk(cfg, conn, progress=progress, cancel=cancel)
        print(
            f"      파일 {meta['files']}개 (신규/변경 {meta['new_meta']}, 재사용 {meta['reused']}), "
            f"건너뜀 {meta['skipped']}, 오류 {meta['errors']}, 사라짐 {meta.get('removed',0)}, "
            f"찌꺼기 {meta.get('junk',0)}"
        )
        if meta.get("cancelled"):
            return {**meta, "cancelled": True}
        print("[2/3] 중복 후보 해시 계산 중 (크기겹침 -> 부분 -> 전체)...")
        h = compute_hashes(conn, progress=progress, cancel=cancel, cfg=cfg)
        print(f"      부분해시 {h['partial']}개, 전체해시 {h['full']}개 계산 (병렬 {h.get('workers',1)})")
        print("[3/3] 이미지/문서/압축 내용 지문 계산 중...")
        from . import fingerprint
        fp = fingerprint.compute(conn, cfg, progress=progress, cancel=cancel)
        print(f"      이미지 {fp['img']} · 문서 {fp['txt']} · zip {fp['zip']} 지문(재사용 {fp['reused']})")
        ne = find_empty_dirs(cfg, conn, cancel=cancel)
        print(f"      빈 폴더 {ne}개")
        return {**meta, **h, **{f"fp_{k}": v for k, v in fp.items()}, "empty_dirs": ne}
    except Cancelled:
        print("[취소] 스캔이 멈췄습니다(부분 색인 보존).")
        return {"cancelled": True}
    finally:
        conn.close()
