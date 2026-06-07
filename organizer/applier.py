"""decisions.json 을 읽어 선택 파일을 격리폴더로 이동(또는 휴지통)하고
되돌리기(undo) 매니페스트를 남긴다.

안전 원칙:
  - 기본은 드라이런(dry-run): 실제 이동하지 않고 목록만 출력
  - --apply 플래그가 있을 때만 실제 이동
  - 영구 삭제 코드는 존재하지 않음 (이동만)
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from . import config
from .hashing import long_path


def _load_decisions(path: Path) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    moves = data.get("move", [])
    # 존재하는 파일만, 중복 제거
    seen, result = set(), []
    for p in moves:
        ap = os.path.abspath(p)
        key = os.path.normcase(ap)
        if key in seen:
            continue
        seen.add(key)
        result.append(ap)
    return result


def _quarantine_base(cfg: dict, ts: str, src: str) -> Path:
    """이 파일을 옮길 격리 루트. per_drive 면 파일과 같은 드라이브 안에 만든다."""
    if cfg.get("quarantine_per_drive"):
        drive, _ = os.path.splitdrive(os.path.abspath(src))
        if not drive:
            drive = os.path.splitdrive(os.path.abspath(cfg["quarantine_dir"]))[0]
        folder = os.path.basename(os.path.normpath(cfg.get("quarantine_dir", "_정리보관"))) or "_정리보관"
        return Path(drive + os.sep) / folder / ts
    return Path(cfg["quarantine_dir"]) / ts


def _quarantine_target(quarantine_base: Path, src: str, per_drive: bool) -> Path:
    """원본 경로 구조를 보존한 격리 대상 경로 생성."""
    drive, rest = os.path.splitdrive(src)
    rel = rest.lstrip("\\/")
    if per_drive:
        # 이미 같은 드라이브이므로 드라이브 폴더는 생략
        target = quarantine_base / rel
    else:
        drive_label = drive.replace(":", "").strip("\\/") or "root"
        target = quarantine_base / drive_label / rel
    # 이름 충돌 회피 (긴 경로도 정확히 판정)
    if os.path.exists(long_path(str(target))):
        stem = target.stem
        suffix = target.suffix
        i = 1
        while True:
            cand = target.with_name(f"{stem}_{i}{suffix}")
            if not os.path.exists(long_path(str(cand))):
                target = cand
                break
            i += 1
    return target


def _try_send2trash(path: str) -> bool:
    try:
        from send2trash import send2trash  # type: ignore
    except ImportError:
        return False
    send2trash(path)
    return True


def apply_decisions(decisions_path: Path, cfg: dict, do_apply: bool = False) -> dict:
    moves = _load_decisions(decisions_path)
    if not moves:
        print("[적용] decisions.json 에 이동할 파일이 없습니다.")
        return {"planned": 0, "moved": 0, "errors": 0}

    use_trash = cfg.get("use_recycle_bin", False)
    per_drive = bool(cfg.get("quarantine_per_drive")) and not use_trash
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    if use_trash:
        mode_label = "휴지통"
    elif per_drive:
        mode_label = "격리폴더(각 드라이브별)"
    else:
        mode_label = "격리폴더"
    print(f"[적용] 대상 {len(moves)}개 · 모드: {mode_label} "
          f"· {'실제 실행' if do_apply else '드라이런(미실행)'}")
    if not do_apply:
        print("       (실제로 옮기려면 --apply 를 붙이세요)")

    manifest = []
    moved = errors = 0
    total_bytes = 0

    for src in moves:
        # 파일 또는 (빈)폴더 모두 허용
        if not os.path.exists(long_path(src)):
            print(f"   건너뜀(없음): {src}")
            continue
        try:
            size = os.path.getsize(long_path(src)) if os.path.isfile(long_path(src)) else 0
        except OSError:
            size = 0

        if not do_apply:
            dest = "(휴지통)" if use_trash else str(
                _quarantine_target(_quarantine_base(cfg, ts, src), src, per_drive))
            print(f"   이동예정: {src}\n          -> {dest}")
            total_bytes += size
            continue

        try:
            if use_trash and _try_send2trash(src):
                manifest.append({"src": src, "dest": "(recycle-bin)", "size": size})
            else:
                dest = _quarantine_target(_quarantine_base(cfg, ts, src), src, per_drive)
                os.makedirs(long_path(str(dest.parent)), exist_ok=True)
                shutil.move(long_path(src), long_path(str(dest)))
                manifest.append({"src": src, "dest": str(dest), "size": size})
            moved += 1
            total_bytes += size
        except (OSError, shutil.Error) as e:
            errors += 1
            print(f"   오류: {src} - {e}")

    if do_apply and manifest:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        undo_path = config.DATA_DIR / f"undo_{ts}.json"
        with open(undo_path, "w", encoding="utf-8") as f:
            json.dump({
                "timestamp": ts,
                "mode": "recycle-bin" if use_trash else "quarantine",
                "per_drive": per_drive,
                "items": manifest,
            }, f, ensure_ascii=False, indent=2)
        print(f"\n[완료] {moved}개 이동 ({_fmt(total_bytes)}), 오류 {errors}개")
        print(f"       되돌리기 기록: {undo_path}")
        print(f"       복원하려면: py main.py undo {ts}")
    elif not do_apply:
        print(f"\n[드라이런] 이동 예정 {len([m for m in moves if os.path.isfile(m)])}개 "
              f"(약 {_fmt(total_bytes)}). --apply 로 실제 실행하세요.")

    return {"planned": len(moves), "moved": moved, "errors": errors}


def undo(timestamp: str) -> dict:
    undo_path = config.DATA_DIR / f"undo_{timestamp}.json"
    if not undo_path.exists():
        print(f"[되돌리기] 기록을 찾을 수 없음: {undo_path}")
        return {"restored": 0, "errors": 1}
    with open(undo_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("mode") == "recycle-bin":
        print("[되돌리기] 이 작업은 휴지통으로 보냈습니다. Windows 휴지통에서 직접 복원하세요.")
        return {"restored": 0, "errors": 0}

    restored = errors = 0
    for item in data.get("items", []):
        src, dest = item["src"], item["dest"]
        try:
            if not os.path.exists(long_path(dest)):
                print(f"   건너뜀(격리본 없음): {dest}")
                continue
            os.makedirs(long_path(os.path.dirname(src)), exist_ok=True)
            if os.path.exists(long_path(src)):
                print(f"   건너뜀(원위치에 이미 파일 있음): {src}")
                continue
            shutil.move(long_path(dest), long_path(src))
            restored += 1
        except (OSError, shutil.Error) as e:
            errors += 1
            print(f"   오류: {dest} -> {src} - {e}")
    print(f"[되돌리기] {restored}개 복원, 오류 {errors}개")
    return {"restored": restored, "errors": errors}


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(long_path(os.path.join(root, f)))
            except OSError:
                pass
    return total


def purge_quarantine(cfg: dict, days=None, do_apply: bool = False) -> dict:
    """격리폴더의 날짜별 보관본을 휴지통으로 보낸다(영구삭제 아님).

    days=None 이면 전체, 정수면 그만큼 지난 보관본만. send2trash 가 없으면 비우지 않음.
    """
    qroot = Path(cfg.get("quarantine_dir", ""))
    res = {"groups": 0, "bytes": 0, "purged": 0, "errors": 0, "no_trash": False, "items": []}
    if not qroot.exists():
        return res
    cutoff = (time.time() - days * 86400) if days else None
    targets = []
    for child in sorted(qroot.iterdir()):
        if not child.is_dir():
            continue
        try:
            mt = child.stat().st_mtime
        except OSError:
            continue
        if cutoff is None or mt < cutoff:
            targets.append(child)
    res["groups"] = len(targets)
    res["bytes"] = sum(_dir_size(str(t)) for t in targets)
    res["items"] = [str(t) for t in targets]
    if not do_apply or not targets:
        return res

    try:
        from send2trash import send2trash  # type: ignore
    except ImportError:
        res["no_trash"] = True
        return res
    for t in targets:
        try:
            send2trash(str(t))
            res["purged"] += 1
        except OSError as e:
            res["errors"] += 1
            print(f"   격리 비우기 오류: {t} - {e}")
    print(f"[격리 비우기] {res['purged']}개 보관본을 휴지통으로 ({_fmt(res['bytes'])})")
    return res


def _fmt(b: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    n = float(b)
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.1f} {units[i]}" if i else f"{int(n)} {units[i]}"
