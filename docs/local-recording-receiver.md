# 本機對話錄製接收器

Phoenix 的本機錄製功能只在明確呼叫 start 後收集指定 Codex 或 Kiro 對話的 trace。它不會因為 Phoenix 與 Codex／Kiro 同時開啟，就自動錄下所有對話。

目前的 HTTP 控制端點只接受 loopback 呼叫，並拒絕瀏覽器 `Origin`。這是本機呼叫邊界，不是對 Codex 或 Kiro 執行程序的身分驗證。Codex helper 使用 `CODEX_THREAD_ID` 作為目前對話識別；它不接受使用者自行指定另一個 conversation ID。

## 啟用與持久化

設定 `PHOENIX_RECORDING_ENABLED=true` 並以單一 Phoenix web worker 執行（`WEB_CONCURRENCY=1`）。收據 ledger 使用獨立 SQLite 檔案，不會新增 Phoenix trace 資料表：

- SQLite 資料庫預設將 ledger 放在 `<PHOENIX_WORKING_DIR 或 ~/.phoenix>/recording/receipts.sqlite3`。
- 非 SQLite 資料庫必須設定絕對路徑 `PHOENIX_RECORDING_STATE_DIR`；也可以先設定 `PHOENIX_WORKING_DIR`。
- Ledger 使用檔案鎖避免多個 receiver 同時管理同一份收據，並以資料庫連線字串的雜湊值綁定其 Phoenix 資料庫。路徑、綁定或 schema 不一致時，錄製會 fail closed。
- `PHOENIX_RECORDING_ORIGIN` 可設定 Timeline 網址來源；只接受 loopback HTTP(S)。預設使用 `http://127.0.0.1:<Phoenix port>`。

收據 ledger 只保存 capture 欄位、gap 與 span 身分，不保存原始 span 內容。Phoenix `BulkInserter` 仍是唯一 trace writer。它對 managed spans 設有 10,000 筆、估算保留記憶體 64 MiB，以及 120 秒的佇列上限；超限或逾時的 span 會被丟棄並記錄 gap metric。Lifecycle 預設值為 drain 5 秒、idle lease 30 分鐘、hard limit 2 小時。這些是暫定上限，尚未依實際 Codex／Kiro 事件延遲校準；64 MiB 是保留記憶體估算值，尚未以 live heap 使用量校準。

## 控制 API

所有端點使用 `POST` 與 JSON：

| 端點 | 必要欄位 | 用途 |
| --- | --- | --- |
| `/v1/recordings/start` | `source`、`conversation_id`、`task`、`request_id` | 為目前對話建立具名 capture；重試同一個 start 時沿用 request UUID。 |
| `/v1/recordings/status` | `source`、`conversation_id`；可選 `capture_id` | 查詢此對話的收據，未錄製時回傳 `state=off`。 |
| `/v1/recordings/stop` | `source`、`conversation_id`、`capture_id` | 停止收集並開始有限時間的 drain。Kiro 可另外提供 `observed_event_ids` 與 `manifest_complete`。 |

`source` 限定為 `codex` 或 `kiro`；Codex conversation ID、request ID 與 capture ID 必須是 UUID。`task` 長度為 1–200 字元。只有 Kiro stop manifest 接受事件 ID；清單最多 10,000 筆，每個 ID 最多 64 字元。Kiro hook 沒有共用的 Pre/Post invocation ID，因此接收器不會猜配對；未配對、ambiguous 或 point observation 會保留為 incomplete gap。

## 收據與結果

收據包含 `capture_id`、owner、task、`state`、開始／停止時間、idle／hard／drain deadline、`queued_spans`、`persisted_spans`、`gaps`、`storage`、`session_id`、`project_name`、`timeline_url` 與 `manifest_complete`。

`state` 可能為 `recording`、`draining`、`observed`、`empty`、`incomplete` 或 `failed`；`storage` 可能為 `unverified`、`partial` 或 `verified`。

- Stop 先回傳 `draining`。預設 drain grace 為 15 秒；截止後在共用 5 秒回讀預算內，以新的 primary DB session 每 100 ms 重查預期 `(trace_id, span_id)`、capture provenance、project 與 session。全部驗證後立即結束；預算用完保留最後完整查詢的部分結果與真實 Session 連結；首次查詢即失敗則不產生 Timeline URL。重查不重開接收，也不延長 drain deadline。
- 只有 exact readback 全部成功、storage verified 且沒有 gap，收據才會是 `observed`。空 capture 回傳 `empty`。
- Capture span 上限觸發時停止接收新 span，保留已收到的資料進行 drain/readback，並以 `capture_limit` gap 標為 incomplete。若 Phoenix 已有實際的 Relay project/session ID，仍可提供部分結果的 Timeline URL。
- Phoenix process restart 會把原本 recording/draining 的收據標為 incomplete，不會恢復 span admission；watchdog 可對已記錄的 span 身分做一次有限 readback。
- Token 用量只會來自實際 trace attributes。沒有 token 欄位表示未知，不會推算成零。

配套的 Codex CLI helper 支援 `start`、`status` 與 `stop`。它保留來源 API 的 state/storage/gap 與時間欄位，只接受結構正確的 loopback Phoenix Timeline URL，不會把 unverified 收據包裝成 verified。

## 本機測試與限制

`make test-recording-gate` 執行接收器與 readback unittest；`make check-recording-gate` 檢查 receiver、readback、BulkInserter、Prometheus 及其測試檔。

這些測試使用本機 HTTP fixture 與 SQLite 測試資料庫。另已在隔離 16016／14327 服務，以合成 Codex／Kiro 資料驗證 HTTP 持久化、十組負載與真正瀏覽器 Timeline；未在目前使用中的 Phoenix service、實際 Codex session 或實際 Kiro session 上驗證完整錄製或 Token。隔離合成驗收不得描述為真實客戶端驗收。

隔離 HTTP 負載曾在 5 秒 grace 下只驗證 996／1,000 筆，最後全部寫入；另一次 5 秒 grace 加 5 秒回讀仍只有 939 筆。因此目前調整為 15 秒 grace 加共用 5 秒回讀，需以重測確認適用範圍。實際 Codex export／Kiro hook 尾端延遲分布仍屬 Ticket 04；64 MiB 是 writer 的估計受管佇列上限，並非整個服務 RSS 上限。
