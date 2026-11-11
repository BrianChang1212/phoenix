# 本機 Session 時間軸

此功能延伸專案既有的追蹤 Sessions 列表，於 Session 詳情加入 Timeline 頁籤。它不是內建 assistant 的 AgentSession，也不建立第二套 Session 資料庫。

## 資料與來源

Session 的 REST 回應提供 project 與 Trace 關聯，時間軸依序讀完每個 Trace 的 span 分頁，驗證回應後才呈現。錯誤與無法解析的回應不當作完整成功資料。

可識別操作包含 TOOL、LLM、Codex dispatch phase，以及帶用量的 handle_responses。Session envelope、無用量的串流處理與 guardian 觀測不當成使用者操作。摘要只取已記錄的 Kiro 摘要；沒有摘要時明示不可用。原始 span 與 events 保留供輪次、用量及追溯功能使用。

來源與輪次只沿相同 Trace 的 parent span 關聯繼承，未知結果不轉為成功。工具傳輸事件中的 success 不是 shell 退出碼。

Sessions 列表的 Source 選單使用現有 Session DSL 在伺服器端篩選，避免為標記來源而下載所有工作階段：Codex 以 run_turn、Kiro 以 kiro.session 或 kiro.observed_tool_window 作為已觀測 instrumentation 簽章。Unknown 表示這些簽章未被觀測到，不保證不存在其他來源。來源篩選會保留使用者原有條件，分頁仍由既有表格負責。

## 版本與部署限制

2026-11-11 查核 source 與既有 venv 均標示 Phoenix 21.0.0，但 API 與前端可能不完全相同。資料轉接使用已實際查證的 Session／span REST 契約。修改 source 不會自動更新正在執行的 venv 服務；本票不變更正式資料庫、不停止現有服務。

基礎頁籤完成操作選取、完整名稱清單、起訖、耗時、狀態與原始 Trace 入口。平行區塊、耗時環圖、輪次放大、Token 與自動更新由後續票補齊。沒有取得 span 的 Session 顯示沒有操作資料，不補造示範值。

## 驗證紀錄

頁面選取與狀態測試、來源篩選條件保留測試，以及 REST 完整分頁／來源輪次繼承測試合計五項通過。來源篩選在尚未實作時先出現缺少模組的 RED；資料測試另實際抓到模型來源未繼承的 RED，修正後為 GREEN。

整體 TypeScript 檢查目前受尚未建置的 workspace phoenix-client／phoenix-evals 型別影響，錯誤位於既有 evals。新檔案的 lint 通過；真實瀏覽器與正式部署驗收須以後續整合紀錄為準。
