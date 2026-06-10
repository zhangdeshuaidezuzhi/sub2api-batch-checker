from __future__ import annotations

import asyncio
import ctypes
import csv
import os
import shutil
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .checker import CHATGPT_CODEX_RESPONSES_URL, CHATGPT_ME_URL, SUB2API_OAUTH_COMPAT_URL, check_many
from .loader import load_sub2api_accounts, write_sub2api_bundle


TOOL_DIR = Path(os.environ.get("SUB2API_CHECKER_HOME", Path(__file__).resolve().parent.parent))
OUTPUT_DIR = TOOL_DIR / "outputs"
DEFAULT_INPUT = Path(os.environ.get("SUB2API_CHECKER_DEFAULT_INPUT", Path.cwd()))
DEFAULT_PROXY = os.environ.get("SUB2API_CHECKER_PROXY", "http://127.0.0.1:7897")

STATUS_CN = {
    "ok": "可用",
    "codex_login_only": "Codex登录有效/API权限不足",
    "sub2api_compatible": "Sub2API兼容可用",
    "model_unsupported": "模型不支持/请求方式待适配",
    "request_shape_error": "请求方式待适配",
    "quota_or_rate_limited": "额度用尽/限流",
    "permission_or_scope_missing": "登录有效/API权限不足",
    "forbidden_or_banned": "权限不足/账号禁用",
    "auth_invalid": "认证失效/需要重新登录",
    "expired_locally": "本地判断已过期",
    "network_or_proxy": "网络/代理失败",
    "unsupported": "暂不支持的账号格式",
    "failed_unknown": "未知失败",
}

CLEANUP_STATUS_OPTIONS = [
    ("forbidden_or_banned", "权限/禁用", True),
    ("codex_login_only", "Codex登录/API不足", False),
    ("sub2api_compatible", "Sub2API兼容", False),
    ("model_unsupported", "模型不支持", False),
    ("request_shape_error", "请求待适配", False),
    ("permission_or_scope_missing", "登录有效/API权限不足", False),
    ("auth_invalid", "认证失败", True),
    ("expired_locally", "本地过期", False),
    ("unsupported", "格式不支持", False),
    ("quota_or_rate_limited", "额度/限流", False),
    ("network_or_proxy", "网络/代理", False),
    ("failed_unknown", "未知失败", False),
]


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "ok",
        "status",
        "status_cn",
        "name",
        "platform",
        "type",
        "source_format",
        "account_id",
        "http_status",
        "latency_ms",
        "error_code",
        "message",
        "model",
        "endpoint",
        "attempts",
        "raw_meta",
        "source_file",
        "fingerprint",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class BatchCheckerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Sub2API 批量账号验活工具")
        self.geometry("900x640")
        self.minsize(820, 560)

        self.selected_paths: list[Path] = [DEFAULT_INPUT]
        self.input_var = tk.StringVar(value=str(DEFAULT_INPUT))
        self.mode_var = tk.StringVar(value="sub2api_oauth")
        self.concurrency_var = tk.IntVar(value=20)
        self.timeout_var = tk.IntVar(value=12)
        self.refresh_var = tk.BooleanVar(value=False)
        self.use_proxy_var = tk.BooleanVar(value=True)
        self.proxy_url_var = tk.StringVar(value=DEFAULT_PROXY)
        self.limit_var = tk.StringVar(value="")
        self.cleanup_status_vars = {
            status: tk.BooleanVar(value=checked) for status, _label, checked in CLEANUP_STATUS_OPTIONS
        }

        self.running = False
        self.last_csv: Path | None = None
        self.last_good: Path | None = None
        self.last_bad: Path | None = None
        self.last_run_dir: Path | None = None
        self.last_good_dir: Path | None = None
        self.last_bad_dir: Path | None = None
        self.last_bad_original_dir: Path | None = None
        self.last_moved_bad_dir: Path | None = None
        self.last_bad_source_groups: dict[Path, str] = {}

        self._build_ui()
        self.after(800, self._warn_if_multiple_gui_processes)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=16)
        root.pack(fill=tk.BOTH, expand=True)

        title = ttk.Label(root, text="Sub2API 批量账号验活", font=("Microsoft YaHei UI", 18, "bold"))
        title.pack(anchor=tk.W)

        note = ttk.Label(
            root,
            text="本地运行，不打印 token。建议先快速初筛；需要精测时再勾选自动刷新 OAuth token。",
            foreground="#555555",
        )
        note.pack(anchor=tk.W, pady=(4, 14))

        path_row = ttk.Frame(root)
        path_row.pack(fill=tk.X, pady=4)
        ttk.Label(path_row, text="账号 JSON 目录/文件：").pack(side=tk.LEFT)
        ttk.Entry(path_row, textvariable=self.input_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        ttk.Button(path_row, text="选择文件夹", command=self.choose_folder).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(path_row, text="选择单个文件", command=self.choose_file).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(path_row, text="选择多个JSON", command=self.choose_files).pack(side=tk.LEFT)

        path_hint = ttk.Label(
            root,
            text="提示：选择文件夹窗口只显示文件夹，不显示 token 文件；要点选具体 token_*.json，请用“选择单个文件”或“选择多个JSON”。",
            foreground="#666666",
        )
        path_hint.pack(anchor=tk.W, pady=(0, 6))

        options = ttk.LabelFrame(root, text="验活设置", padding=12)
        options.pack(fill=tk.X, pady=12)

        mode_row = ttk.Frame(options)
        mode_row.pack(fill=tk.X, pady=3)
        ttk.Radiobutton(
            mode_row,
            text="轻量验活：只测认证是否还活，不代表一定能推理",
            variable=self.mode_var,
            value="models",
        ).pack(side=tk.LEFT, padx=(0, 24))
        ttk.Radiobutton(
            mode_row,
            text="官方API真实调用：直连 api.openai.com，可能消耗少量额度",
            variable=self.mode_var,
            value="responses",
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            mode_row,
            text="Sub2API兼容验活：适合 OAuth/Codex 账号，不直连官方 Responses",
            variable=self.mode_var,
            value="sub2api_oauth",
        ).pack(side=tk.LEFT, padx=(24, 0))
        ttk.Radiobutton(
            mode_row,
            text="Codex登录诊断：只测 ChatGPT/Codex 登录链路，不判坏",
            variable=self.mode_var,
            value="codex_login",
        ).pack(side=tk.LEFT, padx=(24, 0))
        ttk.Radiobutton(
            mode_row,
            text="Codex真实诊断：调用 Codex 后端，可能较慢",
            variable=self.mode_var,
            value="codex_real",
        ).pack(side=tk.LEFT, padx=(24, 0))

        advanced_row = ttk.Frame(options)
        advanced_row.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(advanced_row, text="并发数：").pack(side=tk.LEFT)
        ttk.Spinbox(advanced_row, from_=1, to=50, textvariable=self.concurrency_var, width=6).pack(side=tk.LEFT)
        ttk.Label(advanced_row, text="  超时秒数：").pack(side=tk.LEFT)
        ttk.Spinbox(advanced_row, from_=5, to=180, textvariable=self.timeout_var, width=6).pack(side=tk.LEFT)
        ttk.Label(advanced_row, text="  只测前 N 个：").pack(side=tk.LEFT)
        ttk.Entry(advanced_row, textvariable=self.limit_var, width=8).pack(side=tk.LEFT)
        ttk.Checkbutton(advanced_row, text="自动刷新 OAuth token（较慢）", variable=self.refresh_var).pack(side=tk.LEFT, padx=18)

        proxy_row = ttk.Frame(options)
        proxy_row.pack(fill=tk.X, pady=(10, 0))
        ttk.Checkbutton(proxy_row, text="使用代理", variable=self.use_proxy_var).pack(side=tk.LEFT)
        ttk.Label(proxy_row, text="代理地址：").pack(side=tk.LEFT, padx=(18, 0))
        ttk.Entry(proxy_row, textvariable=self.proxy_url_var, width=34).pack(side=tk.LEFT)
        ttk.Label(proxy_row, text="例：http://127.0.0.1:7897").pack(
            side=tk.LEFT,
            padx=(8, 0),
        )

        action_row = ttk.Frame(root)
        action_row.pack(fill=tk.X, pady=(2, 10))
        self.start_button = ttk.Button(action_row, text="开始验活", command=self.start_check)
        self.start_button.pack(side=tk.LEFT)
        ttk.Button(action_row, text="打开结果目录", command=self.open_output_dir).pack(side=tk.LEFT, padx=8)
        ttk.Button(action_row, text="打开 CSV 总表", command=self.open_last_csv).pack(side=tk.LEFT)
        ttk.Button(action_row, text="打开可用账号", command=self.open_good_dir).pack(side=tk.LEFT, padx=8)
        ttk.Button(action_row, text="打开坏账号", command=self.open_bad_dir).pack(side=tk.LEFT)
        ttk.Button(action_row, text="打开坏账号原始JSON", command=self.open_bad_original_dir).pack(side=tk.LEFT, padx=8)

        cleanup_row = ttk.Frame(root)
        cleanup_row.pack(fill=tk.X, pady=(0, 10))
        ttk.Button(cleanup_row, text="移动坏账号原文件", command=self.move_bad_source_files).pack(side=tk.LEFT)
        ttk.Label(cleanup_row, text="进回收站类型：").pack(side=tk.LEFT, padx=(12, 4))
        for status, label, _checked in CLEANUP_STATUS_OPTIONS:
            ttk.Checkbutton(
                cleanup_row,
                text=label,
                variable=self.cleanup_status_vars[status],
            ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            cleanup_row,
            text="选中类型进回收站",
            command=self.recycle_selected_source_files,
        ).pack(side=tk.LEFT, padx=(8, 0))

        progress_row = ttk.Frame(root)
        progress_row.pack(fill=tk.X, pady=(0, 10))
        self.progress = ttk.Progressbar(progress_row, mode="indeterminate")
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.state_label = ttk.Label(progress_row, text="待开始", width=18, anchor=tk.E)
        self.state_label.pack(side=tk.LEFT, padx=(12, 0))

        summary = ttk.LabelFrame(root, text="结果汇总", padding=12)
        summary.pack(fill=tk.X, pady=(0, 10))
        self.summary_text = tk.StringVar(value="还没有运行。")
        ttk.Label(summary, textvariable=self.summary_text, justify=tk.LEFT).pack(anchor=tk.W)

        log_frame = ttk.LabelFrame(root, text="运行日志", padding=8)
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log = tk.Text(log_frame, height=12, wrap=tk.WORD)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log.configure(yscrollcommand=scroll.set)

    def choose_folder(self) -> None:
        path = filedialog.askdirectory(title="选择 Sub2API JSON 文件夹")
        if path:
            self.selected_paths = [Path(path)]
            self.input_var.set(path)

    def choose_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 Sub2API JSON 或 ZIP 文件",
            filetypes=[("JSON/ZIP 文件", "*.json *.zip"), ("JSON 文件", "*.json"), ("ZIP 文件", "*.zip"), ("所有文件", "*.*")],
        )
        if path:
            self.selected_paths = [Path(path)]
            self.input_var.set(path)

    def choose_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择一个或多个 token JSON/ZIP 文件",
            filetypes=[("JSON/ZIP 文件", "*.json *.zip"), ("JSON 文件", "*.json"), ("ZIP 文件", "*.zip"), ("所有文件", "*.*")],
        )
        if paths:
            self.selected_paths = [Path(path) for path in paths]
            self.input_var.set(f"已选择 {len(self.selected_paths)} 个 JSON 文件")

    def open_output_dir(self) -> None:
        path = self.last_run_dir or OUTPUT_DIR
        path.mkdir(parents=True, exist_ok=True)
        self._open_path(path)

    def open_last_csv(self) -> None:
        if not self.last_csv or not self.last_csv.exists():
            messagebox.showinfo("提示", "还没有可打开的 CSV 总表。")
            return
        self._open_path(self.last_csv)

    def open_good_dir(self) -> None:
        if not self.last_good_dir or not self.last_good_dir.exists():
            messagebox.showinfo("提示", "还没有可打开的可用账号文件夹。")
            return
        self._open_path(self.last_good_dir)

    def open_bad_dir(self) -> None:
        if not self.last_bad_dir or not self.last_bad_dir.exists():
            messagebox.showinfo("提示", "还没有可打开的坏账号文件夹。")
            return
        self._open_path(self.last_bad_dir)

    def open_bad_original_dir(self) -> None:
        if not self.last_bad_original_dir or not self.last_bad_original_dir.exists():
            messagebox.showinfo("提示", "还没有可打开的坏账号原始 JSON 文件夹。")
            return
        self._open_path(self.last_bad_original_dir)

    def move_bad_source_files(self) -> None:
        if self.running:
            messagebox.showinfo("提示", "正在验活，完成后再移动。")
            return
        if not self.last_bad_source_groups:
            messagebox.showinfo("提示", "还没有可移动的坏账号原文件。请先完成一次验活。")
            return
        if not self.last_run_dir:
            messagebox.showinfo("提示", "还没有结果目录。")
            return

        answer = messagebox.askyesno(
            "确认移动",
            f"将把本次识别出的 {len(self.last_bad_source_groups)} 个坏账号原始 JSON 从原目录移动到隔离文件夹，并按失败原因分开。\n\n"
            "移动后，它们不会再和没测的账号混在一起。\n\n"
            "是否继续？",
        )
        if not answer:
            return

        target_dir = self.last_run_dir / "已移出的坏账号原始JSON"
        moved, skipped = self._move_source_files(self.last_bad_source_groups, target_dir)
        self.last_moved_bad_dir = target_dir
        messagebox.showinfo("完成", f"已移动：{moved} 个\n跳过：{skipped} 个\n\n位置：{target_dir}")
        self._open_path(target_dir)

    def recycle_selected_source_files(self) -> None:
        if self.running:
            messagebox.showinfo("提示", "正在验活，完成后再处理。")
            return
        if not self.last_bad_source_groups:
            messagebox.showinfo("提示", "还没有可处理的结果。请先完成一次验活。")
            return

        selected_statuses = [
            status for status, _label, _checked in CLEANUP_STATUS_OPTIONS if self.cleanup_status_vars[status].get()
        ]
        if not selected_statuses:
            messagebox.showinfo("提示", "请先勾选至少一种要进回收站的失败类型。")
            return

        selected_groups = {self._status_folder_name(status) for status in selected_statuses}
        selected_files = [
            path for path, group_name in self.last_bad_source_groups.items() if group_name in selected_groups
        ]
        if not selected_files:
            selected_labels = "、".join(STATUS_CN.get(status, status) for status in selected_statuses)
            messagebox.showinfo("提示", f"本次结果里没有这些类型的原始 JSON：{selected_labels}")
            return

        counts = Counter(
            group_name for group_name in self.last_bad_source_groups.values() if group_name in selected_groups
        )
        count_lines = "\n".join(f"{group_name}：{count}" for group_name, count in sorted(counts.items()))
        risky_statuses = {"quota_or_rate_limited", "network_or_proxy", "failed_unknown"}
        risky_selected = [STATUS_CN.get(status, status) for status in selected_statuses if status in risky_statuses]
        risky_note = ""
        if risky_selected:
            risky_note = "\n\n注意：你勾选了可能误判的类型：" + "、".join(risky_selected) + "，建议复测后再清理。"

        answer = messagebox.askyesno(
            "确认进回收站",
            f"将把 {len(selected_files)} 个选中类型的原始 JSON 移到 Windows 回收站。\n\n"
            f"{count_lines}"
            f"{risky_note}\n\n"
            "是否继续？",
        )
        if not answer:
            return

        recycled, skipped = self._send_to_recycle_bin(selected_files)
        recycled_set = {path for path in selected_files if not path.exists()}
        self.last_bad_source_groups = {
            path: group_name for path, group_name in self.last_bad_source_groups.items() if path not in recycled_set
        }
        messagebox.showinfo("完成", f"已进回收站：{recycled} 个\n跳过：{skipped} 个")

    def recycle_banned_source_files(self) -> None:
        self.cleanup_status_vars["forbidden_or_banned"].set(True)
        self.recycle_selected_source_files()

    def start_check(self) -> None:
        if self.running:
            return
        input_paths = self._get_input_paths()
        missing = [path for path in input_paths if not path.exists()]
        if not input_paths:
            messagebox.showerror("路径不存在", "请先选择 JSON 文件夹或 JSON 文件。")
            return
        if missing:
            messagebox.showerror("路径不存在", "找不到这些路径：\n" + "\n".join(str(path) for path in missing[:8]))
            return

        try:
            concurrency = max(1, int(self.concurrency_var.get()))
            timeout = max(5, float(self.timeout_var.get()))
            limit_text = self.limit_var.get().strip()
            limit = int(limit_text) if limit_text else 0
        except ValueError:
            messagebox.showerror("设置错误", "并发数、超时秒数、只测前 N 个必须是数字。")
            return

        endpoint = "https://api.openai.com/v1/models"
        mode_name = "轻量验活"
        suffix = "models"
        if self.mode_var.get() == "responses":
            endpoint = "https://api.openai.com/v1/responses"
            mode_name = "官方 API 真实调用"
            suffix = "responses"
        elif self.mode_var.get() == "sub2api_oauth":
            endpoint = SUB2API_OAUTH_COMPAT_URL
            mode_name = "Sub2API 兼容验活"
            suffix = "sub2api_oauth"
        elif self.mode_var.get() == "codex_login":
            endpoint = CHATGPT_ME_URL
            mode_name = "Codex 登录诊断"
            suffix = "codex_login"
        elif self.mode_var.get() == "codex_real":
            endpoint = CHATGPT_CODEX_RESPONSES_URL
            mode_name = "Codex 真实诊断"
            suffix = "codex_real"

        proxy_url = self.proxy_url_var.get().strip() if self.use_proxy_var.get() else ""
        if self.use_proxy_var.get() and not proxy_url:
            messagebox.showerror("代理设置错误", "已勾选使用代理，请填写代理地址。")
            return
        if proxy_url and not (proxy_url.startswith("http://") or proxy_url.startswith("https://")):
            messagebox.showerror("代理设置错误", "当前图形版请填写 HTTP 代理，例如：http://127.0.0.1:7890")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.last_run_dir = OUTPUT_DIR / f"{suffix}_{timestamp}"
        if self.mode_var.get() in {"responses", "codex_real"}:
            good_label = "API可用账号"
            bad_label = "API不可用账号"
        elif self.mode_var.get() == "sub2api_oauth":
            good_label = "Sub2API兼容可用账号"
            bad_label = "Sub2API兼容异常账号"
        elif self.mode_var.get() == "codex_login":
            good_label = "Codex登录有效账号"
            bad_label = "Codex登录异常账号"
        else:
            good_label = "认证通过或待确认账号"
            bad_label = "认证失败账号"

        self.last_good_dir = self.last_run_dir / good_label
        self.last_bad_dir = self.last_run_dir / bad_label
        self.last_bad_original_dir = self.last_bad_dir / f"{bad_label}原始JSON副本"
        self.last_csv = self.last_run_dir / "验活总表.csv"
        self.last_good = self.last_good_dir / f"sub2api_{good_label}导入包.json"
        self.last_bad = self.last_bad_dir / f"sub2api_{bad_label}包.json"
        self.last_bad_source_groups = {}

        self.running = True
        self.start_button.configure(state=tk.DISABLED)
        self.progress.start(10)
        self.state_label.configure(text="运行中")
        self.summary_text.set("正在读取账号并验活，请稍等。")
        self.log.delete("1.0", tk.END)
        self._append_log("输入路径：")
        for path in input_paths[:10]:
            self._append_log(f"  {path}")
        if len(input_paths) > 10:
            self._append_log(f"  ... 还有 {len(input_paths) - 10} 个")
        self._append_log(f"模式：{mode_name}")
        if self.mode_var.get() in {"models", "responses"}:
            self._append_log("提醒：官方 API 检测失败不等于 Sub2API 不可用；OAuth/Codex/CPA 账号建议优先用 Sub2API 兼容验活。")
        self._append_log(f"并发：{concurrency}，超时：{int(timeout)} 秒")
        self._append_log(f"代理：{'启用 ' + proxy_url if proxy_url else '未启用'}")
        self._append_log(f"本次结果目录：{self.last_run_dir}")

        thread = threading.Thread(
            target=self._run_check_worker,
            args=(input_paths, endpoint, concurrency, timeout, limit, self.refresh_var.get(), proxy_url),
            daemon=True,
        )
        thread.start()

    def _get_input_paths(self) -> list[Path]:
        text = self.input_var.get().strip()
        if self.selected_paths and text.startswith("已选择 ") and text.endswith(" 个 JSON 文件"):
            return self.selected_paths
        if self.selected_paths and text == str(self.selected_paths[0]):
            return self.selected_paths
        if not text:
            return []
        return [Path(part.strip().strip('"')) for part in text.replace("\n", ";").split(";") if part.strip()]

    def _run_check_worker(
        self,
        input_paths: list[Path],
        endpoint: str,
        concurrency: int,
        timeout: float,
        limit: int,
        refresh: bool,
        proxy_url: str,
    ) -> None:
        try:
            accounts, errors = load_sub2api_accounts(input_paths, dedupe=True)
            if limit and len(accounts) > limit:
                accounts = accounts[:limit]

            self._ui(lambda: self._append_log(f"读取账号：{len(accounts)} 个，解析错误：{len(errors)} 个"))
            for err in errors[:10]:
                self._ui(lambda err=err: self._append_log(f"解析错误：{err}"))

            if not accounts:
                self._ui(lambda: self._finish_with_error("没有读取到账号。"))
                return

            def on_progress(done: int, total: int, result) -> None:
                if done == 1 or done == total or done % 10 == 0:
                    status_cn = STATUS_CN.get(result.status, result.status)
                    self._ui(
                        lambda done=done, total=total, status_cn=status_cn: self._update_progress_log(
                            done,
                            total,
                            status_cn,
                        )
                    )

            results = asyncio.run(
                check_many(
                    accounts=accounts,
                    concurrency=concurrency,
                    timeout=timeout,
                    endpoint=endpoint,
                    model="gpt-4.1-nano",
                    local_expiry_guard_sec=60,
                    refresh=refresh,
                    proxy_url=proxy_url,
                    progress=False,
                    progress_callback=on_progress,
                )
            )

            result_by_fp = {r.account.fingerprint: r for r in results}
            ok_accounts = [a for a in accounts if result_by_fp.get(a.fingerprint) and result_by_fp[a.fingerprint].ok]
            bad_accounts = [
                a for a in accounts if not (result_by_fp.get(a.fingerprint) and result_by_fp[a.fingerprint].ok)
            ]
            bad_source_groups = self._bad_only_source_groups(accounts, ok_accounts, results)
            copied_bad_sources = self._copy_source_files(bad_source_groups, self.last_bad_original_dir)
            self.last_bad_source_groups = bad_source_groups

            rows = []
            for result in results:
                row = result.to_csv_row()
                row["status_cn"] = STATUS_CN.get(result.status, result.status)
                rows.append(row)

            _write_csv(self.last_csv, rows)
            write_sub2api_bundle(self.last_good, ok_accounts)
            write_sub2api_bundle(self.last_bad, bad_accounts)

            counts = Counter(result.status for result in results)
            self._ui(
                lambda: self._finish_success(
                    len(accounts),
                    len(ok_accounts),
                    len(bad_accounts),
                    counts,
                    copied_bad_sources,
                    len(bad_source_groups),
                )
            )
        except Exception as exc:
            self._ui(lambda: self._finish_with_error(str(exc)))

    def _finish_success(
        self,
        total: int,
        ok_count: int,
        bad_count: int,
        counts: Counter,
        copied_bad_sources: int,
        bad_source_count: int,
    ) -> None:
        lines = [
            f"加载账号：{total}",
            f"可用账号：{ok_count}",
            f"坏账号：{bad_count}",
            f"坏账号原始 JSON 副本：{copied_bad_sources}/{bad_source_count}",
            "",
            "分类：",
        ]
        for status, count in sorted(counts.items()):
            lines.append(f"{STATUS_CN.get(status, status)}：{count}")

        lines.extend(
            [
                "",
                f"本次结果目录：{self.last_run_dir}",
                f"CSV 总表：{self.last_csv}",
                f"可用账号文件夹：{self.last_good_dir}",
                f"坏账号文件夹：{self.last_bad_dir}",
                f"坏账号原始 JSON 副本：{self.last_bad_original_dir}",
            ]
        )
        self.summary_text.set("\n".join(lines))
        self._append_log("验活完成。")
        self._append_log(f"坏账号原始 JSON 已复制：{copied_bad_sources}/{bad_source_count}")
        self._set_idle("完成")

    @staticmethod
    def _bad_only_source_groups(accounts, ok_accounts, results) -> dict[Path, str]:
        ok_fps = {account.fingerprint for account in ok_accounts}
        result_by_fp = {result.account.fingerprint: result for result in results}
        source_to_fps: dict[str, set[str]] = {}
        for account in accounts:
            source_to_fps.setdefault(account.source_file, set()).add(account.fingerprint)

        groups: dict[Path, str] = {}
        for source_file, fps in source_to_fps.items():
            if not fps or fps.intersection(ok_fps):
                continue
            if not all(fp in result_by_fp and not result_by_fp[fp].ok for fp in fps):
                continue
            path = Path(source_file)
            if path.is_file():
                statuses = [result_by_fp[fp].status for fp in fps if fp in result_by_fp]
                groups[path] = BatchCheckerApp._status_group(statuses)
        return dict(sorted(groups.items(), key=lambda item: str(item[0]).lower()))

    @staticmethod
    def _status_group(statuses: list[str]) -> str:
        priority = [
            "network_or_proxy",
            "quota_or_rate_limited",
            "codex_login_only",
            "sub2api_compatible",
            "model_unsupported",
            "request_shape_error",
            "permission_or_scope_missing",
            "forbidden_or_banned",
            "auth_invalid",
            "expired_locally",
            "unsupported",
            "failed_unknown",
        ]
        for status in priority:
            if status in statuses:
                return BatchCheckerApp._status_folder_name(status)
        return BatchCheckerApp._status_folder_name(statuses[0] if statuses else "failed_unknown")

    @staticmethod
    def _status_folder_name(status: str) -> str:
        names = {
            "network_or_proxy": "网络代理失败_建议开代理复测",
            "quota_or_rate_limited": "额度用尽或限流_暂不删除",
            "codex_login_only": "Codex登录有效但API权限不足_暂不删除",
            "sub2api_compatible": "Sub2API兼容可用_暂不删除",
            "model_unsupported": "模型不支持或请求方式待适配_暂不删除",
            "request_shape_error": "请求方式待适配_暂不删除",
            "permission_or_scope_missing": "登录有效但API权限不足_暂不删除",
            "forbidden_or_banned": "权限不足或账号禁用",
            "auth_invalid": "认证失效需要重新登录",
            "expired_locally": "本地判断已过期",
            "unsupported": "暂不支持的账号格式",
            "failed_unknown": "未知失败_建议复测",
        }
        return names.get(status, "未知失败_建议复测")

    @staticmethod
    def _safe_copy_name(source: Path, index: int) -> str:
        return f"{index:05d}_{source.name}"

    def _copy_source_files(self, source_groups: dict[Path, str], target_dir: Path | None) -> int:
        if not target_dir:
            return 0
        target_dir.mkdir(parents=True, exist_ok=True)
        copied = 0
        for index, (source, group_name) in enumerate(source_groups.items(), start=1):
            if not source.exists() or not source.is_file():
                continue
            group_dir = target_dir / group_name
            group_dir.mkdir(parents=True, exist_ok=True)
            target = group_dir / self._safe_copy_name(source, index)
            shutil.copy2(source, target)
            copied += 1
        return copied

    def _move_source_files(self, source_groups: dict[Path, str], target_dir: Path) -> tuple[int, int]:
        target_dir.mkdir(parents=True, exist_ok=True)
        moved = 0
        skipped = 0
        for index, (source, group_name) in enumerate(source_groups.items(), start=1):
            if not source.exists() or not source.is_file():
                skipped += 1
                continue
            group_dir = target_dir / group_name
            group_dir.mkdir(parents=True, exist_ok=True)
            target = group_dir / self._safe_copy_name(source, index)
            try:
                shutil.move(str(source), str(target))
                moved += 1
            except Exception:
                skipped += 1
        return moved, skipped

    @staticmethod
    def _send_to_recycle_bin(source_files: list[Path]) -> tuple[int, int]:
        existing = [path.resolve() for path in source_files if path.exists() and path.is_file()]
        skipped = len(source_files) - len(existing)
        if not existing:
            return 0, skipped

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.c_void_p),
                ("wFunc", ctypes.c_uint),
                ("pFrom", ctypes.c_wchar_p),
                ("pTo", ctypes.c_wchar_p),
                ("fFlags", ctypes.c_ushort),
                ("fAnyOperationsAborted", ctypes.c_bool),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", ctypes.c_wchar_p),
            ]

        fo_delete = 3
        fof_allow_undo = 0x0040
        fof_no_confirmation = 0x0010
        fof_no_error_ui = 0x0400
        fof_silent = 0x0004
        flags = fof_allow_undo | fof_no_confirmation | fof_no_error_ui | fof_silent

        recycled = 0
        chunk_size = 80
        for start in range(0, len(existing), chunk_size):
            chunk = existing[start : start + chunk_size]
            from_buffer = "\0".join(str(path) for path in chunk) + "\0\0"
            operation = SHFILEOPSTRUCTW(
                None,
                fo_delete,
                from_buffer,
                None,
                flags,
                False,
                None,
                None,
            )
            result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
            if result == 0 and not operation.fAnyOperationsAborted:
                recycled += sum(1 for path in chunk if not path.exists())
                skipped += sum(1 for path in chunk if path.exists())
            else:
                skipped += len(chunk)
        return recycled, skipped

    def _update_progress_log(self, done: int, total: int, status_cn: str) -> None:
        self.state_label.configure(text=f"{done}/{total}")
        self.summary_text.set(f"正在验活：{done}/{total}\n最新结果：{status_cn}")
        self._append_log(f"进度：{done}/{total}，最新：{status_cn}")

    def _finish_with_error(self, message: str) -> None:
        self.summary_text.set(f"运行失败：{message}")
        self._append_log(f"运行失败：{message}")
        self._set_idle("失败")

    def _set_idle(self, state: str) -> None:
        self.running = False
        self.progress.stop()
        self.state_label.configure(text=state)
        self.start_button.configure(state=tk.NORMAL)

    def _append_log(self, text: str) -> None:
        self.log.insert(tk.END, text + "\n")
        self.log.see(tk.END)

    def _ui(self, func) -> None:
        self.after(0, func)

    @staticmethod
    def _open_path(path: Path) -> None:
        import os

        os.startfile(str(path))

    def _warn_if_multiple_gui_processes(self) -> None:
        try:
            import subprocess

            current_pid = os.getpid()
            command = (
                "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
                "Where-Object { $_.CommandLine -like '*sub2api_batch_checker.gui*' } | "
                "Select-Object -ExpandProperty ProcessId"
            )
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            pids = [int(line.strip()) for line in proc.stdout.splitlines() if line.strip().isdigit()]
            other_pids = [pid for pid in pids if pid != current_pid]
            if other_pids:
                self._append_log(f"提醒：检测到还有 {len(other_pids)} 个图形版窗口进程在运行，多个同时验活会明显变慢。")
        except Exception:
            return


def main() -> None:
    app = BatchCheckerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
