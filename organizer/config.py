"""설정 로드 및 기본값 생성.

config.json 이 없으면 안전한 기본값으로 자동 생성한다.
모든 경로/패턴은 사용자가 직접 편집할 수 있다.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# 프로젝트 루트: 일반 실행이면 소스 폴더, exe(frozen)면 exe 가 있는 폴더.
# (onefile exe 는 임시폴더에 풀리므로 config/data 는 exe 옆에 저장해야 보존됨)
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
DATA_DIR = PROJECT_ROOT / "data"
CATALOG_DB = DATA_DIR / "catalog.db"
REPORT_HTML = DATA_DIR / "report.html"


def _default_user_dirs() -> list[str]:
    """현재 사용자의 대표 폴더 목록 (존재하는 것만)."""
    home = Path(os.path.expanduser("~"))
    candidates = [
        home / "Desktop",
        home / "Documents",
        home / "Downloads",
        home / "Pictures",
        home / "Videos",
        home / "OneDrive" / "Desktop",
        home / "OneDrive" / "문서",
        home / "바탕 화면",
        home / "문서",
        home / "다운로드",
    ]
    found = []
    seen = set()
    for c in candidates:
        try:
            if c.is_dir():
                key = str(c).lower()
                if key not in seen:
                    seen.add(key)
                    found.append(str(c))
        except OSError:
            continue
    if not found:
        found = [str(home)]
    return found


def default_config() -> dict:
    home = Path(os.path.expanduser("~"))
    quarantine = home / "Desktop" / "_정리보관"
    return {
        "_설명": "파일 정리 도구 설정. scan_roots 에 검사할 폴더를 추가/수정하세요.",
        "scan_roots": _default_user_dirs(),
        # 폴더 이름이 이 목록과 (대소문자 무시) 일치하면 통째로 건너뜀
        "exclude_dir_names": [
            "Windows", "Program Files", "Program Files (x86)", "ProgramData",
            "AppData", "$Recycle.Bin", "System Volume Information",
            "node_modules", ".git", ".svn", "__pycache__", ".venv", "venv",
            "_정리보관",  # 격리폴더 자기 자신
        ],
        # 파일 이름이 이 glob 패턴과 일치하면 건너뜀
        "exclude_file_globs": [
            "*.tmp", "*.temp", "~$*", "*.lnk", "desktop.ini",
            "*.sys", "*.part", "*.crdownload",
        ],
        # 이 크기 미만 파일은 무시 (바이트). 기본 100KB — 작은 중복도 잡되 소음은 억제.
        "min_size_bytes": 102_400,
        # 정리 대상 파일을 옮길 격리 폴더
        "quarantine_dir": str(quarantine),
        # true 면 휴지통으로 보냄(send2trash 필요), false 면 격리폴더로 이동
        "use_recycle_bin": False,
        # true 면 각 파일이 있던 '그 드라이브 안'에 격리폴더를 만들어 이동
        # (외장/USB 정리 시 드라이브 간 복사 방지). use_recycle_bin 이 켜져 있으면 무시됨.
        "quarantine_per_drive": False,
        # 심볼릭 링크/정션 따라가지 않기 (무한루프·클라우드 다운로드 방지)
        "follow_symlinks": False,
        # 클라우드 전용(offline) 파일 건너뛰기 (OneDrive 등 다운로드 유발 방지)
        "skip_offline_files": True,
        # 버전 이상 탐지에서 "최신 의미" 키워드 (뒤로 갈수록 최신으로 간주)
        "version_keywords": [
            "원본", "초안", "draft", "v1", "구버전", "old",
            "수정", "revised", "rev", "v2", "v3",
            "최종", "final", "fin",
            "진짜최종", "finalfinal", "real_final", "최종본", "최최종",
        ],
        # 이미지 시각중복: "strict"=dHash 정확일치(엄격), "loose"=비슷한 사진도 묶기
        "image_match": "strict",
        "image_threshold": 6,        # loose 일 때 허용 해밍거리(작을수록 엄격)
        # 비슷한 문서(근접 중복) 탐지: 약간 수정된 문서도 유사도로 묶기
        # 캘리브레이션: 소편집 1.00·중편집 0.55·대편집 0.02·무관 0.00 → 0.5 면 소·중편집만 묶임
        "text_near_match": True,
        "text_near_threshold": 0.5,   # 0~1, 클수록 더 비슷해야 묶임
        # 영상 perceptual 중복(재인코딩·해상도 다른 같은 영상). 느림.
        # 캘리브레이션: 동일영상 재인코딩 해밍 ≤4, 무관영상 ≥155 → 40 이면 안전(4≪40≪155)
        "video_match": True,
        "video_threshold": 40,        # 키프레임 dHash 누적 해밍거리 허용치
        # OCR(글자 없는 PDF/이미지에서 글자 추출 → 문서중복 합류). 매우 느림.
        "ocr_enabled": True,
        "ocr_images": True,           # 이미지 파일도 OCR (False면 스캔 PDF만)
        "ocr_max_pages": 10,          # PDF OCR 최대 페이지
        "ocr_min_px": 200,            # 긴 변이 이보다 작으면 OCR 생략(아이콘/썸네일)
        "ocr_max_px": 2000,           # 긴 변이 이보다 크면 축소 후 OCR
        # 오디오 perceptual 중복(재인코딩 음악). 실험적·정확도 낮음 → 기본 OFF.
        # 캘리브레이션: 동일군(해밍 0~23)과 무관군(20~36) 분포가 겹침 → 자체지문 신뢰도 낮음.
        # 보수적으로 10 유지(엄격→오탐 최소). 켜도 정확도 한계 있음(튜닝_결과.md 참고).
        "audio_match": False,
        "audio_threshold": 10,        # 오디오 지문(63비트) 해밍거리 허용치
        # 무거운 작업(영상/오디오/RAW/OCR) 동시 실행 수(0=자동: cpu//4, 최소 2)
        "media_workers": 0,
        # 이미지 해시: "dhash"(기본·빠름) 또는 "phash"(DCT, 단조이미지 정밀도↑)
        "image_hash": "dhash",
        # 회전/반전된 사진도 같은 것으로 매칭(다이히드럴 canonical).
        # 기본 OFF: 켜면 회전본도 잡지만 재압축과 겹치면 가끔 놓칠 수 있음(실험적).
        "image_rotation": False,
        # 병렬 해시 워커 수(0/미설정 = 자동)
        "hash_workers": 0,
        # 격리폴더 자동 비우기: N일 지난 격리 항목을 휴지통으로(0 = 사용 안 함)
        "quarantine_purge_days": 0,
        # 시스템 찌꺼기 파일(맥/윈도우) — 크기 제한과 무관하게 찾아 정리 대상으로.
        # 삭제 안전한 것만 기본 포함(desktop.ini 는 폴더 설정일 수 있어 제외).
        "junk_globs": [
            ".DS_Store", "._*", ".AppleDouble", ".LSOverride", ".Spotlight-V100",
            ".Trashes", ".fseventsd", ".TemporaryItems", ".apdisk", ".localized",
            "__MACOSX", "Thumbs.db", "ehthumbs.db",
        ],
    }


def ensure_config() -> dict:
    """config.json 을 읽고, 없으면 기본값으로 생성한다."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        cfg = default_config()
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print(f"[설정] 기본 설정 파일을 생성했습니다: {CONFIG_PATH}")
        print("       검사할 폴더를 바꾸려면 이 파일의 scan_roots 를 편집하세요.")
        return cfg
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError):
        cfg = {}
    # 기본값과 병합 (새 키가 추가돼도 깨지지 않도록)
    merged = default_config()
    merged.update(cfg)

    # 다른 PC로 옮겨졌는지 감지: 검사 폴더가 '전부' 존재하지 않으면
    # 이 PC 기준(현재 사용자 폴더)으로 자동 재설정 → 복사 후 바로 사용 가능.
    roots = merged.get("scan_roots", [])
    if roots and all(not os.path.isdir(r) for r in roots):
        merged["scan_roots"] = _default_user_dirs()
        home = Path(os.path.expanduser("~"))
        merged["quarantine_dir"] = str(home / "Desktop" / "_정리보관")
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
        print("[설정] 다른 PC로 인식 → 검사 폴더를 이 PC 기준으로 재설정했습니다.")
    return merged


def load_config() -> dict:
    return ensure_config()
