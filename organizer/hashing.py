"""2단계 해시 (부분 → 전체).

대용량 파일에서 모든 파일을 전체 해시하면 매우 느리므로:
  1) 크기로 1차 그룹핑 (scanner/analyzer 단계)
  2) 부분 해시(앞 64KB + 뒤 64KB)로 2차 그룹핑
  3) 부분 해시까지 같은 파일만 전체 해시로 최종 확인

blake2b 는 표준 라이브러리이며 SHA-256 보다 빠르다.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

CHUNK = 64 * 1024          # 부분 해시용 청크 (앞/뒤 각 64KB)
READ_BLOCK = 1024 * 1024   # 전체 해시 읽기 블록 (1MB)


def long_path(path: str) -> str:
    r"""Windows 260자 경로 제한 우회용 \\?\ 접두사 부착."""
    if os.name != "nt":
        return path
    p = os.path.abspath(path)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):  # UNC 경로
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def partial_hash(path: str, size: int) -> str | None:
    """파일 앞 64KB + 뒤 64KB + 크기로 빠른 지문 생성.

    실패하면 None 반환 (잠긴 파일/권한 오류 등). 호출 측에서 건너뜀.
    """
    h = hashlib.blake2b(digest_size=16)
    h.update(str(size).encode())
    try:
        with open(long_path(path), "rb") as f:
            head = f.read(CHUNK)
            h.update(head)
            if size > CHUNK * 2:
                f.seek(-CHUNK, os.SEEK_END)
                tail = f.read(CHUNK)
                h.update(tail)
    except OSError:
        return None
    return h.hexdigest()


def full_hash(path: str) -> str | None:
    """파일 전체 내용 해시. 실패하면 None."""
    h = hashlib.blake2b(digest_size=32)
    try:
        with open(long_path(path), "rb") as f:
            while True:
                block = f.read(READ_BLOCK)
                if not block:
                    break
                h.update(block)
    except OSError:
        return None
    return h.hexdigest()
