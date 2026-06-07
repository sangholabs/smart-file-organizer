"""중복 그룹에서 '보관할 1개(keeper)' 선택 — 사본보다 원본을 우선.

점수가 낮을수록 보관 우선. 완전중복·이미지·문서·zip·근접문서 등 모든 그룹에서 공용.
analyzer ↔ fingerprint 순환참조를 피하려고 별도 모듈로 둔다.
"""

from __future__ import annotations

import os
import re

# 경로에 이 단어가 들어가면 '사본/임시'로 보고 보관 우선순위를 낮춤(패널티)
_PENALTY_DIR = ("download", "다운로드", "temp", "임시", "tmp", "cache", "캐시",
                "$recycle", "복사본", "정리보관", "_정리보관", "new folder", "새 폴더")
# 파일명에 이런 사본 표시가 있으면 패널티
_RE_COPY = re.compile(r"복사본|사본|copy|\(\d+\)|\d+\s*$|- ?copy", re.IGNORECASE)


def pick_keeper(files: list[dict]) -> str:
    """가장 '원본다운' 파일의 path 반환. files: [{"path","mtime",...}]"""
    best = None
    best_key = None
    for f in files:
        path = f.get("path", "")
        low = path.lower()
        name = os.path.splitext(os.path.basename(path))[0]
        penalty = 0
        if any(w in low for w in _PENALTY_DIR):
            penalty += 100
        if _RE_COPY.search(name):
            penalty += 50
        depth = low.replace("\\", "/").count("/")
        mtime = f.get("mtime") or 0
        # 낮을수록 우선: 패널티↓, 깊이↓, 오래된 것↓, 경로 짧은 것↓
        key = (penalty, depth, mtime, len(path))
        if best_key is None or key < best_key:
            best_key = key
            best = path
    return best
