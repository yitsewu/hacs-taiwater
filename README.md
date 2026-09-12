# Taiwan Water HACS

[![Open your Home Assistant instance and open a repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=yitsewu&repository=taiwater-hacs&category=integration)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

在 Home Assistant 查詢台水帳單、用水量與費用。填入水號及戶名即可使用，OCR 在自己的 HA 主機執行，不必另外安裝附加元件或下載模型。

非台水官方整合，適用台灣自來水公司，不適用臺北自來水事業處。

[English](docs/README.en.md) · [使用指南](docs/usage.md) · [更新紀錄](CHANGELOG.md)

## 功能

- 帳單明細、用水量、費用、歷史紀錄及可取得的碳排資料。
- 自動排程、立即查詢最新一期、補抓歷史與重建統計按鈕。
- 上次／下次查詢時間，以及獨立的查詢、OCR、台水驗證及統計狀態。
- 能源面板可用的「台灣自來水公司 總用水量」與費用統計，無須自建 helper 或 YAML。
- 支援多帳戶、自訂顯示名稱及人工驗證備援；查詢失敗保留已保存帳單。

## 安裝

需要 **Home Assistant 2026.9.1 以上**。已驗證 Core 2026.9.1 的 amd64／aarch64；其他版本及 32 位元平台尚未驗證。

> 儲存庫目前為私人開發版，HACS 無法直接下載，請使用下方 ZIP 安裝。公開後即可使用頁首 HACS 按鈕；「HACS Custom」表示自訂儲存庫。

**HACS（儲存庫公開後）**

1. 先安裝 HACS，點頁首按鈕開啟儲存庫；也可在 HACS「自訂儲存庫」加入 `https://github.com/yitsewu/taiwater-hacs`，類型選「整合」。
2. 下載整合，重新啟動 Home Assistant。

**ZIP 安裝**

1. 從 [Releases](https://github.com/yitsewu/taiwater-hacs/releases) 下載 `taiwater-版本.zip`。
2. 將壓縮檔中的完整 `custom_components/taiwater` 資料夾放入 HA 的 `/config/custom_components/`，重新啟動 HA。

更新前先備份 HA；更新後保留既有帳戶與歷史，不需重填水號、戶名。

## 設定

1. 前往「設定 → 裝置與服務 → 新增整合」，搜尋「台灣自來水公司」。
2. 填入水號與戶名，可自訂顯示名稱。首次預設取得原站可提供的全部帳期。
3. 在整合選項調整查詢排程；預設每週一 09:00。
4. 在能源面板的用水設定選擇「台灣自來水公司 總用水量」及對應費用統計。

### 月度分攤

新帳戶預設按帳單涵蓋月份平均分配。例如兩個月一期 **60 度、600 元 → 每月 30 度、300 元**；其他帳期依實際涵蓋月份分配。

這是**帳單分攤估算，不是每月實際抄表量**。進階選項可改為按實際天數分攤。既有帳戶沿用原設定；切換方式後會重建統計，也可按「重建長期統計」重試，歷史 ID 保持不變。

碳排使用原站數值，或自行設定含來源與年份的碳排係數；缺少資料時顯示未知。

### 問題回報

回報問題請到 [Issues](https://github.com/yitsewu/taiwater-hacs/issues)，勿附上水號、戶名、Cookie、驗證碼或原始帳單。

### 資料來源

資料來自台灣自來水公司的[水費查詢網站](https://www.water.gov.tw/ch/EQuery/WaterFeeQuery?nodeId=753)。本整合使用填入的水號與戶名，依網站驗證流程取得帳單明細，再整理為 Home Assistant 的用水量、費用與歷史統計。

## 授權

程式碼採用 [MIT License](LICENSE)。內建 OCR 模型沿用 ddddocr 的 [MIT 授權](custom_components/taiwater/models/LICENSE.ddddocr.txt)，並保留[來源與完整性資訊](custom_components/taiwater/models/source.json)。台水標誌權利歸台灣自來水公司所有，不包含在程式碼的 MIT 授權中。
