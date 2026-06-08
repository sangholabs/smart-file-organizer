# -*- coding: utf-8 -*-
"""고도화 기능 검증: 병렬 해시 정확성 / 인사이트 / 격리 비우기 / 이미지 느슨 매칭.

실행:  py tests/test_advanced.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from organizer import analyzer, applier, config, fingerprint, insights, scanner  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="advtest_"))
        self.src = self.tmp / "src"
        self.db = self.tmp / "catalog.db"
        self.src.mkdir(parents=True)
        self._old_data = config.DATA_DIR
        config.DATA_DIR = self.tmp / "data"
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        config.DATA_DIR = self._old_data
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cfg(self, **over):
        c = {"scan_roots": [str(self.src)], "exclude_dir_names": [], "exclude_file_globs": [],
             "min_size_bytes": 0, "quarantine_dir": str(self.tmp / "_정리보관"),
             "use_recycle_bin": False, "quarantine_per_drive": False,
             "follow_symlinks": False, "skip_offline_files": True, "version_keywords": []}
        c.update(over)
        return c

    def run_all(self, cfg=None):
        cfg = cfg or self.cfg()
        conn = scanner.connect(self.db)
        scanner.walk(cfg, conn)
        scanner.compute_hashes(conn, cfg=cfg)
        fingerprint.compute(conn, cfg)
        conn.close()
        return analyzer.analyze(db_path=self.db, cfg=cfg)


class TestParallelHash(Base):
    def test_many_duplicates_one_group(self):
        data = b"DUP" * 50000
        for i in range(40):
            (self.src / f"copy{i}.bin").write_bytes(data)
        (self.src / "unique.bin").write_bytes(b"X" * 1000)
        a = self.run_all()
        self.assertEqual(a["stats"]["exact_dup_groups"], 1)
        self.assertEqual(a["exact_duplicates"][0]["count"], 40)
        # 보관 1개, 이동 39개
        moves = [f for f in a["exact_duplicates"][0]["files"] if f["recommend"] == "move"]
        self.assertEqual(len(moves), 39)


class TestInsights(Base):
    def test_basic(self):
        (self.src / "big.bin").write_bytes(b"A" * 5_000_000)
        (self.src / "mid.dat").write_bytes(b"B" * 2_000_000)
        old = self.src / "old.zip"
        old.write_bytes(b"C" * 3_000_000)
        t = time.time() - 900 * 86400
        os.utime(old, (t, t))
        a = self.run_all()
        ins = a["insights"]
        self.assertEqual(ins["total_files"], 3)
        self.assertEqual(ins["total_size"], 10_000_000)
        self.assertTrue(ins["largest_files"][0]["path"].endswith("big.bin"))
        self.assertTrue(any(f["path"].endswith("old.zip") for f in ins["old_files"]))
        exts = {e["ext"] for e in ins["by_ext"]}
        self.assertTrue({".bin", ".dat", ".zip"} <= exts)
        self.assertTrue(ins["top_folders"])


class TestPurge(Base):
    def test_preview_by_age(self):
        q = self.tmp / "_정리보관"
        oldf = q / "2020-01-01_000000"
        oldf.mkdir(parents=True)
        (oldf / "a.bin").write_bytes(b"X" * 100000)
        os.utime(oldf, (time.time() - 100 * 86400,) * 2)
        newf = q / "2026-06-06_120000"
        newf.mkdir(parents=True)
        (newf / "b.bin").write_bytes(b"Y" * 50000)
        cfg = self.cfg()
        self.assertEqual(applier.purge_quarantine(cfg, days=None, do_apply=False)["groups"], 2)
        self.assertEqual(applier.purge_quarantine(cfg, days=30, do_apply=False)["groups"], 1)
        # do_apply 안 했으므로 실제로 남아 있어야
        self.assertTrue(oldf.exists() and newf.exists())


class TestJunk(Base):
    def test_junk_bypasses_size_and_excludes(self):
        # 찌꺼기(작음)는 min_size 와 무관하게 잡히고, 일반 작은 파일은 제외되어야
        (self.src / "._photo.jpg").write_bytes(b"x" * 200)
        (self.src / ".DS_Store").write_bytes(b"y" * 1000)
        (self.src / "sub").mkdir()
        (self.src / "sub" / "Thumbs.db").write_bytes(b"z" * 500)
        (self.src / "small.txt").write_bytes(b"normal small file")  # min_size 미만, 찌꺼기 아님
        (self.src / "big.bin").write_bytes(b"A" * 2_000_000)
        cfg = self.cfg(min_size_bytes=1_048_576,
                       junk_globs=[".DS_Store", "._*", "Thumbs.db"])
        a = self.run_all(cfg)
        self.assertEqual(a["stats"]["junk_count"], 3)
        names = {os.path.basename(f["path"]) for f in a["junk_files"]}
        self.assertEqual(names, {"._photo.jpg", ".DS_Store", "Thumbs.db"})

    def test_junk_move_and_undo(self):
        import json
        jp = self.src / ".DS_Store"
        jp.write_bytes(b"junk" * 50)
        cfg = self.cfg(junk_globs=[".DS_Store"])
        a = self.run_all(cfg)
        paths = [f["path"] for f in a["junk_files"]]
        dpath = self.tmp / "decisions.json"
        dpath.write_text(json.dumps({"version": 1, "move": paths}), encoding="utf-8")
        applier.apply_decisions(dpath, cfg, do_apply=True)
        self.assertFalse(jp.exists())  # 격리로 이동됨
        import glob
        import re
        logs = sorted(glob.glob(str(__import__("organizer").config.DATA_DIR / "undo_*.json")))
        ts = re.search(r"undo_(.+)\.json", os.path.basename(logs[-1])).group(1)
        applier.undo(ts)
        self.assertTrue(jp.exists())  # 복원됨


class TestEmptyDirs(Base):
    def test_detect_and_move_undo(self):
        import json
        (self.src / "real").mkdir()
        (self.src / "real" / "f.bin").write_bytes(b"A" * 200000)
        (self.src / "empty1").mkdir()
        (self.src / "empty2" / "sub").mkdir(parents=True)        # 하위만 빔 → empty2 대표
        (self.src / "onlyjunk").mkdir()
        (self.src / "onlyjunk" / ".DS_Store").write_bytes(b"x" * 80)
        cfg = self.cfg(junk_globs=[".DS_Store"])
        conn = scanner.connect(self.db)
        scanner.walk(cfg, conn)
        scanner.compute_hashes(conn, cfg=cfg)
        n = scanner.find_empty_dirs(cfg, conn)
        conn.close()
        self.assertEqual(n, 3)
        a = analyzer.analyze(db_path=self.db, cfg=cfg)
        names = sorted(os.path.basename(d["path"]) for d in a["empty_dirs"])
        self.assertEqual(names, ["empty1", "empty2", "onlyjunk"])
        self.assertNotIn("sub", names)  # 최대 빈 폴더만(부모 대표)
        # 폴더째 이동 + undo
        dp = self.tmp / "d.json"
        dp.write_text(json.dumps({"version": 1, "move": [d["path"] for d in a["empty_dirs"]]}),
                      encoding="utf-8")
        applier.apply_decisions(dp, cfg, do_apply=True)
        self.assertFalse((self.src / "empty1").exists())
        import glob
        import re
        ts = re.search(r"undo_(.+)\.json",
                       os.path.basename(sorted(glob.glob(str(config.DATA_DIR / "undo_*.json")))[-1])).group(1)
        applier.undo(ts)
        self.assertTrue((self.src / "empty1").exists())


class TestArchiveAndDocs(Base):
    def test_jar_and_zip_same_content_grouped(self):
        import zipfile
        files = {"a.txt": b"hi" * 200}
        with zipfile.ZipFile(self.src / "p.zip", "w", zipfile.ZIP_STORED) as z:
            for n, d in files.items():
                z.writestr(n, d)
        with zipfile.ZipFile(self.src / "p.jar", "w", zipfile.ZIP_DEFLATED) as z:
            for n, d in files.items():
                z.writestr(n, d)
        a = self.run_all()
        self.assertEqual(a["stats"]["zip_dup_groups"], 1)  # zip+jar 교차 그룹

    def test_docx_includes_header_text(self):
        import zipfile

        def docx(p, body, header):
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("word/document.xml",
                           f"<w:document><w:body><w:t>{body}</w:t></w:body></w:document>")
                z.writestr("word/header1.xml", f"<w:hdr><w:t>{header}</w:t></w:hdr>")
        # 본문 같지만 머리글 다름 → 내용 다름 → 그룹 안 됨(머리글이 비교에 포함된다는 증거)
        docx(self.src / "a.docx", "같은본문", "머리글A")
        docx(self.src / "b.docx", "같은본문", "머리글B")
        a = self.run_all()
        self.assertEqual(a["stats"]["doc_dup_groups"], 0)

    def test_odt_same_text_grouped(self):
        import zipfile

        def odt(p, text, meta):
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("content.xml", f"<office><text:p>{text}</text:p></office>")
                z.writestr("meta.xml", f"<meta>{meta}</meta>")
        odt(self.src / "a.odt", "동일한 오픈오피스 본문", "author1")
        odt(self.src / "b.odt", "동일한 오픈오피스 본문", "author2")
        a = self.run_all()
        self.assertEqual(a["stats"]["doc_dup_groups"], 1)


@unittest.skipUnless(__import__("importlib").util.find_spec("pillow_heif"), "pillow-heif 미설치")
class TestHeic(Base):
    def test_heic_resize_grouped(self):
        from PIL import Image
        import pillow_heif
        pillow_heif.register_heif_opener()
        im = Image.new("RGB", (160, 160))
        im.putdata([((x) % 256, (x + y) % 256, y % 256) for y in range(160) for x in range(160)])
        im.save(self.src / "a.heic", format="HEIF")
        im.resize((80, 80)).save(self.src / "b.heic", format="HEIF")
        a = self.run_all()
        self.assertEqual(a["stats"]["image_dup_groups"], 1)


class TestKeeper(unittest.TestCase):
    def test_prefers_original_over_copy(self):
        from organizer import keeper
        files = [{"path": r"C:\docs\report.pdf", "mtime": 100},
                 {"path": r"C:\Users\me\Downloads\report (1).pdf", "mtime": 50}]
        self.assertTrue(keeper.pick_keeper(files).endswith("report.pdf"))


class TestNearDoc(Base):
    def test_modified_doc_is_near_not_exact(self):
        base = " ".join(f"paragraph {i} content stays here" for i in range(80))
        (self.src / "reportA.txt").write_text(base, encoding="utf-8")
        (self.src / "reportB.txt").write_text(base + " a newly added closing line", encoding="utf-8")
        (self.src / "other.txt").write_text(
            " ".join(f"unrelated {i} xyz" for i in range(80)), encoding="utf-8")
        a = self.run_all(self.cfg(text_near_match=True, text_near_threshold=0.8))
        self.assertEqual(a["stats"]["doc_near_groups"], 1)
        names = {os.path.basename(f["path"]) for f in a["doc_near"][0]["files"]}
        self.assertEqual(names, {"reportA.txt", "reportB.txt"})


try:
    import py7zr as _py7zr
    _HAVE_7Z = True
except Exception:
    _HAVE_7Z = False


@unittest.skipUnless(_HAVE_7Z, "py7zr 미설치")
class TestSevenZip(Base):
    def test_7z_and_zip_same_content_grouped(self):
        import zipfile
        import py7zr
        payload = self.src / "payload.txt"
        payload.write_bytes(b"archive content " * 400)
        with zipfile.ZipFile(self.src / "p.zip", "w", zipfile.ZIP_DEFLATED) as z:
            z.write(payload, "payload.txt")
        with py7zr.SevenZipFile(self.src / "p.7z", "w") as z:
            z.write(payload, "payload.txt")
        a = self.run_all()
        self.assertEqual(a["stats"]["zip_dup_groups"], 1)


class TestArchiveLoose(Base):
    def test_archive_redundant_when_contents_loose(self):
        import zipfile
        (self.src / "payload.txt").write_bytes(b"data " * 5000)
        with zipfile.ZipFile(self.src / "arch.zip", "w") as z:
            z.write(self.src / "payload.txt", "payload.txt")
        a = self.run_all()
        self.assertEqual(a["stats"]["archive_loose_count"], 1)


try:
    import imageio_ffmpeg as _iioff
    _HAVE_FFMPEG = True
except Exception:
    _HAVE_FFMPEG = False


@unittest.skipUnless(_HAVE_FFMPEG, "imageio-ffmpeg 미설치")
class TestVideo(Base):
    def _mkvid(self, name, src_filter="testsrc", scale=None):
        import subprocess
        ff = _iioff.get_ffmpeg_exe()
        out = self.src / name
        cmd = [ff, "-y", "-f", "lavfi", "-i", f"{src_filter}=duration=2:size=160x120:rate=10"]
        if scale:
            cmd += ["-vf", f"scale={scale}"]
        cmd.append(str(out))
        subprocess.run(cmd, capture_output=True)
        return out

    def test_reencoded_video_grouped(self):
        import subprocess
        ff = _iioff.get_ffmpeg_exe()
        self._mkvid("movie.mp4")
        subprocess.run([ff, "-y", "-i", str(self.src / "movie.mp4"),
                        "-vf", "scale=120:90", str(self.src / "movie2.mp4")], capture_output=True)
        self._mkvid("other.mp4", src_filter="testsrc2")
        a = self.run_all(self.cfg(video_match=True, video_threshold=40, ocr_enabled=False))
        self.assertEqual(a["stats"]["video_dup_groups"], 1)
        names = {os.path.basename(f["path"]) for f in a["video_dups"][0]["files"]}
        self.assertEqual(names, {"movie.mp4", "movie2.mp4"})


try:
    import rapidocr_onnxruntime as _rocr
    _HAVE_OCR = True
except Exception:
    _HAVE_OCR = False


@unittest.skipUnless(_HAVE_OCR, "rapidocr 미설치")
class TestOCR(unittest.TestCase):
    def test_ocr_extracts_text(self):
        import tempfile
        from pathlib import Path
        from PIL import Image, ImageDraw
        from organizer import fingerprint as fp
        tmp = Path(tempfile.mkdtemp(prefix="ocrt_"))
        try:
            im = Image.new("RGB", (400, 120), "white")
            ImageDraw.Draw(im).text((20, 40), "INVOICE 98765", fill="black")
            p = tmp / "scan.png"
            im.save(p)
            txt = fp.ocr_text(str(p), ".png", {"ocr_enabled": True, "ocr_images": True})
            self.assertTrue(any(c.isdigit() for c in txt))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(_HAVE_FFMPEG, "imageio-ffmpeg 미설치")
class TestAudioFp(unittest.TestCase):
    def test_audio_fingerprint_deterministic(self):
        import subprocess, tempfile
        from pathlib import Path
        from organizer import fingerprint as fp
        tmp = Path(tempfile.mkdtemp(prefix="audfp_"))
        try:
            ff = _iioff.get_ffmpeg_exe()
            wav = tmp / "s.wav"
            subprocess.run([ff, "-y", "-f", "lavfi", "-i",
                            "aevalsrc=0.5*sin(440*2*PI*t)+0.3*sin(660*2*PI*t):d=6",
                            "-ac", "1", str(wav)], capture_output=True)
            a1 = fp.audio_fingerprint(str(wav))
            a2 = fp.audio_fingerprint(str(wav))
            self.assertTrue(a1 and a1 == a2)   # 비None·결정적
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestImageRotationPhash(Base):
    def _img(self, seed=0):
        from PIL import Image
        im = Image.new("RGB", (160, 120))
        im.putdata([((x * 2 + seed) % 256, (x + y) % 256, (y * 3) % 256)
                    for y in range(120) for x in range(160)])
        return im

    def test_rotation_off_by_default(self):
        im = self._img()
        im.save(self.src / "a.png")
        im.rotate(90, expand=True).save(self.src / "a_rot.png")
        a = self.run_all()   # image_rotation 기본 off
        self.assertEqual(a["stats"]["image_dup_groups"], 0)

    def test_rotation_on_groups_rotated(self):
        im = self._img()
        im.save(self.src / "a.png")
        im.rotate(90, expand=True).save(self.src / "a_rot.png")
        a = self.run_all(self.cfg(image_rotation=True))
        self.assertEqual(a["stats"]["image_dup_groups"], 1)

    def test_phash_groups_resized(self):
        im = self._img()
        im.save(self.src / "a.png")
        im.resize((80, 60)).save(self.src / "a_small.png")
        a = self.run_all(self.cfg(image_hash="phash"))
        self.assertEqual(a["stats"]["image_dup_groups"], 1)


class TestImageClustering(unittest.TestCase):
    def test_hamming_and_cluster(self):
        self.assertEqual(fingerprint._hamming(0b1010, 0b1000), 1)
        self.assertEqual(fingerprint._hamming(0xFFFF, 0x0000), 16)
        by_img = {
            "0000000000000000": [{"path": "a", "size": 1, "mtime": 0, "full_hash": "ha"}],
            "0000000000000003": [{"path": "b", "size": 1, "mtime": 0, "full_hash": "hb"}],  # 2비트 차
            "ffffffffffffffff": [{"path": "c", "size": 1, "mtime": 0, "full_hash": "hc"}],  # 멀리
        }
        # threshold 4 → a,b 한 묶음, c 별개
        clusters = fingerprint._cluster_images(by_img, 4)
        sizes = sorted(len(c) for c in clusters)
        self.assertEqual(sizes, [1, 2])


class TestExport(Base):
    def test_csv_and_json(self):
        import csv as _csv
        import json as _json
        from organizer import exporter
        data = b"DUPLICATE-CONTENT" * 4000
        (self.src / "a.bin").write_bytes(data)
        (self.src / "a_copy.bin").write_bytes(data)   # 완전중복 그룹
        (self.src / ".DS_Store").write_bytes(b"junk")  # 찌꺼기(flat)
        result = self.run_all(self.cfg(junk_globs=[".DS_Store"]))

        rows = list(exporter.iter_rows(result))
        self.assertTrue(rows, "내보낼 행이 있어야 함")
        # 완전중복 keeper/move 역할이 표기되는지
        roles = {r["role"] for r in rows}
        self.assertTrue({"keeper", "move"} & roles)

        csv_path = exporter.export(result, self.tmp / "out.csv")
        self.assertTrue(csv_path.exists())
        with open(csv_path, encoding="utf-8-sig", newline="") as fp:
            reader = list(_csv.DictReader(fp))
        self.assertEqual(len(reader), len(rows))
        self.assertEqual(set(exporter.CSV_HEADER), set(reader[0].keys()))

        json_path = exporter.export(result, self.tmp / "out.json", fmt="json")
        obj = _json.loads(json_path.read_text(encoding="utf-8"))
        self.assertIn("categories", obj)
        keys = {c["key"] for c in obj["categories"]}
        self.assertIn("dup", keys)

    def test_bad_format(self):
        from organizer import exporter
        with self.assertRaises(ValueError):
            exporter.export({}, self.tmp / "x.txt", fmt="txt")


class TestDoctor(unittest.TestCase):
    def test_check_shape(self):
        from organizer import doctor
        rows = doctor.check()
        self.assertTrue(rows)
        keys = {r["key"] for r in rows}
        # 주요 기능키가 모두 점검에 포함되는지
        for k in ("image", "pdf", "video", "ocr", "sevenzip", "trash"):
            self.assertIn(k, keys)
        for r in rows:
            self.assertIn("ok", r)
            self.assertIsInstance(r["ok"], bool)
            self.assertTrue(r["name"] and r["hint"] and r["enables"])
        # 핵심 기능 표시
        self.assertTrue(any(r["core"] for r in rows))

    def test_report_text(self):
        from organizer import doctor
        txt = doctor.format_report()
        self.assertIn("환경 점검", txt)
        line = doctor.summary_line()
        self.assertIn("·", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
