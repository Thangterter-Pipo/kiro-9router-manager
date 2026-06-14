from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from types import SimpleNamespace

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import kiro_9router_app as app
from scripts.kiro_account_store import Account, AccountStore

try:
    from scripts import kiro_ide_login
except Exception:  # pragma: no cover - optional module
    kiro_ide_login = None

try:
    from scripts import kiro_json_login
except Exception:  # pragma: no cover - optional module
    kiro_json_login = None

try:
    from scripts import kiro_device_login
except Exception:  # pragma: no cover - optional module
    kiro_device_login = None


APP_TITLE = "Kiro → 9router Manager"

# ---- Dark theme palette --------------------------------------------------
BG = "#0f172a"          # slate-900 page background
SURFACE = "#1e293b"     # slate-800 panels
SURFACE2 = "#273449"    # raised controls
BORDER = "#334155"      # slate-700
TEXT = "#e2e8f0"        # slate-200
MUTED = "#94a3b8"       # slate-400
ACCENT = "#38bdf8"      # sky-400
ACCENT_DK = "#0ea5e9"   # sky-500
OK = "#4ade80"          # green-400
ERR = "#f87171"         # red-400
WARN = "#fbbf24"        # amber-400
FONT = "Segoe UI"
MONO = "Consolas"


# ===== Friendly Vietnamese step mapping ==================================
_STEP_TABLE = [
    ("registering oidc client", "🔑 Đang đăng ký ứng dụng với AWS..."),
    ("opening login page", "🌐 Đang mở trang đăng nhập..."),
    ("username filled", "👤 Đã điền tài khoản..."),
    ("password filled", "🔒 Đã điền mật khẩu..."),
    ("mfa setup detected", "📱 Đang đăng ký thiết bị MFA mới..."),
    ("secret captured", "💾 Đã lưu mã bí mật MFA (an toàn)..."),
    ("existing-mfa code typed", "🔢 Đang nhập mã MFA..."),
    ("existing-mfa code accepted", "✅ AWS đã chấp nhận mã MFA!"),
    ("totp entered", "🔢 Đang nhập mã MFA..."),
    ("new password saved", "💾 Đã lưu mật khẩu mới (an toàn)..."),
    ("new password set form filled", "🔑 Đang đổi mật khẩu lần đầu..."),
    ("nav -> http://127.0.0.1", "↪️ AWS đã trả mã uỷ quyền về..."),
    ("exchanging token", "🎫 Đang đổi lấy token..."),
]


def friendly_step(line: str) -> str | None:
    low = line.lower()
    for key, friendly in _STEP_TABLE:
        if key in low:
            return friendly
    return None


def run_accounts(accounts: list[Account], args: SimpleNamespace, *,
                 log_cb, store: AccountStore | None = None) -> dict:
    """Run the .mjs importer for a batch of Account objects and persist results.

    Returns the raw runner result dict. Updates the store per-account if given.
    """
    node_modules = app.ensure_playwright_core(node_modules=None, install=True)
    runner_accounts = [
        app.AccountLine(
            name=a.name, password=a.password, start_url=a.start_url,
            region=a.region or "us-east-1",
            new_password=a.new_password or args.new_password,
            mfa_secret=a.mfa_secret or "",
        )
        for a in accounts
    ]
    result = app.run_import(args, runner_accounts, node_modules, log_callback=log_cb)
    if store is not None and isinstance(result, dict):
        by_name = {a.name: a for a in accounts}
        for item in result.get("results", []):
            acc = by_name.get(item.get("name"))
            if not acc:
                continue
            store.record_result(
                acc.id,
                ok=bool(item.get("ok")),
                error=str(item.get("error", "")),
                connection_id=str(item.get("connectionId", "") or ""),
            )
    return result


class ManagerGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1100x760")
        self.minsize(960, 660)
        self.configure(bg=BG)
        self.store = AccountStore()
        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self._last_result: dict | None = None
        self._build_style()
        self._build_ui()
        self._refresh_table()
        self.after(100, self._drain_log_queue)

    # ---- theme -------------------------------------------------------
    def _build_style(self) -> None:
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=TEXT, fieldbackground=SURFACE2,
                     bordercolor=BORDER, font=(FONT, 10))
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=SURFACE)
        st.configure("TLabel", background=BG, foreground=TEXT)
        st.configure("Card.TLabel", background=SURFACE, foreground=TEXT)
        st.configure("Muted.TLabel", background=BG, foreground=MUTED)
        st.configure("Title.TLabel", background=BG, foreground=TEXT, font=(FONT, 18, "bold"))
        st.configure("Step.TLabel", background=BG, foreground=ACCENT, font=(FONT, 11))
        st.configure("TButton", background=SURFACE2, foreground=TEXT,
                     bordercolor=BORDER, padding=(12, 7), relief="flat")
        st.map("TButton", background=[("active", BORDER), ("disabled", SURFACE)],
               foreground=[("disabled", MUTED)])
        st.configure("Primary.TButton", background=ACCENT_DK, foreground="#001018",
                     font=(FONT, 10, "bold"), padding=(16, 8))
        st.map("Primary.TButton", background=[("active", ACCENT), ("disabled", BORDER)])
        st.configure("Danger.TButton", background="#7f1d1d", foreground="#fee2e2")
        st.map("Danger.TButton", background=[("active", "#991b1b")])
        st.configure("TEntry", fieldbackground=SURFACE2, foreground=TEXT,
                     insertcolor=TEXT, bordercolor=BORDER)
        st.configure("TCheckbutton", background=SURFACE, foreground=TEXT)
        st.map("TCheckbutton", background=[("active", SURFACE)])
        st.configure("TNotebook", background=BG, bordercolor=BORDER, tabmargins=(2, 6, 2, 0))
        st.configure("TNotebook.Tab", background=SURFACE, foreground=MUTED,
                     padding=(18, 9), font=(FONT, 10, "bold"))
        st.map("TNotebook.Tab", background=[("selected", SURFACE2)],
               foreground=[("selected", ACCENT)])
        st.configure("Treeview", background=SURFACE, fieldbackground=SURFACE,
                     foreground=TEXT, bordercolor=BORDER, rowheight=28)
        st.configure("Treeview.Heading", background=SURFACE2, foreground=TEXT,
                     font=(FONT, 9, "bold"), relief="flat")
        st.map("Treeview", background=[("selected", ACCENT_DK)],
               foreground=[("selected", "#001018")])
        # Dark trough for progressbar + scrollbars (clam shows light by default).
        st.configure("Horizontal.TProgressbar", troughcolor=SURFACE2,
                     background=ACCENT, bordercolor=BORDER, lightcolor=ACCENT, darkcolor=ACCENT)
        st.configure("Vertical.TScrollbar", background=SURFACE2, troughcolor=BG,
                     bordercolor=BORDER, arrowcolor=MUTED)
        st.configure("Horizontal.TScrollbar", background=SURFACE2, troughcolor=BG,
                     bordercolor=BORDER, arrowcolor=MUTED)
        st.configure("TLabelframe", background=SURFACE, foreground=ACCENT,
                     bordercolor=BORDER)
        st.configure("TLabelframe.Label", background=SURFACE, foreground=ACCENT,
                     font=(FONT, 10, "bold"))

    # ---- layout ------------------------------------------------------
    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=14)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        header = ttk.Frame(root)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Kiro → 9router Manager", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.status_var = tk.StringVar(value="● Sẵn sàng")
        self.status_lbl = ttk.Label(header, textvariable=self.status_var, foreground=OK,
                                    background=BG, font=(FONT, 10, "bold"))
        self.status_lbl.grid(row=0, column=1, sticky="e")
        self.step_var = tk.StringVar(value="Chưa chạy")
        ttk.Label(header, textvariable=self.step_var, style="Step.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self.progress = ttk.Progressbar(root, mode="determinate", maximum=100)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(10, 8))

        nb = ttk.Notebook(root)
        nb.grid(row=2, column=0, sticky="nsew")
        self.tab_accounts = ttk.Frame(nb, style="Card.TFrame", padding=12)
        self.tab_add = ttk.Frame(nb, style="Card.TFrame", padding=12)
        self.tab_json = ttk.Frame(nb, style="Card.TFrame", padding=12)
        self.tab_device = ttk.Frame(nb, style="Card.TFrame", padding=12)
        self.tab_settings = ttk.Frame(nb, style="Card.TFrame", padding=12)
        nb.add(self.tab_accounts, text="  Tài khoản  ")
        nb.add(self.tab_add, text="  Thêm / Nhập  ")
        nb.add(self.tab_json, text="  Đăng nhập JSON  ")
        nb.add(self.tab_device, text="  Builder ID / SSO  ")
        nb.add(self.tab_settings, text="  Cài đặt  ")
        self._build_accounts_tab(self.tab_accounts)
        self._build_add_tab(self.tab_add)
        self._build_json_tab(self.tab_json)
        self._build_device_tab(self.tab_device)
        self._build_settings_tab(self.tab_settings)

        log_frame = ttk.LabelFrame(root, text="Nhật ký", padding=8)
        log_frame.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        root.rowconfigure(3, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=9, wrap="word", state="disabled",
                                bg="#0b1220", fg=TEXT, insertbackground=TEXT,
                                font=(MONO, 9), relief="flat", borderwidth=0)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.tag_configure("ok", foreground=OK)
        self.log_text.tag_configure("err", foreground=ERR)
        self.log_text.tag_configure("warn", foreground=WARN)
        self.log_text.tag_configure("info", foreground=ACCENT)

    # ---- tab: accounts ----------------------------------------------
    def _build_accounts_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        bar = ttk.Frame(parent, style="Card.TFrame")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.btn_login = ttk.Button(bar, text="▶  Đăng nhập (đã chọn)", style="Primary.TButton",
                                    command=lambda: self._start_selected(relogin=False))
        self.btn_login.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_relogin = ttk.Button(bar, text="↻  Đăng nhập lại", command=lambda: self._start_selected(relogin=True))
        self.btn_relogin.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_all = ttk.Button(bar, text="⇊  Đăng nhập tất cả", command=self._start_all)
        self.btn_all.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(bar, text="🖥  Vào IDE Kiro", command=self._login_ide).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(bar, text="✎  Sửa", command=self._edit_selected).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(bar, text="🗑  Xóa", style="Danger.TButton", command=self._delete_selected).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(bar, text="⟳", width=3, command=self._refresh_table).pack(side=tk.RIGHT)

        wrap = ttk.Frame(parent, style="Card.TFrame")
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        cols = ("name", "start_url", "mfa", "status", "last_login")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="extended")
        headings = {"name": "Tài khoản", "start_url": "Start URL", "mfa": "MFA",
                    "status": "Trạng thái", "last_login": "Đăng nhập lần cuối"}
        widths = {"name": 220, "start_url": 320, "mfa": 60, "status": 110, "last_login": 180}
        for c in cols:
            self.tree.heading(c, text=headings[c])
            self.tree.column(c, width=widths[c], anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        tsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        tsb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.tag_configure("ok", foreground=OK)
        self.tree.tag_configure("err", foreground=ERR)
        self.tree.tag_configure("none", foreground=MUTED)
        self.tree.bind("<Double-1>", lambda e: self._edit_selected())

        self.count_var = tk.StringVar(value="0 tài khoản")
        ttk.Label(parent, textvariable=self.count_var, style="Card.TLabel",
                  foreground=MUTED).grid(row=2, column=0, sticky="w", pady=(8, 0))

    # ---- tab: add / bulk import -------------------------------------
    def _build_add_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(0, weight=1)

        # left: single add form
        form = ttk.LabelFrame(parent, text="Thêm một tài khoản", padding=12)
        form.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        form.columnconfigure(1, weight=1)
        self.f_name = tk.StringVar()
        self.f_pass = tk.StringVar()
        self.f_url = tk.StringVar()
        self.f_mfa = tk.StringVar()
        self.f_newpass = tk.StringVar()
        self.f_note = tk.StringVar()
        rows = [
            ("Tài khoản (email)", self.f_name, None),
            ("Mật khẩu", self.f_pass, "*"),
            ("Start URL", self.f_url, None),
            ("MFA secret (tuỳ chọn)", self.f_mfa, None),
            ("Mật khẩu mới (tuỳ chọn)", self.f_newpass, "*"),
            ("Ghi chú", self.f_note, None),
        ]
        for i, (label, var, show) in enumerate(rows):
            ttk.Label(form, text=label, style="Card.TLabel").grid(row=i, column=0, sticky="w", pady=5, padx=(0, 10))
            ttk.Entry(form, textvariable=var, show=show).grid(row=i, column=1, sticky="ew", pady=5)
        ttk.Button(form, text="＋  Lưu tài khoản", style="Primary.TButton",
                   command=self._add_single).grid(row=len(rows), column=0, columnspan=2, sticky="ew", pady=(12, 0))

        # right: bulk paste
        bulk = ttk.LabelFrame(parent, text="Nhập hàng loạt (mỗi dòng: mail|pass|startUrl|mfaSecret)", padding=12)
        bulk.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        bulk.columnconfigure(0, weight=1)
        bulk.rowconfigure(0, weight=1)
        self.bulk_text = tk.Text(bulk, height=12, wrap="none", bg="#0b1220", fg=TEXT,
                                 insertbackground=TEXT, font=(MONO, 10), relief="flat", borderwidth=0)
        self.bulk_text.grid(row=0, column=0, sticky="nsew", columnspan=2)
        bsb = ttk.Scrollbar(bulk, orient="vertical", command=self.bulk_text.yview)
        bsb.grid(row=0, column=2, sticky="ns")
        self.bulk_text.configure(yscrollcommand=bsb.set)
        ttk.Button(bulk, text="📂  Nạp từ .txt", command=self._load_bulk_file).grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Button(bulk, text="⇪  Nhập vào danh sách", style="Primary.TButton",
                   command=self._import_bulk).grid(row=1, column=1, sticky="e", pady=(10, 0))

    # ---- tab: JSON login --------------------------------------------
    def _build_json_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        ttk.Label(parent, text="Dán token Kiro dạng JSON (1 object, mảng nhiều acc, hoặc file kiro-auth-token.json)",
                  style="Card.TLabel", foreground=MUTED).grid(row=0, column=0, sticky="w", pady=(0, 8))

        box = ttk.Frame(parent, style="Card.TFrame")
        box.grid(row=1, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.json_text = tk.Text(box, height=12, wrap="none", bg="#0b1220", fg=TEXT,
                                 insertbackground=TEXT, font=(MONO, 10), relief="flat", borderwidth=0)
        self.json_text.grid(row=0, column=0, sticky="nsew")
        jsb = ttk.Scrollbar(box, orient="vertical", command=self.json_text.yview)
        jsb.grid(row=0, column=1, sticky="ns")
        self.json_text.configure(yscrollcommand=jsb.set)

        opts = ttk.Frame(parent, style="Card.TFrame")
        opts.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.json_target_9router = tk.BooleanVar(value=True)
        self.json_target_ide = tk.BooleanVar(value=False)
        self.json_refresh = tk.BooleanVar(value=True)
        self.json_save_store = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Nạp vào 9router", variable=self.json_target_9router).pack(side=tk.LEFT, padx=(0, 14))
        ttk.Checkbutton(opts, text="Nạp vào IDE Kiro", variable=self.json_target_ide).pack(side=tk.LEFT, padx=(0, 14))
        ttk.Checkbutton(opts, text="Làm mới token", variable=self.json_refresh).pack(side=tk.LEFT, padx=(0, 14))
        ttk.Checkbutton(opts, text="Lưu vào danh sách", variable=self.json_save_store).pack(side=tk.LEFT, padx=(0, 14))

        actions = ttk.Frame(parent, style="Card.TFrame")
        actions.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        actions.columnconfigure(2, weight=1)
        ttk.Button(actions, text="📂  Nạp từ file .json", command=self._load_json_file).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(actions, text="🔍  Kiểm tra", command=self._validate_json).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(actions, text="▶  Đăng nhập từ JSON", style="Primary.TButton",
                   command=self._run_json_login).grid(row=0, column=3, sticky="e")

    def _load_json_file(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if path:
            self.json_text.delete("1.0", tk.END)
            self.json_text.insert("1.0", Path(path).read_text(encoding="utf-8-sig"))

    def _validate_json(self) -> None:
        if kiro_json_login is None:
            messagebox.showerror(APP_TITLE, "Thiếu module kiro_json_login.")
            return
        try:
            entries = kiro_json_login.parse_json_text(self.json_text.get("1.0", tk.END))
            norms = [kiro_json_login._normalize(e, i) for i, e in enumerate(entries, start=1)]
            names = ", ".join(n["name"] for n in norms)
            self._log(f"JSON hợp lệ: {len(norms)} tài khoản ({names})", "ok")
            messagebox.showinfo(APP_TITLE, f"Hợp lệ: {len(norms)} tài khoản.\n{names}")
        except Exception as exc:
            self._log(f"JSON lỗi: {exc}", "err")
            messagebox.showerror(APP_TITLE, str(exc))

    def _run_json_login(self) -> None:
        if kiro_json_login is None:
            messagebox.showerror(APP_TITLE, "Thiếu module kiro_json_login.")
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_TITLE, "Đang chạy, vui lòng đợi.")
            return
        text = self.json_text.get("1.0", tk.END)
        targets = []
        if self.json_target_9router.get():
            targets.append("9router")
        if self.json_target_ide.get():
            targets.append("ide")
        if not targets:
            messagebox.showinfo(APP_TITLE, "Chọn ít nhất một đích (9router hoặc IDE Kiro).")
            return
        try:
            entries = kiro_json_login.parse_json_text(text)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self._set_running(True)
        self.progress.configure(maximum=len(entries), value=0)
        self._log(f"Đăng nhập JSON: {len(entries)} tài khoản → {', '.join(targets)}", "info")
        self.worker = threading.Thread(
            target=self._json_worker,
            args=(text, tuple(targets), self.json_refresh.get(),
                  Path(self.db_var.get().strip()), self.json_save_store.get()),
            daemon=True)
        self.worker.start()

    def _json_worker(self, text, targets, refresh, db_path, save_store) -> None:
        try:
            result = kiro_json_login.login_from_json(
                text, targets=targets, refresh=refresh, db_path=db_path,
                verify="9router" in targets, base_url=self.base_url_var.get().strip())
            self._last_result = result
            # Save successful accounts to the store for later relogin.
            if save_store:
                for item in result.get("results", []):
                    if not item.get("ok"):
                        continue
                    try:
                        entries = kiro_json_login.parse_json_text(text)
                        norm = kiro_json_login._normalize(entries[item["index"] - 1], item["index"])
                        existing = self.store.get_by_name(norm["name"])
                        acc = existing or Account(name=norm["name"], password="",
                                                  start_url=norm["start_url"], region=norm["region"],
                                                  mfa_secret="")
                        if not existing:
                            self.store.add(acc)
                        self.store.record_result(acc.id, ok=True,
                                                 connection_id=str(item.get("connectionId", "") or ""))
                    except Exception:
                        pass
            self.log_queue.put(("result", json.dumps(result, ensure_ascii=False, indent=2)))
            self.log_queue.put(("done", "Done" if result.get("ok") else "Finished with errors"))
        except Exception as exc:
            self.log_queue.put(("log", f"ERROR: {exc}"))
            self.log_queue.put(("done", "Error"))

    # ---- tab: Builder ID / SSO device-flow --------------------------
    def _build_device_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)

        ttk.Label(parent, text="Đăng nhập bằng AWS Builder ID hoặc IAM Identity Center (không cần mật khẩu / MFA)",
                  style="Card.TLabel", foreground=ACCENT, font=(FONT, 11, "bold")).grid(
                      row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(parent, text="Bấm nút bên dưới → mở link hiện ra → bấm Allow trên trình duyệt. "
                  "Tool tự lấy token thật (refresh được lâu dài) và nạp vào 9router / IDE Kiro.",
                  style="Card.TLabel", foreground=MUTED, wraplength=640, justify="left").grid(
                      row=1, column=0, sticky="w", pady=(0, 12))

        form = ttk.Frame(parent, style="Card.TFrame")
        form.grid(row=2, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        self.device_mode = tk.StringVar(value="builderId")
        ttk.Label(form, text="Loại đăng nhập:", style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=4)
        modes = ttk.Frame(form, style="Card.TFrame")
        modes.grid(row=0, column=1, sticky="w", pady=4)
        ttk.Radiobutton(modes, text="AWS Builder ID (cá nhân)", variable=self.device_mode,
                        value="builderId", command=self._on_device_mode).pack(side=tk.LEFT, padx=(0, 16))
        ttk.Radiobutton(modes, text="IAM Identity Center (doanh nghiệp)", variable=self.device_mode,
                        value="idc", command=self._on_device_mode).pack(side=tk.LEFT)

        ttk.Label(form, text="Start URL:", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=4)
        self.device_start_url = tk.StringVar(value=kiro_device_login.BUILDER_ID_START_URL
                                             if kiro_device_login else "https://view.awsapps.com/start")
        self.device_start_entry = ttk.Entry(form, textvariable=self.device_start_url)
        self.device_start_entry.grid(row=1, column=1, sticky="ew", pady=4)

        ttk.Label(form, text="Tên hiển thị:", style="Card.TLabel").grid(row=2, column=0, sticky="w", pady=4)
        self.device_name = tk.StringVar(value="")
        ttk.Entry(form, textvariable=self.device_name).grid(row=2, column=1, sticky="ew", pady=4)

        opts = ttk.Frame(parent, style="Card.TFrame")
        opts.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.device_target_9router = tk.BooleanVar(value=True)
        self.device_target_ide = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Nạp vào 9router", variable=self.device_target_9router).pack(side=tk.LEFT, padx=(0, 14))
        ttk.Checkbutton(opts, text="Nạp vào IDE Kiro", variable=self.device_target_ide).pack(side=tk.LEFT, padx=(0, 14))

        # vùng hiển thị link + code
        self.device_prompt = ttk.LabelFrame(parent, text="Hướng dẫn đăng nhập", padding=10)
        self.device_prompt.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        self.device_prompt.columnconfigure(0, weight=1)
        self.device_code_var = tk.StringVar(value="Chưa bắt đầu.")
        self.device_link_var = tk.StringVar(value="")
        ttk.Label(self.device_prompt, textvariable=self.device_code_var, style="Card.TLabel",
                  foreground=TEXT, font=(MONO, 12, "bold")).grid(row=0, column=0, sticky="w")
        self.device_link_entry = ttk.Entry(self.device_prompt, textvariable=self.device_link_var)
        self.device_link_entry.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        btns = ttk.Frame(self.device_prompt, style="Card.TFrame")
        btns.grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Button(btns, text="🌐  Mở link", command=self._open_device_link).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btns, text="📋  Copy link", command=self._copy_device_link).pack(side=tk.LEFT)

        actions = ttk.Frame(parent, style="Card.TFrame")
        actions.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        actions.columnconfigure(1, weight=1)
        self.device_btn = ttk.Button(actions, text="▶  Bắt đầu đăng nhập Builder ID",
                                     style="Primary.TButton", command=self._run_device_login)
        self.device_btn.grid(row=0, column=0, sticky="w")
        self.device_cancel_flag = False

    def _on_device_mode(self) -> None:
        if self.device_mode.get() == "builderId":
            self.device_start_url.set(kiro_device_login.BUILDER_ID_START_URL
                                      if kiro_device_login else "https://view.awsapps.com/start")
            self.device_start_entry.configure(state="readonly")
            self.device_btn.configure(text="▶  Bắt đầu đăng nhập Builder ID")
        else:
            if self.device_start_url.get() == (kiro_device_login.BUILDER_ID_START_URL
                                               if kiro_device_login else "https://view.awsapps.com/start"):
                self.device_start_url.set("https://d-xxxxxxxxxx.awsapps.com/start")
            self.device_start_entry.configure(state="normal")
            self.device_btn.configure(text="▶  Bắt đầu đăng nhập IdC")

    def _open_device_link(self) -> None:
        url = self.device_link_var.get().strip()
        if url:
            import webbrowser
            webbrowser.open(url)

    def _copy_device_link(self) -> None:
        url = self.device_link_var.get().strip()
        if url:
            self.clipboard_clear()
            self.clipboard_append(url)
            self._log("Đã copy link đăng nhập vào clipboard.", "info")

    def _run_device_login(self) -> None:
        if kiro_device_login is None:
            messagebox.showerror(APP_TITLE, "Thiếu module kiro_device_login.")
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_TITLE, "Đang chạy, vui lòng đợi.")
            return
        targets = []
        if self.device_target_9router.get():
            targets.append("9router")
        if self.device_target_ide.get():
            targets.append("ide")
        if not targets:
            messagebox.showinfo(APP_TITLE, "Chọn ít nhất một đích (9router hoặc IDE Kiro).")
            return
        start_url = self.device_start_url.get().strip()
        if not start_url or "xxxxxxxxxx" in start_url:
            messagebox.showinfo(APP_TITLE, "Nhập Start URL hợp lệ (với IdC: https://<dir>.awsapps.com/start).")
            return
        self.device_cancel_flag = False
        self._set_running(True)
        self.device_btn.configure(state="disabled")
        self.device_code_var.set("Đang khởi tạo phiên đăng nhập...")
        self.progress.configure(maximum=100, value=0)
        self._log("Bắt đầu device-flow login...", "info")
        self.worker = threading.Thread(
            target=self._device_worker,
            args=(start_url, tuple(targets), self.device_name.get().strip(),
                  Path(self.db_var.get().strip())),
            daemon=True)
        self.worker.start()

    def _device_worker(self, start_url, targets, name, db_path) -> None:
        try:
            def on_prompt(session: dict) -> None:
                self.log_queue.put(("device_prompt", json.dumps({
                    "user_code": session["user_code"],
                    "verification_uri": session["verification_uri"]})))
                self.log_queue.put(("log", f"🌐 Mở link và bấm Allow — mã: {session['user_code']}"))

            def on_tick(_wait: int) -> None:
                self.log_queue.put(("log", "⏳ Đang chờ bạn phê duyệt trên trình duyệt..."))

            result = kiro_device_login.login_device_flow(
                start_url=start_url, name=name, targets=targets, open_browser=True,
                db_path=db_path, on_prompt=on_prompt, on_tick=on_tick,
                should_cancel=lambda: self.device_cancel_flag,
                verify="9router" in targets)
            self._last_result = result

            # lưu vào store để relogin sau
            try:
                acc = self.store.get_by_name(result.get("name", "")) or Account(
                    name=result.get("name", "device-acc"), password="",
                    start_url=start_url, mfa_secret="")
                if not self.store.get_by_name(acc.name):
                    self.store.add(acc)
                self.store.record_result(acc.id, ok=True,
                                         connection_id=str(result.get("connectionId", "") or ""))
            except Exception:
                pass

            self.log_queue.put(("result", json.dumps(result, ensure_ascii=False, indent=2)))
            self.log_queue.put(("done", "Done" if result.get("ok") else "Finished with errors"))
        except Exception as exc:
            self.log_queue.put(("log", f"ERROR: {exc}"))
            self.log_queue.put(("done", "Error"))

    # ---- tab: settings ----------------------------------------------
    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)
        self.new_password_var = tk.StringVar(value=app.DEFAULT_NEW_PASSWORD)
        self.base_url_var = tk.StringVar(value=app.DEFAULT_BASE_URL)
        self.redirect_var = tk.StringVar(value=app.DEFAULT_REDIRECT_URI)
        self.timeout_var = tk.StringVar(value="15")
        self.db_var = tk.StringVar(value=str(app._default_db()))
        self.chrome_var = tk.StringVar(value=app._default_chrome())
        self.notify_tg_var = tk.BooleanVar(value=True)

        fields = [
            ("Mật khẩu mới mặc định", self.new_password_var, "*", None),
            ("Địa chỉ 9router", self.base_url_var, None, None),
            ("Redirect URI", self.redirect_var, None, None),
            ("Timeout mỗi tài khoản (phút)", self.timeout_var, None, None),
            ("9router DB", self.db_var, None, self._pick_db),
            ("Chrome / Edge", self.chrome_var, None, self._pick_chrome),
        ]
        for i, (label, var, show, browse) in enumerate(fields):
            ttk.Label(parent, text=label, style="Card.TLabel").grid(row=i, column=0, sticky="w", pady=7, padx=(0, 12))
            cell = ttk.Frame(parent, style="Card.TFrame")
            cell.grid(row=i, column=1, sticky="ew", pady=7)
            cell.columnconfigure(0, weight=1)
            ttk.Entry(cell, textvariable=var, show=show).grid(row=0, column=0, sticky="ew")
            if browse:
                ttk.Button(cell, text="...", width=4, command=browse).grid(row=0, column=1, padx=(6, 0))
        ttk.Checkbutton(parent, text="Báo Telegram khi xong", variable=self.notify_tg_var).grid(
            row=len(fields), column=0, columnspan=2, sticky="w", pady=(12, 0))
        ttk.Label(parent, text="Tài khoản lưu tại: " + str(self.store.path), style="Card.TLabel",
                  foreground=MUTED).grid(row=len(fields) + 1, column=0, columnspan=2, sticky="w", pady=(16, 0))

    # ---- table helpers ----------------------------------------------
    def _refresh_table(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        accounts = self.store.all()
        for a in accounts:
            mfa = "✓" if a.mfa_secret else "—"
            status = {"ok": "● OK", "error": "✕ Lỗi"}.get(a.last_status, "○ Chưa")
            tag = {"ok": "ok", "error": "err"}.get(a.last_status, "none")
            last = (a.last_login_at or "")[:19].replace("T", " ")
            self.tree.insert("", tk.END, iid=a.id,
                             values=(a.name, a.start_url, mfa, status, last), tags=(tag,))
        self.count_var.set(f"{len(accounts)} tài khoản")

    def _selected_ids(self) -> list[str]:
        return list(self.tree.selection())

    def _selected_accounts(self) -> list[Account]:
        return self.store.select(self._selected_ids())

    # ---- CRUD handlers ----------------------------------------------
    def _add_single(self) -> None:
        name = self.f_name.get().strip()
        password = self.f_pass.get().strip()
        url = self.f_url.get().strip()
        if not name or not password or not url.startswith("https://"):
            messagebox.showerror(APP_TITLE, "Cần email, mật khẩu và Start URL (https://...)")
            return
        self.store.add(Account(
            name=name, password=password, start_url=url,
            mfa_secret=self.f_mfa.get().strip(), new_password=self.f_newpass.get().strip(),
            note=self.f_note.get().strip(),
        ))
        for v in (self.f_name, self.f_pass, self.f_url, self.f_mfa, self.f_newpass, self.f_note):
            v.set("")
        self._refresh_table()
        self._log(f"Đã lưu tài khoản {name}", "ok")

    def _import_bulk(self) -> None:
        raw = self.bulk_text.get("1.0", tk.END).splitlines()
        added, errors = 0, 0
        for line in raw:
            if not line.strip():
                continue
            try:
                if self.store.upsert_from_line(line):
                    added += 1
            except Exception:
                errors += 1
        self.bulk_text.delete("1.0", tk.END)
        self._refresh_table()
        self._log(f"Nhập hàng loạt: {added} tài khoản" + (f", {errors} dòng lỗi" if errors else ""),
                  "warn" if errors else "ok")

    def _load_bulk_file(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if path:
            self.bulk_text.delete("1.0", tk.END)
            self.bulk_text.insert("1.0", Path(path).read_text(encoding="utf-8-sig"))

    def _delete_selected(self) -> None:
        accounts = self._selected_accounts()
        if not accounts:
            return
        if not messagebox.askyesno(APP_TITLE, f"Xóa {len(accounts)} tài khoản khỏi danh sách?"):
            return
        for a in accounts:
            self.store.delete(a.id)
        self._refresh_table()
        self._log(f"Đã xóa {len(accounts)} tài khoản", "warn")

    def _edit_selected(self) -> None:
        accounts = self._selected_accounts()
        if not accounts:
            return
        self._open_edit_dialog(accounts[0])

    def _pick_db(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("SQLite", "*.sqlite;*.db"), ("All", "*.*")])
        if path:
            self.db_var.set(path)

    def _pick_chrome(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Executable", "*.exe"), ("All", "*.*")])
        if path:
            self.chrome_var.set(path)

    # ---- edit dialog ------------------------------------------------
    def _open_edit_dialog(self, account: Account) -> None:
        dlg = tk.Toplevel(self)
        dlg.title(f"Sửa: {account.name}")
        dlg.configure(bg=SURFACE)
        dlg.geometry("520x360")
        dlg.transient(self)
        dlg.grab_set()
        frm = ttk.Frame(dlg, style="Card.TFrame", padding=16)
        frm.pack(fill=tk.BOTH, expand=True)
        frm.columnconfigure(1, weight=1)
        v_name = tk.StringVar(value=account.name)
        v_pass = tk.StringVar(value=account.password)
        v_url = tk.StringVar(value=account.start_url)
        v_mfa = tk.StringVar(value=account.mfa_secret)
        v_new = tk.StringVar(value=account.new_password)
        v_note = tk.StringVar(value=account.note)
        rows = [
            ("Tài khoản", v_name, None), ("Mật khẩu", v_pass, "*"),
            ("Start URL", v_url, None), ("MFA secret", v_mfa, None),
            ("Mật khẩu mới", v_new, "*"), ("Ghi chú", v_note, None),
        ]
        for i, (label, var, show) in enumerate(rows):
            ttk.Label(frm, text=label, style="Card.TLabel").grid(row=i, column=0, sticky="w", pady=5, padx=(0, 10))
            ttk.Entry(frm, textvariable=var, show=show).grid(row=i, column=1, sticky="ew", pady=5)

        def save() -> None:
            self.store.update(account.id, name=v_name.get().strip(), password=v_pass.get().strip(),
                              start_url=v_url.get().strip(), mfa_secret=v_mfa.get().strip(),
                              new_password=v_new.get().strip(), note=v_note.get().strip())
            self._refresh_table()
            self._log(f"Đã cập nhật {v_name.get().strip()}", "ok")
            dlg.destroy()

        btns = ttk.Frame(frm, style="Card.TFrame")
        btns.grid(row=len(rows), column=0, columnspan=2, sticky="ew", pady=(14, 0))
        ttk.Button(btns, text="Lưu", style="Primary.TButton", command=save).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(btns, text="Huỷ", command=dlg.destroy).pack(side=tk.RIGHT)

    # ---- run login / relogin ----------------------------------------
    def _args(self) -> SimpleNamespace:
        return SimpleNamespace(
            node=None, chrome=self.chrome_var.get().strip(),
            db=Path(self.db_var.get().strip()), base_url=self.base_url_var.get().strip(),
            redirect_uri=self.redirect_var.get().strip(),
            timeout_minutes=float(self.timeout_var.get().strip() or "15"),
            new_password=self.new_password_var.get().strip() or app.DEFAULT_NEW_PASSWORD,
        )

    def _start_selected(self, *, relogin: bool) -> None:
        accounts = self._selected_accounts()
        if not accounts:
            messagebox.showinfo(APP_TITLE, "Chọn tài khoản trong danh sách trước.")
            return
        self._launch(accounts, relogin=relogin)

    def _start_all(self) -> None:
        accounts = self.store.all()
        if not accounts:
            messagebox.showinfo(APP_TITLE, "Chưa có tài khoản nào.")
            return
        self._launch(accounts, relogin=False)

    def _launch(self, accounts: list[Account], *, relogin: bool) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_TITLE, "Đang chạy, vui lòng đợi.")
            return
        try:
            args = self._args()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        verb = "Đăng nhập lại" if relogin else "Đăng nhập"
        self._set_running(True)
        self.progress.configure(maximum=len(accounts), value=0)
        self._log(f"{verb} {len(accounts)} tài khoản: " + ", ".join(a.name for a in accounts), "info")
        self.worker = threading.Thread(target=self._run_worker, args=(args, accounts), daemon=True)
        self.worker.start()

    def _run_worker(self, args: SimpleNamespace, accounts: list[Account]) -> None:
        try:
            result = run_accounts(accounts, args, store=self.store,
                                  log_cb=lambda line: self.log_queue.put(("log", line)))
            self._last_result = result
            self.log_queue.put(("result", json.dumps(result, ensure_ascii=False, indent=2)))
            self.log_queue.put(("done", "Done" if result.get("ok") else "Finished with errors"))
        except Exception as exc:
            self.log_queue.put(("log", f"ERROR: {exc}"))
            self.log_queue.put(("done", "Error"))

    # ---- IDE login --------------------------------------------------
    def _login_ide(self) -> None:
        if kiro_ide_login is None:
            messagebox.showerror(APP_TITLE, "Thiếu module kiro_ide_login.")
            return
        accounts = self._selected_accounts()
        if not accounts:
            messagebox.showinfo(APP_TITLE, "Chọn 1 tài khoản đã có connection trong 9router.")
            return
        acc = accounts[0]
        target = acc.connection_id or acc.name
        if not messagebox.askyesno(
            APP_TITLE,
            f"Ghi token của '{acc.name}' vào AWS SSO cache để mở Kiro IDE là đã đăng nhập?\n"
            "Token hiện tại trong cache sẽ được sao lưu lại."):
            return
        try:
            db = Path(self.db_var.get().strip()) or None
            result = kiro_ide_login.write_ide_login_from_9router(target, db_path=db)
            self._log(f"🖥 Đã ghi token IDE cho {result.get('name', acc.name)} → {result.get('tokenPath')}", "ok")
            if result.get("backups"):
                self._log(f"   (đã sao lưu {len(result['backups'])} file cũ)", "info")
            messagebox.showinfo(APP_TITLE, f"Xong! Mở Kiro IDE là đã đăng nhập bằng {acc.name}.")
        except Exception as exc:
            self._log(f"IDE login lỗi: {exc}", "err")
            messagebox.showerror(APP_TITLE, str(exc))

    # ---- log / queue ------------------------------------------------
    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        for b in (self.btn_login, self.btn_relogin, self.btn_all):
            b.configure(state=state)
        if hasattr(self, "device_btn"):
            self.device_btn.configure(state=state)
        if running:
            self.status_var.set("● Đang chạy")
            self.status_lbl.configure(foreground=WARN)
        else:
            self.status_var.set("● Sẵn sàng")
            self.status_lbl.configure(foreground=OK)

    def _drain_log_queue(self) -> None:
        try:
            while True:
                kind, message = self.log_queue.get_nowait()
                if kind in {"log", "result"}:
                    tag = self._log_tag(message)
                    self._log(message, tag)
                    if kind == "log":
                        step = friendly_step(message)
                        if step:
                            self.step_var.set(step)
                        # advance progress on per-account completion markers
                        low = message.lower()
                        if "exchanging token" in low or "imported" in low or '"ok": true' in low:
                            try:
                                self.progress.step(1)
                            except tk.TclError:
                                pass
                elif kind == "device_prompt":
                    try:
                        info = json.loads(message)
                        self.device_code_var.set(f"Mã xác thực: {info.get('user_code', '')}  —  Bấm Allow trên trình duyệt")
                        self.device_link_var.set(info.get("verification_uri", ""))
                        self.step_var.set("🌐 Mở link và bấm Allow để tiếp tục...")
                    except Exception:
                        pass
                elif kind == "done":
                    is_ok = message.lower().startswith("done")
                    self._set_running(False)
                    self.step_var.set("🎉 Hoàn tất!" if is_ok else "⚠️ Kết thúc (có lỗi)")
                    self.status_var.set("● Xong" if is_ok else "● Có lỗi")
                    self.status_lbl.configure(foreground=OK if is_ok else ERR)
                    self.progress.configure(value=self.progress["maximum"])
                    self._refresh_table()
                    if self.notify_tg_var.get():
                        threading.Thread(target=self._notify_telegram,
                                         args=(message, self._last_result), daemon=True).start()
        except queue.Empty:
            pass
        self.after(100, self._drain_log_queue)

    def _log_tag(self, message: str) -> str:
        low = message.lower()
        if low.startswith("error") or "lỗi" in low or '"ok": false' in low:
            return "err"
        if "✅" in message or "accepted" in low or "đã lưu" in low or '"ok": true' in low:
            return "ok"
        if message.startswith(("🔑", "🌐", "👤", "🔒", "🎫", "↪️", "📤", "🖥")):
            return "info"
        return ""

    def _log(self, message: str, tag: str = "") -> None:
        self.log_text.configure(state="normal")
        if tag:
            self.log_text.insert(tk.END, message + "\n", tag)
        else:
            self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def _notify_telegram(self, status_message: str, result: object) -> None:
        try:
            lines: list[str] = []
            ok_count = fail_count = 0
            if isinstance(result, dict):
                for item in result.get("results", []):
                    name = item.get("name", "?")
                    if item.get("ok"):
                        ok_count += 1
                        lines.append(f"✅ {name} → vào 9router (refreshToken OK)")
                    else:
                        fail_count += 1
                        lines.append(f"❌ {name} → {str(item.get('error', 'lỗi'))[:120]}")
                verify = result.get("verify", {})
                if isinstance(verify, dict) and verify.get("kiroConnections") is not None:
                    lines.append(f"📊 Tổng kết nối Kiro trong 9router: {verify['kiroConnections']}")
            header = f"🤖 Kiro Manager: {ok_count} thành công, {fail_count} lỗi"
            msg = header + ("\n" + "\n".join(lines) if lines else "")
            hermes = shutil.which("hermes") or os.environ.get("HERMES_EXE", "")
            if not hermes or not os.path.isfile(hermes):
                self.log_queue.put(("log", "Telegram: không tìm thấy hermes.exe, bỏ qua"))
                return
            chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
            if not chat_id:
                self.log_queue.put(("log", "Telegram: chưa đặt TELEGRAM_CHAT_ID, bỏ qua"))
                return
            env = os.environ.copy()
            env.pop("_HERMES_GATEWAY", None)
            env.pop("HERMES_GATEWAY_DETACHED", None)
            subprocess.run([hermes, "send", "-t", f"telegram:{chat_id}", msg],
                           timeout=90, capture_output=True, env=env)
            self.log_queue.put(("log", "📤 Đã gửi báo cáo Telegram"))
        except Exception as exc:
            self.log_queue.put(("log", f"Telegram notify failed: {exc}"))


def self_test() -> int:
    checks = {
        "resourceRoot": str(app._resource_root()),
        "outputRoot": str(app._output_root()),
        "dbDefaultExists": app._default_db().is_file(),
        "chrome": app._default_chrome(),
        "storePath": str(AccountStore().path),
        "ideModule": kiro_ide_login is not None,
        "jsonModule": kiro_json_login is not None,
        "deviceModule": kiro_device_login is not None,
    }
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--db-helper":
        return app.main(["--db-helper"])
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    ManagerGui().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
