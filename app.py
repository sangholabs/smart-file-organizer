"""파일/폴더 정리 도구 — 네이티브 GUI (tkinter).

cmd 창 없이 창 하나에서 스캔·결과확인·선택·이동·되돌리기를 모두 처리한다.
핵심 로직은 organizer/ 모듈을 그대로 재사용한다.
모든 처리는 이 PC 안에서만 이루어지며 파일 내용은 외부로 전송되지 않는다.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# PyInstaller(onefile)에서도 organizer 패키지를 찾도록 경로 보정
BASE_DIR = Path(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(BASE_DIR))

from organizer import analyzer, applier, config, exporter, fingerprint, scanner  # noqa: E402

# 이미지 미리보기용(선택). 없으면 미리보기만 자동 비활성, 나머지 기능은 정상.
try:
    from PIL import Image, ImageTk  # noqa: F401
    HAVE_IMAGETK = True
except Exception:
    HAVE_IMAGETK = False

CHECK_ON = "☑"
CHECK_OFF = "☐"
LOCK = "\U0001F512"   # 🔒
FONT = "Malgun Gothic"

# 색상
C_DARK = "#1f2937"
C_ACCENT = "#2563eb"
C_DANGER = "#dc2626"
C_HINT = "#6b7280"


def fmt_size(b):
    if b is None:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    n = float(b)
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.1f} {units[i]}" if i else f"{int(n)} {units[i]}"


def fmt_date(t):
    if not t:
        return ""
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")


class QueueWriter:
    """print() 출력을 큐로 보내 메인 스레드에서 로그창에 표시."""

    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(s)

    def flush(self):
        pass


class Tooltip:
    """간단한 마우스 오버 툴팁."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _=None):
        if self.tip:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, bg="#111827", fg="white",
                 font=(FONT, 9), padx=8, pady=4, justify="left").pack()

    def _hide(self, _=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


CATS = [("dup", "🟥 완전중복"), ("name", "🟨 이름충돌"), ("ver", "🟦 버전이상"),
        ("image", "🟪 비슷한 이미지"), ("video", "🎬 비슷한 영상"), ("audio", "🎵 비슷한 오디오"),
        ("doc", "🟩 같은 내용 문서"), ("docnear", "🟫 비슷한 문서"),
        ("zip", "📦 같은 내용 압축"), ("archloose", "🗂 풀린 압축"),
        ("junk", "🧹 시스템 찌꺼기"), ("empty", "📂 빈 폴더")]

MINSIZE_OPTS = {"0 (전체)": 0, "100KB": 102_400, "500KB": 512_000,
                "1MB": 1_048_576, "5MB": 5_242_880}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("파일 / 폴더 정리 도구")
        self.geometry("1100x800")
        self.minsize(960, 680)

        self.cfg = config.load_config()
        geo = self.cfg.get("window_geometry")
        if geo:
            try:
                self.geometry(geo)
            except tk.TclError:
                pass
        self.analysis = None
        self.item_meta = {}          # iid -> meta dict
        self.trees = {}              # key -> Treeview
        self.log_q = queue.Queue()
        self.result_q = queue.Queue()
        self.progress_q = queue.Queue()
        self.busy = False
        self.log_visible = False
        self._uid = 0
        self._stage_t0 = {}          # stage -> 시작 시각(ETA 계산용)
        self._prog_mode = None       # 현재 progressbar 모드
        self.preview_q = queue.Queue()
        self._thumb_cache = {}       # path -> (mtime, PIL.Image)
        self._thumb_imgs = []        # PhotoImage 참조 유지(GC 방지)
        self._preview_seq = 0        # 최신 미리보기 요청만 반영
        self.cancel_event = threading.Event()
        self.sort_state = {}         # (tree_key, col) -> bool(desc)
        self._scan_count = 0

        self._setup_style()
        self._build_ui()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll)
        self.after(700, self._auto_purge)

    def _on_close(self):
        try:
            self.cfg["window_geometry"] = self.geometry()
            self._save_cfg()
        except Exception:
            pass
        self.destroy()

    # ---------------- 테마 ----------------
    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=(FONT, 10))
        style.configure("Treeview", font=(FONT, 10), rowheight=28,
                        background="#ffffff", fieldbackground="#ffffff")
        style.configure("Treeview.Heading", font=(FONT, 10, "bold"))
        style.configure("TNotebook.Tab", font=(FONT, 10, "bold"), padding=(16, 7))
        style.configure("Step.TLabel", font=(FONT, 12, "bold"), foreground=C_DARK)
        style.configure("Hint.TLabel", foreground=C_HINT, font=(FONT, 9))
        style.configure("Count.TLabel", font=(FONT, 11, "bold"))
        style.configure("Card.TLabelframe", padding=10)
        style.configure("Card.TLabelframe.Label", font=(FONT, 11, "bold"))
        style.configure("Big.TButton", font=(FONT, 11, "bold"), padding=(14, 8))
        style.configure("Accent.TButton", font=(FONT, 11, "bold"), padding=(14, 8))
        style.map("Accent.TButton",
                  background=[("!disabled", C_ACCENT), ("active", "#1d4ed8"), ("disabled", "#93b4f0")],
                  foreground=[("!disabled", "white")])
        style.configure("Danger.TButton", font=(FONT, 11, "bold"), padding=(14, 8))
        style.map("Danger.TButton",
                  background=[("!disabled", C_DANGER), ("active", "#b91c1c"), ("disabled", "#e7a3a3")],
                  foreground=[("!disabled", "white")])

    # ---------------- UI 구성 ----------------
    def _build_ui(self):
        # 헤더 배너
        banner = tk.Frame(self, bg=C_DARK)
        banner.pack(fill="x")
        tk.Label(banner, text="📁  파일 / 폴더 정리 도구", bg=C_DARK, fg="white",
                 font=(FONT, 15, "bold")).pack(side="left", padx=16, pady=(10, 2))
        tk.Label(banner, text="🔒 모든 처리는 이 PC 안에서만 — 파일 내용은 외부로 전송되지 않습니다",
                 bg=C_DARK, fg="#cbd5e1", font=(FONT, 9)).pack(side="left", padx=4, pady=(14, 2))

        # 하단 푸터 + 3단계 액션바 (먼저 bottom 에 배치)
        self.foot = ttk.Label(self, padding=(12, 4), style="Hint.TLabel")
        self.foot.pack(side="bottom", fill="x")

        action = ttk.Frame(self, padding=(12, 8))
        action.pack(side="bottom", fill="x")
        ttk.Separator(self, orient="horizontal").pack(side="bottom", fill="x")
        ttk.Label(action, text="3단계 · 정리 실행", style="Step.TLabel").pack(side="left")
        self.count_lbl = ttk.Label(action, text="선택됨 0개", style="Count.TLabel")
        self.count_lbl.pack(side="left", padx=16)
        self.btn_apply = ttk.Button(action, text="③ 실제 이동", style="Danger.TButton",
                                    command=lambda: self.on_apply(True))
        self.btn_apply.pack(side="right", padx=4)
        self.btn_preview = ttk.Button(action, text="② 미리보기", style="Big.TButton",
                                      command=lambda: self.on_apply(False))
        self.btn_preview.pack(side="right", padx=4)
        ttk.Button(action, text="되돌리기", command=self.on_undo).pack(side="right", padx=4)
        ttk.Button(action, text="🗑 격리 비우기", command=self.on_purge).pack(side="right", padx=4)
        ttk.Button(action, text="⬇ 결과 내보내기", command=self.on_export).pack(side="right", padx=4)
        self.btn_log = ttk.Button(action, text="진행 상황 ▾", command=self._toggle_log)
        self.btn_log.pack(side="right", padx=4)

        # 본문
        content = ttk.Frame(self, padding=(10, 8))
        content.pack(side="top", fill="both", expand=True)

        # --- 1단계: 폴더 선택 ---
        step1 = ttk.LabelFrame(content, text="1단계 · 검사할 폴더 선택", style="Card.TLabelframe")
        step1.pack(fill="x", pady=(0, 8))
        left = ttk.Frame(step1)
        left.pack(side="left", fill="both", expand=True)
        lb_wrap = ttk.Frame(left)
        lb_wrap.pack(fill="x")
        self.roots_list = tk.Listbox(lb_wrap, height=4, font=(FONT, 10),
                                     activestyle="none", highlightthickness=1,
                                     highlightbackground="#d1d5db")
        rsb = ttk.Scrollbar(lb_wrap, orient="vertical", command=self.roots_list.yview)
        self.roots_list.configure(yscrollcommand=rsb.set)
        self.roots_list.pack(side="left", fill="x", expand=True)
        rsb.pack(side="right", fill="y")
        ttk.Label(left, text="D:\\, Z:\\ 등 다른 드라이브/폴더도 '폴더 추가'로 넣을 수 있습니다.",
                  style="Hint.TLabel").pack(anchor="w", pady=(4, 0))

        right = ttk.Frame(step1)
        right.pack(side="left", fill="y", padx=(12, 0))
        ttk.Button(right, text="＋ 폴더 추가", command=self.add_root).pack(fill="x", pady=2)
        ttk.Button(right, text="－ 선택 제거", command=self.remove_root).pack(fill="x", pady=2)
        self.btn_scan = ttk.Button(right, text="🔍 스캔 및 분석 (F5)", style="Accent.TButton",
                                   command=self._scan_or_cancel)
        self.btn_scan.pack(fill="x", pady=(10, 2))

        # 정리 방식 옵션
        opt = ttk.Frame(step1)
        opt.pack(side="left", fill="y", padx=(16, 0))
        ttk.Label(opt, text="정리 방식", style="Hint.TLabel").pack(anchor="w")
        self.trash_var = tk.BooleanVar(value=bool(self.cfg.get("use_recycle_bin", False)))
        c1 = ttk.Checkbutton(opt, text="삭제 대신 휴지통으로 보내기", variable=self.trash_var,
                             command=self.on_toggle_trash)
        c1.pack(anchor="w", pady=(2, 0))
        Tooltip(c1, "켜면 Windows 휴지통으로 보냅니다(복원은 휴지통에서).\n끄면 격리폴더로 이동(앱에서 되돌리기 가능).")
        self.perdrive_var = tk.BooleanVar(value=bool(self.cfg.get("quarantine_per_drive", False)))
        self.perdrive_chk = ttk.Checkbutton(opt, text="외장정리: 같은 드라이브에 격리",
                                            variable=self.perdrive_var, command=self.on_toggle_perdrive)
        self.perdrive_chk.pack(anchor="w", pady=(2, 0))
        Tooltip(self.perdrive_chk, "외장/USB 정리 시 각 파일이 있던 드라이브 안에\n격리폴더를 만들어 드라이브 간 복사를 막습니다.")
        self.image_loose_var = tk.BooleanVar(value=(self.cfg.get("image_match") == "loose"))
        c3 = ttk.Checkbutton(opt, text="이미지: 비슷한 사진도(느슨)", variable=self.image_loose_var,
                             command=self.on_toggle_image)
        c3.pack(anchor="w", pady=(2, 0))
        Tooltip(c3, "끄면 사실상 동일한 사진만(엄격).\n켜면 약간 다른(크롭·보정) 사진도 묶음 — 오탐 가능.\n스캔돼 있으면 즉시 재분석합니다.")
        msrow = ttk.Frame(opt)
        msrow.pack(anchor="w", pady=(6, 0))
        ttk.Label(msrow, text="최소 크기", style="Hint.TLabel").pack(side="left")
        cur_b = int(self.cfg.get("min_size_bytes", 102_400))
        cur_label = next((k for k, v in MINSIZE_OPTS.items() if v == cur_b), "100KB")
        self.minsize_var = tk.StringVar(value=cur_label)
        mcb = ttk.Combobox(msrow, textvariable=self.minsize_var, width=9, state="readonly",
                           values=list(MINSIZE_OPTS.keys()))
        mcb.pack(side="left", padx=4)
        mcb.bind("<<ComboboxSelected>>", lambda e: self.on_minsize())
        Tooltip(mcb, "이 크기 미만 파일은 스캔 제외.\n작은 중복도 잡으려면 100KB 이하 권장. 다음 스캔부터 적용.")
        # 무거운 탐지 토글(다음 스캔부터 적용)
        self.ocr_var = tk.BooleanVar(value=bool(self.cfg.get("ocr_enabled", True)))
        co = ttk.Checkbutton(opt, text="스캔 시 OCR(느림)", variable=self.ocr_var,
                             command=lambda: self._save_flag("ocr_enabled", self.ocr_var))
        co.pack(anchor="w", pady=(6, 0))
        Tooltip(co, "글자 없는 스캔 PDF·이미지에서 글자를 읽어 문서중복에 합류.\n매우 느림. 다음 스캔부터 적용.")
        self.video_var = tk.BooleanVar(value=bool(self.cfg.get("video_match", True)))
        cv = ttk.Checkbutton(opt, text="영상 중복 탐지(느림)", variable=self.video_var,
                             command=lambda: self._save_flag("video_match", self.video_var))
        cv.pack(anchor="w", pady=(2, 0))
        Tooltip(cv, "재인코딩·해상도 다른 같은 영상 탐지. 느림. 다음 스캔부터 적용.")
        self.audio_var = tk.BooleanVar(value=bool(self.cfg.get("audio_match", False)))
        ca = ttk.Checkbutton(opt, text="오디오 중복(실험적·느림)", variable=self.audio_var,
                             command=lambda: self._save_flag("audio_match", self.audio_var))
        ca.pack(anchor="w", pady=(2, 0))
        Tooltip(ca, "재인코딩 음악 탐지. 실험적이라 정확도가 낮을 수 있음. 다음 스캔부터 적용.")
        if self.trash_var.get():
            self.perdrive_chk.configure(state="disabled")

        # --- 2단계: 결과 선택 ---
        head2 = ttk.Frame(content)
        head2.pack(fill="x")
        ttk.Label(head2, text="2단계 · 정리할 항목 선택", style="Step.TLabel").pack(side="left")
        self.status = ttk.Label(head2, text="아직 스캔하지 않았습니다. '스캔 및 분석'(F5)을 눌러 시작하세요.",
                                style="Hint.TLabel")
        self.status.pack(side="left", padx=12)
        # 진행 표시(스캔 중에만 보임)
        self.prog_lbl = ttk.Label(head2, text="", style="Hint.TLabel")
        self.prog_lbl.pack(side="right", padx=(6, 0))
        self.progress = ttk.Progressbar(head2, mode="indeterminate", length=180)
        # pack 은 busy 시에만

        # 요약 칩 바 (클릭하면 해당 탭으로 이동)
        chipbar = ttk.Frame(content)
        chipbar.pack(fill="x", pady=(6, 2))
        self.chips = {}
        chip_colors = {"dup": "#fee2e2", "name": "#fef9c3", "ver": "#dbeafe",
                       "image": "#ede9fe", "video": "#fce7f3", "audio": "#cffafe",
                       "doc": "#dcfce7", "docnear": "#fae8d6", "zip": "#e5e7eb",
                       "archloose": "#e2e8f0", "junk": "#d1fae5", "empty": "#fde68a"}
        for key, title in CATS:
            lbl = tk.Label(chipbar, text=f"{title} 0", bg=chip_colors[key], fg="#374151",
                           font=(FONT, 9, "bold"), padx=10, pady=4, cursor="hand2")
            lbl.pack(side="left", padx=(0, 6))
            lbl.bind("<Button-1>", lambda e, k=key: self._goto_tab(k))
            self.chips[key] = lbl
        self.chip_reclaim = tk.Label(chipbar, text="회수가능 0", bg="#1f2937", fg="white",
                                     font=(FONT, 9, "bold"), padx=10, pady=4)
        self.chip_reclaim.pack(side="right")

        # 검색 · 필터 바
        sbar = ttk.Frame(content)
        sbar.pack(fill="x", pady=(0, 4))
        ttk.Label(sbar, text="🔎 검색", style="Hint.TLabel").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self._schedule_filter())
        ent = ttk.Entry(sbar, textvariable=self.search_var, width=28)
        ent.pack(side="left", padx=6)
        ttk.Button(sbar, text="지우기", command=lambda: self.search_var.set("")).pack(side="left")
        ttk.Label(sbar, text="크기 ≥", style="Hint.TLabel").pack(side="left", padx=(14, 4))
        self.size_var = tk.StringVar(value="전체")
        size_cb = ttk.Combobox(sbar, textvariable=self.size_var, width=8, state="readonly",
                               values=["전체", "1MB", "10MB", "100MB", "1GB"])
        size_cb.pack(side="left")
        size_cb.bind("<<ComboboxSelected>>", lambda e: self._apply_filter())
        ttk.Label(sbar, text="· 컬럼 머리글 클릭 = 정렬", style="Hint.TLabel").pack(side="left", padx=12)

        self.notebook = ttk.Notebook(content)
        self.notebook.pack(fill="both", expand=True, pady=(4, 0))
        self.cat_frames = {}
        self._make_dashboard_tab()
        for key, title in CATS:
            self._make_tab(key, title)
        self._make_insights_tab()

        # 이미지 미리보기 패널(접이식, 아래쪽)
        self._make_preview_pane(content)

        # 로그 (접이식)
        self.log_frame = ttk.LabelFrame(content, text="진행 로그", style="Card.TLabelframe")
        self.log = tk.Text(self.log_frame, height=7, wrap="word", state="disabled",
                           font=("Consolas", 9), bg="#0f172a", fg="#e2e8f0")
        lsb = ttk.Scrollbar(self.log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")
        # 기본 숨김

        self._update_footer()

    def _make_tab(self, key, title):
        frame = ttk.Frame(self.notebook, padding=6)
        top = ttk.Frame(frame)
        top.pack(fill="x", pady=(0, 6))
        ttk.Button(top, text="추천만 선택", command=lambda: self.set_all(None, key)).pack(side="left", padx=2)
        ttk.Button(top, text="모두 선택", command=lambda: self.set_all(True, key)).pack(side="left", padx=2)
        ttk.Button(top, text="모두 해제", command=lambda: self.set_all(False, key)).pack(side="left", padx=2)
        ttk.Label(top, text="👉 ☑칸 클릭=선택/해제 · 파일명 클릭=열기 · 그룹 줄 클릭=그룹 전체 · 우클릭=메뉴",
                  style="Hint.TLabel").pack(side="left", padx=12)

        tv = ttk.Treeview(frame, columns=("name", "folder", "size", "date"),
                          show="tree headings", selectmode="none")
        tv.heading("#0", text="선택")
        tv.heading("name", text="파일  (☑ 정리대상 · ☐ 보관 · 🔒 항상보관)",
                   command=lambda: self._sort_by(key, "name"))
        tv.heading("folder", text="폴더", command=lambda: self._sort_by(key, "folder"))
        tv.heading("size", text="크기", command=lambda: self._sort_by(key, "size"))
        tv.heading("date", text="수정일", command=lambda: self._sort_by(key, "date"))
        tv.column("#0", width=52, anchor="center", stretch=False)
        tv.column("name", width=340, stretch=True)
        tv.column("folder", width=320, stretch=True)
        tv.column("size", width=85, anchor="e", stretch=False)
        tv.column("date", width=135, anchor="center", stretch=False)
        sb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tv.bind("<Button-1>", self.on_tree_click)
        tv.bind("<Button-3>", self.on_tree_right)
        tv.bind("<space>", self._kb_toggle)
        tv.bind("<Return>", self._kb_open)
        tv.tag_configure("oddrow", background="#f6f8fb")
        tv.tag_configure("evenrow", background="#ffffff")
        tv.tag_configure("checked", background="#cfe6ff")
        tv.tag_configure("group", font=(FONT, 10, "bold"), background="#e8edf3")
        tv.tag_configure("keep", foreground="#059669")
        tv.tag_configure("info", foreground="#9ca3af")
        self.notebook.add(frame, text=title)
        self.cat_frames[key] = frame
        self.trees[key] = tv

    def _make_insights_tab(self):
        frame = ttk.Frame(self.notebook, padding=6)
        self.ins_summary = ttk.Label(frame, text="스캔하면 디스크 용량 분석이 표시됩니다.",
                                     style="Step.TLabel")
        self.ins_summary.pack(anchor="w", pady=(0, 6))
        ttk.Label(frame, text="더블클릭 = 파일/폴더 열기 · 정리 후보가 아니라 '용량 현황' 보기입니다",
                  style="Hint.TLabel").pack(anchor="w", pady=(0, 6))

        grid = ttk.Frame(frame)
        grid.pack(fill="both", expand=True)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        grid.rowconfigure(0, weight=1)
        grid.rowconfigure(1, weight=1)

        self.ins_trees = {}
        self.ins_paths = {}  # iid -> (kind, path)

        def make_panel(row, col, title, columns, headings, widths):
            lf = ttk.LabelFrame(grid, text=title, style="Card.TLabelframe")
            lf.grid(row=row, column=col, sticky="nsew", padx=4, pady=4)
            tv = ttk.Treeview(lf, columns=columns, show="tree headings",
                              selectmode="browse", height=8)
            tv.heading("#0", text=headings[0])
            tv.column("#0", width=widths[0], stretch=True)
            for c, h, w in zip(columns, headings[1:], widths[1:]):
                tv.heading(c, text=h)
                anchor = "e" if h in ("크기", "개수") else "w"
                tv.column(c, width=w, anchor=anchor, stretch=False)
            sb = ttk.Scrollbar(lf, orient="vertical", command=tv.yview)
            tv.configure(yscrollcommand=sb.set)
            tv.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            tv.bind("<Double-Button-1>", self._ins_open)
            return tv

        self.ins_trees["folders"] = make_panel(
            0, 0, "📁 용량 많이 쓰는 폴더 (하위 포함)", ("size",),
            ["폴더", "크기"], [320, 90])
        self.ins_trees["ext"] = make_panel(
            0, 1, "🧩 확장자별 용량", ("size", "count"),
            ["확장자", "크기", "개수"], [120, 90, 70])
        self.ins_trees["largest"] = make_panel(
            1, 0, "⬆️ 가장 큰 파일", ("folder", "size"),
            ["파일", "폴더", "크기"], [200, 200, 90])
        self.ins_trees["old"] = make_panel(
            1, 1, "🕰️ 오래된 대용량 파일 (2년+ 미수정)", ("size", "date"),
            ["파일", "크기", "수정일"], [240, 90, 110])

        self.notebook.add(frame, text="📊 용량 분석")

    def _ins_open(self, event):
        tv = event.widget
        iid = tv.identify_row(event.y)
        info = self.ins_paths.get(iid)
        if not info:
            return
        kind, path = info
        if kind == "folder":
            if os.path.isdir(path):
                os.startfile(path)  # noqa: S606
        else:
            self._open_file(path)

    def _make_dashboard_tab(self):
        frame = ttk.Frame(self.notebook, padding=14)
        ttk.Label(frame, text="스캔 결과 요약", style="Step.TLabel").pack(anchor="w")
        self.dash_status = ttk.Label(
            frame, text="'스캔 및 분석'(F5)을 누르면 결과 요약이 표시됩니다.", style="Hint.TLabel")
        self.dash_status.pack(anchor="w", pady=(2, 10))
        grid = ttk.Frame(frame)
        grid.pack(fill="x")
        self.dash_cards = {}
        cols = 4
        for i, (key, title) in enumerate(CATS):
            card = tk.Frame(grid, bg="#f1f5f9", highlightbackground="#cbd5e1",
                            highlightthickness=1, cursor="hand2")
            card.grid(row=i // cols, column=i % cols, sticky="nsew", padx=6, pady=6, ipadx=8, ipady=8)
            grid.columnconfigure(i % cols, weight=1)
            num = tk.Label(card, text="0", bg="#f1f5f9", fg="#111827", font=(FONT, 20, "bold"))
            num.pack(anchor="w", padx=10, pady=(6, 0))
            tk.Label(card, text=title, bg="#f1f5f9", fg="#475569",
                     font=(FONT, 10)).pack(anchor="w", padx=10, pady=(0, 6))
            for w in (card, num):
                w.bind("<Button-1>", lambda e, k=key: self._goto_tab(k))
            self.dash_cards[key] = num
        # 환경 점검: 어떤 선택 기능이 이 PC에서 동작 가능한지 한 줄 + 누르면 상세
        env = ttk.Frame(frame)
        env.pack(fill="x", pady=(14, 0))
        self.dash_env = ttk.Label(env, text="", style="Hint.TLabel", cursor="hand2")
        self.dash_env.pack(anchor="w")
        self.dash_env.bind("<Button-1>", lambda e: self._show_env_detail())
        self._refresh_env_line()
        self.notebook.add(frame, text="📊 요약")
        self.dash_frame = frame

    def _refresh_env_line(self):
        try:
            from organizer import doctor
            rows = doctor.check()
        except Exception:
            self.dash_env.configure(text="")
            return
        miss = [c["name"] for c in rows if not c["ok"]]
        if not miss:
            txt = "환경 점검: 모든 선택 기능 사용 가능 ✓ (눌러서 상세)"
        else:
            txt = f"환경 점검: {len(miss)}개 기능 비활성 — {', '.join(miss[:4])}{'…' if len(miss) > 4 else ''} (눌러서 설치법)"
        self.dash_env.configure(text=txt)

    def _show_env_detail(self):
        try:
            from organizer import doctor
            from tkinter import messagebox
            messagebox.showinfo("환경 점검", doctor.format_report())
        except Exception:
            pass

    def _update_dashboard(self, counts, st):
        self.dash_status.configure(
            text=(f"스캔 {st['total_files']:,}개 · 정리후보 {sum(counts.values())}그룹 · "
                  f"회수가능 약 {fmt_size(st['reclaimable_bytes'])} · 카드를 누르면 해당 탭으로 이동"))
        for key, num in self.dash_cards.items():
            n = counts.get(key, 0)
            num.configure(text=str(n), fg=("#111827" if n else "#9ca3af"))

    def _populate_insights(self, ins):
        for tv in self.ins_trees.values():
            tv.delete(*tv.get_children())
        self.ins_paths.clear()
        if not ins or not ins.get("total_files"):
            self.ins_summary.configure(text="분석할 색인이 없습니다.")
            return
        self.ins_summary.configure(
            text=f"총 {ins['total_files']:,}개 · {fmt_size(ins['total_size'])} (색인 기준)")

        for d in ins.get("top_folders", []):
            iid = self._new_iid()
            self.ins_trees["folders"].insert("", "end", iid=iid, text=d["path"],
                                             values=(fmt_size(d["size"]),))
            self.ins_paths[iid] = ("folder", d["path"])
        for e in ins.get("by_ext", [])[:40]:
            iid = self._new_iid()
            self.ins_trees["ext"].insert("", "end", iid=iid, text=e["ext"],
                                         values=(fmt_size(e["size"]), f"{e['count']:,}"))
        for f in ins.get("largest_files", []):
            iid = self._new_iid()
            self.ins_trees["largest"].insert(
                "", "end", iid=iid, text=os.path.basename(f["path"]),
                values=(os.path.dirname(f["path"]), fmt_size(f["size"])))
            self.ins_paths[iid] = ("file", f["path"])
        for f in ins.get("old_files", []):
            iid = self._new_iid()
            self.ins_trees["old"].insert(
                "", "end", iid=iid, text=os.path.basename(f["path"]),
                values=(fmt_size(f["size"]), fmt_date(f["mtime"])))
            self.ins_paths[iid] = ("file", f["path"])

    def _update_footer(self):
        if self.cfg.get("use_recycle_bin"):
            dest = "정리 대상은 Windows 휴지통으로 보냅니다(복원은 휴지통에서)."
        elif self.cfg.get("quarantine_per_drive"):
            dest = "정리 대상은 각 파일이 있던 드라이브 안의 격리폴더로 이동합니다(영구삭제 없음, 되돌리기 가능)."
        else:
            dest = "정리 대상은 격리폴더로 이동합니다(영구삭제 없음, 되돌리기 가능)."
        self.foot.configure(text="🔒 로컬 전용 · " + dest)

    # ---------------- 로그 토글 ----------------
    def _toggle_log(self):
        if self.log_visible:
            self._hide_log()
        else:
            self._show_log()

    def _show_log(self):
        if not self.log_visible:
            self.log_frame.pack(fill="both", expand=False, pady=(8, 0))
            self.log_visible = True
            self.btn_log.configure(text="진행 상황 ▴")

    def _hide_log(self):
        if self.log_visible:
            self.log_frame.pack_forget()
            self.log_visible = False
            self.btn_log.configure(text="진행 상황 ▾")

    # ---------------- 스캔 폴더 편집 ----------------
    def _reload_roots(self):
        self.roots_list.delete(0, "end")
        for r in self.cfg.get("scan_roots", []):
            self.roots_list.insert("end", r)

    def add_root(self):
        d = filedialog.askdirectory(title="검사할 폴더 선택")
        if d:
            d = os.path.normpath(d)
            if d not in self.cfg["scan_roots"]:
                self.cfg["scan_roots"].append(d)
                self._save_cfg()
                self._reload_roots()

    def remove_root(self):
        sel = self.roots_list.curselection()
        if sel:
            val = self.roots_list.get(sel[0])
            self.cfg["scan_roots"] = [r for r in self.cfg["scan_roots"] if r != val]
            self._save_cfg()
            self._reload_roots()

    def _save_cfg(self):
        with open(config.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, ensure_ascii=False, indent=2)

    def on_toggle_trash(self):
        val = self.trash_var.get()
        if val:
            try:
                import send2trash  # noqa: F401
            except Exception:
                messagebox.showwarning(
                    "휴지통 기능 사용 불가",
                    "send2trash 모듈이 없어 휴지통으로 보낼 수 없습니다.\n"
                    "격리폴더 이동 모드로 유지합니다.\n\n"
                    "(직접 실행 시: pip install send2trash 후 다시 시도)")
                self.trash_var.set(False)
                val = False
        self.cfg["use_recycle_bin"] = val
        self._save_cfg()
        self.perdrive_chk.configure(state="disabled" if val else "normal")
        self._update_footer()

    def on_toggle_perdrive(self):
        self.cfg["quarantine_per_drive"] = self.perdrive_var.get()
        self._save_cfg()
        self._update_footer()

    def on_toggle_image(self):
        self.cfg["image_match"] = "loose" if self.image_loose_var.get() else "strict"
        self._save_cfg()
        self._reanalyze()  # 색인은 그대로, 분석만 다시(빠름)

    def on_minsize(self):
        self.cfg["min_size_bytes"] = MINSIZE_OPTS.get(self.minsize_var.get(), 102_400)
        self._save_cfg()
        self.status.configure(text="최소 크기를 바꿨습니다. 다시 스캔하면 적용됩니다.")

    def _save_flag(self, key, var):
        self.cfg[key] = bool(var.get())
        self._save_cfg()
        self.status.configure(text=f"'{key}' 설정을 바꿨습니다. 다시 스캔하면 적용됩니다.")

    def _reanalyze(self):
        if self.busy or not self.analysis or not config.CATALOG_DB.exists():
            return
        self.status.configure(text="다시 분석 중...")

        def job():
            result = analyzer.analyze(cfg=self.cfg)
            self.result_q.put(("analysis", result))

        self._run_thread(job)

    def open_data(self):
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(config.DATA_DIR))  # noqa: S606

    # ---------------- 로그 ----------------
    def log_write(self, s):
        self.log.configure(state="normal")
        self.log.insert("end", s)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _poll(self):
        self._drain_preview()
        while not self.log_q.empty():
            self.log_write(self.log_q.get_nowait())
        # 진행 표시(마지막 값만 반영)
        last = None
        while not self.progress_q.empty():
            last = self.progress_q.get_nowait()
        if last is not None:
            stage, done, total, msg = last
            short = os.path.basename((msg or "").rstrip("\\/")) or (msg or "")
            label = {"collect": "수집", "hash": "중복 해시", "fingerprint": "내용 지문"}.get(stage, stage)
            if total:  # 총량을 아는 단계 → 결정형 막대 + % + ETA
                self._set_prog_mode("determinate")
                pct = max(0, min(100, done * 100 // total))
                self.progress.configure(value=pct)
                eta = self._eta(stage, done, total)
                self.prog_lbl.configure(
                    text=f"{label} {done:,}/{total:,} ({pct}%){eta} · {short}")
            else:      # 총량 모름(수집) → 무한 막대 + 카운트
                self._set_prog_mode("indeterminate")
                self._stage_t0.pop(stage, None)
                self.prog_lbl.configure(text=f"{label} {done:,}개 · {short}")
        while not self.result_q.empty():
            kind, payload = self.result_q.get_nowait()
            if kind == "analysis":
                self.analysis = payload
                self.populate_results()
                self._record_history(payload)
                self._set_busy(False)
            elif kind == "cancelled":
                self._set_busy(False)
                self.btn_scan.configure(state="normal")
                self.status.configure(text=payload)
            elif kind == "done":
                self._set_busy(False)
                if payload:
                    self.log_write("\n" + payload + "\n")
                    self.status.configure(text=payload)
            elif kind == "error":
                self._set_busy(False)
                messagebox.showerror("오류", payload)
        self.after(80, self._poll)

    def _eta(self, stage, done, total):
        """경과/처리율 기반 남은 시간 문자열(' · 남은 ~m:ss'). 못 구하면 빈 문자열."""
        now = time.time()
        t0 = self._stage_t0.get(stage)
        if t0 is None:
            self._stage_t0[stage] = now
            return ""
        elapsed = now - t0
        if done <= 0 or elapsed < 1.0:
            return ""
        remain = elapsed / done * (total - done)
        if remain < 1:
            return ""
        m, s = divmod(int(remain), 60)
        return f" · 남은 ~{m}:{s:02d}" if m else f" · 남은 ~{s}초"

    def _set_prog_mode(self, mode):
        """determinate/indeterminate 전환(중복 호출 안전)."""
        if self._prog_mode == mode:
            return
        self._prog_mode = mode
        try:
            if mode == "indeterminate":
                self.progress.configure(mode="indeterminate")
                self.progress.start(12)
            else:
                self.progress.stop()
                self.progress.configure(mode="determinate", value=0)
        except tk.TclError:
            pass

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.btn_preview, self.btn_apply):
            b.configure(state=state)
        # 스캔 버튼은 진행 중엔 '멈춤'으로 전환(계속 활성)
        if busy:
            self.btn_scan.configure(text="■ 멈춤 (Esc)", state="normal")
            self.progress.pack(side="right", padx=(8, 0))
            self._stage_t0.clear()
            self._prog_mode = None
            self._set_prog_mode("indeterminate")
        else:
            self.btn_scan.configure(text="🔍 스캔 및 분석 (F5)", state="normal")
            try:
                self.progress.stop()
            except tk.TclError:
                pass
            self._prog_mode = None
            self.progress.pack_forget()
            self.prog_lbl.configure(text="")

    def _run_thread(self, fn):
        if self.busy:
            return
        self._set_busy(True)
        self._bg(fn)

    def _bg(self, fn):
        def worker():
            old = sys.stdout
            sys.stdout = QueueWriter(self.log_q)
            try:
                fn()
            except Exception:
                self.result_q.put(("error", traceback.format_exc()))
            finally:
                sys.stdout = old

        threading.Thread(target=worker, daemon=True).start()

    def on_export(self):
        if not self.analysis:
            messagebox.showinfo("결과 내보내기", "먼저 '스캔 및 분석'을 실행하세요.")
            return
        path = filedialog.asksaveasfilename(
            title="결과 내보내기",
            defaultextension=".csv",
            initialfile="정리결과.csv",
            filetypes=[("CSV (Excel)", "*.csv"), ("JSON", "*.json")])
        if not path:
            return
        try:
            out = exporter.export(self.analysis, path)
            n = sum(1 for _ in exporter.iter_rows(self.analysis))
            if messagebox.askyesno(
                    "내보내기 완료",
                    f"{n:,}개 항목을 저장했습니다.\n{out}\n\n폴더를 열까요?"):
                subprocess.Popen(["explorer", "/select,", str(out)])
        except Exception as e:
            messagebox.showerror("내보내기 오류", str(e))

    # ---------------- 격리 비우기 / 스캔 이력 ----------------
    def on_purge(self):
        prev = applier.purge_quarantine(self.cfg, days=None, do_apply=False)
        if prev["groups"] == 0:
            messagebox.showinfo("격리 비우기", "격리폴더가 비어 있습니다.")
            return
        if not messagebox.askyesno(
                "격리 비우기",
                f"격리폴더의 보관본 {prev['groups']}개({fmt_size(prev['bytes'])})를 "
                f"Windows 휴지통으로 보냅니다.\n(휴지통에서 복원 가능)\n\n진행할까요?"):
            return
        self._show_log()

        def job():
            r = applier.purge_quarantine(self.cfg, days=None, do_apply=True)
            if r.get("no_trash"):
                self.result_q.put(("done", "휴지통 기능(send2trash)이 없어 비우지 못했습니다. 격리폴더를 직접 비우세요."))
            else:
                self.result_q.put(("done", f"격리폴더 정리: {r['purged']}개 보관본을 휴지통으로 보냈습니다."))

        self._run_thread(job)

    def _auto_purge(self):
        days = self.cfg.get("quarantine_purge_days") or 0
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = 0
        if days <= 0:
            return

        def job():
            r = applier.purge_quarantine(self.cfg, days=days, do_apply=True)
            if r.get("purged"):
                self.result_q.put(("done", f"오래된 격리본 {r['purged']}개 자동 정리(휴지통)."))

        self._bg(job)

    def _record_history(self, a):
        path = config.DATA_DIR / "scan_history.json"
        try:
            hist = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        except Exception:
            hist = []
        st = a.get("stats", {})
        hist.append({"at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                     "files": st.get("total_files", 0),
                     "dup": st.get("exact_dup_groups", 0),
                     "reclaim": st.get("reclaimable_bytes", 0)})
        hist = hist[-50:]
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        self._scan_count = len(hist)

    # ---------------- 스캔/분석 ----------------
    def _scan_or_cancel(self):
        if self.busy:
            self.cancel_event.set()
            self.status.configure(text="멈추는 중...")
            self.btn_scan.configure(state="disabled")
        else:
            self.on_scan()

    def on_scan(self):
        self.cfg = config.load_config()
        self._reload_roots()
        self._show_log()
        self.status.configure(text="스캔 중...")
        self.cancel_event.clear()

        def prog(stage, done, total, msg):
            self.progress_q.put((stage, done, total, msg))

        def job():
            res = scanner.run_scan(self.cfg, progress=prog, cancel=self.cancel_event)
            if res.get("cancelled"):
                self.result_q.put(("cancelled", "스캔을 멈췄습니다(부분 색인 보존). 다시 스캔하면 이어집니다."))
                return
            print("\n분석 중...")
            result = analyzer.analyze(cfg=self.cfg)
            self.result_q.put(("analysis", result))

        self._run_thread(job)

    # ---------------- 결과 표시 ----------------
    def _new_iid(self):
        self._uid += 1
        return f"i{self._uid}"

    def _add_group(self, tv, label):
        iid = self._new_iid()
        tv.insert("", "end", iid=iid, text="", values=(label, "", "", ""),
                  tags=("group",), open=True)
        self.item_meta[iid] = {"type": "group", "tree": tv, "children": []}
        return iid

    def _add_file(self, tv, gid, f, idx, checkable, checked, prefix=""):
        iid = self._new_iid()
        name = os.path.basename(f["path"])
        folder = os.path.dirname(f["path"])
        zebra = "oddrow" if idx % 2 else "evenrow"
        if not checkable:
            box, tag = LOCK, "keep"
        else:
            box = CHECK_ON if checked else CHECK_OFF
            tag = "checked" if checked else zebra
        tv.insert(gid, "end", iid=iid, text=box,
                  values=(f"{prefix}{name}", folder, fmt_size(f["size"]), fmt_date(f["mtime"])),
                  tags=(tag,))
        self.item_meta[iid] = {"type": "file", "tree": tv, "path": f["path"], "size": f["size"],
                               "mtime": f.get("mtime", 0),
                               "checkable": checkable, "checked": checked, "rec": checked, "zebra": zebra}
        self.item_meta[gid]["children"].append(iid)

    def populate_results(self, fresh=True):
        if not self.analysis:
            return
        for tv in self.trees.values():
            tv.delete(*tv.get_children())
        self.item_meta.clear()
        a = self.analysis
        st = a["stats"]

        counts = {"dup": len(a["exact_duplicates"]),
                  "name": len(a["name_conflicts"]),
                  "ver": len(a["version_anomalies"]),
                  "image": len(a.get("image_dups", [])),
                  "video": len(a.get("video_dups", [])),
                  "audio": len(a.get("audio_dups", [])),
                  "doc": len(a.get("doc_dups", [])),
                  "docnear": len(a.get("doc_near", [])),
                  "zip": len(a.get("zip_dups", [])),
                  "archloose": len(a.get("archive_loose", [])),
                  "junk": len(a.get("junk_files", [])),
                  "empty": len(a.get("empty_dirs", []))}
        # 탭 라벨/상태 + 요약 칩 갱신(원본 건수 기준)
        for key, title in CATS:
            n = counts[key]
            self.notebook.tab(self.cat_frames[key], text=f"{title} ({n})",
                              state=("normal" if n else "disabled"))
            self.chips[key].configure(text=f"{title} {n}",
                                      fg=("#111827" if n else "#9ca3af"))
        self.chip_reclaim.configure(text=f"회수가능 {fmt_size(st['reclaimable_bytes'])}")
        self._update_dashboard(counts, st)
        total = sum(counts.values())
        filt = self.search_var.get().strip() or self._min_size()
        self.status.configure(
            text=(f"스캔 {st['total_files']:,}개 · 정리후보 {total}그룹 · "
                  f"회수가능 약 {fmt_size(st['reclaimable_bytes'])}"
                  + ("  ·  🔎 필터 적용 중" if filt else "")))

        def opts_dup(f):
            keep = f["recommend"] == "keep"
            return (not keep, f["recommend"] == "move", "[보관] " if keep else "")

        def opts_review(f):
            return (True, False, "")

        def opts_ver(f):
            pre = ""
            if "이름상_최신" in f.get("tags", []):
                pre += "[이름상 최신] "
            if "실제_최근수정" in f.get("tags", []):
                pre += "[실제 최근수정] "
            return (True, False, pre)

        def opts_keeper(f):
            keep = f["recommend"] == "keep"
            return (not keep, False, "[보관] " if keep else "")

        self._fill("dup", a["exact_duplicates"],
                   lambda g: f"중복 {g['count']}개 · 각 {fmt_size(g['size'])} · 정리 시 {fmt_size(g['size']*(g['count']-1))} 회수",
                   "완전히 동일한 중복 파일이 없습니다.", opts_dup)
        self._fill("name", a["name_conflicts"],
                   lambda g: f"{g['name']} · {g['count']}개 (내용 {g['distinct']}종)",
                   "이름이 같지만 내용이 다른 파일이 없습니다.", opts_review)
        self._fill("ver", a["version_anomalies"],
                   lambda g: f"{g['family']}{g['ext']} · {g['count']}개",
                   "버전 표기와 수정일이 어긋난 파일이 없습니다.", opts_ver)
        self._fill("image", a.get("image_dups", []),
                   lambda g: f"리사이즈·재압축·메타데이터만 다른 사실상 같은 사진 · {g['count']}개",
                   "사실상 같은 이미지가 없습니다.", opts_keeper)
        self._fill("video", a.get("video_dups", []),
                   lambda g: f"재인코딩·해상도만 다른 사실상 같은 영상 · {g['count']}개",
                   "사실상 같은 영상이 없습니다.", opts_keeper)
        self._fill("audio", a.get("audio_dups", []),
                   lambda g: f"재인코딩된 같은 오디오(실험적) · {g['count']}개",
                   "비슷한 오디오가 없습니다(또는 오디오 탐지 꺼짐).", opts_keeper)
        self._fill("doc", a.get("doc_dups", []),
                   lambda g: f"글자 내용이 같은 문서(서식·메타데이터 무시) · {g['count']}개",
                   "내용이 같은 문서가 없습니다.", opts_keeper)
        self._fill("docnear", a.get("doc_near", []),
                   lambda g: f"내용이 거의 같은 문서(약간 수정된 버전) · {g['count']}개",
                   "비슷한(근접) 문서가 없습니다.", opts_keeper)
        self._fill("zip", a.get("zip_dups", []),
                   lambda g: f"압축방식·날짜만 다른 같은 내용의 압축파일 · {g['count']}개",
                   "내용이 같은 압축파일이 없습니다.", opts_keeper)

        # 🗂 풀린 압축(내용이 이미 디스크에 다 풀려있는 압축) — 검토(미체크)
        at = self.trees["archloose"]
        all_arch = a.get("archive_loose", [])
        arch = [f for f in all_arch if self._passes(f)]
        if arch:
            total = sum(f["size"] for f in all_arch)
            gid = self._add_group(
                at, f"내용이 이미 풀려있는 압축 {len(all_arch)}개 · 합계 {fmt_size(total)}")
            for idx, f in enumerate(arch):
                self._add_file(at, gid, f, idx, checkable=True, checked=False)
        else:
            self._empty(at, "내용이 이미 풀려있는 압축이 없습니다.")

        # (G) 시스템 찌꺼기 — 삭제 안전하므로 기본 체크(이동도 격리폴더라 복원 가능)
        jt = self.trees["junk"]
        all_junk = a.get("junk_files", [])
        junk = [f for f in all_junk if self._passes(f)]
        if junk:
            total = sum(f["size"] for f in all_junk)
            gid = self._add_group(
                jt, f"맥/윈도우 시스템 찌꺼기 {len(all_junk)}개 · 합계 {fmt_size(total)} (삭제 안전)")
            for idx, f in enumerate(junk):
                self._add_file(jt, gid, f, idx, checkable=True, checked=True)
        else:
            has_filter = bool(self.search_var.get().strip() or self._min_size())
            self._empty(jt, "🔎 검색/필터에 맞는 항목이 없습니다." if (has_filter and all_junk)
                        else "시스템 찌꺼기 파일이 없습니다.")

        # (H) 빈 폴더 — 기본 미체크(검토). 폴더째 격리 이동(복원 가능)
        et = self.trees["empty"]
        all_empty = a.get("empty_dirs", [])
        emp = [f for f in all_empty if self._passes(f)]
        if emp:
            gid = self._add_group(et, f"빈 폴더(또는 찌꺼기만 남은 폴더) {len(all_empty)}개")
            for idx, f in enumerate(emp):
                self._add_file(et, gid, f, idx, checkable=True, checked=False)
        else:
            self._empty(et, "빈 폴더가 없습니다.")

        self._populate_insights(a.get("insights"))

        self._update_counter()
        if fresh:
            self._select_best_tab(counts)

    def _fill(self, key, groups, label_fn, empty_msg, file_opts):
        """필터를 적용해 한 카테고리 트리를 채운다. 표시된 그룹 수 반환."""
        tv = self.trees[key]
        shown = 0
        for g in groups:
            files = [f for f in g["files"] if self._passes(f)]
            if not files:
                continue
            gid = self._add_group(tv, label_fn(g))
            for idx, f in enumerate(files):
                ck, chk, pre = file_opts(f)
                self._add_file(tv, gid, f, idx, checkable=ck, checked=chk, prefix=pre)
            shown += 1
        if shown == 0:
            has_filter = bool(self.search_var.get().strip() or self._min_size())
            self._empty(tv, "🔎 검색/필터에 맞는 항목이 없습니다." if (has_filter and groups) else empty_msg)
        return shown

    # ---------------- 검색 · 필터 ----------------
    def _min_size(self):
        return {"전체": 0, "1MB": 1 << 20, "10MB": 10 << 20,
                "100MB": 100 << 20, "1GB": 1 << 30}.get(self.size_var.get(), 0)

    def _passes(self, f):
        q = self.search_var.get().strip().lower()
        if q:
            p = f["path"].lower()
            if q not in os.path.basename(p) and q not in p:
                return False
        ms = self._min_size()
        if ms and f["size"] < ms:
            return False
        return True

    def _schedule_filter(self):
        if getattr(self, "_filter_after", None):
            try:
                self.after_cancel(self._filter_after)
            except Exception:
                pass
        self._filter_after = self.after(250, self._apply_filter)

    def _apply_filter(self):
        self._filter_after = None
        if self.analysis:
            self.populate_results(fresh=False)

    # ---------------- 탭 이동 / 정렬 ----------------
    def _goto_tab(self, key):
        try:
            self.notebook.select(self.cat_frames[key])
        except (tk.TclError, KeyError):
            pass  # 비활성(0건) 탭은 이동 불가

    def _select_best_tab(self, counts):
        # 찌꺼기·빈폴더는 보통 수가 많아 주 목적(중복)을 가리므로 자동선택에서 제외
        cand = [(k, counts.get(k, 0)) for k, _ in CATS if k not in ("junk", "empty")]
        cand.sort(key=lambda kv: kv[1], reverse=True)
        if cand and cand[0][1] > 0:
            try:
                self.notebook.select(self.cat_frames[cand[0][0]])
            except (tk.TclError, KeyError):
                pass

    def _sort_by(self, key, col):
        tv = self.trees[key]
        desc = not self.sort_state.get((key, col), False)
        self.sort_state[(key, col)] = desc

        def keyf(iid):
            m = self.item_meta.get(iid, {})
            if col == "size":
                return m.get("size", 0)
            if col == "date":
                return m.get("mtime", 0)
            if col == "folder":
                return os.path.dirname(m.get("path", "")).lower()
            return os.path.basename(m.get("path", "")).lower()

        for gid in tv.get_children(""):
            m = self.item_meta.get(gid)
            if not m or m.get("type") != "group":
                continue
            kids = list(tv.get_children(gid))
            kids.sort(key=keyf, reverse=desc)
            for i, iid in enumerate(kids):
                tv.move(iid, gid, i)

    # ---------------- 키보드 ----------------
    def _bind_keys(self):
        self.bind("<F5>", lambda e: self._scan_or_cancel())
        self.bind("<Escape>", lambda e: (self.cancel_event.set() if self.busy else None))
        self.bind("<Control-a>", self._kb_select_all)
        self.bind("<Control-A>", self._kb_select_all)

    def _current_key(self):
        try:
            return CATS[self.notebook.index(self.notebook.select())][0]
        except Exception:
            return None

    def _kb_select_all(self, event):
        if isinstance(self.focus_get(), (ttk.Entry, tk.Entry)):
            return  # 검색창에선 기본 전체선택
        key = self._current_key()
        if key:
            self.set_all(True, key)
        return "break"

    def _row_under_pointer(self, tv):
        y = tv.winfo_pointery() - tv.winfo_rooty()
        return tv.identify_row(y)

    def _kb_toggle(self, event):
        tv = event.widget
        m = self.item_meta.get(self._row_under_pointer(tv))
        if not m:
            return "break"
        if m["type"] == "file" and m["checkable"]:
            self._set_checked(tv, self._row_under_pointer(tv), not m["checked"])
            self._update_counter()
        elif m["type"] == "group":
            kids = [c for c in m["children"] if self.item_meta[c]["checkable"]]
            if kids:
                tgt = not all(self.item_meta[c]["checked"] for c in kids)
                for c in kids:
                    self._set_checked(tv, c, tgt)
                self._update_counter()
        return "break"

    def _kb_open(self, event):
        tv = event.widget
        m = self.item_meta.get(self._row_under_pointer(tv))
        if m and m.get("type") == "file":
            self._open_file(m["path"])
        return "break"

    def _empty(self, tv, msg):
        iid = self._new_iid()
        tv.insert("", "end", iid=iid, text="", values=(msg, "", "", ""), tags=("info",))
        self.item_meta[iid] = {"type": "info"}

    # ---------------- 체크박스 토글 ----------------
    def _set_checked(self, tv, iid, checked):
        meta = self.item_meta[iid]
        meta["checked"] = checked
        box = CHECK_ON if checked else CHECK_OFF
        tv.item(iid, text=box, tags=("checked",) if checked else (meta["zebra"],))

    # ---------------- 이미지 미리보기 ----------------
    def _make_preview_pane(self, parent):
        self.preview_on = tk.BooleanVar(value=bool(self.cfg.get("ui_preview", True)))
        wrap = ttk.Frame(parent)
        wrap.pack(side="bottom", fill="x", pady=(4, 0))
        bar = ttk.Frame(wrap)
        bar.pack(fill="x")
        cb = ttk.Checkbutton(bar, text="🖼 미리보기", variable=self.preview_on,
                             command=self._toggle_preview)
        cb.pack(side="left")
        self.preview_hint = ttk.Label(
            bar, text=("그룹/파일 줄을 클릭하면 사진을 나란히 비교합니다."
                       if HAVE_IMAGETK else "미리보기에는 Pillow 가 필요합니다(설치.bat)."),
            style="Hint.TLabel")
        self.preview_hint.pack(side="left", padx=10)
        # 썸네일이 놓일 가로 스트립
        self.preview_strip = ttk.Frame(wrap, height=190)
        if self.preview_on.get() and HAVE_IMAGETK:
            self.preview_strip.pack(fill="x", pady=(4, 0))

    def _toggle_preview(self):
        self.cfg["ui_preview"] = self.preview_on.get()
        self._save_cfg()
        if self.preview_on.get() and HAVE_IMAGETK:
            self.preview_strip.pack(fill="x", pady=(4, 0))
        else:
            self.preview_strip.pack_forget()

    def _clear_strip(self):
        for w in self.preview_strip.winfo_children():
            w.destroy()
        self._thumb_imgs = []

    def _show_preview(self, tv, iid):
        if not (HAVE_IMAGETK and self.preview_on.get()):
            return
        meta = self.item_meta.get(iid)
        if not meta:
            return
        # 그룹이면 그 그룹, 파일이면 부모 그룹의 파일들을 모은다
        if meta.get("type") == "group":
            kids = meta.get("children", [])
        elif meta.get("type") == "file":
            gid = tv.parent(iid)
            kids = self.item_meta.get(gid, {}).get("children", [iid])
        else:
            return
        items = []
        for c in kids:
            m = self.item_meta.get(c, {})
            p = m.get("path")
            if not p:
                continue
            ext = os.path.splitext(p)[1].lower()
            if (ext in fingerprint.IMAGE_EXTS or ext in fingerprint.RAW_EXTS
                    or ext in fingerprint.VIDEO_EXTS):
                role = "보관" if not m.get("checkable", True) else (
                    "정리대상" if m.get("checked") else "검토")
                items.append({"path": p, "size": m.get("size", 0),
                              "mtime": m.get("mtime", 0), "role": role})
            if len(items) >= 8:
                break
        self._clear_strip()
        if not items:
            ttk.Label(self.preview_strip,
                      text="이 그룹에는 미리볼 이미지/영상이 없습니다(문서/압축 등).",
                      style="Hint.TLabel").pack(anchor="w")
            return
        ttk.Label(self.preview_strip, text="불러오는 중…",
                  style="Hint.TLabel").pack(anchor="w")
        self._preview_seq += 1
        seq = self._preview_seq
        threading.Thread(target=self._thumb_worker, args=(seq, items), daemon=True).start()

    def _thumb_worker(self, seq, items, px=150):
        for it in items:
            if seq != self._preview_seq:
                return
            img = self._load_thumb(it["path"], it["mtime"], px)
            self.preview_q.put((seq, it, img))
        self.preview_q.put((seq, None, None))  # 끝 신호

    def _load_thumb(self, path, mtime, px):
        cached = self._thumb_cache.get(path)
        if cached and abs(cached[0] - float(mtime or 0)) < 1e-6:
            return cached[1]
        try:
            ext = os.path.splitext(path)[1].lower()
            if ext in fingerprint.RAW_EXTS:
                import rawpy
                with rawpy.imread(path) as raw:
                    thumb = raw.extract_thumb()
                import io
                im = Image.open(io.BytesIO(thumb.data)) if getattr(thumb, "format", None) \
                    else Image.fromarray(thumb.data)
            elif ext in fingerprint.VIDEO_EXTS:
                import io
                png = fingerprint.video_frame_image(path, px=max(px * 2, 320))
                if not png:
                    return None
                im = Image.open(io.BytesIO(png))
            else:
                im = Image.open(path)
            im = im.convert("RGB")
            orig = im.size
            im.thumbnail((px, px))
            im._orig_size = orig  # 캡션용
            self._thumb_cache[path] = (float(mtime or 0), im)
            if len(self._thumb_cache) > 200:
                self._thumb_cache.pop(next(iter(self._thumb_cache)))
            return im
        except Exception:
            return None

    def _drain_preview(self):
        while not self.preview_q.empty():
            seq, it, img = self.preview_q.get_nowait()
            if seq != self._preview_seq:
                continue
            if it is None:
                continue
            self._add_thumb_cell(it, img)

    def _add_thumb_cell(self, it, img):
        # 첫 셀이 들어오면 "불러오는 중" 라벨 제거
        kids = self.preview_strip.winfo_children()
        if kids and isinstance(kids[0], ttk.Label) and not getattr(kids[0], "_cell", False):
            kids[0].destroy()
        cell = ttk.Frame(self.preview_strip)
        cell._cell = True
        cell.pack(side="left", padx=4)
        if img is not None:
            photo = ImageTk.PhotoImage(img)
            self._thumb_imgs.append(photo)
            lbl = tk.Label(cell, image=photo, bg="#0f172a", bd=1, relief="solid")
            res = "×".join(map(str, getattr(img, "_orig_size", ("", ""))))
        else:
            lbl = tk.Label(cell, text="미리보기\n불가", width=18, height=8,
                           bg="#1f2937", fg="#9ca3af")
            res = ""
        lbl.pack()
        cap = f"{it['role']} · {fmt_size(it['size'])}" + (f" · {res}" if res else "")
        tk.Label(cell, text=cap, font=(FONT, 8), fg="#475569").pack()
        name = os.path.basename(it["path"])
        tk.Label(cell, text=(name[:20] + "…") if len(name) > 21 else name,
                 font=(FONT, 8), fg="#94a3b8").pack()

    def on_tree_click(self, event):
        tv = event.widget
        region = tv.identify_region(event.x, event.y)
        if region not in ("tree", "cell"):
            return
        iid = tv.identify_row(event.y)
        meta = self.item_meta.get(iid)
        if not meta:
            return
        col = tv.identify_column(event.x)  # '#0' = 체크(선택) 칸
        # 어떤 줄을 누르든 미리보기 갱신(이미지 그룹이면 나란히 표시)
        self._show_preview(tv, iid)
        if meta["type"] == "group":
            # 그룹의 펼침 삼각형 클릭은 기본 동작(열기/닫기)에 맡김
            if "indicator" in tv.identify_element(event.x, event.y):
                return
            # 그룹 줄은 어느 칸을 눌러도 그룹 전체 토글
            kids = [c for c in meta["children"] if self.item_meta[c]["checkable"]]
            if not kids:
                return
            target = not all(self.item_meta[c]["checked"] for c in kids)
            for c in kids:
                self._set_checked(tv, c, target)
            self._update_counter()
        elif meta["type"] == "file":
            if col == "#0":
                # 체크 칸 → 선택/해제
                if meta["checkable"]:
                    self._set_checked(tv, iid, not meta["checked"])
                    self._update_counter()
            else:
                # 그 외 영역(파일명·폴더·크기·수정일) → 파일 열기
                self._open_file(meta["path"])

    def on_tree_right(self, event):
        """우클릭 → 파일 열기 / 폴더에서 보기 메뉴."""
        tv = event.widget
        meta = self.item_meta.get(tv.identify_row(event.y))
        if not meta or meta.get("type") != "file":
            return
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="📄 파일 열기", command=lambda: self._open_file(meta["path"]))
        m.add_command(label="📂 폴더에서 보기", command=lambda: self._reveal(meta["path"]))
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _open_file(self, path):
        # 빠른 더블클릭으로 같은 파일이 두 번 열리는 것 방지
        import time
        now = time.monotonic()
        if path == getattr(self, "_last_open", None) and now - getattr(self, "_last_open_t", 0.0) < 0.6:
            return
        self._last_open, self._last_open_t = path, now
        if not os.path.exists(path):
            messagebox.showwarning("열기", f"파일을 찾을 수 없습니다(이동/삭제됨):\n{path}")
            return
        try:
            os.startfile(path)  # noqa: S606  (기본 프로그램으로 열기)
        except OSError as e:
            messagebox.showerror("열기 실패", f"{path}\n\n{e}")

    def _reveal(self, path):
        if not os.path.exists(path):
            messagebox.showwarning("폴더에서 보기", f"파일을 찾을 수 없습니다(이동/삭제됨):\n{path}")
            return
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        except OSError as e:
            messagebox.showerror("폴더 열기 실패", str(e))

    def set_all(self, value, key=None):
        """value=None 추천값 리셋, True/False 전체 설정. key 지정 시 해당 탭만."""
        if not self.analysis:
            return
        target_trees = [self.trees[key]] if key else list(self.trees.values())
        for iid, meta in self.item_meta.items():
            if meta.get("type") != "file" or not meta["checkable"]:
                continue
            if meta["tree"] not in target_trees:
                continue
            want = meta["rec"] if value is None else value
            self._set_checked(meta["tree"], iid, want)
        self._update_counter()

    def checked_paths(self):
        return [m["path"] for m in self.item_meta.values()
                if m.get("type") == "file" and m["checkable"] and m["checked"]]

    def _update_counter(self):
        n = 0
        total = 0
        for m in self.item_meta.values():
            if m.get("type") == "file" and m["checkable"] and m["checked"]:
                n += 1
                total += m["size"]
        self.count_lbl.configure(text=f"선택됨 {n}개 · 예상 회수 {fmt_size(total)}")

    # ---------------- 적용/되돌리기 ----------------
    def on_apply(self, real):
        paths = self.checked_paths()
        if not paths:
            messagebox.showinfo("알림", "선택된 파일이 없습니다. 항목을 클릭해 체크하세요.")
            return
        self._show_log()
        if real:
            if self.cfg.get("use_recycle_bin"):
                msg = (f"{len(paths)}개 파일을 Windows 휴지통으로 보냅니다.\n"
                       f"(복원은 휴지통에서 직접)\n\n실제로 진행할까요?")
            else:
                msg = (f"{len(paths)}개 파일을 격리폴더로 이동합니다.\n"
                       f"(영구삭제 아님, 되돌리기 가능)\n\n실제로 진행할까요?")
            if not messagebox.askyesno("확인", msg):
                return
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        dpath = config.DATA_DIR / "decisions.json"
        with open(dpath, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "move": paths}, f, ensure_ascii=False, indent=2)

        def job():
            applier.apply_decisions(dpath, self.cfg, do_apply=real)
            if real:
                self.result_q.put(("done", "이동 완료. 결과를 새로 보려면 다시 '스캔 및 분석'을 실행하세요."))

        self._run_thread(job)

    def on_undo(self):
        logs = applier_logs()
        if not logs:
            messagebox.showinfo("되돌리기", "되돌릴 기록이 없습니다.")
            return
        win = tk.Toplevel(self)
        win.title("되돌리기 - 작업 선택")
        win.geometry("440x320")
        ttk.Label(win, text="복원할 작업을 선택하세요:", padding=8,
                  font=(FONT, 10, "bold")).pack(anchor="w")
        lb = tk.Listbox(win, font=(FONT, 10))
        for ts in logs:
            lb.insert("end", ts)
        lb.pack(fill="both", expand=True, padx=8)

        def do():
            sel = lb.curselection()
            if not sel:
                return
            ts = logs[sel[0]]
            win.destroy()
            self._show_log()
            self._run_thread(lambda: applier.undo(ts))

        ttk.Button(win, text="복원", style="Big.TButton", command=do).pack(pady=8)


def applier_logs():
    import glob
    import re
    out = []
    for p in sorted(glob.glob(str(config.DATA_DIR / "undo_*.json")), reverse=True):
        m = re.search(r"undo_(.+)\.json", os.path.basename(p))
        if m:
            out.append(m.group(1))
    return out


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
