# -*- coding: utf-8 -*-
"""정리 도구 핵심 로직 검증 (stdlib unittest, 완전 격리).

각 테스트는 임시폴더 + 임시 catalog.db 를 사용하며 실제 사용자 데이터/설정을 건드리지 않는다.
실행:  py -m unittest -v tests.test_organizer
       또는  py tests/test_organizer.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from organizer import analyzer, applier, config, hashing, scanner  # noqa: E402

CHUNK = hashing.CHUNK  # 64KB


def days_ago(d):
    return time.time() - d * 86400


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orgtest_"))
        self.src = self.tmp / "src"
        self.quar = self.tmp / "_정리보관"
        self.db = self.tmp / "catalog.db"
        self.src.mkdir(parents=True)
        # applier 의 undo 로그가 임시폴더로 가도록 DATA_DIR 우회
        self._old_data = config.DATA_DIR
        config.DATA_DIR = self.tmp / "data"
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        config.DATA_DIR = self._old_data
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----
    def cfg(self, **over):
        c = {
            "scan_roots": [str(self.src)],
            "exclude_dir_names": ["_정리보관", "__skip__"],
            "exclude_file_globs": ["*.tmp"],
            "min_size_bytes": 0,
            "quarantine_dir": str(self.quar),
            "use_recycle_bin": False,
            "quarantine_per_drive": False,
            "follow_symlinks": False,
            "skip_offline_files": True,
            "version_keywords": ["원본", "초안", "draft", "v1", "구버전", "old",
                                 "수정", "revised", "rev", "v2", "v3",
                                 "최종", "final", "fin", "진짜최종", "최종본", "최최종"],
        }
        c.update(over)
        return c

    def write(self, rel, data: bytes, d=1.0):
        p = self.src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        t = days_ago(d)
        os.utime(p, (t, t))
        return p

    def scan(self, cfg=None):
        cfg = cfg or self.cfg()
        conn = scanner.connect(self.db)
        scanner.walk(cfg, conn)
        scanner.compute_hashes(conn)
        conn.close()
        return cfg

    def analyze(self, cfg=None):
        cfg = cfg or self.cfg()
        return analyzer.analyze(db_path=self.db, cfg=cfg)

    def scan_analyze(self, cfg=None):
        cfg = cfg or self.cfg()
        self.scan(cfg)
        return self.analyze(cfg)


# =====================================================================
# A. 중복 탐지 정확성 — "내용이 다른데 정리되는" 사고 방지가 핵심
# =====================================================================
class TestDuplicateAccuracy(Base):
    def test_identical_grouped(self):
        data = b"A" * (200 * 1024)
        self.write("folder1/report.bin", data, 10)
        self.write("folder2/copy_of_report.bin", data, 3)
        a = self.analyze_after()
        self.assertEqual(a["stats"]["exact_dup_groups"], 1)
        g = a["exact_duplicates"][0]
        self.assertEqual(g["count"], 2)
        moves = [f for f in g["files"] if f["recommend"] == "move"]
        keeps = [f for f in g["files"] if f["recommend"] == "keep"]
        self.assertEqual(len(keeps), 1)          # 최소 1개 보관
        self.assertEqual(len(moves), 1)
        self.assertEqual(a["stats"]["reclaimable_bytes"], len(data))

    def test_same_size_diff_middle_not_dup(self):
        """200KB, 앞/뒤 64KB는 같고 중간만 다름 → 부분해시는 같아도 전체해시로 구분."""
        size = 200 * 1024
        d1 = bytearray(b"X" * size)
        d2 = bytearray(b"X" * size)
        d2[size // 2] = ord("Y")   # 중간 1바이트만 변경
        self.write("a.bin", bytes(d1), 5)
        self.write("b.bin", bytes(d2), 5)
        a = self.analyze_after()
        self.assertEqual(a["stats"]["exact_dup_groups"], 0,
                         "내용이 다른 파일이 중복으로 묶이면 안 됨")

    def test_same_size_diff_after_head_not_dup(self):
        """100KB(64~128KB 구간), 앞 64KB 동일·이후만 다름 → 중복 아님."""
        size = 100 * 1024
        d1 = bytearray(b"Z" * size)
        d2 = bytearray(b"Z" * size)
        d2[70 * 1024] = ord("Q")
        self.write("c.bin", bytes(d1), 2)
        self.write("d.bin", bytes(d2), 2)
        a = self.analyze_after()
        self.assertEqual(a["stats"]["exact_dup_groups"], 0)

    def test_small_diff_not_dup(self):
        self.write("s1.txt", b"hello world A", 1)
        self.write("s2.txt", b"hello world B", 1)
        a = self.analyze_after()
        self.assertEqual(a["stats"]["exact_dup_groups"], 0)

    def test_three_copies_keeper_shortest(self):
        data = b"K" * (150 * 1024)
        self.write("deep/very/long/name_report.bin", data, 1)
        self.write("r.bin", data, 9)            # 가장 짧은 경로 → 보관 후보
        self.write("mid/r.bin", data, 5)
        a = self.analyze_after()
        g = a["exact_duplicates"][0]
        self.assertEqual(g["count"], 3)
        keeper = [f for f in g["files"] if f["recommend"] == "keep"][0]
        self.assertTrue(keeper["path"].endswith("r.bin"))
        self.assertEqual(a["stats"]["reclaimable_bytes"], 2 * len(data))

    def test_zero_byte_not_grouped(self):
        """0바이트 파일들은 '중복'으로 묶지 않는다(노이즈 방지)."""
        self.write("empty1.dat", b"", 1)
        self.write("empty2.dat", b"", 1)
        self.write("sub/empty3.dat", b"", 1)
        a = self.analyze_after()
        self.assertEqual(a["stats"]["exact_dup_groups"], 0)

    def analyze_after(self):
        return self.scan_analyze()


# =====================================================================
# B. 이름 같음 · 내용 다름
# =====================================================================
class TestNameConflict(Base):
    def test_name_conflict_diff_content(self):
        self.write("A/budget.xlsx", b"OLD" * 100, 30)
        self.write("B/budget.xlsx", b"NEW-DATA-DIFFERENT" * 100, 1)
        a = self.scan_analyze()
        self.assertEqual(a["stats"]["name_conflict_groups"], 1)
        g = a["name_conflicts"][0]
        self.assertEqual(g["distinct"], 2)
        self.assertTrue(all(f["recommend"] == "review" for f in g["files"]))

    def test_identical_same_name_is_dup_not_conflict(self):
        data = b"SAME" * 5000
        self.write("A/plan.doc", data, 5)
        self.write("B/plan.doc", data, 2)
        a = self.scan_analyze()
        self.assertEqual(a["stats"]["name_conflict_groups"], 0)
        self.assertEqual(a["stats"]["exact_dup_groups"], 1)

    def test_case_insensitive_name(self):
        self.write("A/Report.PDF", b"contentAAA" * 50, 3)
        self.write("B/report.pdf", b"contentBBB-different" * 50, 1)
        a = self.scan_analyze()
        self.assertEqual(a["stats"]["name_conflict_groups"], 1)


# =====================================================================
# C. 버전 이상 (휴리스틱) — 기본 미체크(review)라 자동 정리되지 않아야 함
# =====================================================================
class TestVersionAnomaly(Base):
    def test_anomaly_flagged(self):
        self.write("proposal_v1.docx", b"v1-content-aaaa" * 20, 1)        # 최근 수정
        self.write("proposal_최종.docx", b"final-content-bb" * 20, 30)     # 이름은 최종, 더 옛날
        a = self.scan_analyze()
        self.assertEqual(a["stats"]["version_anomaly_groups"], 1)
        g = a["version_anomalies"][0]
        self.assertTrue(g["claimed_latest"].endswith("최종.docx"))
        self.assertTrue(g["mtime_latest"].endswith("v1.docx"))
        self.assertTrue(all(f["recommend"] == "review" for f in g["files"]))

    def test_consistent_versions_no_anomaly(self):
        self.write("doc_v1.docx", b"one" * 30, 30)
        self.write("doc_v2.docx", b"two-diff" * 30, 1)   # 최신 버전이 실제로도 최근
        a = self.scan_analyze()
        self.assertEqual(a["stats"]["version_anomaly_groups"], 0)

    def test_unrelated_names_not_grouped(self):
        self.write("apple.docx", b"aaa" * 40, 5)
        self.write("banana.docx", b"bbbbb" * 40, 1)
        a = self.scan_analyze()
        self.assertEqual(a["stats"]["version_anomaly_groups"], 0)

    def test_substring_not_overgrouped(self):
        """'자료'와 '원본자료'는 서로 다른 파일 → 한 가족으로 묶지 않는다."""
        self.write("자료.docx", b"data-aaa" * 30, 1)        # 최근
        self.write("원본자료.docx", b"orig-bbbb" * 30, 30)   # 옛날
        a = self.scan_analyze()
        self.assertEqual(a["stats"]["version_anomaly_groups"], 0)

    def test_identical_content_versions_no_anomaly(self):
        data = b"identical" * 100
        self.write("memo_v1.txt", data, 1)
        self.write("memo_최종.txt", data, 30)
        a = self.scan_analyze()
        # 내용이 같으면 버전 이상(내용 손실 위험)이 아님 → 완전중복으로만 처리
        self.assertEqual(a["stats"]["version_anomaly_groups"], 0)


# =====================================================================
# D. 스캐너 견고성
# =====================================================================
class TestScanner(Base):
    def test_min_size_filter(self):
        self.write("big.bin", b"A" * 2048, 1)
        self.write("tiny.bin", b"A" * 10, 1)
        cfg = self.cfg(min_size_bytes=1024)
        self.scan(cfg)
        conn = scanner.connect(self.db)
        names = {r[0] for r in conn.execute("SELECT name FROM files")}
        conn.close()
        self.assertIn("big.bin", names)
        self.assertNotIn("tiny.bin", names)

    def test_exclude_dir_and_glob(self):
        self.write("keep.bin", b"A" * 2000, 1)
        self.write("__skip__/hidden.bin", b"A" * 2000, 1)
        self.write("note.tmp", b"A" * 2000, 1)
        self.scan()
        conn = scanner.connect(self.db)
        names = {r[0] for r in conn.execute("SELECT name FROM files")}
        conn.close()
        self.assertEqual(names, {"keep.bin"})

    def test_incremental_reflects_change(self):
        data = b"A" * (120 * 1024)
        self.write("x/a.bin", data, 5)
        self.write("y/b.bin", b"B" * (120 * 1024), 5)
        a = self.scan_analyze()
        self.assertEqual(a["stats"]["exact_dup_groups"], 0)
        # b 를 a 와 동일 내용으로 변경 → 재스캔 후 중복으로 잡혀야
        self.write("y/b.bin", data, 2)
        a2 = self.scan_analyze()
        self.assertEqual(a2["stats"]["exact_dup_groups"], 1)

    def test_removed_file_purged(self):
        self.write("a.bin", b"A" * 2000, 1)
        p = self.write("b.bin", b"B" * 2000, 1)
        self.scan()
        os.remove(p)
        self.scan()
        conn = scanner.connect(self.db)
        names = {r[0] for r in conn.execute("SELECT name FROM files")}
        conn.close()
        self.assertEqual(names, {"a.bin"})

    def test_unicode_and_space_names(self):
        data = b"U" * (90 * 1024)
        self.write("사진 모음/여행 사진.jpg", data, 3)
        self.write("백업/여행 사진 (사본).jpg", data, 1)
        a = self.scan_analyze()
        self.assertEqual(a["stats"]["exact_dup_groups"], 1)

    def test_junction_skipped(self):
        """디렉터리 정션(reparse point)은 따라가지 않는다."""
        real = self.src / "realdir"
        real.mkdir()
        (real / "f.bin").write_bytes(b"A" * 2000)
        link = self.src / "linkdir"
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            self.skipTest("정션 생성 불가(권한/환경)")
        self.scan()
        conn = scanner.connect(self.db)
        paths = [row[0] for row in conn.execute("SELECT path FROM files")]
        conn.close()
        # 정션을 통한 경로는 색인되지 않아야 함
        self.assertFalse(any("linkdir" in p for p in paths), f"정션 내부가 스캔됨: {paths}")
        self.assertTrue(any("realdir" in p for p in paths))


# =====================================================================
# E. 적용 / 되돌리기 / 안전
# =====================================================================
class TestApplier(Base):
    def _decisions(self, paths):
        import json
        dp = self.tmp / "decisions.json"
        dp.write_text(__import__("json").dumps({"version": 1, "move": [str(p) for p in paths]},
                                               ensure_ascii=False), encoding="utf-8")
        return dp

    def test_dryrun_no_move(self):
        p = self.write("a.bin", b"A" * 3000, 1)
        applier.apply_decisions(self._decisions([p]), self.cfg(), do_apply=False)
        self.assertTrue(p.exists(), "드라이런은 파일을 옮기면 안 됨")

    def test_apply_then_undo(self):
        p1 = self.write("A/a.bin", b"A" * 3000, 1)
        p2 = self.write("B/b.bin", b"B" * 3000, 1)
        cfg = self.cfg()
        applier.apply_decisions(self._decisions([p1, p2]), cfg, do_apply=True)
        self.assertFalse(p1.exists())
        self.assertFalse(p2.exists())
        # 격리폴더에 보존
        moved = list(self.quar.rglob("a.bin")) + list(self.quar.rglob("b.bin"))
        self.assertEqual(len(moved), 2)
        # undo
        ts = self._latest_ts()
        applier.undo(ts)
        self.assertTrue(p1.exists() and p2.exists(), "되돌리기로 원위치 복원되어야")

    def test_missing_path_skipped(self):
        p = self.write("a.bin", b"A" * 1000, 1)
        ghost = self.src / "ghost.bin"
        res = applier.apply_decisions(self._decisions([p, ghost]), self.cfg(), do_apply=True)
        self.assertEqual(res["moved"], 1)  # 존재하는 1개만 이동, 없는 건 건너뜀

    def test_duplicate_paths_moved_once(self):
        p = self.write("a.bin", b"A" * 1000, 1)
        res = applier.apply_decisions(self._decisions([p, p, p]), self.cfg(), do_apply=True)
        self.assertEqual(res["moved"], 1)

    def test_undo_no_overwrite(self):
        p = self.write("a.bin", b"ORIGINAL", 1)
        cfg = self.cfg()
        applier.apply_decisions(self._decisions([p]), cfg, do_apply=True)
        # 원위치에 새 파일이 다시 생김 → undo 가 덮어쓰면 안 됨
        p.write_bytes(b"NEW-RECREATED")
        ts = self._latest_ts()
        applier.undo(ts)
        self.assertEqual(p.read_bytes(), b"NEW-RECREATED", "undo 는 기존 파일을 덮어쓰지 않아야")

    def test_one_failure_does_not_break_batch(self):
        """한 파일 이동이 실패해도 나머지는 정상 이동(복원력)."""
        p1 = self.write("a.bin", b"A" * 1000, 1)
        p2 = self.write("b.bin", b"B" * 1000, 1)
        cfg = self.cfg()
        real_move = shutil.move
        def flaky(src, dst, *a, **k):
            if str(src).endswith("a.bin"):
                raise OSError("강제 실패(테스트)")
            return real_move(src, dst, *a, **k)
        shutil.move = flaky
        try:
            res = applier.apply_decisions(self._decisions([p1, p2]), cfg, do_apply=True)
        finally:
            shutil.move = real_move
        self.assertEqual(res["errors"], 1)
        self.assertEqual(res["moved"], 1)
        self.assertTrue(p1.exists(), "실패한 파일은 원위치 유지")
        self.assertFalse(p2.exists(), "정상 파일은 이동")

    def test_collision_autonumber(self):
        p1 = self.write("A/dup.bin", b"one" * 500, 1)
        p2 = self.write("B/dup.bin", b"two" * 500, 1)
        # per_drive=False → 격리폴더/드라이브라벨/원경로 보존이라 보통 충돌 안 나지만
        # 같은 이름이 같은 대상 폴더로 갈 때 자동 번호가 붙는지 별도 확인
        cfg = self.cfg()
        applier.apply_decisions(self._decisions([p1, p2]), cfg, do_apply=True)
        files = list(self.quar.rglob("dup*.bin"))
        self.assertEqual(len(files), 2, "두 파일 모두 보존(덮어쓰기 금지)")

    def test_move_all_copies_recoverable(self):
        """사용자가 모든 사본을 옮겨도(수동) 영구삭제가 아니라 격리폴더에 보존."""
        data = b"D" * 4000
        p1 = self.write("A/x.bin", data, 1)
        p2 = self.write("B/x.bin", data, 1)
        applier.apply_decisions(self._decisions([p1, p2]), self.cfg(), do_apply=True)
        self.assertEqual(len(list(self.quar.rglob("x*.bin"))), 2)

    def test_per_drive_path_logic(self):
        cfg = self.cfg(quarantine_per_drive=True)
        src = r"F:\photos\2024\img.jpg"
        base = applier._quarantine_base(cfg, "TS", src)
        tgt = applier._quarantine_target(base, src, True)
        self.assertTrue(str(tgt).upper().startswith("F:"), f"per-drive 는 같은 드라이브여야: {tgt}")

    def test_no_permanent_delete_in_source(self):
        """applier 에 영구삭제 호출이 없어야 한다(이동만)."""
        text = (ROOT / "organizer" / "applier.py").read_text(encoding="utf-8")
        for forbidden in ("os.remove(", "os.unlink(", "shutil.rmtree(", "Path.unlink", ".unlink("):
            self.assertNotIn(forbidden, text, f"영구삭제 호출 발견: {forbidden}")

    def test_long_path_move(self):
        """긴 경로(>260)도 격리 이동이 성공해야 한다."""
        seg = "x" * 40
        deep = self.src
        for _ in range(6):
            deep = deep / seg
        target_file = deep / "file.bin"
        try:
            os.makedirs(hashing.long_path(str(deep)), exist_ok=True)
            with open(hashing.long_path(str(target_file)), "wb") as f:
                f.write(b"L" * 1000)
        except OSError:
            self.skipTest("긴 경로 생성 불가")
        res = applier.apply_decisions(self._decisions([target_file]), self.cfg(), do_apply=True)
        self.assertEqual(res["errors"], 0)
        self.assertEqual(res["moved"], 1, "긴 경로 파일 이동 실패")
        self.assertFalse(os.path.exists(hashing.long_path(str(target_file))))

    def _latest_ts(self):
        import glob
        import re
        logs = sorted(glob.glob(str(config.DATA_DIR / "undo_*.json")))
        return re.search(r"undo_(.+)\.json", os.path.basename(logs[-1])).group(1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
