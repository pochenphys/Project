# Smart Food Management System (LINE Bot + Dify + MySQL)

一個整合 LINE Bot、Dify AI 和 MySQL 資料庫的智能食物管理系統，專為現代家庭設計。
系統支援食譜推薦、智慧食物記錄、即時庫存查詢和精確消耗管理，助您輕鬆管理冰箱食材，減少浪費。

## 📋 目錄

- [✨ 功能特色](#-功能特色)
- [🛠️ 技術架構](#-技術架構)
- [📦 環境需求](#-環境需求)
- [🚀 快速開始](#-快速開始)
- [⚙️ 詳細配置](#%EF%B8%8F-詳細配置)
- [📱 使用指南](#-使用指南)
- [📁 專案結構](#-專案結構)
- [🔌 API 說明](#-api-說明)
- [💾 資料庫設計](#-資料庫設計)
- [🌐 部署指南](#-部署指南)
- [❓ 常見問題](#-常見問題)

## ✨ 功能特色

### 🍳 智慧食譜 (RECIPE)
- **AI 影像辨識**: 上傳食材圖片，AI 自動分析並辨識食材種類。
- **個性化推薦**: 根據辨識出的食材，提供詳細的食譜步驟和烹飪建議。
- **視覺化展示**: 生成精美的食譜圖片，以 Image Carousel 方式直觀展示。
- **多圖支援**: 支援一次上傳多張圖片，綜合分析食材。

### 📝 智慧記錄 (RECORD)
- **自動入庫**: 拍照即記錄，自動識別食物名稱。
- **詳細追蹤**: 自動記錄入庫時間，支援數量標記。
- **無縫存儲**: 資料自動同步至 MySQL 資料庫。

### 🔍 庫存查詢 (VIEW)
- **清單總覽**: 一鍵查看所有庫存食物。
- **效期追蹤**: 顯示購買時間及已存放天數，即時掌握食材新鮮度。
- **排序顯示**: 按時間順序排列（先進先出），優先處理即將過期的食材。

### 🗑️ 精確消耗 (DELETE)
- **靈活扣除**: 支援按編號刪除或按名稱扣除。
- **智慧邏輯**: 按名稱扣除時，自動從最舊的庫存開始扣除（FIFO）。
- **部分消耗**: 支援更新數量（例如：從 3 個蘋果中消耗 1 個）。

## 🛠️ 技術架構

- **Backend Framework**: Flask (Python 3.8+)
- **Messaging Integration**: LINE Messaging API (Line Bot SDK)
- **AI & LLM**:
  - **Dify API**: 核心工作流引擎 (Workflow Mode)
  - **Google Gemini API**: 圖片生成與視覺辨識能力 (Optional)
- **Database**: MySQL (Compatible with AWS RDS)
- **Key Libraries**:
  - `pymysql`: 資料庫連接與操作
  - `Pillow`: 影像處理
  - `requests`: HTTP 請求處理
  - `python-dotenv`: 環境變數管理

## 📦 環境需求

- **Run Time**: Python 3.8+
- **Database**: MySQL 5.7+ (Local or Cloud like AWS RDS)
- **Accounts**:
  - LINE Developer Account (Messaging API)
  - Dify Account (API Access)
  - Google Cloud Account (Optional, for Gemini API)

## 🚀 快速開始

### 1. 取得專案程式碼

```bash
git clone <repository-url>
cd Project
```

### 2. 環境準備

建立並啟用虛擬環境（強烈建議）：

```bash
# Windows
python -m venv venv
venv\Scripts\activate

# Linux/macOS
python3 -m venv venv
source venv/bin/activate
```

安裝必要套件：

```bash
pip install -r LINE_requirements.txt
```

### 3. 環境變數設定

複製範例檔案並填入您的金鑰：

```bash
# LINE Bot 設定
touch LINE.env
# Dify 設定
cp Dify.env.example .env
```

編輯 `LINE.env`，填入以下內容：

```env
# LINE Messaging API Configuration
LINE_CHANNEL_ACCESS_TOKEN=your_channel_access_token
LINE_CHANNEL_SECRET=your_channel_secret

# Dify API Configuration
DIFY_API_KEY=your_dify_api_key
DIFY_API_ENDPOINT=https://api.dify.ai

# Google Gemini API (Optional for Image Generation)
GEMINI_API_KEY=your_gemini_api_key

# MySQL Database Configuration
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=your_password
MYSQL_DATABASE=LINE
MYSQL_CHARSET=utf8mb4
```

### 4. 資料庫初始化

登入 MySQL 並執行以下 SQL 建立資料表：

```sql
CREATE DATABASE IF NOT EXISTS LINE CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE LINE;

CREATE TABLE IF NOT EXISTS foods (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(255) NOT NULL,
    food_name VARCHAR(255) NOT NULL,
    quantity FLOAT,
    storage_time DATETIME NOT NULL,
    INDEX idx_username (username),
    INDEX idx_storage_time (storage_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

### 5. 啟動服務

```bash
python LINE_Bot_Router.py
```
預設運行於 `http://0.0.0.0:5000`。

> **注意**: 開發環境請使用 `ngrok http 5000` 來轉發 HTTPS 請求至本地，並將 ngrok 網址填入 LINE Developer Console 的 Webhook URL。

## ⚙️ 詳細配置

### LINE Bot Setup
1. 登入 [LINE Developers Console](https://developers.line.biz/)。
2. 創建 Provider 與 Channel (Messaging API)。
3. 獲取 `Channel Secret` 與 `Channel Access Token`。
4. 設定 Webhook URL，並開啟 Use Webhook。

### Dify API Setup
1. 登入 [Dify](https://dify.ai/)。
2. 匯入專案中的工作流檔案：`食材辨識助手測試 20260122.yml`。
   - 進入 Studio -> Create from YAML -> Upload `食材辨識助手測試 20260122.yml`。
3. **發布 (Publish)** 該應用。
4. 在 "API Access" 頁面獲取 API Key。

> **工作流變數對照**:
> - `User`: User ID
> - `foodphoto`: Image File(s)
> - `text`: User Query (Optional)
> - `freshrecord`: Record Flag

## 📱 使用指南

| 功能命令 | 說明 | 範例操作 |
|---------|------|---------|
| **食譜功能** | 啟用食譜推薦模式 | 輸入「食譜」 -> 上傳食材照片 -> 獲得食譜 |
| **記錄功能** | 啟用食物記錄模式 | 輸入「記錄」 -> 上傳食材照片 -> 自動存檔 |
| **查看功能** | 查詢庫存清單 | 輸入「查看」 -> 顯示所有記錄清單 |
| **刪除功能** | 啟用消耗/刪除模式 | 輸入「刪除」 -> 根據提示輸入編號或名稱 |
| **幫助 / Help** | 顯示功能選單 | 輸入「幫助」 |
| **退出 / Exit** | 退出當前模式 | 輸入「退出」 |

## 📁 專案結構

```
Project/
├── LINE_Bot_Router.py            # [核心] 應用程式入口與路由控制器
├── Line2Dify.py                  # [核心] LINE處理邏輯與Dify API整合封裝
├── record.py                     # [模組] 食物記錄與資料庫管理模組
├── LINE_requirements.txt         # Python 依賴清單
├── LINE.env                      # LINE 與 資料庫 環境變數 (需自行建立)
├── Dify.env.example              # Dify 環境變數範本
├── 食材辨識助手測試 20260122.yml  # Dify Workflow 定義檔 (最新版)
└── README.md                     # 專案說明文件
```

## 🔌 API 說明

- **POST /webhook**: LINE Platform 的主要回調入口。負責接收所有訊息事件。
- **GET /**: 健康檢查 (Health Check) 與首頁訊息。
- **GET /temp_image/<image_id>**: 臨時圖片讀取接口 (用於 Image Carousel 顯示動態生成的圖片)。

## 💾 資料庫設計

主要資料表 `foods`:

| Column | Type | Description |
|--------|------|-------------|
| `id` | INT | Primary Key |
| `username` | VARCHAR | User ID (LINE user_id) |
| `food_name` | VARCHAR | 食物名稱 |
| `quantity` | FLOAT | 數量 |
| `storage_time` | DATETIME | 入庫時間 |

## 🌐 部署指南 (Google Cloud Run)

### 1. 準備 Dockerfile

在專案根目錄建立 `Dockerfile`：

```dockerfile
FROM python:3.9-slim

WORKDIR /app

# 安裝系統依賴 (如果需要)
# RUN apt-get update && apt-get install -y --no-install-recommends gcc libmariadb-dev && rm -rf /var/lib/apt/lists/*

COPY LINE_requirements.txt .
RUN pip install --no-cache-dir -r LINE_requirements.txt

COPY . .

# Cloud Run 預設監聽 8080，但我們的程式預設 5000
# 可以在啟動時覆蓋，或是調整程式
ENV PORT=5000
EXPOSE 5000

CMD ["python", "LINE_Bot_Router.py", "--host", "0.0.0.0", "--port", "5000"]
```

### 2. 部署到 Cloud Run

執行部署命令：
```bash
gcloud run deploy line-bot-food \
  --source . \
  --platform managed \
  --region asia-east1 \
  --allow-unauthenticated \
  --set-env-vars-load LINE.env
```
*(建議將敏感資訊移至 Secret Manager)*

## ❓ 常見問題

**Q: 圖片生成太慢或失敗？**
A: 請檢查 `GEMINI_API_KEY` 是否正確設定。Dify 工作流中的圖片生成步驟可能需要較長時間，請確保 LINE Bot 的回覆超時設定允許較長的等待（LINE 預設 Webhook 超時較短，本系統採用非同步回覆機制解決）。

**Q: 資料庫連線中斷？**
A: 系統已實作自動重連機制與 Context Manager 連線管理 (`db_manager.get_connection()`)，有效解決 "MySQL server has gone away" 問題。

---

**Last Update**: 2026/01/22
