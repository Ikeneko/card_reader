"""
FeliCa 入退室システム（整理版）
- 環境変数で Slack TOKEN/CHANNEL を管理
- 週次送信とカード監視をクラス化
- スレッドの安全な停止（終了処理）
- 例外処理とログ整備
- GUI 初期化と中央配置は現行踏襲
"""
from __future__ import annotations

import os
import sys
import json
import time
import threading
import datetime as dt
import subprocess
from dataclasses import dataclass
from threading import Event, Lock

# GUI / カードリーダ
import tkinter as tk
from tkinter import messagebox
from smartcard.System import readers
from smartcard.util import toHexString
from smartcard.Exceptions import NoCardException

# 通信
import requests

# =========================
# 設定
# =========================
@dataclass(frozen=True)
class Config:
    # Slack
    slack_token: str = os.environ.get("SLACK_BOT_TOKEN", "xxxxx")
    slack_channel: str = os.environ.get("SLACK_CHANNEL", "xxxxx")
    slack_url: str = os.environ.get("SLACK_POST_URL", "https://slack.com/api/chat.postMessage")

    # エンドポイント（週次送信）
    weekly_post_url: str = os.environ.get("WEEKLY_POST_URL", "http://localhost:5001/card_entry")

    # ファイル
    log_file: str = os.environ.get("LOG_FILE", "entry_log.json")
    uid_map_file: str = os.environ.get("UID_MAP_FILE", "uid_to_name.json")
    weekly_sent_file: str = os.environ.get("WEEKLY_SENT_FILE", "weekly_sent.json")
    weekly_marker_file: str = os.environ.get("WEEKLY_LAST_RUN_FILE", "weekly_last_run.txt")

    # サウンド設定（任意のWAVファイルを指定。未指定ならビープ音にフォールバック）
    entry_sound_path: str = os.environ.get("ENTRY_SOUND", "")
    exit_sound_path: str = os.environ.get("EXIT_SOUND", "")

    # カード UID 取得 APDU（FeliCa/PCSC 一般的）
    apdu_get_uid: tuple[int, int, int, int, int] = (0xFF, 0xCA, 0x00, 0x00, 0x00)


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
    # Python weekday: Mon=0..Sun=6
    days_until_sun = (6 - now.weekday()) % 7
    target = (now + dt.timedelta(days=days_until_sun)).replace(hour=0, minute=0, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=7)
    return target


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
        self.log_data: dict[str, list[dict]] = load_json(cfg.log_file, {})
        self.uid_map: dict[str, dict] = load_json(cfg.uid_map_file, {})
        self.weekly_sent: dict[str, dict] = load_json(cfg.weekly_sent_file, {})  # {uid: {"YYYY-MM-DD": hours}}

    def save_log(self) -> None:
        with self.log_lock:
            dump_json(self.cfg.log_file, self.log_data)

    def save_weekly_sent(self) -> None:
        dump_json(self.cfg.weekly_sent_file, self.weekly_sent)

    # 便宜関数
    def resolve_student_id(self, uid: str | None) -> str | None:
        if not uid:
            return None
        info = self.uid_map.get(str(uid).strip().upper())
        return info.get("student_id") if info else None


# =========================
# Slack 通知
# =========================
class Notifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def post(self, text: str) -> None:
        if not self.cfg.slack_token:
            return  # トークン未設定時は通知をスキップ
        try:
            headers = {"Authorization": f"Bearer {self.cfg.slack_token}"}
            data = {"channel": self.cfg.slack_channel, "text": text}
            requests.post(self.cfg.slack_url, headers=headers, data=data, timeout=10)
        except Exception:
            pass  # 通知失敗は致命的ではないため無視


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
            # macOS
            if sys.platform == "darwin":
                subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            # Linux (Raspberry Pi)
            for cmd in ("aplay", "paplay", "play"):
                try:
                    subprocess.Popen([cmd, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return
                except Exception:
                    continue
        except Exception:
            pass
        # 失敗時はビープ
        self._beep("fallback")

    def _beep(self, kind: str):
        try:
            if sys.platform.startswith("win"):
                try:
                    import winsound
                    # 種別で周波数を少し変える
                    if kind == "entry":
                        winsound.Beep(880, 120)
                    elif kind == "exit":
                        winsound.Beep(660, 120)
                    else:
                        winsound.MessageBeep()
                    return
                except Exception:
                    pass
            # Tk のベル（クロスプラットフォーム）
            if hasattr(self.gui, "root"):
                self.gui.root.bell()
        except Exception:
            pass


# =========================
# 21:00 一括 exit 記録スレッド（Slack通知つき）
# =========================
class DailyCloser(threading.Thread):
    """
    毎日21:00になったら、exitが未設定のレコードに exit=その日の21:00 を記録。
    その結果をSlackに1件のメッセージで通知する。
    """
    def __init__(self, cfg: Config, store: Store, notifier: Notifier, gui: "GUIApp", stop: Event):
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
        """
        exit未設定の最後のセッションに exit=cutoff_dt を記録。
        Slackへまとめて通知。
        """
        closed = []  # 通知用のまとめ
        with self.store.log_lock:
            changed = False
            for uid, sessions in self.store.log_data.items():
                if not sessions:
                    continue
                last = sessions[-1]
                if "entry" in last and "exit" not in last:
                    entry_str = last["entry"]
                    last["exit"] = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

                    # 通知用に在室時間を計算（失敗時は None）
                    hours = None
                    try:
                        s = dt.datetime.strptime(entry_str, "%Y-%m-%d %H:%M:%S")
                        hours = round((cutoff_dt - s).total_seconds() / 3600.0, 2)
                    except Exception:
                        pass

                    info = self.store.uid_map.get(uid, {})
                    closed.append({
                        "name": info.get("name", "不明"),
                        "student_id": info.get("student_id", "不明"),
                        "entry": entry_str,
                        "exit": last["exit"],
                        "hours": hours,
                    })
                    changed = True

            if changed:
                dump_json(self.store.cfg.log_file, self.store.log_data)

        if closed:
            self.gui.log(f"21:00自動退室: {len(closed)} 件を exit={cutoff_dt.strftime('%Y-%m-%d %H:%M:%S')} で記録")

            # Slack メッセージを1通にまとめる
            lines = [f":bell: 21:00自動退室 {len(closed)}件（{cutoff_dt.strftime('%Y-%m-%d %H:%M')}）"]
            for c in closed:
                if c["hours"] is not None:
                    lines.append(f"- {c['name']}（{c['student_id']}） {c['entry']} → {c['exit']} 〔{c['hours']}h〕")
                else:
                    lines.append(f"- {c['name']}（{c['student_id']}） {c['entry']} → {c['exit']}")

            # 通知
            try:
                self.notifier.post("\n".join(lines))
            except Exception:
                pass
        else:
            self.gui.log("21:00自動退室: 対象なし")
        return len(closed)

    def run(self):
        while not self.stop.is_set():
            # 次の21:00まで待機
            wait_s = max(0.0, self._seconds_until_next_21())
            if self.stop.wait(wait_s):
                break  # 終了指示

            # 21:00 到達。少し遅延があっても当日の 21:00 を明示
            now = dt.datetime.now()
            cutoff_dt = now.replace(hour=21, minute=0, second=0, microsecond=0)
            self._close_open_entries(cutoff_dt)


# =========================
# 週次集計・送信
# =========================
class WeeklySender(threading.Thread):
    def __init__(self, cfg: Config, store: Store, stop: Event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.store = store
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

    def build_week_payload_for_uid(self, uid: str, week_start: dt.datetime, week_end: dt.datetime):
        sessions = self.store.log_data.get(uid, [])
        total_hours = self.calc_weekly_total_hours(sessions, week_start, week_end)
        if total_hours <= 0:
            return None
        student_id = self.store.resolve_student_id(uid)
        if not student_id:
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
                # 逆引き（student_id -> uid）
                uid = next((k for k, v in self.store.uid_map.items() if v.get("student_id") == student_id), student_id)
                m = self.store.weekly_sent.get(uid, {})
                m[week_key] = obj.get("total_hours", 0.0)
                self.store.weekly_sent[uid] = m
        self.store.save_weekly_sent()

    def post_weekly_payload(self, payload: dict[str, list[dict]]):
        if not payload:
            return
        try:
            requests.post(self.cfg.weekly_post_url, json=payload, timeout=10)
            self.mark_sent(payload)
            print(f"[weekly] 送信完了: {list(payload.keys())}")
        except Exception as e:
            print("[weekly] 送信失敗:", e)

    def build_pending_weeks_payload(self, now_dt: dt.datetime):
        # ログ中の最古日
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

        earliest_week_start, _ = sunday_range(earliest)
        latest_candidate = now_dt - dt.timedelta(seconds=1)
        latest_week_start, _ = sunday_range(latest_candidate)

        payload: dict[str, list[dict]] = {}
        cur = earliest_week_start
        while cur <= latest_week_start:
            week_start = cur
            week_end = week_start + dt.timedelta(days=6, hours=23, minutes=59, seconds=59)
            week_key = week_start.strftime("%Y-%m-%d")

            # 🔒 起動直後の未送信送出では、「その週の送信タイミングである日曜0:00」を過ぎていない週は除外
            #    → 定刻（日曜0:00）に送る週（直前週）をここで誤送しない
            sunday_after_week = week_start + dt.timedelta(days=7)  # 翌週日曜0:00
            if sunday_after_week >= now_dt:
                cur += dt.timedelta(days=7)
                continue

            for uid in list(self.store.log_data.keys()):
                last_map = self.store.weekly_sent.get(uid, {})
                if last_map.get(week_key) is not None:
                    continue
                obj = self.build_week_payload_for_uid(uid, week_start, week_end)
                if obj:
                    payload.setdefault(week_key, []).append(obj)
            cur += dt.timedelta(days=7)
        return payload

    # マーカーの読み書き
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
        last_run = self.read_last_run_marker()
        while not self.stop.is_set():
            now = dt.datetime.now()
            # 起動直後/復帰時の未送信分を送る
            pending = self.build_pending_weeks_payload(now)
            if pending:
                self.post_weekly_payload(pending)
                # 処理した中で最も新しい「送信対象週の翌日曜0:00」をマーカーにする
                try:
                    latest_sun0 = max(
                        dt.datetime.fromisoformat(k + "T00:00:00") + dt.timedelta(days=7)
                        for k in pending.keys()
                    )
                    self.write_last_run_marker(latest_sun0)
                    last_run = latest_sun0
                except Exception:
                    pass

            # 定刻（日曜0:00）
            next_sun0 = next_sunday_zero(now)
            should_run = (now >= next_sun0 and (last_run is None or last_run < next_sun0))
            if should_run:
                week_start = next_sun0 - dt.timedelta(days=7)
                week_end = week_start + dt.timedelta(days=6, hours=23, minutes=59, seconds=59)
                week_key = week_start.strftime("%Y-%m-%d")

                payload: dict[str, list[dict]] = {}
                for uid in list(self.store.log_data.keys()):
                    if self.store.weekly_sent.get(uid, {}).get(week_key) is not None:
                        continue
                    obj = self.build_week_payload_for_uid(uid, week_start, week_end)
                    if obj:
                        payload.setdefault(week_key, []).append(obj)
                if payload:
                    self.post_weekly_payload(payload)
                self.write_last_run_marker(next_sun0)
                last_run = next_sun0

            # ポーリング間隔
            self.stop.wait(30)


# =========================
# カード監視
# =========================
class CardWatcher(threading.Thread):
    def __init__(self, cfg: Config, store: Store, notifier: Notifier, sound: "SoundPlayer", gui: "GUIApp", stop: Event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.store = store
        self.notifier = notifier
        self.sound = sound
        self.gui = gui
        self.stop = stop

    def run(self):
        rlist = readers()
        if not rlist:
            self.gui.log("カードリーダーが見つかりません")
            return
        reader = rlist[0]
        self.gui.log(f"リーダー：{reader}")

        while not self.stop.is_set():
            try:
                conn = reader.createConnection()
                conn.connect()
                response, sw1, sw2 = conn.transmit(list(self.cfg.apdu_get_uid))
                uid = toHexString(response)
                now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                info = self.store.uid_map.get(uid, {})
                name = info.get("name", "不明")
                student_id = info.get("student_id", "不明")
                self.gui.set_user(student_id, name)

                # 入室 or 退室
                sessions = self.store.log_data.setdefault(uid, [])
                if not sessions or "exit" in sessions[-1]:
                    # 入室
                    sessions.append({"entry": now})
                    self.gui.status_in()
                    self.sound.play("entry")
                    self.gui.log(f"{now} ▶ 入室 - {name}")
                    self.notifier.post(f"{now} {name}さんが入室しました :tada:")
                else:
                    # 退室
                    sessions[-1]["exit"] = now
                    try:
                        dt_entry = dt.datetime.strptime(sessions[-1]["entry"], "%Y-%m-%d %H:%M:%S")
                        dt_exit = dt.datetime.strptime(sessions[-1]["exit"], "%Y-%m-%d %H:%M:%S")
                        hours = round((dt_exit - dt_entry).total_seconds() / 3600.0, 2)
                    except Exception:
                        hours = 0.0
                    self.gui.status_out(hours)
                    self.sound.play("exit")
                    self.gui.log(f"{now} ◀ 退室 - {name}")
                    self.notifier.post(f"{now} {name}さんが退出しました :wave:")

                self.store.save_log()
                time.sleep(2)

            except NoCardException:
                time.sleep(1)
            except Exception as e:
                self.gui.log(f"エラー: {e}")
                time.sleep(1)


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

        self.lbl_name = tk.Label(center, text="氏名：", font=("Arial", 18))
        self.lbl_name.pack(pady=10)

        self.lbl_status = tk.Label(center, text="", font=("Arial", 24, "bold"), fg="green")
        self.lbl_status.pack(pady=20)

        self.txt_log = tk.Text(center, height=10, width=60, state="disabled")
        self.txt_log.pack(pady=10)

    def toggle_fullscreen(self, event=None):
        self.root.attributes("-fullscreen", not self.root.attributes("-fullscreen"))

    def set_user(self, student_id: str, name: str):
        self.lbl_student.config(text=f"学籍番号：{student_id}")
        self.lbl_name.config(text=f"氏名：{name}")

    def status_in(self):
        self.lbl_status.config(text="入室しました", fg="green")

    def status_out(self, hours: float):
        self.lbl_status.config(text=f"退室しました\n在室時間: {hours:.2f} 時間", fg="blue")

    def log(self, msg: str):
        self.txt_log.config(state="normal")
        self.txt_log.insert(tk.END, f"{msg}\n")
        self.txt_log.see(tk.END)
        self.txt_log.config(state="disabled")

    def on_close(self, stop_evt: Event):
        # スレッド停止シグナルを出してから少し待つ
        stop_evt.set()
        self.root.after(200, self.root.destroy)

    def run(self, stop_evt: Event):
        self.root.protocol("WM_DELETE_WINDOW", lambda: self.on_close(stop_evt))
        self.root.mainloop()


# =========================
# エントリポイント
# =========================
def main():
    # 依存チェック（Slack TOKEN 未設定時は通知しないだけで続行する）
    store = Store(CFG)
    notifier = Notifier(CFG)
    gui = GUIApp()

    stop_evt = Event()

    weekly = WeeklySender(CFG, store, stop_evt)
    sound = SoundPlayer(CFG, gui)
    watcher = CardWatcher(CFG, store, notifier, sound, gui, stop_evt)

    daily = DailyCloser(CFG, store, notifier, gui, stop_evt)
    daily.start()
    
    weekly.start()
    watcher.start()

    gui.run(stop_evt)


if __name__ == "__main__":
    main()
