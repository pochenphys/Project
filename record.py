"""
食物記錄系統 - record.py
功能：
1. 接收 LINE 用戶上傳的圖片（支援多張，帶緩衝機制）
2. 獲取用戶的 USERNAME（顯示名稱）
3. 將圖片傳送給 Dify（變數：foodphoto, freshrecord="True"）
4. 將 USERNAME、Dify 回傳結果、現在時間存入 MySQL 資料庫
5. 回傳確認訊息給用戶
"""

import os
import json
import base64
import hmac
import hashlib
import threading
import time
import re
from typing import Dict, List, Optional
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort
import requests
from dotenv import load_dotenv
import pymysql

# 從 Line2Dify.py 導入必要的類
from Line2Dify import (
    LINEWebhookHandler,
    LINEAPIClient,
    DifyAPIClient,
    ImageProcessor
)

# 載入環境變數
load_dotenv('LINE.env')

app = Flask(__name__)

# 從環境變數讀取設定
DIFY_API_KEY = os.getenv('DIFY_API_KEY')
DIFY_API_ENDPOINT = os.getenv('DIFY_API_ENDPOINT', 'https://api.dify.ai')
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')

# 驗證必要的環境變數
if not DIFY_API_KEY:
    raise ValueError("錯誤: DIFY_API_KEY 環境變數未設定，請檢查 LINE.env 檔案")
if not LINE_CHANNEL_ACCESS_TOKEN:
    raise ValueError("錯誤: LINE_CHANNEL_ACCESS_TOKEN 環境變數未設定，請檢查 LINE.env 檔案")
if not LINE_CHANNEL_SECRET:
    raise ValueError("錯誤: LINE_CHANNEL_SECRET 環境變數未設定，請檢查 LINE.env 檔案")

# MySQL 配置（支援 AWS RDS）
mysql_config_base = {
    'host': os.getenv('MYSQL_HOST', 'localhost'),
    'port': int(os.getenv('MYSQL_PORT', 3306)),
    'user': os.getenv('MYSQL_USER', 'root'),
    'password': os.getenv('MYSQL_PASSWORD'),
    'database': os.getenv('MYSQL_DATABASE', 'LINE'),
    'charset': os.getenv('MYSQL_CHARSET', 'utf8mb4'),
    'connect_timeout': int(os.getenv('MYSQL_CONNECT_TIMEOUT', 10))
}

# 如果啟用 SSL，添加 SSL 配置（AWS RDS 建議使用）
if os.getenv('MYSQL_SSL_ENABLED', 'false').lower() == 'true':
    # AWS RDS 通常支援 SSL，但不驗證主機名
    mysql_config_base['ssl'] = {'check_hostname': False}
    print("已啟用 SSL 連接（AWS RDS）")

MYSQL_CONFIG = mysql_config_base

# 台灣時區（UTC+8）
TAIWAN_TZ = timezone(timedelta(hours=8))

# LINE Profile API 端點
LINE_PROFILE_URL = 'https://api.line.me/v2/bot/profile/{userId}'

# 初始化 LINE 和 Dify 客戶端
webhook_handler = LINEWebhookHandler(LINE_CHANNEL_SECRET)
line_client = LINEAPIClient(LINE_CHANNEL_ACCESS_TOKEN)
dify_client = DifyAPIClient(DIFY_API_KEY, DIFY_API_ENDPOINT)

# 圖片緩衝區（用於收集多張圖片）
image_buffer = defaultdict(list)
user_timers = {}
buffer_lock = threading.Lock()
BUFFER_WAIT_TIME = 10.0  # 緩衝等待時間（秒）


class DatabaseManager:
    """MySQL 資料庫管理器"""
    
    def __init__(self, config: Dict):
        self.config = config
        self.connection = None
    
    def connect(self):
        """連接到 MySQL 資料庫（支援 AWS RDS）"""
        try:
            print(f"正在連接到 MySQL 資料庫: {self.config.get('host')}:{self.config.get('port')}")
            if 'ssl' in self.config:
                print("使用 SSL 連接（AWS RDS）")
            self.connection = pymysql.connect(**self.config)
            print("✓ MySQL 連接成功！")
            return True
        except Exception as e:
            print(f"✗ MySQL 連接失敗: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def insert_food_record(self, username: str, food_name: str, quantity: float = None, storage_time: datetime = None) -> bool:
        """
        插入食物記錄到 foods 表
        
        Args:
            username: 使用者名稱
            food_name: 食物名稱（從 Dify 回傳結果提取）
            quantity: 數量（可選，從 Dify 回傳結果提取）
            storage_time: 入庫時間（預設為現在時間）
        
        Returns:
            bool: 是否成功插入
        """
        if not self.connection:
            if not self.connect():
                return False
        
        try:
            if storage_time is None:
                storage_time = datetime.now(TAIWAN_TZ)
            
            with self.connection.cursor() as cursor:
                sql = """
                INSERT INTO foods (username, food_name, quantity, storage_time)
                VALUES (%s, %s, %s, %s)
                """
                cursor.execute(sql, (username, food_name, quantity, storage_time))
                self.connection.commit()
                quantity_str = f"{quantity}" if quantity is not None else "未指定"
                print(f"✓ 資料已存入資料庫: {username} - {food_name} - 數量: {quantity_str} - {storage_time}")
                return True
        except Exception as e:
            print(f"✗ 資料庫插入失敗: {e}")
            self.connection.rollback()
            return False
    
    def close(self):
        """關閉資料庫連接"""
        if self.connection:
            self.connection.close()


# 初始化資料庫管理器
db_manager = DatabaseManager(MYSQL_CONFIG)


def get_user_profile(user_id: str) -> Optional[Dict]:
    """
    獲取 LINE 用戶的 Profile 資訊（包含顯示名稱）
    
    Args:
        user_id: LINE 用戶 ID
    
    Returns:
        Optional[Dict]: 用戶資訊，包含 displayName, userId 等
    """
    try:
        url = LINE_PROFILE_URL.format(userId=user_id)
        headers = {
            'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}'
        }
        
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        profile = response.json()
        print(f"✓ 獲取用戶資訊成功: {profile.get('displayName', 'N/A')} ({user_id})")
        return profile
    except Exception as e:
        print(f"✗ 獲取用戶資訊失敗: {e}")
        return None


def parse_food_items_from_dify_response(dify_response: Dict) -> List[Dict]:
    """
    從 Dify 回傳結果中解析多個食物項目（包含食物名稱和數量）
    
    Args:
        dify_response: Dify API 回應
    
    Returns:
        List[Dict]: 食物項目列表，每個項目包含 'food_name' 和 'quantity'
                    例如: [{'food_name': '蘋果', 'quantity': 2.0}, {'food_name': '橘子', 'quantity': 2.0}]
    """
    try:
        # Dify 工作流回應格式：data.outputs.text
        data = dify_response.get('data', {})
        outputs = data.get('outputs', {}) if isinstance(data, dict) else {}
        text = outputs.get('text', '') if isinstance(outputs, dict) else ''
        
        if not text:
            return []
        
        # 解析多行文本，格式可能是：
        # "蘋果 2個\n橘子 2個\n青棗 1個"
        # 或 "蘋果 2個 橘子 2個"
        food_items = []
        
        # 按換行符分割
        lines = text.strip().split('\n')
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # 使用正則表達式匹配：食物名稱 + 數量（可選）
            # 匹配模式：食物名稱（中文、英文、數字）+ 可選的數量（數字 + 單位）
            # 例如：蘋果 2個、橘子 2個、青棗 1個
            pattern = r'([^\d\s]+?)\s*(\d+(?:\.\d+)?)\s*(個|件|包|盒|瓶|罐|條|根|片|塊|斤|公斤|克|kg|g)?'
            match = re.search(pattern, line)
            
            if match:
                food_name = match.group(1).strip()
                quantity_str = match.group(2)
                unit = match.group(3) if match.group(3) else ''
                
                try:
                    quantity = float(quantity_str)
                except ValueError:
                    quantity = None
                
                food_items.append({
                    'food_name': food_name,
                    'quantity': quantity
                })
            else:
                # 如果沒有匹配到數量，只提取食物名稱
                # 移除可能的標點符號和空格
                food_name = re.sub(r'[^\w\s\u4e00-\u9fff]', '', line).strip()
                if food_name:
                    food_items.append({
                        'food_name': food_name,
                        'quantity': None
                    })
        
        # 如果沒有解析到任何項目，嘗試將整個文本作為單一食物名稱
        if not food_items and text.strip():
            food_items.append({
                'food_name': text.strip()[:255],  # 限制長度
                'quantity': None
            })
        
        return food_items
        
    except Exception as e:
        print(f"解析食物項目失敗: {e}")
        import traceback
        traceback.print_exc()
        # 如果解析失敗，返回空列表或包含原始文本的單一項目
        return []


def process_buffered_images_timeout(user_id: str):
    """
    定時器觸發：處理緩衝區中的圖片
    
    Args:
        user_id: 使用者 ID
    """
    with buffer_lock:
        if user_id in image_buffer and len(image_buffer[user_id]) > 0:
            events = image_buffer[user_id].copy()
            image_buffer[user_id].clear()
            if user_id in user_timers:
                del user_timers[user_id]
            # 在背景線程中處理
            threading.Thread(target=process_images, args=(user_id, events), daemon=True).start()


def add_image_to_buffer(event: Dict) -> bool:
    """
    將圖片事件加入緩衝區（支援多張圖片緩衝）
    
    Args:
        event: LINE 圖片事件
    
    Returns:
        bool: 是否成功加入緩衝區
    """
    user_id = event.get('user_id')
    if not user_id:
        print("錯誤: 缺少使用者 ID")
        return False
    
    # 檢查是否有 imageSet 資訊（LINE 多張圖片標記）
    message = event.get('message', {})
    image_set = message.get('imageSet')
    
    with buffer_lock:
        # 將事件加入緩衝區
        image_buffer[user_id].append(event)
        buffer_size = len(image_buffer[user_id])
        
        print(f"圖片事件已加入緩衝區（使用者: {user_id}, 目前: {buffer_size} 張）")
        
        # 如果有 imageSet 資訊，檢查是否已收集完整
        if image_set:
            total = image_set.get('total')
            if total and buffer_size >= total:
                # 已收集完整，立即處理
                print(f"已收集完整圖片組（{buffer_size}/{total}），立即處理")
                events = image_buffer[user_id].copy()
                image_buffer[user_id].clear()
                if user_id in user_timers:
                    user_timers[user_id].cancel()
                    del user_timers[user_id]
                # 在背景線程中處理
                threading.Thread(target=process_images, args=(user_id, events), daemon=True).start()
                return True
        
        # 取消舊的定時器（如果存在）
        if user_id in user_timers:
            user_timers[user_id].cancel()
        
        # 設置新的定時器
        timer = threading.Timer(BUFFER_WAIT_TIME, process_buffered_images_timeout, args=(user_id,))
        timer.start()
        user_timers[user_id] = timer
    
    return True


def process_images(user_id: str, events: List[Dict]):
    """
    處理圖片事件（實際處理邏輯）
    
    Args:
        user_id: 使用者 ID
        events: 圖片事件列表
    """
    try:
        print(f"開始處理緩衝區中的 {len(events)} 張圖片（使用者: {user_id}）")
        
        # 步驟 1: 獲取用戶資訊（用於顯示，但存儲時使用 user_id）
        user_profile = get_user_profile(user_id)
        username = user_profile.get('displayName', '未知用戶') if user_profile else '未知用戶'
        print(f"使用者 ID: {user_id}, 顯示名稱: {username}")
        
        # 步驟 2: 下載所有圖片
        image_data_list = []
        for i, event in enumerate(events):
            message_id = event.get('message_id')
            if not message_id:
                print(f"警告: 事件 {i+1} 缺少訊息 ID，跳過")
                continue
            
            print(f"正在下載圖片 {i+1}/{len(events)} (訊息 ID: {message_id})...")
            image_data = ImageProcessor.download_from_line(message_id, LINE_CHANNEL_ACCESS_TOKEN)
            
            if not image_data:
                print(f"警告: 圖片 {i+1} 下載失敗，跳過")
                continue
            
            # 驗證檔案格式
            if not ImageProcessor.is_valid_image(image_data):
                print(f"錯誤: 檔案 {i+1} 不是有效的圖片格式")
                error_msg = "上傳格式錯誤，請重新上傳。"
                line_client.send_text_message(user_id, error_msg)
                return
            
            print(f"✓ 圖片 {i+1} 下載成功 (大小: {len(image_data)} bytes)")
            
            # 可選 - 調整圖片大小（如果太大）
            if len(image_data) > 5 * 1024 * 1024:  # 5MB
                print(f"圖片 {i+1} 過大，正在調整大小...")
                image_data = ImageProcessor.resize_image(image_data, max_size=(1024, 1024))
            
            image_data_list.append(image_data)
        
        if not image_data_list:
            print("錯誤: 沒有成功下載任何圖片")
            line_client.send_text_message(user_id, "圖片下載失敗，請重新上傳。")
            return
        
        # 步驟 3: 發送圖片到 Dify
        # 設定 freshrecord="True"
        print(f"正在發送 {len(image_data_list)} 張圖片到 Dify...")
        
        try:
            # 上傳圖片獲取 file_ids
            file_ids = []
            for i, img_data in enumerate(image_data_list):
                print(f"正在上傳圖片 {i+1}/{len(image_data_list)} 到 Dify...")
                # 使用 DifyAPIClient 的私有方法上傳圖片
                # 注意：如果 _upload_file 不可訪問，可以改用 send_image 方法
                try:
                    file_id = dify_client._upload_file(img_data, user_id)
                except AttributeError:
                    # 如果 _upload_file 不可訪問，使用 send_image 方法
                    # 但需要手動構建包含 freshrecord 的請求
                    print("使用替代方法上傳圖片...")
                    import mimetypes
                    url = f"{dify_client.base_url}/v1/files/upload"
                    headers = {'Authorization': f'Bearer {dify_client.api_key}'}
                    data = {'user': user_id}
                    
                    # 判斷檔案類型
                    mime_type = 'image/jpeg'
                    filename = 'image.jpg'
                    if img_data[:4] == b'\x89PNG':
                        mime_type = 'image/png'
                        filename = 'image.png'
                    elif img_data[:2] == b'\xff\xd8':
                        mime_type = 'image/jpeg'
                        filename = 'image.jpg'
                    
                    files = {'file': (filename, img_data, mime_type)}
                    response = requests.post(url, headers=headers, data=data, files=files, timeout=30)
                    if response.status_code in (200, 201):
                        body = response.json()
                        file_id = body.get('id')
                    else:
                        file_id = None
                
                if not file_id:
                    print(f"❌ 圖片 {i+1} 上傳失敗")
                    line_client.send_text_message(user_id, "圖片上傳到 Dify 失敗，請稍後再試。")
                    return
                file_ids.append(file_id)
                print(f"✓ 圖片 {i+1} 上傳成功，file_id: {file_id}")
            
            # 構建工作流請求（包含 freshrecord 變數）
            url = f'{dify_client.base_url}/v1/workflows/run'
            headers = dify_client.headers
            
            inputs = {
                'User': user_id,
                'foodphoto': [
                    {
                        "type": "image",
                        "transfer_method": "local_file",
                        "upload_file_id": file_id
                    }
                    for file_id in file_ids
                ],
                'freshrecord': 'True'  # 設定 freshrecord 為 "True"
            }
            
            payload = {
                'inputs': inputs,
                'response_mode': 'blocking',
                'user': user_id
            }
            
            print(f"發送工作流請求到 Dify（包含 freshrecord='True'）...")
            response = requests.post(url, headers=headers, json=payload, timeout=120)
            response.raise_for_status()
            
            dify_response = response.json()
            print(f"✓ Dify 工作流執行成功")
            
            # 檢查 Dify 回傳值是否為特定訊息（找不到食材）
            data = dify_response.get('data', {})
            outputs = data.get('outputs', {}) if isinstance(data, dict) else {}
            text = outputs.get('text', '') if isinstance(outputs, dict) else ''
            
            # 添加調試信息
            print(f"[除錯] Dify 回傳文本: {text[:200]}...")
            
            # 正確的回傳值文本（包含逗号和句号）
            response_message = "此圖片中找不到食材，請換一張圖片再嘗試。"
            
            # 檢查是否包含此回傳值（使用原始文本匹配）
            if response_message in text:
                print(f"✓ 檢測到回傳值: {response_message}")
                # 直接回傳給用戶，不進行後續解析和入庫
                line_client.send_text_message(user_id, response_message)
                return
            else:
                print(f"[除錯] 未檢測到特定回傳值，繼續處理...")
            
            # 步驟 4: 解析多個食物項目（包含名稱和數量）
            food_items = parse_food_items_from_dify_response(dify_response)
            print(f"解析到 {len(food_items)} 個食物項目")
            for i, item in enumerate(food_items, 1):
                print(f"  項目 {i}: {item['food_name']} - 數量: {item['quantity'] if item['quantity'] is not None else '未指定'}")
            
            # 步驟 5: 為每個食物項目存入 MySQL 資料庫
            storage_time = datetime.now(TAIWAN_TZ)
            success_count = 0
            failed_count = 0
            
            for item in food_items:
                food_name = item['food_name']
                quantity = item['quantity']
                
                # 如果只有名稱沒有數量，則數量設為1
                if quantity is None:
                    quantity = 1.0
                    item['quantity'] = 1.0  # 同時更新food_items中的數量，以便後續顯示
                    print(f"  項目 '{food_name}' 沒有數量，已設為 1")
                
                # 使用 user_id 而不是 username 來存儲記錄
                success = db_manager.insert_food_record(user_id, food_name, quantity, storage_time)
                if success:
                    success_count += 1
                else:
                    failed_count += 1
                    print(f"警告: 食物項目 '{food_name}' 插入失敗")
            
            print(f"資料庫插入完成: 成功 {success_count} 筆，失敗 {failed_count} 筆")
            
            # 步驟 6: 回傳確認訊息給用戶
            storage_time_str = storage_time.strftime("%Y-%m-%d %H:%M:%S")
            
            # 構建確認訊息（列出所有記錄的食物）
            confirm_message = f"✅ 已記錄 {len(food_items)} 項食品！\n\n"
            
            for i, item in enumerate(food_items, 1):
                # 確保數量顯示正確（如果為None則顯示1）
                quantity = item['quantity'] if item['quantity'] is not None else 1.0
                quantity_str = f"{quantity}"
                confirm_message += f"{i}. {item['food_name']} - 數量: {quantity_str}\n"
            
            confirm_message += f"\n⏰ 記錄時間：{storage_time_str}\n"
            confirm_message += f"👤 使用者：{username}"
            
            line_client.send_text_message(user_id, confirm_message)
            print(f"✓ 已發送確認訊息給用戶 {username}")
            
        except Exception as e:
            print(f"處理圖片失敗: {e}")
            import traceback
            traceback.print_exc()
            error_msg = "處理圖片時發生錯誤，請稍後再試。"
            line_client.send_text_message(user_id, error_msg)
    
    except Exception as e:
        print(f"處理圖片失敗: {e}")
        import traceback
        traceback.print_exc()
        error_msg = "處理圖片時發生錯誤，請稍後再試。"
        line_client.send_text_message(user_id, error_msg)


@app.route('/webhook', methods=['POST'])
def webhook():
    """
    LINE Webhook 端點
    """
    # 取得請求簽名
    signature = request.headers.get('X-Line-Signature', '')
    if not signature:
        print("警告: 缺少簽名")
        abort(400)
    
    # 取得請求主體
    request_body = request.get_data()
    
    # 驗證簽名
    if not webhook_handler.verify_signature(request_body, signature):
        print("錯誤: 簽名驗證失敗")
        abort(401)
    
    # 解析事件
    try:
        request_data = request.get_json()
        events = webhook_handler.parse_webhook_event(request_data)
        
        for event in events:
            # 處理圖片事件
            image_event = webhook_handler.handle_image_event(event)
            if image_event:
                print(f"收到圖片事件（使用者: {image_event.get('user_id')}）")
                add_image_to_buffer(image_event)
            else:
                # 處理文字訊息（可選：提供使用說明）
                message_event = webhook_handler.handle_message_event(event)
                if message_event and message_event['message_type'] == 'text':
                    user_id = message_event.get('user_id')
                    text = message_event['message'].get('text', '').strip()
                    reply_token = message_event.get('reply_token')
                    
                    if text.lower() in ['幫助', 'help', '說明']:
                        help_message = (
                            "📸 食物記錄功能\n\n"
                            "請上傳食物圖片，系統會自動：\n"
                            "• 識別食物名稱\n"
                            "• 記錄入庫時間\n"
                            "• 儲存到資料庫\n\n"
                            "支援一次上傳多張圖片！"
                        )
                        if reply_token:
                            line_client.reply_message(reply_token, help_message)
                        else:
                            line_client.send_text_message(user_id, help_message)
        
        return 'OK', 200
        
    except Exception as e:
        print(f"處理 Webhook 失敗: {str(e)}")
        import traceback
        traceback.print_exc()
        abort(500)


@app.route('/health', methods=['GET'])
def health():
    """健康檢查端點"""
    return {'status': 'ok', 'service': 'Food Record System'}, 200


@app.route('/', methods=['GET'])
def index():
    """首頁"""
    return '''
    <h1>食物記錄系統 (record.py)</h1>
    <p>Webhook 端點: /webhook</p>
    <p>健康檢查: /health</p>
    <p>狀態: 運行中</p>
    <h2>功能：</h2>
    <ul>
        <li>接收 LINE 用戶上傳的圖片</li>
        <li>獲取用戶 USERNAME</li>
        <li>傳送圖片到 Dify 進行識別</li>
        <li>將記錄存入 MySQL 資料庫</li>
        <li>回傳確認訊息給用戶</li>
    </ul>
    '''


def main():
    """主函數"""
    import argparse
    
    parser = argparse.ArgumentParser(description='食物記錄系統 (record.py)')
    parser.add_argument('--host', type=str, default='0.0.0.0',
                       help='伺服器主機 (預設: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=5000,
                       help='伺服器埠號 (預設: 5000)')
    parser.add_argument('--debug', action='store_true',
                       help='啟用除錯模式')
    
    args = parser.parse_args()
    
    # 連接資料庫
    if not db_manager.connect():
        print("警告: 資料庫連接失敗，部分功能可能無法使用")
    
    print("=" * 60)
    print("食物記錄系統 (record.py)")
    print("=" * 60)
    print(f"LINE Channel Secret: {LINE_CHANNEL_SECRET[:20]}...")
    print(f"Dify API Key: {DIFY_API_KEY[:20]}...")
    print(f"Webhook URL: http://{args.host}:{args.port}/webhook")
    print(f"MySQL 資料庫: {MYSQL_CONFIG['database']}")
    print("=" * 60)
    print("\n伺服器啟動中...")
    print("注意: LINE Webhook 需要 HTTPS，本地測試請使用 ngrok")
    print("\n")
    
    try:
        app.run(host=args.host, port=args.port, debug=args.debug)
    finally:
        # 關閉資料庫連接
        db_manager.close()


if __name__ == '__main__':
    main()
