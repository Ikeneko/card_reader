from __future__ import annotations

import os
import sys
import nfc
import json
import time
import queue
import threading
import datetime as dt
import subprocess
from threading import Event, Lock

import tkinter as tk
from tkinter import messagebox

import requests

from dotenv import load_dotenv

# =========================
# 設定
# =========================
class Config:
    def __init__(self) -> None:
        load_dotenv()  # .env を読み込む

        # --- .env に記載する項目（デフォルト値なし） ---
        self.slack_token: str = os.environ.get("SLACK_BOT_TOKEN", "")
        self.slack_channel: str = os.environ.get("SLACK_CHANNEL", "")
        self.weekly_post_url: str = os.environ.get("WEEKLY_POST_URL", "")

        # --- その他（デフォルト値あり・.env で上書き可能） ---
        # Slack
        self.slack_url: str = os.environ.get("SLACK_POST_URL", "https://slack.com/api/chat.postMessage")

        # ファイル
        self.log_file: str = os.environ.get("LOG_FILE", "entry_log.json")
        self.student_map_file: str = os.environ.get("STUDENT_MAP_FILE", "student_map.json")
        self.weekly_sent_file: str = os.environ.get("WEEKLY_SENT_FILE", "weekly_sent.json")
        self.weekly_marker_file: str = os.environ.get("WEEKLY_LAST_RUN_FILE", "weekly_last_run.txt")

        # サウンド設定
        self.entry_sound_path: str = os.environ.get("ENTRY_SOUND", "猫の鳴き声1.wav")
        self.exit_sound_path: str = os.environ.get("EXIT_SOUND", "ずんだ_お疲れ様.wav")

        # FeliCa
        self.service_code: int = int(os.environ.get("FELICA_SERVICE_CODE", "0x200B"), 16)

        # 同一カードの連続読み取り抑止秒
        self.duplicate_guard_seconds: float = float(os.environ.get("DUPLICATE_GUARD_SECONDS", "2.0"))

        # カード種類に関係なく、前回処理からの最小間隔
        self.read_interval_guard_seconds: float = float(os.environ.get("READ_INTERVAL_GUARD_SECONDS", "1.0"))


CFG = Config()


# =========================
# ユーティリティ
# =========================
def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def dump_json(path: str, data) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def sunday_range(dt_in: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """対象日を含む週の日曜00:00:00〜土曜23:59:59（ローカル時刻）"""
    dow_sun0 = (dt_in.weekday() + 1) % 7  # 日曜=0
    start = (dt_in - dt.timedelta(days=dow_sun0)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=6, hours=23, minutes=59, seconds=59)
    return start, end


def next_sunday_zero(now: dt.datetime) -> dt.datetime:
    days_until_sun = (6 - now.weekday()) % 7
    target = (now + dt.timedelta(days=days_until_sun)).replace(hour=0, minute=0, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=7)
    return target


# 年度境界日（3/31と 4/1）で必ず区切りを入れた週とその範囲を生成する
# 戻り値: [(week_start, week_end), ...] のリスト（時系列順）
def iter_fiscal_weeks(
    range_start: dt.datetime, range_end: dt.datetime
) -> list[tuple[dt.datetime, dt.datetime]]:
    """指定範囲内の週を列挙する。
    ルール:
      - 通常は日曜始まり・土曜終わり
      - 3/31 23:59:59 で必ず劇切り（曜日に関わらず）
      - 4/1 00:00:00 から新しい週開始
    """
    MARCH_END_MD = (3, 31)  # 年度末：3/31
    APRIL_START_MD = (4, 1)  # 年度始：4/1

    weeks: list[tuple[dt.datetime, dt.datetime]] = []
    cur = range_start

    while cur <= range_end:
        # 通常の週末（曜日）を計算
        dow_sun0 = (cur.weekday() + 1) % 7  # 日曜=0
        sat = cur + dt.timedelta(days=(6 - dow_sun0))
        normal_end = sat.replace(hour=23, minute=59, second=59, microsecond=0)

        # 3/31 と 4/1 の境界を計算
        year = cur.year
        march_end = dt.datetime(year, 3, 31, 23, 59, 59)
        april_start = dt.datetime(year, 4, 1, 0, 0, 0)

        # cur が 4/1 より前で 3/31 が通常の週末より前にある場合：境界で分割
        if cur < april_start and march_end < normal_end:
            # この週は 3/31 で打ち切り
            week_end = min(normal_end, march_end)
        else:
            week_end = normal_end

        # week_end は本来の週末（3/31または土曜）そのものを返す（range_end で切り捨てない）
        weeks.append((cur, week_end))

        # 次週の開始を決める
        if cur < april_start and week_end == march_end:
            # 3/31 で切れた場合、次は 4/1 から
            cur = april_start
        else:
            # 通常：次の日曜へ
            next_day = week_end + dt.timedelta(seconds=1)
            cur = next_day.replace(hour=0, minute=0, second=0, microsecond=0)

    return weeks


def overlap_seconds(a_start: dt.datetime, a_end: dt.datetime, b_start: dt.datetime, b_end: dt.datetime) -> int:
    s = max(a_start, b_start)
    e = min(a_end, b_end)
    return max(0, int((e - s).total_seconds()))


# =========================
# データストア
# =========================
class Store:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log_lock = Lock()
        self.student_map_lock = Lock()
        self.log_data: dict[str, list[dict]] = load_json(cfg.log_file, {})
        self.student_map: dict[str, dict] = load_json(cfg.student_map_file, {})
        self.weekly_sent: dict[str, dict] = load_json(
            cfg.weekly_sent_file, {}
        )  # {student_id: {"YYYY-MM-DD": hours}}

    def save_log(self) -> None:
        with self.log_lock:
            dump_json(self.cfg.log_file, self.log_data)

    def save_weekly_sent(self) -> None:
        dump_json(self.cfg.weekly_sent_file, self.weekly_sent)

    def save_student_map(self) -> None:
        dump_json(self.cfg.student_map_file, self.student_map)


# =========================
# Slack 通知
# =========================
class Notifier:
    def __init__(self, cfg: Config, gui: "GUIApp | None" = None):
        self.cfg = cfg
        self.gui = gui

    def post(self, text: str) -> None:
        if not self.cfg.slack_token:
            return
        # 別スレッドで送信して即リターン（カード読み取りをブロックしない）
        threading.Thread(target=self._send, args=(text,), daemon=True).start()

    def _send(self, text: str) -> None:
        try:
            headers = {"Authorization": f"Bearer {self.cfg.slack_token}"}
            data = {"channel": self.cfg.slack_channel, "text": text}
            resp = requests.post(self.cfg.slack_url, headers=headers, data=data, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            if self.gui:
                self.gui.log_threadsafe(f"Slack通知失敗: {e}")


# =========================
# サウンド再生
# =========================
class SoundPlayer:
    def __init__(self, cfg: Config, gui: "GUIApp"):
        self.cfg = cfg
        self.gui = gui

    def play(self, kind: str):
        path = self.cfg.entry_sound_path if kind == "entry" else self.cfg.exit_sound_path
        if path and os.path.exists(path):
            self._play_file(path)
        else:
            self._beep(kind)

    def _play_file(self, path: str):
        try:
            if sys.platform.startswith("win"):
                try:
                    import winsound
                    winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
                    return
                except Exception:
                    pass

            if sys.platform == "darwin":
                subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return

            for cmd in ("aplay", "paplay", "play"):
                try:
                    subprocess.Popen([cmd, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return
                except Exception:
                    continue
        except Exception:
            pass

        self._beep("fallback")

    def _beep(self, kind: str):
        try:
            if sys.platform.startswith("win"):
                try:
                    import winsound
                    if kind == "entry":
                        winsound.Beep(880, 120)
                    elif kind == "exit":
                        winsound.Beep(660, 120)
                    else:
                        winsound.MessageBeep()
                    return
                except Exception:
                    pass

            if hasattr(self.gui, "root"):
                self.gui.root.bell()
        except Exception:
            pass


# =========================
# GUI
# =========================
class GUIApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("FeliCa 入退室システム")
        self.root.attributes("-fullscreen", True)
        self.root.bind("<Escape>", self.toggle_fullscreen)

        center = tk.Frame(self.root)
        center.pack(expand=True)

        self.lbl_student = tk.Label(center, text="学籍番号：", font=("Arial", 18))
        self.lbl_student.pack(pady=10)

        self.lbl_name = tk.Label(center, text="名前：", font=("Arial", 18))
        self.lbl_name.pack(pady=10)

        self.lbl_status = tk.Label(center, text="", font=("Arial", 24, "bold"), fg="green")
        self.lbl_status.pack(pady=20)

        self.txt_log = tk.Text(center, height=10, width=70, state="disabled")
        self.txt_log.pack(pady=10)

    def toggle_fullscreen(self, event=None):
        self.root.attributes("-fullscreen", not self.root.attributes("-fullscreen"))

    # ---- GUI直接更新（メインスレッド専用）----
    def set_user(self, student_id: str, name: str):
        self.lbl_student.config(text=f"学籍番号：{student_id}")
        self.lbl_name.config(text=f"名前：{name}")

    def status_in(self):
        self.lbl_status.config(text="入室しました", fg="green")

    def status_out(self, hours: float):
        self.lbl_status.config(text=f"退室しました\n在室時間: {hours:.2f} 時間", fg="blue")

    def log(self, msg: str):
        self.txt_log.config(state="normal")
        self.txt_log.insert(tk.END, f"{msg}\n")
        self.txt_log.see(tk.END)
        self.txt_log.config(state="disabled")

    # ---- スレッドセーフ呼び出し ----
    def call_in_ui(self, func, *args, **kwargs):
        self.root.after(0, lambda: func(*args, **kwargs))

    def log_threadsafe(self, msg: str):
        self.call_in_ui(self.log, msg)

    def set_user_threadsafe(self, student_id: str, name: str):
        self.call_in_ui(self.set_user, student_id, name)

    def status_in_threadsafe(self):
        self.call_in_ui(self.status_in)

    def status_out_threadsafe(self, hours: float):
        self.call_in_ui(self.status_out, hours)

    def show_reader_error(self):
        self.lbl_status.config(text="⚠ リーダー接続エラー\n再接続を試みています…", fg="red")

    def show_reader_error_threadsafe(self):
        self.call_in_ui(self.show_reader_error)

    def prompt_registration(self, student_id: str) -> dict | None:
        if not messagebox.askyesno(
            "未登録の学生証",
            f"学籍番号: {student_id}\n未登録です。新規登録しますか？",
            parent=self.root
        ):
            return None

        dlg = tk.Toplevel(self.root)
        dlg.title("新規登録")
        dlg.transient(self.root)
        dlg.grab_set()

        frm = tk.Frame(dlg, padx=16, pady=16)
        frm.pack()

        tk.Label(frm, text=f"学籍番号: {student_id}", font=("Arial", 12)).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )

        tk.Label(frm, text="苗字").grid(row=1, column=0, sticky="e", padx=8, pady=6)
        ent_ln = tk.Entry(frm, width=24)
        ent_ln.grid(row=1, column=1, sticky="w")

        tk.Label(frm, text="氏名").grid(row=2, column=0, sticky="e", padx=8, pady=6)
        ent_fn = tk.Entry(frm, width=24)
        ent_fn.grid(row=2, column=1, sticky="w")

        result = {"ok": False, "data": None}

        def on_ok():
            ln = ent_ln.get().strip()
            fn = ent_fn.get().strip()
            if not ln or not fn:
                messagebox.showwarning("入力不足", "苗字・氏名を入力してください。", parent=dlg)
                return
            result["ok"] = True
            result["data"] = {
                "student_id": student_id,
                "name": f"{ln} {fn}",
            }
            dlg.destroy()

        def on_cancel():
            dlg.destroy()

        btn_frame = tk.Frame(frm)
        btn_frame.grid(row=3, column=0, columnspan=2, pady=12)

        tk.Button(btn_frame, text="登録", width=10, command=on_ok).pack(side="left", padx=5)
        tk.Button(btn_frame, text="キャンセル", width=10, command=on_cancel).pack(side="left", padx=5)

        # 全ウィジェットを配置してからサイズを確定し、画面中央に配置
        dlg.update_idletasks()
        dw = dlg.winfo_reqwidth()
        dh = dlg.winfo_reqheight()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - dw) // 2
        y = (sh - dh) // 2
        dlg.geometry(f"{dw}x{dh}+{x}+{y}")

        ent_ln.focus_set()
        dlg.bind("<Return>", lambda e: on_ok())
        dlg.bind("<Escape>", lambda e: on_cancel())

        self.root.wait_window(dlg)
        return result["data"] if result["ok"] else None

    def prompt_registration_threadsafe(self, student_id: str) -> dict | None:
        result_q: queue.Queue = queue.Queue(maxsize=1)

        def _show():
            try:
                result_q.put(self.prompt_registration(student_id))
            except Exception as e:
                result_q.put(e)

        self.root.after(0, _show)
        result = result_q.get()

        if isinstance(result, Exception):
            raise result
        return result

    def on_close(self, stop_evt: Event):
        stop_evt.set()
        self.root.after(200, self.root.destroy)

    def run(self, stop_evt: Event):
        self.root.protocol("WM_DELETE_WINDOW", lambda: self.on_close(stop_evt))
        self.root.mainloop()


# =========================
# 21:00 一括 exit 記録スレッド
# =========================
class DailyCloser(threading.Thread):
    def __init__(self, cfg: Config, store: Store, notifier: Notifier, gui: GUIApp, stop: Event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.store = store
        self.notifier = notifier
        self.gui = gui
        self.stop = stop

    def _seconds_until_next_21(self) -> float:
        now = dt.datetime.now()
        today_21 = now.replace(hour=21, minute=0, second=0, microsecond=0)
        if now <= today_21:
            return (today_21 - now).total_seconds()
        next_21 = (now + dt.timedelta(days=1)).replace(hour=21, minute=0, second=0, microsecond=0)
        return (next_21 - now).total_seconds()

    def _close_open_entries(self, cutoff_dt: dt.datetime) -> int:
        closed = []
        with self.store.log_lock:
            changed = False
            for student_id, sessions in self.store.log_data.items():
                if not sessions:
                    continue
                last = sessions[-1]
                if "entry" in last and "exit" not in last:
                    entry_str = last["entry"]
                    last["exit"] = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

                    hours = None
                    try:
                        s = dt.datetime.strptime(entry_str, "%Y-%m-%d %H:%M:%S")
                        hours = round((cutoff_dt - s).total_seconds() / 3600.0, 2)
                    except Exception:
                        pass

                    info = self.store.student_map.get(student_id, {})
                    closed.append({
                        "name": info.get("name", "不明"),
                        "student_id": student_id,
                        "entry": entry_str,
                        "exit": last["exit"],
                        "hours": hours,
                    })
                    changed = True

            if changed:
                dump_json(self.store.cfg.log_file, self.store.log_data)

        if closed:
            self.gui.log_threadsafe(
                f"21:00自動退室: {len(closed)} 件を exit={cutoff_dt.strftime('%Y-%m-%d %H:%M:%S')} で記録"
            )

            lines = [f":bell: 21:00自動退室 {len(closed)}件（{cutoff_dt.strftime('%Y-%m-%d %H:%M')}）"]
            for c in closed:
                if c["hours"] is not None:
                    lines.append(
                        f"- {c['name']} {c['entry']} → {c['exit']} 〔{c['hours']}h〕"
                    )
                else:
                    lines.append(
                        f"- {c['name']} {c['entry']} → {c['exit']}"
                    )

            self.notifier.post("\n".join(lines))
        else:
            self.gui.log_threadsafe("21:00自動退室: 対象なし")
        return len(closed)

    def run(self):
        while not self.stop.is_set():
            wait_s = max(0.0, self._seconds_until_next_21())
            if self.stop.wait(wait_s):
                break

            now = dt.datetime.now()
            cutoff_dt = now.replace(hour=21, minute=0, second=0, microsecond=0)
            self._close_open_entries(cutoff_dt)


# =========================
# 週次集計・送信
# =========================
class WeeklySender(threading.Thread):
    def __init__(self, cfg: Config, store: Store, gui: GUIApp, stop: Event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.store = store
        self.gui = gui
        self.stop = stop

    @staticmethod
    def calc_weekly_total_hours(sessions: list[dict], week_start: dt.datetime, week_end: dt.datetime) -> float:
        total_sec = 0
        for ses in sessions:
            if "entry" in ses and "exit" in ses:
                try:
                    s = dt.datetime.strptime(ses["entry"], "%Y-%m-%d %H:%M:%S")
                    e = dt.datetime.strptime(ses["exit"], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue
                total_sec += overlap_seconds(s, e, week_start, week_end)
        return round(total_sec / 3600.0, 2)

    def build_week_payload_for_student(self, student_id: str, week_start: dt.datetime, week_end: dt.datetime):
        sessions = self.store.log_data.get(student_id, [])
        total_hours = self.calc_weekly_total_hours(sessions, week_start, week_end)
        if total_hours <= 0:
            return None
        return {
            "student_id": student_id,
            "entry_time": week_start.strftime("%Y-%m-%d"),
            "exit_time": week_end.strftime("%Y-%m-%d"),
            "total_hours": total_hours,
        }

    def mark_sent(self, payload: dict[str, list[dict]]):
        for week_key, arr in payload.items():
            for obj in arr:
                student_id = obj.get("student_id")
                if not student_id:
                    continue
                m = self.store.weekly_sent.get(student_id, {})
                m[week_key] = obj.get("total_hours", 0.0)
                self.store.weekly_sent[student_id] = m
        self.store.save_weekly_sent()

    def post_weekly_payload(self, payload: dict[str, list[dict]]):
        if not payload:
            return
        try:
            resp = requests.post(self.cfg.weekly_post_url, json=payload, timeout=10)
            resp.raise_for_status()
            self.mark_sent(payload)
            self.gui.log_threadsafe(f"[weekly] 送信完了: {list(payload.keys())}")
        except Exception as e:
            self.gui.log_threadsafe(f"[weekly] 送信失敗: {e}")

    def build_pending_weeks_payload(self, now_dt: dt.datetime):
        earliest: dt.datetime | None = None
        for sessions in self.store.log_data.values():
            for ses in sessions:
                for key in ("entry", "exit"):
                    if key in ses:
                        try:
                            t = dt.datetime.strptime(ses[key], "%Y-%m-%d %H:%M:%S")
                            earliest = t if earliest is None else min(earliest, t)
                        except Exception:
                            pass

        if earliest is None:
            return {}

        # 週の開始を日曜始まりに合わせる
        earliest_week_start, _ = sunday_range(earliest)
        # iter_fiscal_weeks には十分先の上限を渡す（切り捨てない）
        # 送信スキップの判定は下記 week_end + 1秒 > now_dt で行う
        far_future = now_dt + dt.timedelta(days=365)

        payload: dict[str, list[dict]] = {}

        for week_start, week_end in iter_fiscal_weeks(earliest_week_start, far_future):
            # 週がまだ終わっていない（送信タイミングが未到達）ならスキップ
            if week_end + dt.timedelta(seconds=1) > now_dt:
                break  # 以降の週もまだ終わっていないので break

            week_key = week_start.strftime("%Y-%m-%d")

            for student_id in list(self.store.log_data.keys()):
                last_map = self.store.weekly_sent.get(student_id, {})
                if last_map.get(week_key) is not None:
                    continue
                obj = self.build_week_payload_for_student(student_id, week_start, week_end)
                if obj:
                    payload.setdefault(week_key, []).append(obj)

        return payload

    def read_last_run_marker(self) -> dt.datetime | None:
        try:
            with open(self.cfg.weekly_marker_file, "r", encoding="utf-8") as f:
                return dt.datetime.fromisoformat(f.read().strip())
        except Exception:
            return None

    def write_last_run_marker(self, when: dt.datetime):
        with open(self.cfg.weekly_marker_file, "w", encoding="utf-8") as f:
            f.write(when.isoformat())

    def run(self):
        while not self.stop.is_set():
            now = dt.datetime.now()

            # 終了済みの未送信週をまとめて送信する
            # build_pending が「週が終わっているか」「未送信か」を判断する
            pending = self.build_pending_weeks_payload(now)
            if pending:
                self.post_weekly_payload(pending)

            self.stop.wait(30)


# =========================
# カード監視
# =========================
class CardWatcher(threading.Thread):
    def __init__(self, cfg: Config, store: Store, notifier: Notifier, sound: SoundPlayer, gui: GUIApp, stop: Event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.store = store
        self.notifier = notifier
        self.sound = sound
        self.gui = gui
        self.stop = stop

    def run(self):
        self.gui.log_threadsafe("FeliCa 学籍番号読取モードで待機中")

        # 重複読み取りガード用の状態
        # 同一カードIDが duplicate_guard_seconds 以内に再度来たらスキップ
        last_student_id: str | None = None
        last_processed_at: float = 0.0

        service_code = self.cfg.service_code

        # --- リトライループ ---
        # USB切断・ハードウェアエラーが発生してもスレッドが死なずに自動復旧する。
        while not self.stop.is_set():
            clf: nfc.ContactlessFrontend | None = None
            try:
                clf = nfc.ContactlessFrontend('usb')
                self.gui.log_threadsafe("NFC リーダー接続完了")

                # --- カード読み取りループ ---
                while not self.stop.is_set():
                    detected: dict[str, str | None] = {"student_id": None}

                    def connected(tag, _detected=detected):
                        # クロージャの意図を明示するためデフォルト引数で detected を束縛する
                        if isinstance(tag, nfc.tag.tt3.Type3Tag):
                            try:
                                svcd = nfc.tag.tt3.ServiceCode(service_code >> 6, service_code & 0x3f)
                                blcd = nfc.tag.tt3.BlockCode(0, service=0)
                                block_data = tag.read_without_encryption([svcd], [blcd])
                                _detected["student_id"] = str(block_data[1:8].decode("utf-8"))
                            except Exception as e:
                                self.gui.log_threadsafe(f"カード読み取りエラー: {e}")
                        else:
                            self.gui.log_threadsafe("エラー: FeliCa (Type3Tag) 以外のカードです")
                        # True を返すとカードが離れるまで on-connect を1回だけ呼ぶ
                        return True

                    clf.connect(rdwr={"on-connect": connected})
                    # ↑ ここでカードが検出されるまでブロック。
                    #   sleep不要 — clf.connect が次のカードまで待ってくれる。

                    student_id = detected["student_id"]
                    if not student_id:
                        # Type3Tag以外など読み取り失敗。即座に次のループへ。
                        continue

                    now_ts = time.monotonic()

                    # --- 重複読み取りガード ---
                    # 同一カードが duplicate_guard_seconds 以内に来たら無視する。
                    # 異なるカードは間隔に関わらず即処理する。
                    if (student_id == last_student_id
                            and (now_ts - last_processed_at) < self.cfg.duplicate_guard_seconds):
                        continue

                    last_student_id = student_id
                    last_processed_at = now_ts
                    # -------------------------

                    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                    with self.store.student_map_lock:
                        info = self.store.student_map.get(student_id)
                    if not info:
                        self.gui.set_user_threadsafe(student_id, "不明")
                        reg = self.gui.prompt_registration_threadsafe(student_id)
                        if reg:
                            with self.store.student_map_lock:
                                self.store.student_map[student_id] = {
                                    "student_id": student_id,
                                    "name": reg["name"],
                                }
                            self.store.save_student_map()
                            self.gui.log_threadsafe(f"新規登録: {student_id} / {reg['name']}")
                            with self.store.student_map_lock:
                                info = self.store.student_map[student_id]
                        else:
                            self.gui.log_threadsafe("登録をキャンセルしました")
                            info = {"student_id": student_id, "name": "不明"}

                    name = info.get("name", "不明")
                    self.gui.set_user_threadsafe(student_id, name)

                    with self.store.log_lock:
                        sessions = self.store.log_data.setdefault(student_id, [])

                        if not sessions or "exit" in sessions[-1]:
                            sessions.append({"entry": now_str})
                            entry = True
                        else:
                            sessions[-1]["exit"] = now_str
                            try:
                                dt_entry = dt.datetime.strptime(sessions[-1]["entry"], "%Y-%m-%d %H:%M:%S")
                                dt_exit = dt.datetime.strptime(sessions[-1]["exit"], "%Y-%m-%d %H:%M:%S")
                                hours = round((dt_exit - dt_entry).total_seconds() / 3600.0, 2)
                            except Exception:
                                hours = 0.0
                            entry = False

                    if entry:
                        self.gui.status_in_threadsafe()
                        self.sound.play("entry")
                        self.gui.log_threadsafe(f"{now_str} ▶ 入室 - {name}")
                        self.notifier.post(f"{now_str} {name}さんが入室しました :tada:")
                    else:
                        self.gui.status_out_threadsafe(hours)
                        self.sound.play("exit")
                        self.gui.log_threadsafe(f"{now_str} ◀ 退室 - {name}")
                        self.notifier.post(f"{now_str} {name}さんが退出しました :wave:")

                    self.store.save_log()

            except Exception as e:
                # USB切断・ハードウェア障害など予期せぬエラー。
                # GUIに警告を表示し、5秒後に clf を再初期化してリトライする。
                self.gui.log_threadsafe(f"[警告] CardWatcher エラー（5秒後に再接続します）: {e}")
                self.gui.show_reader_error_threadsafe()
            finally:
                # clf が開いていれば必ずクローズしてリソースを解放する
                if clf is not None:
                    try:
                        clf.close()
                    except Exception:
                        pass

            # stop が立っていれば即終了、そうでなければ 5 秒待って再試行
            if self.stop.wait(5):
                break

        self.gui.log_threadsafe("CardWatcher スレッド終了")


# =========================
# エントリポイント
# =========================
def main():
    store = Store(CFG)
    gui = GUIApp()
    notifier = Notifier(CFG, gui)

    stop_evt = Event()

    weekly = WeeklySender(CFG, store, gui, stop_evt)
    sound = SoundPlayer(CFG, gui)
    watcher = CardWatcher(CFG, store, notifier, sound, gui, stop_evt)
    daily = DailyCloser(CFG, store, notifier, gui, stop_evt)

    daily.start()
    weekly.start()
    watcher.start()

    gui.run(stop_evt)


if __name__ == "__main__":
    main()