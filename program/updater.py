"""
updater.py
card_reader.exe の自動更新ツール

GitHub の最新リリースを確認し、新バージョンがあればダウンロード・上書きする。
card_reader.exe と同じフォルダに置いて使用する。
"""

from __future__ import annotations

import os
import sys
import json
import shutil
import zipfile
import tempfile
import threading
import subprocess
import tkinter as tk
from tkinter import messagebox
from pathlib import Path

import requests

# ============================================================
# 設定（リポジトリ名を実際のものに変更してください）
# ============================================================
GITHUB_OWNER = "Ikeneko"               # GitHubのユーザー名
GITHUB_REPO  = "card_reader"           # リポジトリ名
ASSET_NAME   = "card_reader.zip"       # リリースに添付するZIPファイル名
TARGET_EXE   = "card_reader.exe"       # 更新対象のexeファイル名
VERSION_FILE = "version.txt"           # 現在のバージョンを記録するファイル名

API_URL      = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
TIMEOUT      = 15  # HTTP タイムアウト（秒）
# ============================================================


def get_base_dir() -> Path:
    """exe 実行時と py 実行時の両方でベースディレクトリを返す"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def read_current_version() -> str:
    """version.txt から現在のバージョンを読む。なければ '0.0.0' を返す"""
    path = get_base_dir() / VERSION_FILE
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return "0.0.0"


def write_version(version: str) -> None:
    path = get_base_dir() / VERSION_FILE
    path.write_text(version, encoding="utf-8")


def fetch_latest_release() -> dict:
    """GitHub API から最新リリース情報を取得する"""
    resp = requests.get(API_URL, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def find_asset(release: dict) -> dict | None:
    """リリースから ASSET_NAME に一致するアセットを探す"""
    for asset in release.get("assets", []):
        if asset["name"] == ASSET_NAME:
            return asset
    return None


def download_file(url: str, dest: Path, progress_cb=None) -> None:
    """URL からファイルをダウンロードする。progress_cb(downloaded, total) を呼ぶ"""
    with requests.get(url, stream=True, timeout=TIMEOUT) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        progress_cb(downloaded, total)


def apply_update(zip_path: Path) -> None:
    """
    ZIP を展開して card_reader.exe を上書きする。
    ZIP のルートに TARGET_EXE が入っている前提。
    """
    base = get_base_dir()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmp_path)

        # ZIP 直下または 1 階層下の card_reader/ フォルダを探す
        candidates = list(tmp_path.rglob(TARGET_EXE))
        if not candidates:
            raise FileNotFoundError(f"{TARGET_EXE} が ZIP 内に見つかりません")

        src_exe = candidates[0]
        dst_exe = base / TARGET_EXE

        # 実行中の exe は上書きできないため .old にリネームしてから上書き
        old_exe = dst_exe.with_suffix(".exe.old")
        if dst_exe.exists():
            if old_exe.exists():
                old_exe.unlink()
            dst_exe.rename(old_exe)

        shutil.copy2(src_exe, dst_exe)

        # _internal フォルダも更新（存在する場合）
        src_internal = src_exe.parent / "_internal"
        dst_internal = base / "_internal"
        if src_internal.exists():
            if dst_internal.exists():
                shutil.rmtree(dst_internal)
            shutil.copytree(src_internal, dst_internal)


# ============================================================
# GUI
# ============================================================

class UpdaterApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("card_reader アップデーター")
        self.root.resizable(False, False)

        self._center_window(360, 220)

        # ---- ウィジェット ----
        frm = tk.Frame(self.root, padx=20, pady=16)
        frm.pack(fill="both", expand=True)

        self.lbl_current = tk.Label(frm, text=f"現在のバージョン：{read_current_version()}", anchor="w")
        self.lbl_current.grid(row=0, column=0, columnspan=2, sticky="w", pady=2)

        self.lbl_latest = tk.Label(frm, text="最新バージョン：未確認", anchor="w")
        self.lbl_latest.grid(row=1, column=0, columnspan=2, sticky="w", pady=2)

        self.lbl_status = tk.Label(frm, text="「最新バージョンを確認」を押してください", anchor="w", fg="gray")
        self.lbl_status.grid(row=2, column=0, columnspan=2, sticky="w", pady=6)

        self.progress = tk.IntVar(value=0)
        self.progressbar = tk.Scale(
            frm, variable=self.progress, from_=0, to=100,
            orient="horizontal", state="disabled", showvalue=False,
            length=300, sliderlength=1, troughcolor="#e0e0e0"
        )
        self.progressbar.grid(row=3, column=0, columnspan=2, sticky="ew", pady=4)

        btn_frm = tk.Frame(frm)
        btn_frm.grid(row=4, column=0, columnspan=2, pady=10)

        self.btn_check = tk.Button(
            btn_frm, text="最新バージョンを確認", width=18, command=self._on_check
        )
        self.btn_check.pack(side="left", padx=6)

        self.btn_update = tk.Button(
            btn_frm, text="今すぐ更新", width=12,
            command=self._on_update, state="disabled", bg="#0078d4", fg="white"
        )
        self.btn_update.pack(side="left", padx=6)

        self.btn_launch = tk.Button(
            btn_frm, text="閉じる", width=8, command=self.root.destroy
        )
        self.btn_launch.pack(side="left", padx=6)

        self._release_info: dict | None = None

    def _center_window(self, w: int, h: int):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _set_status(self, msg: str, color: str = "gray"):
        self.lbl_status.config(text=msg, fg=color)

    def _set_buttons(self, checking: bool = False, can_update: bool = False):
        self.btn_check.config(state="disabled" if checking else "normal")
        self.btn_update.config(state="normal" if can_update else "disabled")

    def _on_check(self):
        self._set_buttons(checking=True)
        self._set_status("GitHub を確認中...")
        self.lbl_latest.config(text="最新バージョン：確認中...")
        threading.Thread(target=self._check_thread, daemon=True).start()

    def _check_thread(self):
        try:
            release = fetch_latest_release()
            tag = release.get("tag_name", "不明")
            asset = find_asset(release)
            current = read_current_version()

            self._release_info = release

            def _update_ui():
                self.lbl_latest.config(text=f"最新バージョン：{tag}")
                if asset is None:
                    self._set_status(f"⚠ リリースに {ASSET_NAME} が見つかりません", "orange")
                    self._set_buttons(can_update=False)
                elif tag == current:
                    self._set_status("✓ 最新バージョンです", "green")
                    self._set_buttons(can_update=False)
                else:
                    self._set_status(f"新しいバージョン {tag} があります", "#0078d4")
                    self._set_buttons(can_update=True)

            self.root.after(0, _update_ui)

        except Exception as e:
            self.root.after(0, lambda: (
                self._set_status(f"確認失敗: {e}", "red"),
                self._set_buttons(can_update=False)
            ))

    def _on_update(self):
        if self._release_info is None:
            return
        asset = find_asset(self._release_info)
        if asset is None:
            return

        if not messagebox.askyesno(
            "更新の確認",
            f"バージョン {self._release_info.get('tag_name')} をダウンロードして更新しますか？\n"
            "card_reader.exe が起動中の場合は先に終了してください。",
            parent=self.root
        ):
            return

        self._set_buttons(checking=True)
        self._set_status("ダウンロード中...")
        self.progress.set(0)

        def _do_update():
            try:
                download_url = asset["browser_download_url"]
                tag = self._release_info.get("tag_name", "unknown")

                with tempfile.TemporaryDirectory() as tmp:
                    zip_path = Path(tmp) / ASSET_NAME

                    def _progress(downloaded, total):
                        if total > 0:
                            pct = int(downloaded / total * 100)
                            self.root.after(0, lambda p=pct: self.progress.set(p))
                            self.root.after(0, lambda p=pct: self._set_status(f"ダウンロード中... {p}%"))

                    download_file(download_url, zip_path, _progress)
                    self.root.after(0, lambda: self._set_status("展開・上書き中..."))
                    apply_update(zip_path)

                write_version(tag)

                def _done():
                    self.progress.set(100)
                    self.lbl_current.config(text=f"現在のバージョン：{tag}")
                    self._set_status(f"✓ バージョン {tag} への更新が完了しました", "green")
                    self._set_buttons(can_update=False)
                    if messagebox.askyesno(
                        "更新完了",
                        f"バージョン {tag} への更新が完了しました。\ncard_reader.exe を起動しますか？",
                        parent=self.root
                    ):
                        target = get_base_dir() / TARGET_EXE
                        if target.exists():
                            subprocess.Popen([str(target)], cwd=str(get_base_dir()))
                        self.root.destroy()

                self.root.after(0, _done)

            except Exception as e:
                self.root.after(0, lambda: (
                    self._set_status(f"更新失敗: {e}", "red"),
                    self._set_buttons(can_update=True)
                ))

        threading.Thread(target=_do_update, daemon=True).start()

    def run(self):
        self.root.mainloop()


def main():
    app = UpdaterApp()
    app.run()


if __name__ == "__main__":
    main()
