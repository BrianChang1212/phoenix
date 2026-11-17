# 本機對話錄製接收器

## Kiro 完整 Session 同步（2026-11-16）

完整模式使用獨立的 `kiro-session-history` 專案，保存選定回合結束前的 Session 可見事件。它與下方 START／STOP 範圍錄製不同，不把歷史內容偽裝成即時 capture。

在目前 Kiro 對話明確呼叫 `phoenix-recording`，並要求「完整 Session」或「全部可見步驟」。控制使用 `FULLSTART <request UUID> <task> RECEIPT <nonce>`、`FULLSTATUS RECEIPT <nonce>`、`FULLSTOP RECEIPT <nonce>`，前綴仍為 `Phoenix recording:`，回條讀取器沿用原入口。Session 身分由原生 hook 提供，不能手填其他對話。

FULLSTOP 停止訂閱後續回合，但保留當前最後 Say／observed turn_end 的同步。Hook 將工作交給 Phoenix 服務持有的有界佇列，不依賴 CLI 子程序持續存活；自然 Stop 再交接最後工作。每次交接刷新 30 秒閒置期限，同一工作有固定兩小時硬期限，最多 128 筆佇列工作；逾時或服務重啟後可由 FULLSTATUS 重新交接原範圍。

`subscribed` 代表授權訂閱，`sync_pending` 代表尚待同步。只有回執同時為 `state=imported`、`storage=verified`、`coverage.complete=true`、`gaps=[]`、`sync_pending=false`，才表示選定來源範圍的支援可見步驟已通過主資料庫讀回。最後回覆尚未結束時，不可能在同一則回覆內宣稱它已保存；應由後續 FULLSTATUS 或外部回條讀取確認。

匯入 API 為 `/v1/recordings/kiro-session/import`，只接受 Session ID、任務標籤與選用 `through_turn_id`。`/queue` 與 `/queue/status` 使用相同欄位且必須指定完成回合邊界。三個入口都限定 loopback、拒絕瀏覽器 Origin，不接受任意檔案路徑或 OTLP spans；公開 `/v1/traces` 仍套用原 managed RecordingGate。

來源由固定本機 Kiro store 查找並核對 metadata、檔案身分與連結邊界。`through_turn_id` 截取到相符 observed turn_end 的原始 JSONL prefix；後續查詢回合不會擴大 FULLSTOP 的範圍。相同來源 prefix 與 schema 版本使用固定 import／span IDs，重試不重複；後續完成回合形成新的 immutable Session 快照。回執保留原 Session ID、SHA-256、最後 record ID、來源／工具／步驟數與缺漏。

Sub-agent（schema 5）：`sub_agent_start`／`sub_agent_complete` 成對成為一個 `Sub-agent: <名稱>` span（名稱上限 80 字元，附說明、prompt、response、狀態），掛在發起它的回合之下；`sub-executions/<subSessionId>.jsonl` 內該 sub-agent 自己的 Hook、工具、助理步驟以同一套規則對應並巢狀在其下，樹多一層。`subSessionId` 必須是嚴格 UUID 才會組成路徑，檔案須是 Session 目錄內的一般檔案；不存在才記缺漏碼 `sub_execution_missing`，符號連結、逃出目錄、讀取中被改動或超過上限一律拒絕整個匯入。位元組與列數上限橫跨主檔與所有 sub 檔，SHA-256 涵蓋 sub 檔內容；`through_turn_id` 邊界同時裁切 sub 檔列。只有 start 或只有 complete 記 `unpaired_sub_agent`，時間倒置記 `invalid_sub_agent_interval`；仍在執行的 sub-agent 不標成完成。回合與 sub-agent 的時間窗一律包含其所有子 span（沒有 `turn_end` 的開放回合，終點跟隨最後一筆活動並保留 `open_turn`，不宣稱完成），子 span 的時間不改。大型匯入的讀回期限為 `min(60, 5 + span 數 / 100)` 秒，固定 5 秒曾使約 2500 個 span 的匯入被誤報 `storage_not_verified`。仍在進行的 Session 因 `open_turn`、`unverified_turn_boundary` 與超過 16 KB 的內容截斷，匯入仍會標示不完整。

支援使用者訊息、助理 Say、工具 call/result、權限互動、Hook 名稱及回合／Session 狀態；工具 call/result 合成一次操作並保留雙方來源 ID、行號與時間。Reasoning、agent_note、steering 與隱藏上下文不匯出。只有來源完成快照時間的步驟畫成時間點，不能當作零耗時或反推出開始時間。Shell 結果以明確 exit code 為準，工具完成不等同業務成功。

來源上限為 32 MiB、50,000 列、每列 1 MiB；單項文字上限 16 KiB，匯入最多 10,000 spans／8 MiB。未知格式、截斷、缺失結果或未完成回合須標示不完整；來源損壞或超限也可能拒絕同步。這不承諾任意 Kiro 版本、外部背景程序或私有模型內部活動全部可觀測。

使用 `make test-kiro-session-history` 驗證 reader、API、queue，`make test-recording-gate` 驗證原 capture／讀回契約。本機使用 Phoenix venv 時可指定 `SESSION_FRONTEND_PYTHON`。

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
