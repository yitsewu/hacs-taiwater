# TaiWater OCR

這個 Home Assistant OS 附加元件在 Supervisor 的內部網路提供台灣自來水 CAPTCHA OCR，供同一台 HAOS 上的 TaiWater custom integration 使用。它使用 Python 3.12、ddddocr 1.5.6 與 Pillow 12.1.0，支援 `amd64` 與 `aarch64`。

## 安裝

HACS 只安裝 custom integration，不會安裝 HAOS 附加元件。請另外在 Home Assistant 的「設定 → 附加元件 → 附加元件商店 → 儲存庫」加入：

```text
https://github.com/yitsewu/taiwan-water
```

重新整理商店後安裝並啟動 **TaiWater OCR**。預設會隨系統啟動；Supervisor watchdog 會檢查 `/health`。

私人開發版或離線安裝：將 OCR ZIP 中的 `taiwater_ocr` 資料夾放入 HAOS 的 `/addons/`（可透過 Samba／SSH 管理），在商店重新檢查更新，從本機附加元件安裝。此方式的 slug 為 Supervisor 指派的本機 slug，整合仍可自動尋找，不必公開 repository。

TaiWater integration 會從 Home Assistant 的已安裝附加元件清單找出唯一以 `_taiwater_ocr` 結尾的完整 slug，並使用 Home Assistant 的 `hostname_from_addon_slug()` 解析內部 hostname。建議保留 OCR URL 空白，讓 integration 自動產生：

```text
http://<Home Assistant 解析出的附加元件 hostname>:8080
```

不要猜測或寫死 repository hash hostname；完整 slug 會依 Supervisor 指派的 repository ID 改變。

## 網路與資料邊界

- `8080/tcp` 的 host port 預設為 `null`，沒有 ingress，也不使用 host network；服務只供 Supervisor 內部網路存取。
- 服務不要求 API token，因為現有 integration 沒有 token 契約，且端點預設不暴露到 HAOS host 或外部網路。
- 請求只包含 CAPTCHA 圖片，不包含水號、姓名、cookie、session 或其他個資。
- 圖片與辨識碼只在處理期間存在 container memory；服務不寫檔、不保存歷史，也不記錄 request、圖片、辨識碼或例外內容。
- 每個 body 上限 2,000,000 bytes，完整讀取期限 10 秒，最多同時處理 2 個請求；OCR engine 只初始化一次並序列化使用。

## HTTP API

`GET /health` 成功時回傳：

```json
{"status":"ok"}
```

`POST /recognize` 接收原始圖片 bytes。成功時回傳 4–8 位英數字：

```json
{"code":"12345"}
```

輸入過大回 `413 payload_too_large`；無效圖片或不合規結果回 `422 ocr_failed`；OCR runtime 不可用回 `503 ocr_unavailable`。所有錯誤只回固定代碼，不包含原始資料或內部例外。

## 本機測試

```bash
python -m unittest discover -s tests -p 'test_ocr_server.py' -v
```

## Docker smoke test

以下 CI smoke test 建置 image、只在 CI loopback 映射 port，並由 `scripts/ocr_smoke.py` 在記憶體產生固定高對比數字 PNG 後呼叫 HTTP 服務。它只驗證 ddddocr 能回傳 4–8 位英數字，不把通用 OCR 對合成字型的精確分類當成準確率測試。

```bash
docker build --build-arg BUILD_VERSION=0.1.0 --build-arg BUILD_ARCH=amd64 -t taiwater-ocr-smoke ./taiwater_ocr
container_id="$(docker run --rm -d -p 127.0.0.1:18080:8080 taiwater-ocr-smoke)"
trap 'docker stop "$container_id" >/dev/null 2>&1 || true' EXIT
python scripts/ocr_smoke.py --url http://127.0.0.1:18080
```
