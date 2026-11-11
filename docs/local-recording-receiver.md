# 本機按需錄製接收契約

此文件記錄 ticket 01 的 Phoenix 接收端切片。此版本沒有部署至目前服務，也沒有宣告完整錄製或可用 Timeline。

## 啟用與所有權

`create_app(recording_gate=RecordingGate())` 或 `PHOENIX_RECORDING_ENABLED=true` 會啟用單一程序內的閘門。啟動預設不錄製，不還原先前程序的 capture。`WEB_CONCURRENCY` 必須是 `1`。部署端仍須確保實際只有一個 worker，不能只依賴此環境變數。

控制入口只接受 loopback client，拒絕帶 `Origin` 的請求。它提供本機同一使用者的對話隔離，不提供對抗惡意本機程序的驗證。呼叫端須從 runtime 取得目前對話 ID，不能提供任意對話選擇器。Codex 只接受 UUID 對話 ID，worker 整數 thread ID 不作為對話識別。

## HTTP 控制

三個入口都使用 `POST` 與 JSON。`source` 是 `codex` 或 `kiro`。

| 入口 | JSON 欄位 | 結果 |
| --- | --- | --- |
| `/v1/recordings/start` | `source`, `conversation_id`, `task`, `request_id` | 生效後回傳 capture receipt |
| `/v1/recordings/status` | `source`, `conversation_id`, 選用 `capture_id` | receipt 或 `state=off`，另附 `receiver_gaps` |
| `/v1/recordings/stop` | `source`, `conversation_id`, `capture_id` | 停錄後回傳同一 capture 的 receipt |

`task` 長度為 1 至 200 字元。`request_id` 是 UUID。重試開始使用原本的 `request_id`，相同 ID 不建立新 capture。不同任務重用 ID、同一對話已錄製、容量已滿都回傳 `409`。對話不符或 capture 不存在回傳 `404`，未啟用閘門回傳 `503`。重複停止回傳原本的結果。

receipt 是下列平面物件。

```json
{
  "capture_id": "capture UUID",
  "source": "codex",
  "conversation_id": "runtime conversation UUID",
  "task": "任務名稱",
  "state": "recording",
  "started_at_ns": 1794417928645597100,
  "stopped_at_ns": null,
  "queued_spans": 0,
  "gaps": [],
  "session_id": "capture UUID",
  "storage": "unverified",
  "project_name": "task-recordings"
}
```

`state` 可為 `recording`、`incomplete`、`empty` 或 `failed`。`queued_spans` 是已允許進入背景排程的數量，不能視為已完成 SQLite commit。含資料的停止結果固定標示 `incomplete` 與 `storage_unverified`。沒有允許資料的停止結果為 `empty`。

status 的 `receiver_gaps` 是接收端全域計數，例如 `codex:unknown_or_conflicting_owner`。無法歸屬的 span 不分配給任何 capture，也不把缺漏記到同來源的所有對話。未錄製時 status 回傳 `state`、`source`、`conversation_id`、`capture_id=null` 與 `receiver_gaps`。

## OTLP 保存判斷

`/v1/traces` 在建立背景工作之前執行 admission。受管 Codex 來源包括 `service.name` 為 `codex`、`codex_cli`、`codex_cli_rs`、`codex-tui`，或 scope 名稱以 `codex` 開頭。Kiro 來源使用 `service.name=kiro`、Project `kiro-local` 或 `kiro.*` span 屬性。其餘來源維持原流程。

歸屬證據限同一份有界 OTLP request。Codex 使用直接 `conversation.id`，Kiro 使用直接 `session.id`。沒有直接 ID 時只接受可沿 parent 鏈抵達且無衝突的祖先。整份 Trace 不作為對話證據。重複 span ID 的內容衝突、祖先衝突、循環、深度超限與未知歸屬都不保存。祖先晚於該 request 到達時，不回放已捨棄的 child。

開始前已結束的 span 不保存。跨越開始時間的 span 保留原起訖時間，附 `recording.boundary=crosses_start`。停止立即關閉 admission，不提供尾端收尾。已錄過的原始 Trace 與 span ID 不會再加入下一個 capture。

每個 capture 使用獨立 `session.id`，固定 Project 為 `task-recordings`。Trace ID 依 `uuid5(NAMESPACE_URL, "phoenix-recording/{capture_id}/{original_trace_id}")` 重映射；span ID 與 parent ID 保留。`recording.original_*`、`recording.conversation_id`、`recording.task`、`recording.started_at_ns` 保存來源與分段對照。其他來源屬性、原始時間及原生狀態保留。此切片不新增 Token 推估或加總規則。

啟用閘門後，gRPC 逐筆拒收可識別的受管來源，並回傳 OTLP `partial_success.rejected_spans`。非受管來源仍可透過 gRPC 保存。

## 容量界限與待辦

預設 wire body 與解壓後 body 各最多 8 MiB；超出回傳 `413`。每份 request 最多 2,048 spans、祖先深度 128。程序最多保留 128 個 capture receipt，每個 capture 最多 10,000 個去重識別。超出 request span 容量時捨棄該批受管資料；capture 超限時轉 `failed` 並關閉該對話 admission。記憶體不保留原始 span 歷史。

Ticket 03 仍須提供期限、程序異常收尾、持久 receipt、有限尾端與可靠儲存 barrier。`GET /v1/sessions/{capture_id}` 可在 SQLite 確認後取得 Session GlobalID 與 Project GlobalID；此版本不在停錄時產生未驗證的 Timeline 連結。Ticket 04 仍須驗證目前真實對話、實際服務安裝與瀏覽器 Timeline。

## 驗證

`make test-recording-gate` 執行標準函式庫 unittest。測試使用 source `/v1/traces` handler、真實 `BulkInserter` 背景 writer 與獨立磁碟 SQLite。它核對關閉不保存、只有指定對話保存、獨立 Session 與 Project，以及停止後的新 span 不追加。測試產生的 `.audit/recording-*.db` 是本機證據，不納入 Git。

`make check-recording-gate` 檢查所有修改 Python 檔案的 Ruff lint 與格式，並對新增 domain 與控制 router 執行 strict mypy。現有 `app.py` 與 `grpc_server.py` 的完整 typing 在此環境另受選用 `aioboto3` 缺失與 stub 版本影響。

Windows 可將 `SESSION_FRONTEND_PYTHON` 指向既有 Phoenix venv 的 Python，並以 WSL `make` 執行。測試命令使用該 Python 自行解析原生 `src` 路徑，再為測試程序設定 `PYTHONPATH`，呼叫端不須設定此變數。

`check-recording-gate` 沿用 repo 的 `$(UV) run ruff` 與 `$(UV) run mypy`，使用 source 開發環境，不要求 Phoenix runtime venv 安裝檢查工具。WSL 須具備可用的 UV；`scripts/uv.sh` 須以 LF 換行保存，並能取得 `pyproject.toml` 指定的 UV 版本。
