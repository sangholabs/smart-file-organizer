"""파일/폴더 정리 도구 — CLI 진입점.

사용법:
  py main.py scan              # 폴더 스캔 + 해시 색인
  py main.py report            # 분석 후 HTML 리포트 생성 (자동으로 열기)
  py main.py apply             # decisions.json 드라이런 (실제 이동 안 함)
  py main.py apply --apply     # 실제로 격리폴더/휴지통으로 이동
  py main.py undo <timestamp>  # 이동을 원위치로 복원
  py main.py doctor            # 환경 점검(어떤 선택 기능이 동작하는지)

모든 처리는 이 PC 안에서만 이루어지며, 파일 내용은 외부로 전송되지 않습니다.
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path

from organizer import analyzer, applier, config, doctor, reporter, scanner


def cmd_scan(args):
    cfg = config.load_config()
    print("검사 대상 폴더:")
    for r in cfg.get("scan_roots", []):
        print(f"  - {r}")
    print(f"최소 파일 크기: {cfg.get('min_size_bytes', 0):,} bytes\n")
    scanner.run_scan(cfg)
    print("\n완료. 다음 단계: py main.py report")


def cmd_report(args):
    cfg = config.load_config()
    if not config.CATALOG_DB.exists():
        print("색인이 없습니다. 먼저 'py main.py scan' 을 실행하세요.")
        return 1
    print("분석 중...")
    result = analyzer.analyze(cfg=cfg)
    st = result["stats"]
    print(f"  완전중복 {st['exact_dup_groups']}그룹 · 이름충돌 {st['name_conflict_groups']}그룹 · "
          f"버전이상 {st['version_anomaly_groups']}그룹 · 회수가능 {applier._fmt(st['reclaimable_bytes'])}")
    extra = [
        ("비슷한이미지", "image_dups"), ("같은내용문서", "doc_dups"),
        ("비슷한문서", "doc_near"), ("비슷한영상", "video_dups"),
        ("비슷한오디오", "audio_dups"), ("같은내용압축", "zip_dups"),
        ("풀린압축", "archive_loose"), ("시스템찌꺼기", "junk_files"),
        ("빈폴더", "empty_dirs"),
    ]
    parts = [f"{label} {len(result.get(key, []))}그룹" for label, key in extra
             if result.get(key)]
    if parts:
        print("  " + " · ".join(parts))
    out = reporter.build_report(result)
    print(f"\n리포트 생성: {out}")
    if not args.no_open:
        try:
            webbrowser.open(out.as_uri())
        except Exception:
            pass
    print("리포트에서 정리할 파일을 체크 → '결정 내보내기' → decisions.json 을 data 폴더에 저장 후")
    print("  py main.py apply         (미리보기)")
    print("  py main.py apply --apply (실제 이동)")


def cmd_apply(args):
    cfg = config.load_config()
    dpath = Path(args.decisions) if args.decisions else (config.DATA_DIR / "decisions.json")
    if not dpath.exists():
        print(f"decisions.json 을 찾을 수 없습니다: {dpath}")
        print("리포트에서 '결정 내보내기' 로 받은 파일을 data 폴더에 두거나 --decisions 로 경로를 지정하세요.")
        return 1
    applier.apply_decisions(dpath, cfg, do_apply=args.apply)


def cmd_undo(args):
    applier.undo(args.timestamp)


def cmd_doctor(args):
    print(doctor.format_report())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="파일/폴더 정리 도구 (로컬 전용)")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("scan", help="폴더 스캔 + 해시 색인")
    sp.set_defaults(func=cmd_scan)

    rp = sub.add_parser("report", help="분석 후 HTML 리포트 생성")
    rp.add_argument("--no-open", action="store_true", help="브라우저 자동 열기 안 함")
    rp.set_defaults(func=cmd_report)

    ap = sub.add_parser("apply", help="decisions.json 적용 (기본 드라이런)")
    ap.add_argument("--apply", action="store_true", help="실제로 이동 실행")
    ap.add_argument("--decisions", help="decisions.json 경로 (기본: data/decisions.json)")
    ap.set_defaults(func=cmd_apply)

    up = sub.add_parser("undo", help="이동 되돌리기")
    up.add_argument("timestamp", help="undo_<timestamp> 의 타임스탬프")
    up.set_defaults(func=cmd_undo)

    dp = sub.add_parser("doctor", help="환경 점검(선택 기능 가용성 확인)")
    dp.set_defaults(func=cmd_doctor)

    return p


def _force_utf8_console():
    """Windows 콘솔에서 한글이 깨지지 않도록 stdout/stderr 를 UTF-8 로."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def main(argv=None):
    _force_utf8_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
