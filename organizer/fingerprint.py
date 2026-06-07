"""정규화된 '내용' 지문 — 바이트가 달라도 사실상 같은 파일을 잡는다.

세 가지를 계산한다(모두 선택적: 라이브러리가 없으면 해당 항목만 건너뜀):
  - 이미지 dHash : 리사이즈·재압축·메타데이터만 다른 '사실상 같은 사진' (Pillow 필요)
  - 문서 텍스트 해시 : docx/xlsx/pptx(표준 zipfile) · pdf(pypdf) · 일반 텍스트/코드
  - zip 내용 해시 : 압축방식·타임스탬프가 달라도 '같은 내용'의 zip (표준 zipfile)

지문은 fingerprints 테이블에 (path, mtime, size) 기준으로 캐시되어 재스캔 시 재사용된다.
원본은 읽기만 한다.
"""

from __future__ import annotations

import hashlib
import html
import os
import random as _random
import re
import sqlite3
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from .hashing import long_path

# ---- 확장자 분류 ----
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif",
              ".heic", ".heif", ".avif", ".jfif", ".ico"}
DOCZIP_EXTS = {".docx", ".pptx", ".xlsx", ".odt", ".ods", ".odp", ".hwpx"}
PDF_EXTS = {".pdf"}
HWP_EXTS = {".hwp"}
PLAINTEXT_EXTS = {
    ".txt", ".md", ".csv", ".tsv", ".log", ".json", ".xml", ".yaml", ".yml",
    ".ini", ".cfg", ".py", ".java", ".js", ".ts", ".jsx", ".tsx", ".html",
    ".htm", ".css", ".scss", ".c", ".cpp", ".cc", ".h", ".hpp", ".cs", ".go",
    ".rb", ".php", ".sql", ".sh", ".bat", ".ps1", ".r", ".kt", ".swift", ".rs",
}
ZIP_EXTS = {".zip", ".jar", ".war", ".apk", ".epub"}
SEVENZIP_EXTS = {".7z"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm",
              ".m4v", ".mpg", ".mpeg", ".ts", ".3gp", ".m2ts"}
RAW_EXTS = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf",
            ".srw", ".pef", ".raw"}
AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".wav", ".ogg", ".wma", ".opus"}

# ---- 근접 문서(MinHash) 설정 ----
_MINHASH_N = 64           # 시그니처 길이
_SHINGLE_K = 5            # 단어 n-gram
_MERSENNE = (1 << 61) - 1
_rng = _random.Random(20240607)
_MH_A = [_rng.randrange(1, _MERSENNE) for _ in range(_MINHASH_N)]
_MH_B = [_rng.randrange(0, _MERSENNE) for _ in range(_MINHASH_N)]
_LSH_BANDS = 16          # 16밴드 × 4행 = 64
_LSH_ROWS = _MINHASH_N // _LSH_BANDS

TEXT_EXTS = DOCZIP_EXTS | PDF_EXTS | PLAINTEXT_EXTS | HWP_EXTS


def _norm_text(text: str) -> str | None:
    """공백/줄바꿈을 정규화해 비교용 텍스트로. 내용이 비면 None."""
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 1:
        return None
    return text


def _hash_text(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8", "ignore"), digest_size=20).hexdigest()


def _minhash_sig(text: str) -> str | None:
    """정규화 텍스트의 단어 5-gram MinHash 시그니처(hex,콤마). 너무 짧으면 None."""
    words = text.split()
    if len(words) >= _SHINGLE_K:
        shingles = {" ".join(words[i:i + _SHINGLE_K])
                    for i in range(len(words) - _SHINGLE_K + 1)}
    else:
        shingles = set(words)
    if len(shingles) < 4:        # 셔플 부족 → 근접비교 신뢰도 낮음
        return None
    hs = [int.from_bytes(hashlib.blake2b(s.encode("utf-8", "ignore"), digest_size=8).digest(), "big")
          for s in shingles]
    sig = []
    for a, b in zip(_MH_A, _MH_B):
        sig.append(min(((a * h + b) % _MERSENNE) for h in hs) & 0xFFFFFFFF)
    return ",".join(f"{x:08x}" for x in sig)


def _sig_parse(s: str) -> list[int]:
    return [int(x, 16) for x in s.split(",")]


def _sig_similarity(a: list[int], b: list[int]) -> float:
    if not a or len(a) != len(b):
        return 0.0
    return sum(1 for x, y in zip(a, b) if x == y) / len(a)


# ---------------- 이미지 dHash ----------------
_HEIF_REGISTERED = False


def _ensure_heif():
    """HEIC/HEIF 열기 지원(pillow-heif 있으면 1회 등록). 없으면 무시."""
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED:
        return
    _HEIF_REGISTERED = True
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass


def _dhash_from_gray(px) -> str | None:
    """9x8 그레이스케일 픽셀(72바이트) → dHash 16hex."""
    if not px or len(px) < 72:
        return None
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
    return f"{bits:016x}"


_DCT_BASIS = None


def _phash_of(im) -> str | None:
    """DCT 기반 pHash 64비트(16hex). numpy 필요."""
    try:
        import numpy as np
    except Exception:
        return None
    global _DCT_BASIS
    g = im.convert("L").resize((32, 32))
    a = np.asarray(g, dtype=np.float32)
    if _DCT_BASIS is None:
        k = np.arange(32)
        _DCT_BASIS = np.cos(np.pi * (2 * k[:, None] + 1) * k[None, :] / 64.0)
    dct = _DCT_BASIS @ a @ _DCT_BASIS.T
    low = dct[:8, :8].flatten()
    med = np.median(low[1:])   # DC 제외 중앙값
    bits = 0
    for i in range(64):
        bits = (bits << 1) | (1 if low[i] > med else 0)
    return f"{bits:016x}"


def _img_hash_of(im, mode: str) -> str | None:
    from PIL import Image
    if mode == "phash":
        return _phash_of(im)
    return _dhash_from_gray(im.convert("L").resize((9, 8), Image.LANCZOS).tobytes())


def _dihedral_hash(im, cfg) -> str | None:
    """회전(90/180/270)·좌우반전 8가지 중 사전식 최소값 = 회전불변 canonical."""
    mode = (cfg or {}).get("image_hash", "dhash")
    if not (cfg or {}).get("image_rotation", False):
        return _img_hash_of(im, mode)
    try:
        from PIL import Image
    except Exception:
        return _img_hash_of(im, mode)
    variants = []
    for mir in (False, True):
        base = im.transpose(Image.FLIP_LEFT_RIGHT) if mir else im
        for t in (0, 1, 2, 3):
            v = base.rotate(90 * t, expand=True)
            h = _img_hash_of(v, mode)
            if h:
                variants.append(h)
    return min(variants) if variants else None


def image_fingerprint(path: str, cfg=None) -> str | None:
    try:
        from PIL import Image, ImageFile
        ImageFile.LOAD_TRUNCATED_IMAGES = True
    except Exception:
        return None
    _ensure_heif()
    try:
        with Image.open(long_path(path)) as im:
            im.load()
            return _dihedral_hash(im, cfg)
    except Exception:
        return None


# ---------------- RAW 사진 (rawpy) ----------------
def raw_fingerprint(path: str, cfg=None) -> str | None:
    try:
        import rawpy
        from PIL import Image
    except Exception:
        return None
    try:
        with rawpy.imread(long_path(path)) as raw:
            try:
                thumb = raw.extract_thumb()  # 임베디드 썸네일 우선(빠름)
                import io
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    im = Image.open(io.BytesIO(thumb.data))
                else:
                    im = Image.fromarray(thumb.data)
            except Exception:
                arr = raw.postprocess(half_size=True, no_auto_bright=True, use_camera_wb=True)
                im = Image.fromarray(arr)
            return _dihedral_hash(im, cfg)
    except Exception:
        return None


# ---------------- 영상 perceptual (imageio-ffmpeg) ----------------
import subprocess  # noqa: E402

_VIDEO_POSITIONS = (0.1, 0.3, 0.5, 0.7, 0.9)


def _ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _video_duration(ffmpeg: str, path: str) -> float:
    try:
        p = subprocess.run([ffmpeg, "-i", long_path(path)],
                           capture_output=True, text=True, errors="ignore")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", p.stderr)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return 0.0


def _video_frame_dhash(ffmpeg: str, path: str, ts: float) -> str | None:
    try:
        from PIL import Image
        import io
        p = subprocess.run(
            [ffmpeg, "-ss", f"{ts:.3f}", "-i", long_path(path), "-frames:v", "1",
             "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True)  # 바이너리 stdout (errors= 주면 텍스트로 변해 깨짐)
        if not p.stdout:
            return None
        im = Image.open(io.BytesIO(p.stdout)).convert("L").resize((9, 8))
        return _dhash_from_gray(im.tobytes())
    except Exception:
        return None


def video_fingerprint(path: str) -> str | None:
    """키프레임 5장 dHash 연결(80hex). 재인코딩·해상도 달라도 유사."""
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        return None
    dur = _video_duration(ffmpeg, path)
    if dur > 0:
        times = [max(0.0, dur * p) for p in _VIDEO_POSITIONS]
    else:
        times = [0.0, 1.0, 2.0, 3.0, 4.0]
    parts = []
    for ts in times:
        h = _video_frame_dhash(ffmpeg, path, ts)
        parts.append(h if h else "0000000000000000")
    if all(p == "0000000000000000" for p in parts):
        return None
    return "".join(parts)


# ---------------- 오디오 perceptual (ffmpeg + numpy, best-effort) ----------------
def audio_fingerprint(path: str) -> str | None:
    """앞 30초를 mono 8kHz 로 디코드 → 거친 스펙트로그램 dHash 비트열(hex).

    재인코딩·비트레이트가 달라도 스펙트럼 윤곽이 비슷하면 매칭(베스트에포트).
    """
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        return None
    try:
        import numpy as np
    except Exception:
        return None
    try:
        p = subprocess.run(
            [ffmpeg, "-i", long_path(path), "-t", "60", "-ac", "1", "-ar", "8000",
             "-f", "s16le", "-"], capture_output=True)
        if len(p.stdout) < 8000 * 2:   # 1초 미만이면 신뢰도 낮음
            return None
        x = np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32)
        # 시간축을 합친 '전체 평균 로그 스펙트럼' → 재인코딩(인코더 지연)에 강건
        win = 2048
        n = (len(x) // win) * win
        if n < win:
            return None
        frames = x[:n].reshape(-1, win) * np.hanning(win)
        mag = np.abs(np.fft.rfft(frames, axis=1)).mean(axis=0)   # 평균 스펙트럼
        nb = 64
        edges = np.linspace(1, len(mag), nb + 1).astype(int)     # DC 제외
        bands = np.array([np.log1p(mag[edges[b]:max(edges[b] + 1, edges[b + 1])].mean())
                          for b in range(nb)])
        bits = 0                                                  # 인접 밴드 비교 → 63비트
        for b in range(nb - 1):
            bits = (bits << 1) | (1 if bands[b] > bands[b + 1] else 0)
        return f"{bits:016x}"
    except Exception:
        return None


# ---------------- OCR (rapidocr + PyMuPDF) ----------------
_OCR_ENGINE = None
_OCR_TRIED = False


def _ocr_engine():
    global _OCR_ENGINE, _OCR_TRIED
    if _OCR_TRIED:
        return _OCR_ENGINE
    _OCR_TRIED = True
    try:
        from rapidocr_onnxruntime import RapidOCR
        _OCR_ENGINE = RapidOCR()
    except Exception:
        _OCR_ENGINE = None
    return _OCR_ENGINE


def _ocr_array(arr) -> str:
    eng = _ocr_engine()
    if eng is None:
        return ""
    try:
        result, _ = eng(arr)
        if not result:
            return ""
        return " ".join(line[1] for line in result)
    except Exception:
        return ""


def ocr_text(path: str, ext: str, cfg: dict | None = None) -> str:
    """글자 없는 이미지/스캔 PDF 에서 OCR 텍스트. 실패/엔진없음 → ''. 매우 느림."""
    cfg = cfg or {}
    if not cfg.get("ocr_enabled", True):
        return ""
    ext = ext.lower()
    try:
        if ext in IMAGE_EXTS or ext in RAW_EXTS:
            if not cfg.get("ocr_images", True):
                return ""
            import numpy as np
            from PIL import Image
            _ensure_heif()
            if ext in RAW_EXTS:
                import rawpy
                with rawpy.imread(long_path(path)) as raw:
                    im = Image.fromarray(raw.postprocess(half_size=True))
            else:
                im = Image.open(long_path(path)).convert("RGB")
            if max(im.size) < int(cfg.get("ocr_min_px", 200)):
                return ""   # 아이콘/썸네일 등 극소 이미지는 OCR 생략
            maxpx = int(cfg.get("ocr_max_px", 2000))
            if maxpx and max(im.size) > maxpx:
                im.thumbnail((maxpx, maxpx))   # 거대한 이미지는 축소 후 OCR
            return _ocr_array(np.array(im))
        if ext in PDF_EXTS:
            import fitz
            import numpy as np
            from PIL import Image
            out = []
            doc = fitz.open(long_path(path))
            try:
                maxp = min(len(doc), int(cfg.get("ocr_max_pages", 10)))
                for i in range(maxp):
                    pix = doc.load_page(i).get_pixmap(dpi=150)
                    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    out.append(_ocr_array(np.array(im)))
            finally:
                doc.close()
            return " ".join(out)
    except Exception:
        return ""
    return ""


# ---------------- 문서 텍스트 ----------------
def _docx_text(z: zipfile.ZipFile) -> str:
    # 본문뿐 아니라 머리글·바닥글·각주·텍스트박스(word/*.xml)까지 전부
    out = []
    for name in z.namelist():
        if name.startswith("word/") and name.endswith(".xml"):
            xml = z.read(name).decode("utf-8", "ignore")
            out += re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, re.DOTALL)
    return " ".join(out)


def _odf_text(z: zipfile.ZipFile) -> str:
    """오픈오피스(.odt/.ods/.odp): content.xml 의 텍스트만."""
    try:
        xml = z.read("content.xml").decode("utf-8", "ignore")
    except KeyError:
        return ""
    body = re.sub(r"<[^>]+>", " ", xml)  # 태그 제거
    return body


def _hwpx_text(z: zipfile.ZipFile) -> str:
    """한글 .hwpx(zip): Contents/section*.xml 텍스트."""
    out = []
    for name in z.namelist():
        if name.startswith("Contents/section") and name.endswith(".xml"):
            xml = z.read(name).decode("utf-8", "ignore")
            out.append(re.sub(r"<[^>]+>", " ", xml))
    return " ".join(out)


def _hwp_text(path: str) -> str:
    """구형 한글 .hwp(바이너리): olefile 로 PrvText 프리뷰 텍스트 근사 추출."""
    try:
        import olefile
    except Exception:
        return ""
    try:
        if not olefile.isOleFile(long_path(path)):
            return ""
        ole = olefile.OleFileIO(long_path(path))
        try:
            if not ole.exists("PrvText"):
                return ""
            raw = ole.openstream("PrvText").read()
        finally:
            ole.close()
        return raw.decode("utf-16-le", "ignore")
    except Exception:
        return ""


def _pptx_text(z: zipfile.ZipFile) -> str:
    out = []
    for name in z.namelist():
        if name.startswith("ppt/slides/slide") and name.endswith(".xml"):
            xml = z.read(name).decode("utf-8", "ignore")
            out += re.findall(r"<a:t>(.*?)</a:t>", xml, re.DOTALL)
    return " ".join(out)


def _xlsx_text(z: zipfile.ZipFile) -> str:
    out = []
    try:
        ss = z.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
        out += re.findall(r"<t[^>]*>(.*?)</t>", ss, re.DOTALL)
    except KeyError:
        pass
    for name in z.namelist():
        if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
            xml = z.read(name).decode("utf-8", "ignore")
            out += re.findall(r"<v>(.*?)</v>", xml, re.DOTALL)
    return " ".join(out)


def _pdf_text(path: str) -> str:
    try:
        from pypdf import PdfReader
    except Exception:
        return ""
    try:
        reader = PdfReader(long_path(path))
        return " ".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return ""


def _plaintext(path: str) -> str:
    try:
        with open(long_path(path), "rb") as f:
            raw = f.read()
    except OSError:
        return ""
    for enc in ("utf-8", "utf-16", "cp949", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def _extract_norm_text(path: str, ext: str) -> str | None:
    """포맷별 텍스트 추출 후 정규화. 실패/빈 텍스트면 None."""
    ext = ext.lower()
    try:
        if ext in DOCZIP_EXTS:
            with zipfile.ZipFile(long_path(path)) as z:
                if ext == ".docx":
                    text = _docx_text(z)
                elif ext == ".pptx":
                    text = _pptx_text(z)
                elif ext == ".xlsx":
                    text = _xlsx_text(z)
                elif ext == ".hwpx":
                    text = _hwpx_text(z)
                else:  # .odt / .ods / .odp
                    text = _odf_text(z)
        elif ext in HWP_EXTS:
            text = _hwp_text(path)
        elif ext in PDF_EXTS:
            text = _pdf_text(path)
        elif ext in PLAINTEXT_EXTS:
            text = _plaintext(path)
        else:
            return None
    except (zipfile.BadZipFile, OSError, Exception):
        return None
    return _norm_text(text)


def text_fingerprint(path: str, ext: str) -> str | None:
    norm = _extract_norm_text(path, ext)
    return _hash_text(norm) if norm else None


# ---------------- 압축(zip/7z) 내용 ----------------
def _arch_hash(entries) -> str | None:
    entries = sorted(entries)
    if not entries:
        return None
    return hashlib.blake2b(repr(entries).encode("utf-8"), digest_size=20).hexdigest()


def zip_fingerprint(path: str) -> str | None:
    """zip 계열: (이름, 원본크기, CRC32) 정규화 매니페스트. 폴더 항목 제외."""
    try:
        with zipfile.ZipFile(long_path(path)) as z:
            entries = [(i.filename, i.file_size, i.CRC) for i in z.infolist() if not i.is_dir()]
    except (zipfile.BadZipFile, OSError, RuntimeError, Exception):
        return None
    return _arch_hash(entries)


def sevenzip_fingerprint(path: str) -> str | None:
    """.7z: zip 과 동일 (이름, 원본크기, CRC32) 매니페스트 → 교차 비교 가능. py7zr 필요."""
    try:
        import py7zr
    except Exception:
        return None
    try:
        with py7zr.SevenZipFile(long_path(path), mode="r") as z:
            entries = [(f.filename, f.uncompressed or 0, f.crc32 or 0)
                       for f in z.list() if not f.is_directory]
    except Exception:
        return None
    return _arch_hash(entries)


def archive_fingerprint(path: str, ext: str) -> str | None:
    if ext in SEVENZIP_EXTS:
        return sevenzip_fingerprint(path)
    return zip_fingerprint(path)


# ---------------- 캐시 테이블 / 계산 ----------------
def ensure_table(conn: sqlite3.Connection):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS fingerprints (
               path TEXT PRIMARY KEY, mtime REAL, size INTEGER,
               img TEXT, txt TEXT, zipf TEXT, txtmin TEXT, vid TEXT, aud TEXT
           )"""
    )
    # 기존 DB 마이그레이션: 없는 컬럼 추가
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fingerprints)")}
    for c in ("txtmin", "vid", "aud"):
        if c not in cols:
            conn.execute(f"ALTER TABLE fingerprints ADD COLUMN {c} TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fp_img ON fingerprints(img)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fp_txt ON fingerprints(txt)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fp_zip ON fingerprints(zipf)")


def _compute_one(path: str, ext: str, cfg: dict):
    """한 파일의 (img, txt, txtmin, zipf, vid) 지문 계산 — 스레드에서 실행."""
    is_img = ext in IMAGE_EXTS
    is_raw = ext in RAW_EXTS
    is_txt = ext in TEXT_EXTS
    is_arch = ext in ZIP_EXTS or ext in SEVENZIP_EXTS
    is_vid = ext in VIDEO_EXTS and cfg.get("video_match", True)
    is_aud = ext in AUDIO_EXTS and cfg.get("audio_match", True)
    ocr_on = cfg.get("ocr_enabled", True)

    img = image_fingerprint(path, cfg) if is_img else (raw_fingerprint(path, cfg) if is_raw else None)

    txt = txtmin = None
    norm = None
    if is_txt:
        norm = _extract_norm_text(path, ext)
        if (not norm or len(norm) < 20) and ext in PDF_EXTS and ocr_on:
            o = _norm_text(ocr_text(path, ext, cfg))
            if o:
                norm = o
    if not norm and (is_img or is_raw) and ocr_on and cfg.get("ocr_images", True):
        norm = _norm_text(ocr_text(path, ext, cfg))
    if norm:
        txt = _hash_text(norm)
        txtmin = _minhash_sig(norm)

    zipf = archive_fingerprint(path, ext) if is_arch else None
    vid = video_fingerprint(path) if is_vid else None
    aud = audio_fingerprint(path) if is_aud else None
    return img, txt, txtmin, zipf, vid, aud


def _is_relevant(ext: str, cfg: dict) -> bool:
    return (ext in IMAGE_EXTS or ext in RAW_EXTS or ext in TEXT_EXTS
            or ext in ZIP_EXTS or ext in SEVENZIP_EXTS
            or (ext in VIDEO_EXTS and cfg.get("video_match", True))
            or (ext in AUDIO_EXTS and cfg.get("audio_match", True)))


def _is_heavy(ext: str, cfg: dict) -> bool:
    """ffmpeg/OCR/RAW 디코드가 필요한 무거운 작업?"""
    if ext in VIDEO_EXTS or ext in AUDIO_EXTS or ext in RAW_EXTS:
        return True
    ocr_on = cfg.get("ocr_enabled", True)
    if ocr_on and ext in PDF_EXTS:
        return True
    if ocr_on and cfg.get("ocr_images", True) and ext in IMAGE_EXTS:
        return True
    return False


def _media_workers(cfg: dict) -> int:
    try:
        v = int(cfg.get("media_workers", 0) or 0)
    except (TypeError, ValueError):
        v = 0
    if v > 0:
        return v
    return max(2, (os.cpu_count() or 4) // 4)


def compute(conn: sqlite3.Connection, cfg: dict | None = None,
            progress=None, cancel=None) -> dict:
    """files 테이블의 각 파일에 대해 적용 가능한 지문을 계산/캐시(병렬)."""
    cfg = cfg or {}
    ensure_table(conn)
    stats = {"img": 0, "txt": 0, "zip": 0, "vid": 0, "aud": 0, "reused": 0}

    cache = {}
    for path, mtime, size, img, txt, zipf, txtmin, vid, aud in conn.execute(
        "SELECT path, mtime, size, img, txt, zipf, txtmin, vid, aud FROM fingerprints"
    ):
        cache[path] = (mtime, size, img, txt, zipf, txtmin, vid, aud)

    conn.execute("DELETE FROM fingerprints")
    rows = conn.execute("SELECT path, ext, size, mtime FROM files").fetchall()

    INSERT = ("INSERT OR REPLACE INTO fingerprints"
              "(path,mtime,size,img,txt,zipf,txtmin,vid,aud) VALUES(?,?,?,?,?,?,?,?,?)")
    batch = []
    done = 0
    total = len(rows)

    def _emit(path, mtime, size, img, txt, zipf, txtmin, vid, aud):
        if img or txt or zipf or vid or aud:
            batch.append((path, mtime, size, img, txt, zipf, txtmin, vid, aud))
        if len(batch) >= 400:
            conn.executemany(INSERT, batch)
            conn.commit()
            batch.clear()

    light, heavy = [], []
    for path, ext, size, mtime in rows:
        ext = (ext or "").lower()
        if not _is_relevant(ext, cfg):
            continue
        prev = cache.get(path)
        if prev and prev[0] == mtime and prev[1] == size:
            stats["reused"] += 1
            done += 1
            _emit(path, mtime, size, prev[2], prev[3], prev[4], prev[5], prev[6], prev[7])
        else:
            (heavy if _is_heavy(ext, cfg) else light).append((path, ext, size, mtime))

    try:
        from .scanner import _hash_workers, Cancelled
        light_workers = _hash_workers(cfg)
    except Exception:
        Cancelled = RuntimeError
        light_workers = 4
    heavy_workers = _media_workers(cfg)

    def _run(items, workers):
        nonlocal done
        if not items:
            return
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futmap = {ex.submit(_compute_one, p, e, cfg): (p, sz, mt)
                      for (p, e, sz, mt) in items}
            for fut in as_completed(futmap):
                if cancel is not None and cancel.is_set():
                    ex.shutdown(wait=False, cancel_futures=True)
                    if batch:
                        conn.executemany(INSERT, batch); conn.commit()
                    raise Cancelled()
                p, sz, mt = futmap[fut]
                try:
                    img, txt, txtmin, zipf, vid, aud = fut.result()
                except Exception:
                    img = txt = txtmin = zipf = vid = aud = None
                if img:
                    stats["img"] += 1
                if txt:
                    stats["txt"] += 1
                if zipf:
                    stats["zip"] += 1
                if vid:
                    stats["vid"] += 1
                if aud:
                    stats["aud"] += 1
                done += 1
                if progress is not None and done % 10 == 0:
                    progress("fingerprint", done, total, p)
                _emit(p, mt, sz, img, txt, zipf, txtmin, vid, aud)

    _run(light, light_workers)     # 가벼운 작업(이미지/문서/zip) 먼저 빠르게
    _run(heavy, heavy_workers)     # 무거운 작업(영상/오디오/RAW/OCR) 소규모 풀

    if batch:
        conn.executemany(INSERT, batch)
    conn.commit()
    return stats


# ---------------- 그룹화 ----------------
def _content_key(full_hash, path):
    return ("h:" + full_hash) if full_hash else ("u:" + path)


def _pick_keeper(files):
    from . import keeper
    return keeper.pick_keeper(files)


def _build_groups(file_lists, idkey):
    """[file dicts] 묶음들에서 (count>1 & 내용이 바이트로는 다른) 그룹만 만든다."""
    groups = []
    n = 0
    for files in file_lists:
        if len(files) < 2:
            continue
        # 바이트까지 완전 동일하면 이미 '완전중복'에서 처리됨 → 제외
        if len({_content_key(f["full_hash"], f["path"]) for f in files}) < 2:
            continue
        n += 1
        keeper = _pick_keeper(files)
        members = []
        for f in sorted(files, key=lambda x: x["path"]):
            members.append({
                "path": f["path"], "size": f["size"], "mtime": f["mtime"],
                "recommend": "keep" if f["path"] == keeper else "move",
            })
        groups.append({"id": f"{idkey}{n}", "count": len(files), "files": members})
    groups.sort(key=lambda g: g["count"], reverse=True)
    return groups


def _build(rows_by_fp, idkey):
    return _build_groups(rows_by_fp.values(), idkey)


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


class _BKNode:
    __slots__ = ("v", "idx", "children")

    def __init__(self, v, idx):
        self.v = v
        self.idx = idx
        self.children = {}


def _bk_cluster(items, threshold):
    """items: [(int_value, rec)] (값들의 비트길이 동일). 해밍거리 <= threshold 끼리 묶음.

    BK-tree 로 근접질의 → union-find. 대량에서도 O(n log n) 수준.
    반환: [[rec, ...], ...] (크기 1 포함, 상위에서 필터).
    """
    n = len(items)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    def insert(node, v, idx):
        while True:
            d = _hamming(node.v, v)
            nxt = node.children.get(d)
            if nxt is None:
                node.children[d] = _BKNode(v, idx)
                return
            node = nxt

    def query(root, v, radius):
        out = []
        stack = [root]
        while stack:
            node = stack.pop()
            d = _hamming(node.v, v)
            if d <= radius:
                out.append(node.idx)
            lo, hi = d - radius, d + radius
            for cd, ch in node.children.items():
                if lo <= cd <= hi:
                    stack.append(ch)
        return out

    root = None
    for idx, (v, _rec) in enumerate(items):
        if root is None:
            root = _BKNode(v, idx)
            continue
        for m in query(root, v, threshold):
            union(idx, m)
        insert(root, v, idx)

    groups = {}
    for idx in range(n):
        groups.setdefault(find(idx), []).append(items[idx][1])
    return list(groups.values())


def _cluster_hex(by_hex: dict, threshold: int):
    """{hexhash: [recs]} → 해밍거리 임계값으로 BK-tree 클러스터링."""
    items = []
    for h, recs in by_hex.items():
        try:
            hv = int(h, 16)
        except (TypeError, ValueError):
            continue
        for rec in recs:
            items.append((hv, rec))
    if not items:
        return []
    return _bk_cluster(items, threshold)


def _cluster_images(by_img: dict, threshold: int):
    return _cluster_hex(by_img, threshold)


def _cluster_near_docs(items, threshold):
    """items: [{'sig':[int], 'txt':str, 'rec':dict}] → 근접(유사) 문서 묶음 리스트."""
    n = len(items)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    buckets = {}
    for idx, it in enumerate(items):
        sig = it["sig"]
        for band in range(_LSH_BANDS):
            key = (band, tuple(sig[band * _LSH_ROWS:(band + 1) * _LSH_ROWS]))
            buckets.setdefault(key, []).append(idx)

    checked = set()
    for idxs in buckets.values():
        if len(idxs) < 2:
            continue
        for i in range(len(idxs)):
            for j in range(i + 1, len(idxs)):
                a, b = idxs[i], idxs[j]
                pair = (a, b) if a < b else (b, a)
                if pair in checked:
                    continue
                checked.add(pair)
                if _sig_similarity(items[a]["sig"], items[b]["sig"]) >= threshold:
                    union(a, b)

    groups = {}
    for idx in range(n):
        groups.setdefault(find(idx), []).append(idx)
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        if len({items[m]["txt"] for m in members}) < 2:
            continue  # 전부 동일 텍스트 → '같은 내용 문서'(근접 아님)
        out.append([items[m]["rec"] for m in members])
    return out


def similarity_groups(db_path, cfg: dict | None = None) -> dict:
    """fingerprints + files 를 읽어 image/doc/zip/근접문서 그룹을 만든다.

    실패하거나 데이터가 없으면 빈 결과를 돌려준다(핵심 기능에 영향 없음).
    """
    empty = {
        "image_dups": [], "doc_dups": [], "zip_dups": [], "doc_near": [],
        "video_dups": [], "audio_dups": [],
        "counts": {"image": 0, "doc": 0, "zip": 0, "docnear": 0, "video": 0, "audio": 0},
    }
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return empty
    try:
        ensure_table(conn)
        rows = conn.execute(
            """SELECT f.path, f.size, f.mtime, f.full_hash,
                      p.img, p.txt, p.zipf, p.txtmin, p.vid, p.aud
               FROM files f JOIN fingerprints p ON f.path = p.path"""
        ).fetchall()
    except sqlite3.Error:
        conn.close()
        return empty
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass

    by_img, by_txt, by_zip, by_vid, by_aud = {}, {}, {}, {}, {}
    near_items = []
    for path, size, mtime, fh, img, txt, zipf, txtmin, vid, aud in rows:
        rec = {"path": path, "size": size, "mtime": mtime, "full_hash": fh}
        if img:
            by_img.setdefault(img, []).append(rec)
        if txt:
            by_txt.setdefault(txt, []).append(rec)
        if zipf:
            by_zip.setdefault(zipf, []).append(rec)
        if vid:
            by_vid.setdefault(vid, []).append(rec)
        if aud:
            by_aud.setdefault(aud, []).append(rec)
        if txtmin:
            try:
                near_items.append({"sig": _sig_parse(txtmin), "txt": txt or path, "rec": rec})
            except (ValueError, AttributeError):
                pass

    # 이미지: 기본 엄격(dHash 정확 일치). 느슨 모드면 해밍거리 임계값으로 클러스터링.
    if cfg and cfg.get("image_match") == "loose":
        thr = int(cfg.get("image_threshold", 6))
        image_dups = _build_groups(_cluster_images(by_img, thr), "img")
    else:
        image_dups = _build(by_img, "img")
    doc_dups = _build(by_txt, "doc")
    zip_dups = _build(by_zip, "zip")

    # 영상: 키프레임 dHash 누적 해밍거리로 클러스터(재인코딩 허용)
    vthr = int(cfg.get("video_threshold", 40)) if cfg else 40
    video_dups = _build_groups(_cluster_hex(by_vid, vthr), "vid")

    # 오디오: 스펙트럼 지문 해밍거리로 클러스터(베스트에포트)
    athr = int(cfg.get("audio_threshold", 48)) if cfg else 48
    audio_dups = _build_groups(_cluster_hex(by_aud, athr), "aud")

    # 근접 문서(약간 수정된 문서)
    doc_near = []
    if (not cfg or cfg.get("text_near_match", True)) and near_items:
        thr = float(cfg.get("text_near_threshold", 0.85)) if cfg else 0.85
        doc_near = _build_groups(_cluster_near_docs(near_items, thr), "docnear")

    return {
        "image_dups": image_dups,
        "doc_dups": doc_dups,
        "zip_dups": zip_dups,
        "doc_near": doc_near,
        "video_dups": video_dups,
        "audio_dups": audio_dups,
        "counts": {"image": len(image_dups), "doc": len(doc_dups),
                   "zip": len(zip_dups), "docnear": len(doc_near),
                   "video": len(video_dups), "audio": len(audio_dups)},
    }
