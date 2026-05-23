# card_reader.py — プログラム仕様
FeliCa カードリーダーを使った学生入退室管理システムの本体です。<br>
tkinter による GUI、Slack 通知、週次集計送信、21:00 自動退室記録を備えています。

## クラス構成
```
main()
├── Store          — JSON ファイルの読み書き・ロック管理
├── GUIApp         — tkinter ウィンドウ（メインスレッド）
├── Notifier       — Slack 通知（非同期）
├── SoundPlayer    — 効果音再生
├── CardWatcher    — FeliCa カード読み取りスレッド
├── DailyCloser    — 21:00 自動退室スレッド
└── WeeklySender   — 週次集計・送信スレッド
```

## 各クラスの詳細
### `Config`
`.env` ファイルと環境変数から設定を読み込みます。<br>
起動時に `load_dotenv()` を呼び出し、その後に環境変数を取得することで `.env` の反映を保証しています。

| 設定キー | デフォルト値 | 説明 |
|---|---|---|
| `SLACK_BOT_TOKEN` | （必須） | Slack Bot Token |
| `SLACK_CHANNEL` | （必須） | 送信先チャンネル ID |
| `WEEKLY_POST_URL` | （必須） | 週次集計の送信先 URL |
| `ENTRY_SOUND` | `""` | 入室時の WAV ファイルパス |
| `EXIT_SOUND` | `""` | 退室時の WAV ファイルパス |
| `FELICA_SERVICE_CODE` | `0x200B` | FeliCa サービスコード（16進数） |
| `DUPLICATE_GUARD_SECONDS` | `2.0` | 同一カードの連続読み取り抑止秒 |
| `READ_INTERVAL_GUARD_SECONDS` | `1.0` | 前回処理からの最小間隔（秒） |
| `LOG_FILE` | `entry_log.json` | 入退室ログファイル名 |
| `STUDENT_MAP_FILE` | `student_map.json` | 学生情報ファイル名 |
| `WEEKLY_SENT_FILE` | `weekly_sent.json` | 週次送信済み記録ファイル名 |
| `WEEKLY_LAST_RUN_FILE` | `weekly_last_run.txt` | 最終実行日時ファイル名 |

---

### `Store`
JSON ファイルをインメモリで保持し、スレッドセーフな読み書きを提供します。

| 属性 | 型 | 内容 |
|---|---|---|
| `log_data` | `dict[student_id, list[session]]` | 入退室ログ |
| `student_map` | `dict[student_id, {name, student_id}]` | 学生情報 |
| `weekly_sent` | `dict[student_id, {week_key: hours}]` | 週次送信済み記録 |

`log_data` の書き込みは `log_lock`、`student_map` の読み書きは `student_map_lock` で保護されています。<br>
ファイルへの書き込みは `.tmp` ファイルに書いてから `os.replace()` するアトミック書き込みで行います。

---

### `GUIApp`
tkinter のフルスクリーン GUI です。メインスレッドでのみ直接操作できます。<br>
他スレッドからは `*_threadsafe()` メソッドを通じて `root.after(0, ...)` 経由で呼び出します。

| メソッド | 説明 |
|---|---|
| `set_user(student_id, name)` | 学籍番号・氏名ラベルを更新 |
| `status_in()` | 「入室しました」表示（緑） |
| `status_out(hours)` | 「退室しました / 在室時間: X.XX 時間」表示（青） |
| `log(msg)` | ログテキストボックスに追記 |
| `prompt_registration(student_id)` | 未登録学生の新規登録ダイアログを表示（メインスレッドのみ） |
| `prompt_registration_threadsafe(student_id)` | 上記をワーカースレッドから呼び出す（`queue.Queue` で結果を受け取る） |

`<Escape>` キーでフルスクリーンを切り替えられます。

---

### `Notifier`
Slack の `chat.postMessage` API を呼び出して通知します。<br>
`post()` を呼ぶとダエモンスレッドを起動して即リターンするため、カード読み取りをブロックしません。

---

### `SoundPlayer`
入退室時の効果音を再生します。

| 優先順位 | 方法 |
|---|---|
| 1 | WAV ファイルが存在する場合は `winsound.PlaySound`（Windows）/ `afplay`（macOS）/ `aplay` 等（Linux） |
| 2 | WAV ファイルがない場合は `winsound.Beep`（Windows）または `root.bell()` でビープ音 |

---

### `CardWatcher`（スレッド）
FeliCa カードを監視し、入退室を記録するメインのスレッドです。

**動作フロー：**
```
clf.connect() でカードを待つ
    ↓
Type3Tag（FeliCa）か確認
    ↓
サービスコード 0x200B でブロック 0 を読み取り → 学籍番号（7バイト）を取得
    ↓
重複読み取りガード（同一カードが duplicate_guard_seconds 以内なら無視）
    ↓
student_map を参照して氏名を取得（未登録なら登録ダイアログを表示）
    ↓
log_data の最後のセッションに exit がなければ退室、あれば入室
    ↓
GUI 更新 / 効果音 / Slack 通知 / ログ保存
```

**重複読み取りガード：**<br>
`time.monotonic()` を使い、同一カード ID が `duplicate_guard_seconds`（デフォルト 2.0 秒）以内に再度検出された場合はスキップします。異なるカードは間隔に関わらず即処理します。

---

### `DailyCloser`（スレッド）
毎日 21:00 に、退室記録のない（`exit` がない）セッションをすべて自動で閉じます。
- `exit` を `"YYYY-MM-DD 21:00:00"` で記録します
- 処理件数と在室時間を Slack に通知します
- 次の 21:00 まで正確にスリープして繰り返します

---

### `WeeklySender`（スレッド）
30 秒ごとに起動し、送信タイミングを迎えた週の集計データを送信します。

**週の区切りルール：**
- 通常は日曜始まり・土曜終わり
- **3/31（年度末）で必ず区切る**（曜日に関わらず）
- **4/1（年度始）から新しい週を開始する**

**送信ロジック（`build_pending_weeks_payload`）：**
1. `log_data` 全体から最も古い記録日時を特定
2. その週（日曜始まり）から現在までの全週を `iter_fiscal_weeks()` で列挙
3. 各週について「週が終わっているか」「未送信か」を確認
4. 条件を満たした週の在室時間を集計して送信

**送信データ形式（JSON）：**
```json
{
  "2025-04-01": [
    {
      "student_id": "学籍番号",
      "entry_time": "2025-04-01",
      "exit_time": "2025-04-05",
      "total_hours": 12.5
    }
  ]
}
```

送信成功後に `weekly_sent.json` に記録し、二重送信を防ぎます。

---

## データファイル仕様
### `entry_log.json`
```json
{
  "学籍番号": [
    { "entry": "2025-04-01 10:00:00", "exit": "2025-04-01 18:30:00" },
    { "entry": "2025-04-02 09:30:00" }
  ]
}
```

- キー：学籍番号（文字列）
- `exit` がないセッションは在室中を意味します

### `student_map.json`
```json
{
  "学籍番号": { "student_id": "学籍番号", "name": "氏名" }
}
```

### `weekly_sent.json`
```json
{
  "学籍番号": { "2025-04-01": 12.5 }
}
```

- キー：学籍番号
- 値のキー：週の開始日（`YYYY-MM-DD`）
- 値の値：送信済みの在室時間（時間）

---

## スレッド構成
| スレッド | クラス | 役割 |
|---|---|---|
| メイン | `GUIApp` | tkinter イベントループ |
| ワーカー 1 | `CardWatcher` | FeliCa 読み取り・入退室記録 |
| ワーカー 2 | `DailyCloser` | 21:00 自動退室 |
| ワーカー 3 | `WeeklySender` | 週次集計・送信 |
| 随時 | `Notifier._send` | Slack HTTP 送信（都度起動） |

すべてのワーカースレッドは `daemon=True` で起動し、`stop_evt`（`threading.Event`）で停止を制御します。<br>
GUI を閉じると `stop_evt.set()` が呼ばれ、全スレッドが終了します。

---

## 依存ライブラリ
| ライブラリ | 用途 |
|---|---|
| `nfcpy` | FeliCa カード読み取り |
| `requests` | Slack API・週次送信の HTTP 通信 |
| `python-dotenv` | `.env` ファイルの読み込み |
| `tkinter` | GUI（Python 標準ライブラリ） |
