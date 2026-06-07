"""대화형 메뉴 (한글). 배치 파일 대신 Python 이 메뉴를 처리해 인코딩 문제를 피한다.

정리도구.bat 이 이 파일을 실행한다.
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import main as cli  # noqa: E402
from organizer import config  # noqa: E402


def _utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


def pause():
    input("\n계속하려면 Enter 키를 누르세요...")


def list_undo_logs():
    logs = sorted(glob.glob(str(config.DATA_DIR / "undo_*.json")))
    out = []
    for p in logs:
        m = re.search(r"undo_(.+)\.json", os.path.basename(p))
        if m:
            out.append(m.group(1))
    return out


def do_scan():
    cli.cmd_scan(SimpleNamespace())


def do_report():
    cli.cmd_report(SimpleNamespace(no_open=False))


def do_apply(real: bool):
    cli.cmd_apply(SimpleNamespace(apply=real, decisions=None))


def do_undo():
    logs = list_undo_logs()
    if not logs:
        print("되돌릴 기록이 없습니다.")
        return
    print("\n되돌릴 수 있는 작업:")
    for i, ts in enumerate(logs, 1):
        print(f"  {i}. {ts}")
    sel = input("번호 선택 (취소: Enter): ").strip()
    if not sel.isdigit():
        print("취소됨")
        return
    idx = int(sel) - 1
    if 0 <= idx < len(logs):
        cli.cmd_undo(SimpleNamespace(timestamp=logs[idx]))
    else:
        print("잘못된 번호")


def main():
    _utf8()
    while True:
        print("\n" + "=" * 46)
        print("            파일 / 폴더 정리 도구")
        print("   (모든 처리는 이 PC 안에서만 실행됩니다)")
        print("=" * 46)
        print("  1. 스캔 (폴더 검사 + 색인)")
        print("  2. 리포트 생성 및 열기")
        print("  3. 적용 - 미리보기 (드라이런, 실제 이동 안 함)")
        print("  4. 적용 - 실제 이동 실행")
        print("  5. 되돌리기 (undo)")
        print("  6. 설정 파일 열기 (config.json)")
        print("  0. 종료")
        choice = input("\n번호를 입력하세요: ").strip()

        if choice == "1":
            do_scan(); pause()
        elif choice == "2":
            do_report(); pause()
        elif choice == "3":
            do_apply(real=False); pause()
        elif choice == "4":
            sure = input("정말 실제로 이동하시겠습니까? 격리폴더로 옮겨집니다 [y/N]: ").strip().lower()
            if sure == "y":
                do_apply(real=True)
            else:
                print("취소됨")
            pause()
        elif choice == "5":
            do_undo(); pause()
        elif choice == "6":
            config.ensure_config()
            os.startfile(str(config.CONFIG_PATH))  # noqa: S606
        elif choice == "0":
            break
        else:
            print("올바른 번호를 입력하세요.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
