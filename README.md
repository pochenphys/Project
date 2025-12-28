# LINE Bot - 食物管理系統

一個整合 LINE Bot、Dify AI 和 MySQL 資料庫的智能食物管理系統，支援食譜推薦、食物記錄、庫存查詢和消耗管理等功能。

## 📋 目錄

- [功能特色](#功能特色)
- [技術棧](#技術棧)
- [環境要求](#環境要求)
- [安裝步驟](#安裝步驟)
- [配置說明](#配置說明)
- [使用方法](#使用方法)
- [專案結構](#專案結構)
- [API 端點](#api-端點)
- [資料庫設定](#資料庫設定)
- [部署說明](#部署說明)
- [注意事項](#注意事項)
- [常見問題](#常見問題)

## ✨ 功能特色

### 🍳 食譜功能
- 上傳食物圖片，AI 自動分析食材
- 提供詳細的食譜步驟和烹飪建議
- 支援多張圖片批次處理
- 生成食譜圖片並以 Image Carousel 方式展示

### 📝 記錄功能
- 上傳食物圖片，自動識別食物名稱
- 記錄食物入庫時間
- 支援數量記錄
- 自動儲存到 MySQL 資料庫

### 🔍 查看功能
- 查看所有食物記錄列表
- 顯示購買時間和已購買天數
- 按時間順序排列（最舊的在前）

### 🗑️ 刪除功能（消耗記錄）
- 按編號刪除特定記錄
- 按食物名稱和數量扣除庫存
- 自動從最舊的記錄開始扣除
- 支援部分扣除（更新數量）

## 🛠️ 技術棧

- **後端框架**: Flask
- **程式語言**: Python 3.8+
- **資料庫**: MySQL (支援 AWS RDS)
- **AI 服務**: 
  - Dify API (工作流模式)
  - Google Gemini API (圖片生成，選用)
- **即時通訊**: LINE Bot API
- **其他依賴**:
  - python-dotenv (環境變數管理)
  - pymysql (資料庫連接)
  - Pillow (圖片處理)
  - requests (HTTP 請求)

## 📦 環境要求

- Python 3.8 或更高版本
- MySQL 5.7+ 或 AWS RDS
- LINE Developer Account
- Dify Account (需要 API Key)
- Google Gemini API Key (選用，用於圖片生成功能)

## 🚀 安裝步驟

### 1. 克隆專案

```bash
git clone <repository-url>
cd Project
```

### 2. 建立虛擬環境（建議）

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/Mac
source venv/bin/activate
```

### 3. 安裝依賴套件

```bash
pip install -r LINE_requirements.txt
```

### 4. 設定環境變數

複製範例環境變數檔案：

```bash
# 複製 Dify API 設定範例
cp Dify.env.example .env

# 建立 LINE Bot 環境變數檔案
touch LINE.env
```

編輯 `LINE.env` 檔案，填入以下資訊：

```env
# LINE Bot 設定
LINE_CHANNEL_ACCESS_TOKEN=your_line_channel_access_token
LINE_CHANNEL_SECRET=your_line_channel_secret

# Dify API 設定
DIFY_API_KEY=your_dify_api_key
DIFY_API_ENDPOINT=https://api.dify.ai

# Google Gemini API 設定（選用，用於圖片生成）
GEMINI_API_KEY=your_gemini_api_key

# MySQL 資料庫設定
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=your_mysql_user
MYSQL_PASSWORD=your_mysql_password
MYSQL_DATABASE=LINE
MYSQL_CHARSET=utf8mb4
MYSQL_CONNECT_TIMEOUT=10

# AWS RDS SSL 設定（選用）
MYSQL_SSL_ENABLED=false
```

### 5. 設定資料庫

建立資料庫和資料表：

```sql
CREATE DATABASE LINE CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE LINE;

CREATE TABLE foods (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(255) NOT NULL,
    food_name VARCHAR(255) NOT NULL,
    quantity FLOAT,
    storage_time DATETIME NOT NULL,
    INDEX idx_username (username),
    INDEX idx_storage_time (storage_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

## ⚙️ 配置說明

### LINE Bot 設定

1. 前往 [LINE Developers Console](https://developers.line.biz/)
2. 建立新的 Provider 和 Channel
3. 取得 **Channel Access Token** 和 **Channel Secret**
4. 設定 Webhook URL: `https://your-domain.com/webhook`

### Dify API 設定

1. 前往 [Dify](https://dify.ai/) 註冊帳號
2. 建立工作流（Workflow）
3. 取得 API Key
4. 設定工作流的輸入變數：
   - `User`: 使用者 ID
   - `foodphoto`: 圖片輸入（支援多張）
   - `text`: 文字輸入（選用）
   - `freshrecord`: 記錄標記（用於記錄功能）

#### 使用 YAML 文件快速建立工作流

專案中提供了預設的 Dify 工作流配置檔案，您可以直接匯入使用：

1. 登入 Dify 控制台
2. 進入「工作流」頁面
3. 點擊「匯入」或「Import」按鈕
4. 選擇專案中的 `食材辨識助手測試 1227三軸.yml` 檔案
5. 確認匯入後，工作流會自動建立並包含以下節點：
   - **食材檢測**: 使用 Gemini 模型分析圖片中的食材
   - **食譜生成**: 根據食材推薦三道食譜
   - **碳足跡與熱量計算**: 計算食材的碳足跡和熱量
   - **圖片提示詞生成**: 為每道菜生成圖片生成提示詞
   - **條件分支**: 處理找不到食材的情況和記錄功能標記

> **注意**: 
> - 匯入後請確認工作流的輸入變數名稱與程式碼中的變數名稱一致
> - 工作流需要包含以下輸出變數：`text`、`picture_1`、`picture_2`、`picture_3`、`dish_1`、`dish_2`、`dish_3`
> - 匯入後請記得發布工作流，否則 API 無法使用

### Google Gemini API 設定（選用）

1. 前往 [Google AI Studio](https://makersuite.google.com/app/apikey)
2. 建立 API Key
3. 將 API Key 填入 `LINE.env` 的 `GEMINI_API_KEY`

> **注意**: 如果不設定 Gemini API Key，食譜功能仍可使用，但不會生成圖片。

## 📱 使用方法

### 啟動服務

```bash
# 啟動主服務（LINE_Bot_Router.py）
python LINE_Bot_Router.py --host 0.0.0.0 --port 5000

# 或使用除錯模式
python LINE_Bot_Router.py --debug
```

### LINE Bot 指令

#### 啟用功能
- **食譜功能**: 輸入「食譜功能」或「食譜」
- **記錄功能**: 輸入「記錄功能」或「記錄」
- **查看功能**: 輸入「查看功能」或「查看」
- **刪除功能**: 輸入「刪除功能」或「刪除」
- **幫助**: 輸入「幫助」或「help」
- **退出**: 輸入「退出」或「exit」

#### 使用流程

**食譜功能**:
1. 輸入「食譜功能」啟用
2. 上傳食物圖片（可一次上傳多張）
3. 系統會分析圖片並提供食譜建議
4. 點選圖片查看詳細食譜內容

**記錄功能**:
1. 輸入「記錄功能」啟用
2. 上傳食物圖片（可一次上傳多張）
3. 系統會自動識別食物名稱和數量
4. 記錄會自動存入資料庫

**查看功能**:
1. 輸入「查看功能」
2. 系統會顯示所有食物記錄
3. 顯示格式：食物名稱、數量、購買時間、已購買天數

**刪除功能**:
1. 輸入「刪除功能」啟用
2. 系統會顯示記錄列表
3. 刪除方式：
   - 按編號刪除：輸入「3」（刪除編號 3 的記錄）
   - 按編號部分扣除：輸入「3 1」（編號 3 扣除數量 1）
   - 按食物名稱扣除：輸入「蘋果 2個」（從最舊的開始扣除）

## 📁 專案結構

```
Project/
├── LINE_Bot_Router.py                    # 主程式（功能路由器）
├── Line2Dify.py                          # LINE 與 Dify 整合模組
├── record.py                             # 食物記錄系統模組
├── LINE_requirements.txt                 # Python 依賴套件清單
├── LINE.env                              # 環境變數檔案（需自行建立，不會上傳到 Git）
├── Dify.env.example                      # Dify API 設定範例
├── 食材辨識助手測試 1227三軸.yml          # Dify 工作流配置檔案（可匯入使用）
├── .gitignore                           # Git 忽略檔案
└── README.md                            # 本檔案
```

### 模組說明

- **LINE_Bot_Router.py**: 主入口程式，負責路由用戶請求到對應功能模組
- **Line2Dify.py**: 提供 LINE Webhook 處理、LINE API 客戶端、Dify API 客戶端等核心功能
- **record.py**: 食物記錄系統，處理圖片上傳、Dify 整合、資料庫操作等功能

## 🔌 API 端點

### Webhook 端點

- **POST** `/webhook`: LINE Bot Webhook 入口
  - 驗證 LINE 簽名
  - 處理用戶訊息和圖片上傳
  - 路由到對應功能模組

### 其他端點

- **GET** `/`: 首頁（顯示系統資訊）
- **GET** `/health`: 健康檢查端點
- **GET** `/temp_image/<image_id>`: 臨時圖片訪問（用於 Image Carousel）

## 💾 資料庫設定

### 資料表結構

**foods 表**:

| 欄位名稱 | 類型 | 說明 |
|---------|------|------|
| id | INT | 主鍵（自動遞增） |
| username | VARCHAR(255) | 使用者 ID（LINE user_id） |
| food_name | VARCHAR(255) | 食物名稱 |
| quantity | FLOAT | 數量（可為 NULL） |
| storage_time | DATETIME | 入庫時間 |

### AWS RDS 支援

專案支援使用 AWS RDS MySQL 資料庫，可在環境變數中啟用 SSL 連接：

```env
MYSQL_SSL_ENABLED=true
```

## 🌐 部署說明

### 本地開發

1. 使用 ngrok 建立 HTTPS 隧道（LINE Webhook 需要 HTTPS）：

```bash
ngrok http 5000
```

2. 將 ngrok 提供的 HTTPS URL 設定到 LINE Developers Console

### 雲端部署（Google Cloud Run）

1. 建立 Dockerfile（參考下方範例）
2. 建立 `.dockerignore` 檔案（排除 `.env` 檔案）
3. 部署到 Cloud Run：

```bash
gcloud run deploy line-bot \
  --source . \
  --platform managed \
  --region asia-east1 \
  --allow-unauthenticated \
  --set-env-vars "LINE_CHANNEL_ACCESS_TOKEN=xxx,LINE_CHANNEL_SECRET=xxx,..."
```

### Dockerfile 範例

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY LINE_requirements.txt .
RUN pip install --no-cache-dir -r LINE_requirements.txt

COPY . .

EXPOSE 5000

CMD ["python", "LINE_Bot_Router.py", "--host", "0.0.0.0", "--port", "5000"]
```

## ⚠️ 注意事項

### 安全建議

1. **絕對不要**將 `LINE.env` 檔案提交到 Git（已在 `.gitignore` 中）
2. 生產環境請使用環境變數或密鑰管理服務
3. 定期更新依賴套件以修補安全漏洞
4. 使用強密碼保護資料庫

### 環境變數優先順序

- Cloud Run / 雲端環境: 環境變數優先
- 本地開發: `LINE.env` 檔案優先

### 限制說明

1. LINE Webhook 必須使用 HTTPS（本地測試需使用 ngrok）
2. 圖片大小建議不超過 5MB（系統會自動調整）
3. 支援的圖片格式：JPEG, PNG, GIF, BMP, WebP
4. Dify 工作流必須已發布才能使用

## ❓ 常見問題

### Q: 如何取得 LINE Channel Access Token？

A: 
1. 前往 [LINE Developers Console](https://developers.line.biz/)
2. 選擇您的 Channel
3. 在「Messaging API」頁籤中找到「Channel access token」
4. 點擊「Issue」產生新的 Token

### Q: Dify 工作流如何設定？

A:
**方法一：使用提供的 YAML 檔案（推薦）**
1. 在 Dify 控制台點擊「匯入」或「Import」
2. 選擇專案中的 `食材辨識助手測試 1227三軸.yml` 檔案
3. 確認匯入後，工作流會自動建立
4. 檢查並發布工作流
5. 取得 API Key 並填入環境變數

**方法二：手動建立工作流**
1. 在 Dify 控制台建立工作流
2. 設定輸入變數：
   - `User`: 文字類型
   - `foodphoto`: 圖片類型（支援多張）
   - `text`: 文字類型（選用）
   - `freshrecord`: 文字類型（選用，用於記錄功能）
3. 設定輸出變數：
   - `text`: 文字輸出（碳足跡與熱量計算結果）
   - `picture_1`、`picture_2`、`picture_3`: 圖片生成提示詞
   - `dish_1`、`dish_2`、`dish_3`: 三道食譜內容
4. 發布工作流
5. 取得 API Key 並填入環境變數

### Q: 資料庫連接失敗怎麼辦？

A:
1. 檢查 MySQL 服務是否運行
2. 確認環境變數設定正確
3. 檢查防火牆設定（雲端資料庫需設定 IP 白名單）
4. 確認資料庫使用者權限

### Q: 圖片生成功能無法使用？

A:
1. 確認已設定 `GEMINI_API_KEY`
2. 檢查 API Key 是否有效
3. 查看日誌確認錯誤訊息
4. 如果不使用圖片生成功能，系統仍會以文字方式回覆食譜

### Q: 如何查看系統日誌？

A:
- 本地執行時，日誌會直接輸出到終端
- Cloud Run 部署時，可在 Google Cloud Console 查看日誌

## 📄 授權

本專案為學術研究用途，請遵守 LINE、Dify 和 Google 的服務條款。

## 🤝 貢獻

歡迎提交 Issue 或 Pull Request！

## 📧 聯絡方式

如有問題或建議，請透過 Issue 與我們聯絡。

---

**最後更新**: 2025/12/28

