"""환경 점검: 어떤 선택 기능이 지금 PC에서 동작 가능한지 확인.

새 PC로 옮겼을 때 무엇이 빠졌는지 한눈에 보여준다(설치 안내 포함).
모든 점검은 로컬에서만 수행하며, 외부로 아무것도 전송하지 않는다.
"""

from __future__ import annotations

import importlib.util


def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _fpcalc_ok() -> bool:
    try:
        from . import fingerprint
        return fingerprint._fpcalc_exe() is not None
    except Exception:
        return False


def _ffmpeg_ok() -> bool:
    if not _has("imageio_ffmpeg"):
        return False
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        import os
        return bool(exe) and os.path.exists(exe)
    except Exception:
        return False


# (기능키, 한글이름, 점검함수, 무엇을 가능케 하나, 없을 때 설치 힌트)
_CHECKS = [
    ("image", "비슷한 이미지",
     lambda: _has("PIL"),
     "리사이즈/재압축된 같은 사진 탐지",
     "pip install Pillow"),
    ("heic", "아이폰 이미지(HEIC/AVIF)",
     lambda: _has("pillow_heif"),
     "HEIC/HEIF/AVIF 사진도 이미지 중복에 합류",
     "pip install pillow-heif"),
    ("raw", "RAW 사진",
     lambda: _has("rawpy"),
     "CR2/NEF/ARW 등 RAW 사진 시각 중복",
     "pip install rawpy"),
    ("phash", "정밀 이미지 해시(pHash)",
     lambda: _has("numpy"),
     "단조로운 이미지 정밀도 향상(image_hash=phash)",
     "pip install numpy"),
    ("pdf", "PDF 문서 내용 비교",
     lambda: _has("pypdf"),
     "PDF 글자 추출 → 같은 내용 문서 묶기",
     "pip install pypdf"),
    ("hwp", "한글(.hwp) 문서",
     lambda: _has("olefile"),
     "구버전 한글 .hwp 글자 추출",
     "pip install olefile"),
    ("sevenzip", "7z 압축",
     lambda: _has("py7zr"),
     ".7z 내용 비교 및 .7z↔.zip 교차 탐지",
     "pip install py7zr"),
    ("video", "비슷한 영상",
     _ffmpeg_ok,
     "재인코딩/해상도만 다른 같은 영상 탐지",
     "pip install imageio-ffmpeg"),
    ("audio_cp", "오디오 정밀(chromaprint)",
     lambda: _fpcalc_ok(),
     "음악 재인코딩본을 정확히 매칭(없으면 로컬 chroma로 대체)",
     "winget install -e --id AcoustID.Chromaprint"),
    ("ocr", "OCR(스캔본 글자 인식)",
     lambda: _has("rapidocr_onnxruntime") and _has("fitz"),
     "글자 없는 스캔 PDF/이미지에서 글자 추출",
     "pip install rapidocr-onnxruntime PyMuPDF"),
    ("trash", "휴지통으로 보내기",
     lambda: _has("send2trash"),
     "격리폴더 대신 Windows 휴지통으로 이동",
     "pip install send2trash"),
]

# 없으면 핵심 기능까지 막히는 필수 기능키(영상/OCR 등은 선택이라 제외)
CORE_KEYS = {"image", "pdf"}


def check() -> list[dict]:
    """각 기능의 가용성 목록을 반환. GUI/CLI 양쪽에서 사용."""
    out = []
    for key, name, probe, enables, hint in _CHECKS:
        try:
            ok = bool(probe())
        except Exception:
            ok = False
        out.append({
            "key": key, "name": name, "ok": ok,
            "enables": enables, "hint": hint,
            "core": key in CORE_KEYS,
        })
    return out


def summary_line() -> str:
    """한 줄 요약: '이미지 OK · 영상 OK · OCR 없음 ...'"""
    parts = []
    for c in check():
        parts.append(f"{c['name']} {'OK' if c['ok'] else 'X'}")
    return " · ".join(parts)


def format_report() -> str:
    """CLI/GUI 표시용 여러 줄 텍스트."""
    rows = check()
    lines = ["[환경 점검] 선택 기능 가용성 (없어도 핵심 중복탐지는 동작)\n"]
    missing = []
    for c in rows:
        mark = "OK " if c["ok"] else "-- "
        tag = "(핵심)" if c["core"] else ""
        lines.append(f"  [{mark}] {c['name']}{tag} : {c['enables']}")
        if not c["ok"]:
            lines.append(f"          → 설치: {c['hint']}")
            missing.append(c)
    lines.append("")
    if not missing:
        lines.append("모든 선택 기능 사용 가능. 추가 설치 필요 없음.")
    else:
        core_missing = [c for c in missing if c["core"]]
        if core_missing:
            lines.append("⚠ 핵심 기능 일부가 비활성입니다. 'pip install -r requirements.txt' 권장.")
        else:
            lines.append("일부 선택 기능이 비활성입니다(없어도 정상 동작). 필요하면 위 명령으로 설치.")
        lines.append("한 번에 설치: py -m pip install -r requirements.txt  (또는 설치.bat 실행)")
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_report())
