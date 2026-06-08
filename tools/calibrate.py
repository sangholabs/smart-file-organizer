# -*- coding: utf-8 -*-
"""임계값 캘리브레이션(개발용). 현실적 픽스처를 만들어 '동일군 vs 상이군' 분포를 측정.

실행: py tools/calibrate.py
결과를 보고 organizer/config.py 의 video/audio/text_near threshold 를 정한다.
exe 에는 포함하지 않는다.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from organizer import fingerprint as fp  # noqa: E402


def _ff():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run(args):
    subprocess.run(args, capture_output=True)


def calibrate_video(tmp: Path):
    ff = _ff()
    src = tmp / "vsrc.mp4"
    _run([ff, "-y", "-f", "lavfi", "-i", "testsrc=duration=4:size=1280x720:rate=24", str(src)])
    same = []
    # 재인코딩 변형(동일 콘텐츠)
    variants = {
        "crf28": [ff, "-y", "-i", str(src), "-crf", "28", str(tmp / "v_crf28.mp4")],
        "480p": [ff, "-y", "-i", str(src), "-vf", "scale=854:480", str(tmp / "v_480.mp4")],
        "360p": [ff, "-y", "-i", str(src), "-vf", "scale=640:360", str(tmp / "v_360.mp4")],
        "fps15": [ff, "-y", "-i", str(src), "-r", "15", str(tmp / "v_fps15.mp4")],
        "mpeg4": [ff, "-y", "-i", str(src), "-c:v", "mpeg4", "-q:v", "5", str(tmp / "v_mpeg4.avi")],
    }
    for cmd in variants.values():
        _run(cmd)
    same_files = [src] + [Path(list(v)[-1]) for v in variants.values()]
    # 무관 클립
    _run([ff, "-y", "-f", "lavfi", "-i", "testsrc2=duration=4:size=1280x720:rate=24", str(tmp / "v_other1.mp4")])
    _run([ff, "-y", "-f", "lavfi", "-i", "mandelbrot=size=1280x720:rate=24", "-t", "4", str(tmp / "v_other2.mp4")])
    other_files = [tmp / "v_other1.mp4", tmp / "v_other2.mp4"]

    sigs_same = {p.name: fp.video_fingerprint(str(p)) for p in same_files}
    sigs_other = {p.name: fp.video_fingerprint(str(p)) for p in other_files}
    sigs_same = {k: v for k, v in sigs_same.items() if v}
    sigs_other = {k: v for k, v in sigs_other.items() if v}

    intra = [fp._hamming(int(a, 16), int(b, 16))
             for a, b in combinations(sigs_same.values(), 2)]
    inter = []
    for a in sigs_same.values():
        for b in sigs_other.values():
            inter.append(fp._hamming(int(a, 16), int(b, 16)))
    return intra, inter


def calibrate_audio(tmp: Path):
    ff = _ff()
    src = tmp / "asrc.wav"
    expr = "aevalsrc=0.5*sin(440*2*PI*t)+0.3*sin(660*2*PI*t)+0.2*sin(990*2*PI*t)+0.1*sin(1320*2*PI*t):d=20"
    _run([ff, "-y", "-f", "lavfi", "-i", expr, "-ac", "2", str(src)])
    same_files = [src]
    for name, args in {
        "mp3_128": ["-b:a", "128k", str(tmp / "a_128.mp3")],
        "mp3_320": ["-b:a", "320k", str(tmp / "a_320.mp3")],
        "m4a": [str(tmp / "a.m4a")],
        "ogg": [str(tmp / "a.ogg")],
        "mono": ["-ac", "1", str(tmp / "a_mono.wav")],
    }.items():
        _run([ff, "-y", "-i", str(src)] + args)
        same_files.append(Path(args[-1]))
    _run([ff, "-y", "-f", "lavfi", "-i", "aevalsrc=0.5*sin(300*2*PI*t)+0.4*sin(520*2*PI*t):d=20", "-ac", "2", str(tmp / "a_other1.wav")])
    _run([ff, "-y", "-f", "lavfi", "-i", "anoisesrc=d=20:c=pink", "-ac", "2", str(tmp / "a_other2.wav")])
    other_files = [tmp / "a_other1.wav", tmp / "a_other2.wav"]

    sig_same = [fp.audio_fingerprint(str(p)) for p in same_files]
    sig_other = [fp.audio_fingerprint(str(p)) for p in other_files]
    sig_same = [s for s in sig_same if s]
    sig_other = [s for s in sig_other if s]
    if not sig_same:
        return None, None, None
    tag = sig_same[0].split(":", 1)[0]

    def dist(a, b):
        ta, ha = a.split(":", 1)
        tb, hb = b.split(":", 1)
        if ta != tb:
            return None
        if ta == "cp":
            import numpy as np
            aa = np.frombuffer(bytes.fromhex(ha), dtype="<u4")
            bb = np.frombuffer(bytes.fromhex(hb), dtype="<u4")
            return float(round(fp._cp_ber(aa, bb), 3))
        return int(fp._hamming(int(ha, 16), int(hb, 16)))

    intra = [d for a, b in combinations(sig_same, 2) if (d := dist(a, b)) is not None]
    inter = [d for a in sig_same for b in sig_other if (d := dist(a, b)) is not None]
    return tag, intra, inter


def calibrate_neardoc():
    base = " ".join(f"문단 {i} 입니다 이 문서는 분기 매출 보고와 계획을 담고 있습니다" for i in range(60))
    small = base + " 끝에 한 문장을 덧붙였습니다."
    medium = base.replace("문단 5", "수정된 문단 다섯").replace("문단 30", "크게 바뀐 문단 서른") + " 추가 단락 하나."
    large = " ".join(f"완전히 다른 주제 {i} 라인" for i in range(40)) + " " + " ".join(base.split()[:20])
    other = " ".join(f"무관한 회의록 {i} 항목 점검 결정사항" for i in range(60))

    def sig(t):
        return fp._minhash_sig(fp._norm_text(t))
    sb, ss, sm, sl, so = (sig(base), sig(small), sig(medium), sig(large), sig(other))

    def sim(a, b):
        return fp._sig_similarity(fp._sig_parse(a), fp._sig_parse(b))
    return {
        "base~small(소편집)": sim(sb, ss),
        "base~medium(중편집)": sim(sb, sm),
        "base~large(대편집)": sim(sb, sl),
        "base~other(무관)": sim(sb, so),
    }


def main():
    tmp = Path(tempfile.mkdtemp(prefix="calib_"))
    lines = ["# 임계값 캘리브레이션 결과\n",
             "합성 픽스처 기반(실파일과 차이 가능). 보수적으로(오탐<미탐) 선택.\n"]
    try:
        vi, ve = calibrate_video(tmp)
        lines.append("## 영상 (320비트 해밍)")
        lines.append(f"- 동일군 intra: {sorted(vi)} (max={max(vi) if vi else '-'})")
        lines.append(f"- 상이군 inter: min={min(ve) if ve else '-'}, all={sorted(ve)}")
        vrec = (max(vi) + min(ve)) // 2 if vi and ve else 40
        lines.append(f"- 권장 video_threshold ~= **{vrec}**\n")
    except Exception as e:
        lines.append(f"## 영상 측정 실패: {e}\n"); vrec = None

    try:
        atag, ai, ae = calibrate_audio(tmp)
        unit = "BER 0~1" if atag == "cp" else "256비트 해밍"
        key = "audio_ber" if atag == "cp" else "audio_threshold_lc"
        lines.append(f"## 오디오 [{atag}] ({unit})")
        lines.append(f"- 동일군 intra: {sorted(ai)} (max={max(ai) if ai else '-'})")
        lines.append(f"- 상이군 inter: min={min(ae) if ae else '-'}, all={sorted(ae)}")
        if ai and ae:
            mid = (max(ai) + min(ae)) / 2
            arec = round(mid, 2) if atag == "cp" else int(mid)
            sep = round(min(ae) - max(ai), 3)
            lines.append(f"- 권장 {key} ~= **{arec}** (분리margin={sep})\n")
        else:
            lines.append(f"- 측정 부족 → 기본값 유지\n")
    except Exception as e:
        lines.append(f"## 오디오 측정 실패: {e}\n")

    nd = calibrate_neardoc()
    lines.append("## 근접문서 (MinHash 유사도 0~1)")
    for k, v in nd.items():
        lines.append(f"- {k}: {v:.3f}")
    lines.append("- 권장 text_near_threshold: 소·중 편집은 묶고 무관은 분리되는 값(보통 0.6~0.8)\n")

    out = "\n".join(lines)
    (ROOT / "튜닝_결과.md").write_text(out, encoding="utf-8")
    print(out)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
