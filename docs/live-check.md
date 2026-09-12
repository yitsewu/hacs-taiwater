# 公開台水查詢狀態

README 的徽章連到獨立 `live-check.yaml` workflow，與離線測試及 HACS 驗證分開。

## 檢查內容

- 每 6 小時執行，排定台灣時間 02:23、08:23、14:23、20:23；GitHub 排程可能延遲。
- 從 GitHub Releases 解析最新正式版 tag，下載該版本的原始碼及 manifest 相依套件，使用 Python 3.14 執行真正的爬蟲與內建 OCR。
- 專用測試帳戶只查最新一期；檢查 OCR 成功、台水驗證接受、帳單存在及用水量／總金額欄位可解析，不回填歷史，不連接個人 HA，也不寫入 Recorder。
- 暫時連線、驗證碼或 OCR 失敗最多重試一次，間隔 15 秒；格式不相容及身分失敗不盲目重試。整個 job 最長 5 分鐘。
- 公開 summary 只有 `status`、固定 `code`、`attempts`、UTC `checked_at`、`version`。沒有帳單金額、水號、戶名、原始 HTML、Cookie、驗證碼或查詢結果 artifact。

## 如何解讀

`passing` 代表最近一次完整查詢成功；`failing` 可能是台水連線／解析失敗，也可能是 GitHub 執行環境、套件下載或 Secrets 設定問題。請點擊徽章，開啟最近一次 run 的 Summary；沒有 probe summary 時代表檢查尚未完成，不能推論台水故障。

GitHub 原生徽章不是即時監測，可能有快取；超過 12 小時沒有新執行結果應視為**未知**。公開 repository 60 天沒有活動時，GitHub 可能自動停用排程；維護者須留意排程狀態，必要時手動重新啟用。不要用無意義自動 commit 保持活動。

## 管理者設定

在此 repository 的 Actions Secrets 設定 `TAIWATER_MONITOR_WATER_ID` 與 `TAIWATER_MONITOR_CUSTOMER_NAME`。帳戶資料只在查詢步驟注入，程式不列印例外內容或保存回應；更改資料後以 Actions → 台水實際查詢 → Run workflow 驗證。

Workflow 僅接受主 repository 的 `main` 上的 schedule／workflow_dispatch，沒有 pull_request 觸發；權限僅 `contents: read`。不要改成執行未審查 PR 程式後再注入 Secrets。

停用時先在 Actions 停用該 workflow，必要時移除上述兩個 Secrets；README 應同步標示未監測。此 workflow 不會修改任何 HA、帳戶設定或帳單。

參考：[GitHub 排程限制](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)、[狀態徽章](https://docs.github.com/en/actions/how-tos/monitor-workflows/add-a-status-badge)。
