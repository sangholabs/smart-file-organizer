# -*- coding: utf-8 -*-
"""이미지/문서/zip 내용 지문 탐지 검증 (stdlib unittest, 완전 격리).

실행:  py tests/test_fingerprint.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from organizer import analyzer, fingerprint, scanner  # noqa: E402

try:
    from PIL import Image
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fptest_"))
        self.src = self.tmp / "src"
        self.db = self.tmp / "catalog.db"
        self.src.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cfg(self):
        return {
            "scan_roots": [str(self.src)], "exclude_dir_names": [], "exclude_file_globs": [],
            "min_size_bytes": 0, "quarantine_dir": str(self.tmp / "q"),
            "use_recycle_bin": False, "quarantine_per_drive": False,
            "follow_symlinks": False, "skip_offline_files": True, "version_keywords": [],
        }

    def run_all(self):
        cfg = self.cfg()
        conn = scanner.connect(self.db)
        scanner.walk(cfg, conn)
        scanner.compute_hashes(conn)
        fingerprint.compute(conn, cfg)
        conn.close()
        return analyzer.analyze(db_path=self.db, cfg=cfg)

    def p(self, rel):
        q = self.src / rel
        q.parent.mkdir(parents=True, exist_ok=True)
        return q


def make_docx(path, body, author="anon"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml",
                   f"<w:document><w:body><w:p><w:r><w:t>{body}</w:t></w:r></w:p></w:body></w:document>")
        z.writestr("docProps/core.xml", f"<cp><dc:creator>{author}</dc:creator></cp>")


def make_zip(path, files, comp):
    with zipfile.ZipFile(path, "w", compression=comp) as z:
        for n, d in files.items():
            z.writestr(n, d)


@unittest.skipUnless(HAVE_PIL, "Pillow 미설치")
class TestImage(Base):
    def _img(self, seed=0):
        im = Image.new("RGB", (160, 160))
        im.putdata([((x + seed) % 256, (x + y) % 256, y % 256)
                    for y in range(160) for x in range(160)])
        return im

    def test_resize_recompress_grouped(self):
        base = self._img()
        base.save(self.p("a/photo.png"))
        base.save(self.p("b/photo.jpg"), quality=80)          # 재압축
        base.resize((80, 80)).save(self.p("b/photo_small.png"))  # 리사이즈
        self._img(seed=120).save(self.p("a/different.png"))    # 다른 사진
        a = self.run_all()
        self.assertEqual(a["stats"]["image_dup_groups"], 1)
        names = {os.path.basename(f["path"]) for f in a["image_dups"][0]["files"]}
        self.assertEqual(names, {"photo.png", "photo.jpg", "photo_small.png"})

    def test_byte_identical_not_in_image_dups(self):
        base = self._img()
        base.save(self.p("a/x.png"))
        shutil.copy(self.p("a/x.png"), self.p("b/x_copy.png"))   # 바이트 동일
        a = self.run_all()
        self.assertEqual(a["stats"]["image_dup_groups"], 0, "바이트 동일은 완전중복으로만")
        self.assertEqual(a["stats"]["exact_dup_groups"], 1)


class TestDoc(Base):
    def test_same_text_diff_metadata_grouped(self):
        make_docx(self.p("a/report.docx"), "동일한 본문 내용입니다 매출 보고", "홍길동")
        make_docx(self.p("b/report_v2.docx"), "동일한 본문 내용입니다 매출 보고", "김철수")
        a = self.run_all()
        self.assertEqual(a["stats"]["doc_dup_groups"], 1)

    def test_diff_text_not_grouped(self):
        make_docx(self.p("a/one.docx"), "첫 번째 문서", "a")
        make_docx(self.p("b/two.docx"), "완전히 다른 두 번째 문서", "b")
        a = self.run_all()
        self.assertEqual(a["stats"]["doc_dup_groups"], 0)

    def test_plaintext_diff_encoding_grouped(self):
        text = "같은 텍스트 내용 라인1\n라인2\n"
        self.p("a/note_utf8.txt").write_bytes(text.encode("utf-8"))
        self.p("b/note_utf16.txt").write_bytes(text.encode("utf-16"))   # 인코딩만 다름
        a = self.run_all()
        self.assertEqual(a["stats"]["doc_dup_groups"], 1,
                         "인코딩만 다른 같은 텍스트는 내용중복으로 잡혀야")


class TestZip(Base):
    def test_same_content_diff_compression_grouped(self):
        files = {"readme.txt": b"hello" * 100, "d/x.csv": b"a,b\n1,2\n" * 30}
        make_zip(self.p("a/pack.zip"), files, zipfile.ZIP_STORED)
        make_zip(self.p("b/pack2.zip"), files, zipfile.ZIP_DEFLATED)
        a = self.run_all()
        self.assertEqual(a["stats"]["zip_dup_groups"], 1)

    def test_diff_content_not_grouped(self):
        make_zip(self.p("a/p1.zip"), {"a.txt": b"1" * 100}, zipfile.ZIP_DEFLATED)
        make_zip(self.p("b/p2.zip"), {"a.txt": b"2" * 100}, zipfile.ZIP_DEFLATED)
        a = self.run_all()
        self.assertEqual(a["stats"]["zip_dup_groups"], 0)

    def test_keeper_preserved(self):
        files = {"a.txt": b"x" * 200}
        make_zip(self.p("a/k.zip"), files, zipfile.ZIP_STORED)
        make_zip(self.p("longer/name/k2.zip"), files, zipfile.ZIP_DEFLATED)
        a = self.run_all()
        g = a["zip_dups"][0]
        keepers = [f for f in g["files"] if f["recommend"] == "keep"]
        self.assertEqual(len(keepers), 1, "그룹마다 최소 1개 보존")


if __name__ == "__main__":
    unittest.main(verbosity=2)
