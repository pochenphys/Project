"""
LINE OA 與 Dify 整合系統
根據規格文件 Line_test.md 實作
"""

import os
import json
import base64
import hmac
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from flask import Flask, request, abort
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.exceptions import MaxRetryError, SSLError as Urllib3SSLError
import ssl
from dotenv import load_dotenv
from PIL import Image
import io
# 不再使用 Google Gen AI SDK，改用直接 REST API 調用以提升性能

# 嘗試導入 google.auth（用於 OAuth2 認證）
try:
    from google.auth import default
    from google.auth.transport.requests import Request
    GOOGLE_AUTH_AVAILABLE = True
except ImportError:
    GOOGLE_AUTH_AVAILABLE = False
    print("[提示] google-auth 套件未安裝，Generative Language API 將使用 Metadata Server Token")
    print("[提示] 如需完整支援，請執行: pip install google-auth")


# 載入環境變數
load_dotenv()

app = Flask(__name__)

# 從環境變數讀取設定
DIFY_API_KEY = os.getenv('DIFY_API_KEY')
DIFY_API_ENDPOINT = os.getenv('DIFY_API_ENDPOINT', 'https://api.dify.ai')
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
# Vertex AI 設定（使用服務帳號認證，不使用 API Key）
# GEMINI_API_KEY 已移除，改用服務帳號認證
VERTEX_AI_PROJECT_ID = os.getenv('VERTEX_AI_PROJECT_ID', '')
VERTEX_AI_LOCATION = os.getenv('VERTEX_AI_LOCATION', 'us-central1')

# 多個 Project ID 設定（用於分散並行請求，提高穩定性）
# 格式：用逗號分隔，例如：VERTEX_AI_PROJECT_IDS=project1,project2,project3
# 如果設定，會輪流使用這些 Project ID 來分散請求負載
VERTEX_AI_PROJECT_IDS_STR = os.getenv('VERTEX_AI_PROJECT_IDS', '')
if VERTEX_AI_PROJECT_IDS_STR:
    VERTEX_AI_PROJECT_IDS = [pid.strip() for pid in VERTEX_AI_PROJECT_IDS_STR.split(',') if pid.strip()]
else:
    # 如果沒有設定多個 Project ID，使用單個 Project ID（向後兼容）
    VERTEX_AI_PROJECT_IDS = [VERTEX_AI_PROJECT_ID] if VERTEX_AI_PROJECT_ID else []

# 驗證必要的環境變數
if not DIFY_API_KEY:
    raise ValueError("錯誤: DIFY_API_KEY 環境變數未設定，請檢查 LINE.env 檔案")
if not LINE_CHANNEL_ACCESS_TOKEN:
    raise ValueError("錯誤: LINE_CHANNEL_ACCESS_TOKEN 環境變數未設定，請檢查 LINE.env 檔案")
if not LINE_CHANNEL_SECRET:
    raise ValueError("錯誤: LINE_CHANNEL_SECRET 環境變數未設定，請檢查 LINE.env 檔案")

# LINE API 端點
LINE_REPLY_URL = 'https://api.line.me/v2/bot/message/reply'
LINE_PUSH_URL = 'https://api.line.me/v2/bot/message/push'
LINE_CONTENT_URL = 'https://api-data.line.me/v2/bot/message/{message_id}/content'

# 臨時圖片存儲（用於存儲生成的圖片並提供臨時訪問）
temp_image_storage = {}
temp_image_lock = threading.Lock()
temp_image_counter = 0

# 存儲用戶的食譜數據和 text（用於 Postback 事件）
# 格式: {user_id: {'dish_1': '...', 'dish_2': '...', 'dish_3': '...'}}
user_recipe_storage = {}
user_text_storage = {}
recipe_storage_lock = threading.Lock()


class LINEWebhookHandler:
    """處理 LINE Webhook 請求"""
    
    def __init__(self, channel_secret: str):
        """
        初始化 Webhook 處理器
        
        Args:
            channel_secret: LINE Channel Secret
        """
        self.channel_secret = channel_secret.encode('utf-8')
    
    def verify_signature(self, request_body: bytes, signature: str) -> bool:
        """
        驗證請求簽名
        
        公式: signature = base64(hmac-sha256(channel_secret, request_body))
        
        Args:
            request_body: 請求主體（bytes）
            signature: 請求標頭中的簽名
        
        Returns:
            bool: 簽名是否有效
        """
        try:
            # 計算簽名
            hash_value = hmac.new(
                self.channel_secret,
                request_body,
                hashlib.sha256
            ).digest()
            expected_signature = base64.b64encode(hash_value).decode('utf-8')
            
            # 比較簽名
            return hmac.compare_digest(expected_signature, signature)
        except Exception as e:
            print(f"簽名驗證錯誤: {str(e)}")
            return False
    
    def parse_webhook_event(self, request_data: Dict) -> List[Dict]:
        """
        解析 Webhook 事件
        
        Args:
            request_data: Webhook 請求資料
        
        Returns:
            List[Dict]: 事件列表
        """
        events = request_data.get('events', [])
        return events
    
    def handle_message_event(self, event: Dict) -> Optional[Dict]:
        """
        處理訊息事件
        
        Args:
            event: LINE 事件
        
        Returns:
            Optional[Dict]: 處理後的事件資料
        """
        if event.get('type') != 'message':
            return None
        
        message = event.get('message', {})
        message_type = message.get('type')
        
        return {
            'event_type': 'message',
            'message_type': message_type,
            'user_id': event.get('source', {}).get('userId'),
            'reply_token': event.get('replyToken'),
            'message_id': message.get('id'),
            'timestamp': event.get('timestamp'),
            'message': message
        }
    
    def handle_image_event(self, event: Dict) -> Optional[Dict]:
        """
        處理圖片事件
        
        Args:
            event: LINE 事件
        
        Returns:
            Optional[Dict]: 處理後的圖片事件資料
        """
        message_data = self.handle_message_event(event)
        if message_data and message_data['message_type'] == 'image':
            return message_data
        return None


class LINEAPIClient:
    """與 LINE API 通訊"""
    
    def __init__(self, access_token: str):
        """
        初始化 LINE API 客戶端
        
        Args:
            access_token: LINE Channel Access Token
        """
        self.access_token = access_token
        self.headers = {
            'Authorization': f'Bearer {self.access_token}',
            'Content-Type': 'application/json'
        }
        # 使用 Session 复用连接，提高性能
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        
        # 配置重试策略（处理 SSL 错误和连接问题）
        retry_strategy = Retry(
            total=5,  # 最多重试5次（增加重试次数以应对不稳定的 SSL 连接）
            backoff_factor=2,  # 重试间隔：2秒, 4秒, 8秒, 16秒, 32秒（更长的等待时间）
            status_forcelist=[429, 500, 502, 503, 504],  # 这些状态码会重试
            allowed_methods=["HEAD", "GET", "PUT", "DELETE", "OPTIONS", "TRACE", "POST"],
            raise_on_status=False
        )
        
        # 创建适配器并配置 SSL（增加连接池大小以提高稳定性）
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,  # 连接池大小
            pool_maxsize=20,  # 最大连接数
            pool_block=False  # 不阻塞连接
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        
        # 配置 SSL 上下文（更宽松的 SSL 验证，避免 SSL 错误）
        # 注意：在生产环境中，应该使用严格的 SSL 验证
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except:
            pass
    
    def download_image(self, message_id: str) -> Optional[bytes]:
        """
        從 LINE 下載圖片
        
        API: GET https://api-data.line.me/v2/bot/message/{message_id}/content
        
        Args:
            message_id: 圖片訊息 ID
        
        Returns:
            Optional[bytes]: 圖片資料，失敗返回 None
        """
        max_retries = 5  # 增加重試次數以應對不穩定的 SSL 連接
        for attempt in range(max_retries):
            try:
                url = LINE_CONTENT_URL.format(message_id=message_id)
                headers = {
                    'Authorization': f'Bearer {self.access_token}'
                }
                
                # 使用 session 复用连接，增加超時時間以應對慢速連接
                # timeout=(連接超時, 讀取超時)
                response = self.session.get(url, headers=headers, timeout=(10, 90))
                response.raise_for_status()
                
                return response.content
            except (requests.exceptions.SSLError, Urllib3SSLError, ssl.SSLError) as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # 指数退避：1秒, 2秒, 4秒, 8秒, 16秒
                    print(f"SSL 錯誤（嘗試 {attempt + 1}/{max_retries}）: {str(e)}，{wait_time}秒後重試...")
                    time.sleep(wait_time)
                    # 在重試前，嘗試關閉舊連接並重新建立（避免使用已損壞的連接）
                    try:
                        # 只關閉連接，不刪除 session 對象（保持配置）
                        if hasattr(self.session, 'close'):
                            # 創建新的 session 以確保連接乾淨
                            old_session = self.session
                            self.session = requests.Session()
                            self.session.headers.update(self.headers)
                            # 重新配置適配器
                            retry_strategy = Retry(
                                total=5,
                                backoff_factor=2,
                                status_forcelist=[429, 500, 502, 503, 504],
                                allowed_methods=["HEAD", "GET", "PUT", "DELETE", "OPTIONS", "TRACE", "POST"],
                                raise_on_status=False
                            )
                            adapter = HTTPAdapter(
                                max_retries=retry_strategy,
                                pool_connections=10,
                                pool_maxsize=20,
                                pool_block=False
                            )
                            self.session.mount("https://", adapter)
                            self.session.mount("http://", adapter)
                            # 關閉舊 session
                            old_session.close()
                    except Exception as reconnect_error:
                        print(f"[警告] 重新建立連接時發生錯誤: {reconnect_error}，繼續使用現有連接")
                    continue
                else:
                    print(f"下載圖片失敗（SSL錯誤，已重试{max_retries}次）: {str(e)}")
                    print(f"[提示] 這可能是 LINE API 伺服器的暫時性問題，請稍後再試")
                    return None
            except requests.exceptions.Timeout as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"請求超時（嘗試 {attempt + 1}/{max_retries}）: {str(e)}，{wait_time}秒後重試...")
                    time.sleep(wait_time)
                    continue
                else:
                    print(f"下載圖片失敗（請求超時，已重试{max_retries}次）: {str(e)}")
                    return None
            except requests.exceptions.ConnectionError as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"連接錯誤（嘗試 {attempt + 1}/{max_retries}）: {str(e)}，{wait_time}秒後重試...")
                    time.sleep(wait_time)
                    continue
                else:
                    print(f"下載圖片失敗（連接錯誤，已重试{max_retries}次）: {str(e)}")
                    return None
            except Exception as e:
                print(f"下載圖片失敗: {str(e)}")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"等待 {wait_time}秒後重試...")
                    time.sleep(wait_time)
                    continue
                return None
        return None
    
    def send_text_message(self, user_id: str, text: str) -> bool:
        """
        發送文字訊息到 LINE
        
        API: POST https://api.line.me/v2/bot/message/push
        
        Args:
            user_id: LINE 使用者 ID
            text: 訊息內容
        
        Returns:
            bool: 是否成功發送
        """
        try:
            url = LINE_PUSH_URL
            payload = {
                'to': user_id,
                'messages': [
                    {
                        'type': 'text',
                        'text': text
                    }
                ]
            }
            
            # 使用 session 复用连接
            response = self.session.post(url, json=payload, timeout=10)
            response.raise_for_status()
            return True
        except Exception as e:
            print(f"發送訊息失敗: {str(e)}")
            return False
    
    def reply_message(self, reply_token: str, text: str) -> bool:
        """
        回覆訊息
        
        API: POST https://api.line.me/v2/bot/message/reply
        
        Args:
            reply_token: 回覆 Token
            text: 訊息內容
        
        Returns:
            bool: 是否成功回覆
        """
        max_retries = 3
        for attempt in range(max_retries):
            try:
                url = LINE_REPLY_URL
                payload = {
                    'replyToken': reply_token,
                    'messages': [
                        {
                            'type': 'text',
                            'text': text
                        }
                    ]
                }
                
                # 使用 session 复用连接
                response = self.session.post(url, json=payload, timeout=10)
                response.raise_for_status()
                return True
            except (requests.exceptions.SSLError, Urllib3SSLError, ssl.SSLError) as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"SSL 錯誤（嘗試 {attempt + 1}/{max_retries}）: {str(e)}，{wait_time}秒後重試...")
                    time.sleep(wait_time)
                    continue
                else:
                    print(f"回覆訊息失敗（SSL錯誤，已重试{max_retries}次）: {str(e)}")
                    return False
            except Exception as e:
                print(f"回覆訊息失敗: {str(e)}")
                return False
        return False
    
    def send_image_message(self, user_id: str, image_url: str) -> bool:
        """
        發送圖片訊息
        
        Args:
            user_id: LINE 使用者 ID
            image_url: 圖片 URL
        
        Returns:
            bool: 是否成功發送
        """
        try:
            url = LINE_PUSH_URL
            payload = {
                'to': user_id,
                'messages': [
                    {
                        'type': 'image',
                        'originalContentUrl': image_url,
                        'previewImageUrl': image_url
                    }
                ]
            }
            
            # 使用 session 复用连接
            response = self.session.post(url, json=payload, timeout=10)
            response.raise_for_status()
            return True
        except Exception as e:
            print(f"發送圖片失敗: {str(e)}")
            return False
    
    def reply_text_and_image(self, reply_token: str, text: str, image_url: str) -> bool:
        """
        回覆文字和圖片訊息
        
        Args:
            reply_token: 回覆 Token
            text: 文字訊息內容
            image_url: 圖片 URL
        
        Returns:
            bool: 是否成功回覆
        """
        try:
            url = LINE_REPLY_URL
            payload = {
                'replyToken': reply_token,
                'messages': [
                    {
                        'type': 'text',
                        'text': text
                    },
                    {
                        'type': 'image',
                        'originalContentUrl': image_url,
                        'previewImageUrl': image_url
                    }
                ]
            }
            
            # 使用 session 复用连接
            response = self.session.post(url, json=payload, timeout=30)
            response.raise_for_status()
            return True
        except Exception as e:
            print(f"回覆文字和圖片失敗: {str(e)}")
            return False
    
    def send_text_and_image(self, user_id: str, text: str, image_url: str) -> bool:
        """
        發送文字和圖片訊息
        
        Args:
            user_id: LINE 使用者 ID
            text: 文字訊息內容
            image_url: 圖片 URL
        
        Returns:
            bool: 是否成功發送
        """
        try:
            url = LINE_PUSH_URL
            payload = {
                'to': user_id,
                'messages': [
                    {
                        'type': 'text',
                        'text': text
                    },
                    {
                        'type': 'image',
                        'originalContentUrl': image_url,
                        'previewImageUrl': image_url
                    }
                ]
            }
            
            # 使用 session 复用连接
            response = self.session.post(url, json=payload, timeout=30)
            response.raise_for_status()
            return True
        except Exception as e:
            print(f"發送文字和圖片失敗: {str(e)}")
            return False
    
    def reply_image_carousel(self, reply_token: str, columns: List[Dict]) -> bool:
        """
        回覆 Image Carousel Template Message
        
        Args:
            reply_token: 回覆 Token
            columns: Image Carousel 的列列表，每個列包含 imageUrl 和 action
            
        Returns:
            bool: 是否成功回覆
        """
        try:
            url = LINE_REPLY_URL
            
            # 驗證 columns 格式
            if not columns or len(columns) == 0:
                print("[錯誤] Image Carousel columns 為空")
                return False
            
            # 檢查圖片 URL 是否為 HTTPS
            for i, col in enumerate(columns):
                image_url = col.get('imageUrl', '')
                if image_url and not image_url.startswith('https://'):
                    print(f"[警告] 圖片 URL {i+1} 不是 HTTPS: {image_url}")
            
            payload = {
                'replyToken': reply_token,
                'messages': [
                    {
                        'type': 'template',
                        'altText': '食譜選擇',
                        'template': {
                            'type': 'image_carousel',
                            'columns': columns
                        }
                    }
                ]
            }
            
            # 使用 session 复用连接
            response = self.session.post(url, json=payload, timeout=30)
            
            # 如果失敗，打印詳細錯誤信息
            if response.status_code != 200:
                print(f"[錯誤] LINE API 響應狀態碼: {response.status_code}")
                print(f"[錯誤] LINE API 錯誤響應: {response.text}")
                try:
                    error_data = response.json()
                    print(f"[錯誤] LINE API 錯誤詳情: {json.dumps(error_data, ensure_ascii=False, indent=2)}")
                except:
                    pass
                response.raise_for_status()
            
            return True
        except requests.exceptions.HTTPError as e:
            print(f"回覆 Image Carousel 失敗 (HTTP錯誤): {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"錯誤響應狀態碼: {e.response.status_code}")
            return False
        except Exception as e:
            print(f"回覆 Image Carousel 失敗: {str(e)}")
            import traceback
            traceback.print_exc()
            return False
    
    def send_image_carousel(self, user_id: str, columns: List[Dict]) -> bool:
        """
        發送 Image Carousel Template Message
        
        Args:
            user_id: LINE 使用者 ID
            columns: Image Carousel 的列列表
            
        Returns:
            bool: 是否成功發送
        """
        try:
            url = LINE_PUSH_URL
            
            # 驗證 columns 格式
            if not columns or len(columns) == 0:
                print("[錯誤] Image Carousel columns 為空")
                return False
            
            # 檢查圖片 URL 是否為 HTTPS
            for i, col in enumerate(columns):
                image_url = col.get('imageUrl', '')
                if image_url and not image_url.startswith('https://'):
                    print(f"[警告] 圖片 URL {i+1} 不是 HTTPS: {image_url}")
            
            payload = {
                'to': user_id,
                'messages': [
                    {
                        'type': 'template',
                        'altText': '食譜選擇',
                        'template': {
                            'type': 'image_carousel',
                            'columns': columns
                        }
                    }
                ]
            }
            
            # 使用 session 复用连接
            response = self.session.post(url, json=payload, timeout=30)
            
            # 如果失敗，打印詳細錯誤信息
            if response.status_code != 200:
                print(f"[錯誤] LINE API 響應狀態碼: {response.status_code}")
                print(f"[錯誤] LINE API 錯誤響應: {response.text}")
                try:
                    error_data = response.json()
                    print(f"[錯誤] LINE API 錯誤詳情: {json.dumps(error_data, ensure_ascii=False, indent=2)}")
                except:
                    pass
                response.raise_for_status()
            
            return True
        except requests.exceptions.HTTPError as e:
            print(f"發送 Image Carousel 失敗 (HTTP錯誤): {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"錯誤響應狀態碼: {e.response.status_code}")
            return False
        except Exception as e:
            print(f"發送 Image Carousel 失敗: {str(e)}")
            import traceback
            traceback.print_exc()
            return False


class DifyAPIClient:
    """與 Dify API 通訊（支援工作流模式）"""
    
    def __init__(self, api_key: str, endpoint: str = 'https://api.dify.ai'):
        """
        初始化 Dify API 客戶端
        
        Args:
            api_key: Dify API Key
            endpoint: Dify API 基礎 URL（例如: https://api.dify.ai）
        """
        self.api_key = api_key
        self.base_url = endpoint.rstrip('/')
        self.headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json'
        }
        # 使用 Session 复用连接，提高性能
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        
        # 配置重试策略（处理 SSL 错误和连接问题）
        retry_strategy = Retry(
            total=3,  # 最多重试3次
            backoff_factor=1,  # 重试间隔：1秒, 2秒, 4秒
            status_forcelist=[429, 500, 502, 503, 504],  # 这些状态码会重试
            allowed_methods=["HEAD", "GET", "PUT", "DELETE", "OPTIONS", "TRACE", "POST"],
            raise_on_status=False
        )
        
        # 创建适配器并配置 SSL
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        
        # 配置 SSL 上下文（更宽松的 SSL 验证，避免 SSL 错误）
        # 注意：在生产环境中，应该使用严格的 SSL 验证
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except:
            pass
    
    def send_message(self, text: str, conversation_id: Optional[str] = None, 
                    user_id: str = 'default_user') -> Optional[Dict]:
        """
        發送文字訊息到 Dify
        
        API: POST {endpoint}/chat-messages
        
        Args:
            text: 訊息內容
            conversation_id: 對話 ID（可選）
            user_id: 使用者 ID
        
        Returns:
            Optional[Dict]: Dify 回應，失敗返回 None
        """
        max_retries = 3
        for attempt in range(max_retries):
            try:
                url = f'{self.base_url}/chat-messages'
                payload = {
                    'inputs': {},
                    'query': text,
                    'response_mode': 'blocking',
                    'user': user_id
                }
                
                if conversation_id:
                    payload['conversation_id'] = conversation_id
                
                # 使用 session 复用连接
                response = self.session.post(url, json=payload, timeout=60)
                response.raise_for_status()
                
                return response.json()
            except (requests.exceptions.SSLError, Urllib3SSLError, ssl.SSLError) as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"SSL 錯誤（嘗試 {attempt + 1}/{max_retries}）: {str(e)}，{wait_time}秒後重試...")
                    time.sleep(wait_time)
                    continue
                else:
                    print(f"Dify API 請求失敗（SSL錯誤，已重试{max_retries}次）: {str(e)}")
                    return None
            except Exception as e:
                print(f"Dify API 請求失敗: {str(e)}")
                return None
        return None
    
    def send_image(self, image_data, conversation_id: Optional[str] = None,
                  user_id: str = 'default_user', query: str = "請分析這張圖片",
                  text_variable: str = "text", user_variable: str = "User",
                  image_variable: str = "foodphoto") -> Optional[Dict]:
        """
        發送圖片到 Dify 工作流（支援單張或多張圖片）
        
        API: POST {base_url}/v1/workflows/run
        
        流程:
        1. 先上傳圖片到 /v1/files/upload 取得 file_id
        2. 在 inputs 中使用 local_file 和 upload_file_id
        
        Args:
            image_data: 圖片資料（bytes）或圖片列表（List[bytes]）
            conversation_id: 對話 ID（工作流通常不需要，但保留以備不時之需）
            user_id: 使用者 ID
            query: 查詢文字（可選，用於文字輸入變數）
            text_variable: 文字輸入變數名稱（預設為 "text"）
            user_variable: User 輸入變數名稱（預設為 "User"）
            image_variable: 圖片輸入變數名稱（預設為 "foodphoto"）
        
        Returns:
            Optional[Dict]: Dify 回應，失敗返回 None
        """
        try:
            # 將單張圖片轉換為列表格式（統一處理）
            if isinstance(image_data, bytes):
                image_data_list = [image_data]
            elif isinstance(image_data, list):
                image_data_list = image_data
            else:
                print("[錯誤] image_data 格式不正確，應為 bytes 或 List[bytes]")
                return None
            
            # 步驟 1: 並行上傳所有圖片取得 file_id 列表（優化：使用 ThreadPoolExecutor）
            def upload_single_file(img_data: bytes, index: int) -> Optional[str]:
                """上傳單個文件（用於並行處理）"""
                return self._upload_file(img_data, user_id)
            
            file_ids = []
            with ThreadPoolExecutor(max_workers=min(len(image_data_list), 5)) as executor:
                upload_futures = {
                    executor.submit(upload_single_file, img_data, i): i 
                    for i, img_data in enumerate(image_data_list)
                }
                
                # 收集結果（保持順序）
                temp_file_ids = [None] * len(image_data_list)
                for future in as_completed(upload_futures):
                    index = upload_futures[future]
                    try:
                        file_id = future.result()
                        temp_file_ids[index] = file_id
                    except Exception as e:
                        print(f"❌ 圖片 {index+1} 上傳失敗: {str(e)}")
                
                # 檢查是否所有文件都上傳成功
                file_ids = [fid for fid in temp_file_ids if fid is not None]
                if len(file_ids) != len(image_data_list):
                    print(f"❌ 部分圖片上傳失敗（成功: {len(file_ids)}/{len(image_data_list)}）")
                    return None
            
            # 工作流 API 端點
            url = f'{self.base_url}/v1/workflows/run'
            
            # 工作流 payload 格式（根據範例和 example.py）：
            # inputs 中包含 "User"、文字輸入和圖片輸入
            inputs = {
                user_variable: user_id  # User 變數使用 user_id
            }
            
            # 如果有文字輸入，加入 inputs
            if query:
                inputs[text_variable] = query
            
            # 圖片輸入使用 local_file 格式（列表，可包含多張圖片）
            inputs[image_variable] = [
                {
                    "type": "image",
                    "transfer_method": "local_file",
                    "upload_file_id": file_id
                }
                for file_id in file_ids
            ]
            
            payload = {
                'inputs': inputs,
                'response_mode': 'blocking',
                'user': user_id
            }
            
            # 使用 session 复用连接
            # 注意：Dify API 的 blocking 模式有 Cloudflare 的 100 秒限制
            # 如果工作流執行時間超過 100 秒，會超時
            # 設置 timeout=100 以符合 Cloudflare 限制
            workflow_start_time = time.time()
            print(f"[時間記錄] 開始調用 Dify 工作流 API...")
            
            # 添加重试逻辑处理 SSL 错误
            max_retries = 3
            response = None
            for attempt in range(max_retries):
                try:
                    response = self.session.post(url, json=payload, timeout=100)
                    break  # 成功则退出循环
                except (requests.exceptions.SSLError, Urllib3SSLError, ssl.SSLError) as e:
                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt
                        print(f"SSL 錯誤（嘗試 {attempt + 1}/{max_retries}）: {str(e)}，{wait_time}秒後重試...")
                        time.sleep(wait_time)
                        continue
                    else:
                        print(f"Dify 工作流 API 調用失敗（SSL錯誤，已重试{max_retries}次）: {str(e)}")
                        raise
            
            if response is None:
                return None
                
            workflow_elapsed = time.time() - workflow_start_time
            print(f"[時間記錄] Dify 工作流 API 調用完成，耗時: {workflow_elapsed:.2f}秒")
            
            # 處理錯誤回應
            if response.status_code == 400:
                try:
                    error_data = response.json()
                    error_code = error_data.get('code', '')
                    error_message = error_data.get('message', '')
                    
                    print(f"[失敗] 工作流執行失敗: {error_code} - {error_message}")
                    
                    if error_code == 'invalid_param':
                        print("\n[提示] 輸入參數錯誤")
                        print("請確認工作流需要的輸入變數名稱")
                        print(f"當前使用的變數: {user_variable}, {text_variable}, {image_variable}")
                        print("請在 Dify 控制台檢查工作流的輸入變數設定")
                    elif error_code == 'app_unavailable':
                        print("\n" + "=" * 60)
                        print("❌ Dify 工作流不可用")
                        print("=" * 60)
                        print("可能的原因：")
                        print("1. 工作流未發布 - 請在 Dify 控制台發布工作流")
                        print("2. 工作流配置錯誤 - 請檢查工作流設定")
                        print("3. API Key 錯誤 - 請確認 API Key 是否正確")
                        print("4. 工作流已刪除或停用")
                        print("5. 輸入變數名稱錯誤 - 請確認工作流的輸入變數名稱")
                        print("\n請檢查：")
                        print(f"- API Key: {self.api_key[:30]}...")
                        print(f"- Base URL: {self.base_url}")
                        print(f"- 輸入變數: {user_variable}, {text_variable}, {image_variable}")
                        print("- 登入 Dify 控制台確認工作流狀態")
                        print("=" * 60)
                except json.JSONDecodeError:
                    pass
            
            # 檢查回應狀態
            if response.status_code not in (200, 201):
                print(f"❌ Dify API 錯誤狀態碼: {response.status_code}")
                print(f"錯誤回應: {response.text}")
                response.raise_for_status()
            
            result = response.json()
            # 提取 'data.outputs.text' 欄位（工作流回應格式）
            data = result.get('data', {})
            outputs = data.get('outputs', {}) if isinstance(data, dict) else {}
            answer = outputs.get('text', '') if isinstance(outputs, dict) else ''
            
            # 將 \n 轉換為實際換行符號
            if answer:
                answer = answer.replace('\\n', '\n')
            else:
                print("[警告] Dify 回應為空")
            
            return result
            
        except requests.exceptions.Timeout as e:
            print(f"\n❌ Dify 工作流執行超時（超過 100 秒）")
            print(f"提示：Dify API 的 blocking 模式有 Cloudflare 的 100 秒限制")
            print(f"如果工作流執行時間超過 100 秒，會自動超時")
            print(f"建議：檢查 Dify 工作流的複雜度，或考慮優化工作流執行時間")
            return None
        except (requests.exceptions.SSLError, Urllib3SSLError, ssl.SSLError) as e:
            print(f"\n❌ Dify 圖片處理失敗（SSL錯誤）: {str(e)}")
            print(f"提示：SSL 連接錯誤，可能是網路問題或伺服器 SSL 配置問題")
            import traceback
            traceback.print_exc()
            return None
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if hasattr(e, 'response') and e.response else 'N/A'
            print(f"\n❌ Dify 圖片處理失敗 (HTTP {status_code}): {str(e)}")
            if hasattr(e, 'response') and hasattr(e.response, 'text'):
                print(f"錯誤回應內容: {e.response.text}")
            return None
        except Exception as e:
            print(f"\n❌ Dify 圖片處理失敗: {str(e)}")
            import traceback
            traceback.print_exc()
            return None
    
    def _upload_file(self, file_data: bytes, user_id: str) -> Optional[str]:
        """
        上傳檔案到 Dify 並取得 file_id
        
        API: POST {base_url}/v1/files/upload
        
        Args:
            file_data: 檔案資料（bytes）
            user_id: 使用者 ID
        
        Returns:
            Optional[str]: file_id，失敗返回 None
        """
        try:
            # 驗證檔案資料不為空
            if not file_data or len(file_data) == 0:
                print("[錯誤] 檔案資料為空，無法上傳")
                return None
            
            import mimetypes
            
            url = f"{self.base_url}/v1/files/upload"
            # 對於檔案上傳，需要移除 Content-Type header，讓 requests 自動設置 multipart/form-data
            headers = {
                'Authorization': f'Bearer {self.api_key}'
            }
            data = {
                'user': user_id
            }
            
            # 判斷檔案類型（簡單判斷）
            mime_type = 'image/jpeg'
            filename = 'image.jpg'
            if len(file_data) >= 4 and file_data[:4] == b'\x89PNG':
                mime_type = 'image/png'
                filename = 'image.png'
            elif len(file_data) >= 2 and file_data[:2] == b'\xff\xd8':
                mime_type = 'image/jpeg'
                filename = 'image.jpg'
            
            # 使用 requests 的 files 參數上傳
            # 注意：不要使用 session，因為 session 的 headers 可能包含 Content-Type
            # 這會與 multipart/form-data 衝突
            files = {
                'file': (filename, file_data, mime_type)
            }
            
            # 直接使用 requests.post 而不是 session.post，避免 header 衝突
            # requests 已在檔案頂部導入
            # 添加重试逻辑处理 SSL 错误
            max_retries = 3
            response = None
            for attempt in range(max_retries):
                try:
                    response = requests.post(url, headers=headers, data=data, files=files, timeout=30)
                    break  # 成功则退出循环
                except (requests.exceptions.SSLError, Urllib3SSLError, ssl.SSLError) as e:
                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt
                        print(f"SSL 錯誤（嘗試 {attempt + 1}/{max_retries}）: {str(e)}，{wait_time}秒後重試...")
                        time.sleep(wait_time)
                        continue
                    else:
                        print(f"檔案上傳失敗（SSL錯誤，已重试{max_retries}次）: {str(e)}")
                        return None
            
            if response is None:
                return None
            
            if response.status_code in (200, 201):
                body = response.json()
                file_id = body.get('id')
                if file_id:
                    print(f"[成功] 檔案上傳成功，file_id: {file_id}")
                    return file_id
                else:
                    print(f"[錯誤] 上傳成功但未取得 file_id，回應: {body}")
                    return None
            else:
                print(f"[錯誤] 檔案上傳失敗，狀態碼: {response.status_code}")
                print(f"錯誤回應: {response.text}")
                try:
                    error_data = response.json()
                    print(f"錯誤詳情: {error_data}")
                except:
                    pass
                return None
                
        except Exception as e:
            print(f"[錯誤] 檔案上傳異常: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def get_response(self, conversation_id: str) -> Optional[Dict]:
        """
        取得對話回應（用於非同步模式）
        
        Args:
            conversation_id: 對話 ID
        
        Returns:
            Optional[Dict]: 回應資料
        """
        # 此方法用於非同步模式，目前使用 blocking 模式
        # 保留此方法以備未來擴展
        pass


class GeminiImageAPIClient:
    """與 Google Imagen API 通訊（用於圖片生成）- 優先使用 Generative Language API（更快），備用 Vertex AI API"""
    
    # 類級別的 token 緩存（所有實例共享，用於 Vertex AI API 備用）
    _cached_token = None
    _token_expiry = 0
    _token_lock = threading.Lock()
    
    # 類級別的 Token 緩存字典（支援多個 scopes）
    _token_cache = {}
    
    # 類級別的 Session（所有實例共享，用於連接復用）
    _session = None
    _session_lock = threading.Lock()
    
    def __init__(self, project_id: str = None, location: str = None):
        """
        初始化 Vertex AI Imagen API 客戶端（使用 REST API + 服務帳號認證）
        完全依賴 Cloud Run 的服務帳號，不使用 API Key
        
        Args:
            project_id: Google Cloud Project ID（如果為 None，則從環境變數讀取）
            location: Vertex AI Location（如果為 None，則從環境變數讀取，預設為 us-central1）
        """
        self.project_id = project_id or VERTEX_AI_PROJECT_ID
        self.location = location or VERTEX_AI_LOCATION
        
        if not self.project_id:
            raise ValueError("錯誤: VERTEX_AI_PROJECT_ID 環境變數未設定，請檢查 LINE.env 檔案")
        
        # 使用 Imagen 3.0 作為優先模型（更穩定，避免 SSL 錯誤）
        self.possible_models = [
            "imagen-3.0-generate-002",        # Imagen 3.0（優先，更穩定）
            "imagen-4.0-fast-generate-001",   # Imagen 4.0 Fast（備用）
            "imagen-4.0-generate-001"         # Imagen 4.0（備用）
        ]
        # 預設使用第一個模型（Imagen 3.0）
        self.current_model = self.possible_models[0]
    
    def _get_access_token(self, scopes: List[str] = None) -> str:
        """
        從 Google Cloud Metadata Server 獲取 Access Token（帶緩存）
        支援指定 scopes（用於 Generative Language API）
        
        Args:
            scopes: OAuth2 scopes 列表，例如：['https://www.googleapis.com/auth/cloud-platform']
                   如果為 None，使用默認 scope（向後兼容）
        
        Returns:
            str: OAuth 2 access token
        """
        current_time = time.time()
        
        # 如果指定了 scopes，使用不同的緩存鍵
        scope_key = ','.join(sorted(scopes)) if scopes else 'default'
        
        # 檢查緩存的 token 是否仍然有效（提前 5 分鐘刷新）
        with self._token_lock:
            # 初始化緩存字典（如果不存在）
            if not hasattr(GeminiImageAPIClient, '_token_cache'):
                GeminiImageAPIClient._token_cache = {}
            
            # 檢查是否有緩存的 token
            if scope_key == 'default':
                # 向後兼容：使用舊的緩存方式
                if self._cached_token and current_time < (self._token_expiry - 300):
                    return self._cached_token
            else:
                # 使用新的緩存字典
                cached_data = GeminiImageAPIClient._token_cache.get(scope_key)
                if cached_data and current_time < (cached_data['expiry'] - 300):
                    return cached_data['token']
        
        # 從 Metadata Server 獲取新的 token
        try:
            # 構建 URL，如果指定了 scopes 則添加 scopes 參數（需要 URL 編碼）
            from urllib.parse import urlencode
            metadata_url = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
            if scopes:
                scopes_param = ','.join(scopes)
                # URL 編碼 scopes 參數
                params = {'scopes': scopes_param}
                metadata_url = f"{metadata_url}?{urlencode(params)}"
                print(f"[調試] 請求 Token，scopes: {scopes_param}")
                print(f"[調試] Metadata URL: {metadata_url}")
            
            headers = {"Metadata-Flavor": "Google"}
            
            response = requests.get(metadata_url, headers=headers, timeout=5)
            response.raise_for_status()
            
            token_data = response.json()
            access_token = token_data["access_token"]
            expires_in = token_data.get("expires_in", 3600)  # 預設 1 小時
            
            # 調試：打印 Token 的前幾個字符（不完整顯示，避免洩露）
            print(f"[調試] 成功獲取 Token，有效期: {expires_in}秒，前10字符: {access_token[:10]}...")
            
            # 緩存 token
            with self._token_lock:
                if scope_key == 'default':
                    # 向後兼容：使用舊的緩存方式
                    self._cached_token = access_token
                    self._token_expiry = current_time + expires_in
                else:
                    # 使用新的緩存字典
                    if not hasattr(GeminiImageAPIClient, '_token_cache'):
                        GeminiImageAPIClient._token_cache = {}
                    GeminiImageAPIClient._token_cache[scope_key] = {
                        'token': access_token,
                        'expiry': current_time + expires_in
                    }
            
            return access_token
            
        except Exception as e:
            print(f"[錯誤] 無法從 Metadata Server 獲取 Access Token: {str(e)}")
            import traceback
            traceback.print_exc()
            raise ValueError(f"認證失敗: {str(e)}")
    
    def generate_image(self, prompt: str, size: str = "1024x1024", model: str = None) -> Optional[bytes]:
        """
        使用 Vertex AI Imagen API 生成圖片（已禁用 Generative Language API）
        支援多 Project ID 分散負載，提高穩定性
        
        Args:
            prompt: 圖片描述提示詞
            size: 圖片尺寸（例如: "1024x1024"），對應到 image_size 參數
            model: 使用的模型（如果為 None，則使用預設模型）
        
        Returns:
            Optional[bytes]: 生成的圖片資料（bytes），失敗返回 None
        """
        # 直接使用 Vertex AI API（已禁用 Generative Language API）
        # 已配置多 Project ID 分散負載，提高穩定性
        return self._generate_image_vertex_ai(prompt, size, model)
    
    def _get_oauth2_token(self) -> Optional[str]:
        """
        獲取 OAuth2 Access Token（用於 Generative Language API）
        優先使用 google.auth，如果不可用則使用 Metadata Server
        明確指定所需的 scopes
        """
        try:
            # Generative Language API 所需的 scopes
            required_scopes = ['https://www.googleapis.com/auth/cloud-platform']
            print(f"[調試] 請求 OAuth2 Token，scopes: {required_scopes}")
            
            if GOOGLE_AUTH_AVAILABLE:
                # 使用 google.auth（支援 Service Account JSON 和 Metadata Server）
                # 明確指定 scopes
                print("[調試] 使用 google.auth.default() 獲取憑證")
                credentials, project = default(scopes=required_scopes)
                print(f"[調試] 獲取的 Project: {project}")
                
                if not credentials.valid:
                    print("[調試] 憑證無效，正在刷新...")
                    credentials.refresh(Request())
                
                token = credentials.token
                print(f"[調試] 成功獲取 OAuth2 Token（google.auth），前10字符: {token[:10]}...")
                return token
            else:
                # 回退到 Metadata Server（Cloud Run 環境）
                # 明確指定 scopes
                print("[調試] 使用 Metadata Server 獲取 Token")
                token = self._get_access_token(scopes=required_scopes)
                print(f"[調試] 成功獲取 OAuth2 Token（Metadata Server），前10字符: {token[:10]}...")
                return token
        except Exception as e:
            print(f"[警告] 無法獲取 OAuth2 Token: {str(e)}")
            import traceback
            traceback.print_exc()
            return None
    
    def _generate_image_generative_api(self, prompt: str) -> Optional[bytes]:
        """
        使用 Google Generative Language API 生成圖片（已禁用）
        此方法已不再使用，改為直接使用 Vertex AI API
        """
        # 此方法已禁用，不再使用 Generative Language API
        print("[提示] Generative Language API 已禁用，使用 Vertex AI API")
        return None
        
        # 以下代碼已不再使用（保留作為參考）
        try:
            # 使用 API Key 認證（通過 URL 參數，參考 main.py）
            # 注意：此方法已禁用，不會執行到這裡
            url = f"https://generativelanguage.googleapis.com/v1beta/models/imagen-4.0-fast-generate-001:predict?key=DISABLED"
            headers = {
                "Content-Type": "application/json"
            }
            payload = {
                "instances": [{"prompt": prompt}],
                "parameters": {"sampleCount": 1, "aspectRatio": "1:1"}
            }
            
            # 使用 Session 復用連接，提高性能
            with self._session_lock:
                if GeminiImageAPIClient._session is None:
                    GeminiImageAPIClient._session = requests.Session()
                    adapter = requests.adapters.HTTPAdapter(
                        pool_connections=10,
                        pool_maxsize=20,
                        max_retries=requests.adapters.Retry(
                            total=3,
                            backoff_factor=1,
                            status_forcelist=[500, 502, 503, 504],
                            allowed_methods=["POST"]
                        )
                    )
                    GeminiImageAPIClient._session.mount("https://", adapter)
            
            # 重試機制：最多重試 3 次
            max_retries = 3
            retry_delay = 2
            
            for attempt in range(max_retries):
                try:
                    api_start_time = time.time()
                    response = GeminiImageAPIClient._session.post(
                        url, 
                        headers=headers, 
                        json=payload, 
                        timeout=(10, 300)  # (連接超時10秒, 讀取超時300秒)
                    )
                    api_elapsed = time.time() - api_start_time
                    print(f"[時間記錄] Generative Language API (API Key) 調用耗時: {api_elapsed:.2f}秒")
                    
                    if response.status_code == 200:
                        result = response.json()
                        b64_image = result.get('predictions', [{}])[0].get('bytesBase64Encoded')
                        if b64_image:
                            image_bytes = base64.b64decode(b64_image)
                            return image_bytes
                        else:
                            print("[錯誤] API 回應中沒有找到圖片數據（bytesBase64Encoded）")
                            return None
                    else:
                        error_msg = response.text[:200]
                        print(f"[錯誤] API 調用失敗 ({response.status_code}): {error_msg}")
                        
                        if response.status_code in [401, 403]:
                            # API Key 無效或權限不足（此方法已禁用）
                            if response.status_code == 401:
                                print(f"[錯誤] API Key 認證失敗（此方法已禁用）")
                            else:
                                print(f"[錯誤] API Key 權限不足（此方法已禁用）")
                            return None
                        
                        if attempt < max_retries - 1:
                            wait_time = retry_delay * (2 ** attempt)
                            print(f"[提示] {wait_time}秒後重試...")
                            time.sleep(wait_time)
                            continue
                        return None
                        
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, BrokenPipeError, OSError) as e:
                    if attempt < max_retries - 1:
                        wait_time = retry_delay * (2 ** attempt)
                        print(f"[警告] 連接錯誤（嘗試 {attempt + 1}/{max_retries}）: {str(e)}")
                        print(f"[提示] {wait_time}秒後重試...")
                        time.sleep(wait_time)
                        continue
                    else:
                        print(f"[錯誤] 連接失敗（已重試 {max_retries} 次）: {str(e)}")
                        return None
                        
        except Exception as e:
            print(f"[錯誤] Generative Language API (API Key) 調用失敗: {str(e)}")
            import traceback
            traceback.print_exc()
            return None
    
    def _generate_image_vertex_ai(self, prompt: str, size: str = "1024x1024", model: str = None) -> Optional[bytes]:
        """
        使用 Vertex AI Imagen API 生成圖片（備用方案）
        
        Args:
            prompt: 圖片描述提示詞
            size: 圖片尺寸（例如: "1024x1024"），對應到 image_size 參數
            model: 使用的模型（如果為 None，則使用預設模型）
        
        Returns:
            Optional[bytes]: 生成的圖片資料（bytes），失敗返回 None
        """
        try:
            # 如果沒有指定模型，使用當前模型
            if model is None:
                model = self.current_model
            else:
                self.current_model = model
            
            # 獲取 Access Token
            access_token = self._get_access_token()
            
            # 構建 API URL
            api_url = f"https://{self.location}-aiplatform.googleapis.com/v1/projects/{self.project_id}/locations/{self.location}/publishers/google/models/{model}:predict"
            
            # 設置請求頭
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            }
            
            # 構建請求體（對應 Postman 的格式）
            payload = {
                "instances": [
                    {
                        "prompt": prompt
                    }
                ],
                "parameters": {
                    "sampleCount": 1,
                    "aspectRatio": "1:1"
                }
            }
            
            # 為每個請求創建獨立的 Session（避免併行請求時的連接衝突）
            def create_session():
                """創建新的 Session 和適配器"""
                session = requests.Session()
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=5,
                    pool_maxsize=10,
                    max_retries=requests.adapters.Retry(
                        total=0,  # 禁用 urllib3 的重試，使用代碼層面的重試（更可控）
                        backoff_factor=0,
                        status_forcelist=[],
                        allowed_methods=[]
                    )
                )
                session.mount("https://", adapter)
                return session
            
            session = create_session()
            
            # 重試機制：最多重試 5 次（增加重試次數以應對 SSL 錯誤）
            max_retries = 5
            retry_delay = 2
            
            try:
                for attempt in range(max_retries):
                    try:
                        # 每次重試前重新獲取 Token（以防過期）
                        if attempt > 0:
                            access_token = self._get_access_token()
                            headers["Authorization"] = f"Bearer {access_token}"
                        
                        # 發送請求
                        api_start_time = time.time()
                        response = session.post(
                            api_url, 
                            json=payload, 
                            headers=headers, 
                            timeout=(15, 300)  # 增加連接超時到15秒，讀取超時300秒
                        )
                        api_elapsed = time.time() - api_start_time
                        print(f"[時間記錄] Vertex AI API 調用耗時: {api_elapsed:.2f}秒")
                        
                        response.raise_for_status()
                        
                        # 解析響應
                        result = response.json()
                        
                        # 檢查響應格式
                        if "predictions" in result and len(result["predictions"]) > 0:
                            prediction = result["predictions"][0]
                            
                            # 圖片數據在 "bytesBase64Encoded" 字段中
                            if "bytesBase64Encoded" in prediction:
                                image_base64 = prediction["bytesBase64Encoded"]
                                image_bytes = base64.b64decode(image_base64)
                                return image_bytes
                            else:
                                print("[錯誤] API 回應中沒有找到圖片數據（bytesBase64Encoded）")
                                return None
                        else:
                            print("[錯誤] API 回應中沒有 predictions")
                            return None
                            
                    except (requests.exceptions.SSLError, Urllib3SSLError, ssl.SSLError) as e:
                        # 明確處理 SSL 錯誤
                        if attempt < max_retries - 1:
                            wait_time = retry_delay * (2 ** attempt)
                            print(f"[警告] SSL 錯誤（嘗試 {attempt + 1}/{max_retries}）: {str(e)}")
                            print(f"[提示] {wait_time}秒後重試...")
                            
                            # 關閉舊 Session 並重新創建（避免使用已損壞的連接）
                            try:
                                session.close()
                            except:
                                pass
                            session = create_session()
                            
                            time.sleep(wait_time)
                            continue
                        else:
                            print(f"[錯誤] SSL 錯誤（已重試 {max_retries} 次）: {str(e)}")
                            # 嘗試備用模型
                            if model == self.possible_models[0] and len(self.possible_models) > 1:
                                return self._try_fallback_model(prompt, payload)
                            return None
                            
                    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, BrokenPipeError, OSError) as e:
                        if attempt < max_retries - 1:
                            wait_time = retry_delay * (2 ** attempt)
                            print(f"[警告] 連接錯誤（嘗試 {attempt + 1}/{max_retries}）: {str(e)}")
                            print(f"[提示] {wait_time}秒後重試...")
                            
                            # 重新建立連接
                            try:
                                session.close()
                            except:
                                pass
                            session = create_session()
                            
                            time.sleep(wait_time)
                            continue
                        else:
                            print(f"[錯誤] 連接失敗（已重試 {max_retries} 次）: {str(e)}")
                            # 嘗試備用模型
                            if model == self.possible_models[0] and len(self.possible_models) > 1:
                                return self._try_fallback_model(prompt, payload)
                            return None
                            
                    except requests.exceptions.HTTPError as e:
                        # HTTP 錯誤，嘗試備用模型
                        if model == self.possible_models[0] and len(self.possible_models) > 1:
                            return self._try_fallback_model(prompt, payload)
                        else:
                            print(f"[錯誤] HTTP 錯誤: {e.response.status_code} - {e.response.text[:200]}")
                            return None
                            
                    except Exception as e:
                        print(f"[錯誤] 生成圖片失敗: {str(e)}")
                        if attempt < max_retries - 1:
                            wait_time = retry_delay * (2 ** attempt)
                            print(f"[提示] {wait_time}秒後重試...")
                            time.sleep(wait_time)
                            continue
                        import traceback
                        traceback.print_exc()
                        return None
                
                return None
            finally:
                # 確保 Session 被關閉
                try:
                    session.close()
                except:
                    pass
                
        except Exception as e:
            print(f"[錯誤] Vertex AI API 調用失敗: {str(e)}")
            import traceback
            traceback.print_exc()
            return None
    
    def _try_fallback_model(self, prompt: str, payload: dict) -> Optional[bytes]:
        """嘗試使用備用模型（帶重試機制和 SSL 錯誤處理）"""
        if len(self.possible_models) <= 1:
            return None
            
        fallback_model = self.possible_models[1]
        print(f"[提示] 嘗試備用模型: {fallback_model}")
        
        # 為備用模型創建獨立的 Session
        def create_session():
            """創建新的 Session 和適配器"""
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=5,
                pool_maxsize=10,
                max_retries=requests.adapters.Retry(
                    total=0,  # 禁用 urllib3 的重試，使用代碼層面的重試
                    backoff_factor=0,
                    status_forcelist=[],
                    allowed_methods=[]
                )
            )
            session.mount("https://", adapter)
            return session
        
        session = create_session()
        
        max_retries = 3  # 增加重試次數
        retry_delay = 2
        
        try:
            for attempt in range(max_retries):
                try:
                    time.sleep(1)  # 短暫延遲
                    
                    # 每次重試前重新獲取 token（以防過期）
                    if attempt > 0:
                        access_token = self._get_access_token()
                    else:
                        access_token = self._get_access_token()
                    
                    # 使用備用模型
                    api_url = f"https://{self.location}-aiplatform.googleapis.com/v1/projects/{self.project_id}/locations/{self.location}/publishers/google/models/{fallback_model}:predict"
                    headers = {
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json"
                    }
                    
                    fallback_start_time = time.time()
                    fallback_response = session.post(
                        api_url, 
                        json=payload, 
                        headers=headers, 
                        timeout=(15, 300)  # 增加連接超時到15秒
                    )
                    fallback_elapsed = time.time() - fallback_start_time
                    print(f"[時間記錄] 備用模型 REST API 調用耗時: {fallback_elapsed:.2f}秒")
                    
                    fallback_response.raise_for_status()
                    fallback_result = fallback_response.json()
                    
                    if "predictions" in fallback_result and len(fallback_result["predictions"]) > 0:
                        prediction = fallback_result["predictions"][0]
                        if "bytesBase64Encoded" in prediction:
                            self.current_model = fallback_model
                            image_bytes = base64.b64decode(prediction["bytesBase64Encoded"])
                            return image_bytes
                    
                    print("[錯誤] 備用模型回應中沒有圖片")
                    return None
                    
                except (requests.exceptions.SSLError, Urllib3SSLError, ssl.SSLError) as e:
                    # 明確處理 SSL 錯誤
                    if attempt < max_retries - 1:
                        wait_time = retry_delay * (2 ** attempt)
                        print(f"[警告] 備用模型 SSL 錯誤（嘗試 {attempt + 1}/{max_retries}）: {str(e)}")
                        print(f"[提示] {wait_time}秒後重試...")
                        
                        # 關閉舊 Session 並重新創建
                        try:
                            session.close()
                        except:
                            pass
                        session = create_session()
                        
                        time.sleep(wait_time)
                        continue
                    else:
                        print(f"[錯誤] 備用模型 SSL 錯誤（已重試 {max_retries} 次）: {str(e)}")
                        return None
                        
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, BrokenPipeError, OSError) as e:
                    if attempt < max_retries - 1:
                        wait_time = retry_delay * (2 ** attempt)
                        print(f"[警告] 備用模型連接錯誤（嘗試 {attempt + 1}/{max_retries}）: {str(e)}")
                        print(f"[提示] {wait_time}秒後重試...")
                        
                        # 重新建立連接
                        try:
                            session.close()
                        except:
                            pass
                        session = create_session()
                        
                        time.sleep(wait_time)
                        continue
                    else:
                        print(f"[錯誤] 備用模型也失敗: {str(e)}")
                        return None
                        
                except Exception as fallback_error:
                    print(f"[錯誤] 備用模型也失敗: {str(fallback_error)}")
                    if attempt < max_retries - 1:
                        wait_time = retry_delay * (2 ** attempt)
                        print(f"[提示] {wait_time}秒後重試...")
                        time.sleep(wait_time)
                        continue
                    import traceback
                    traceback.print_exc()
                    return None
            
            return None
        finally:
            # 確保 Session 被關閉
            try:
                session.close()
            except:
                pass


class ImageProcessor:
    """處理圖片"""
    
    @staticmethod
    def is_valid_image(image_data: bytes) -> bool:
        """
        驗證檔案是否為有效的圖片格式
        
        Args:
            image_data: 檔案資料（bytes）
        
        Returns:
            bool: 是否為有效的圖片格式
        """
        if not image_data or len(image_data) < 4:
            return False
        
        # 檢查常見圖片格式的檔案頭（magic numbers）
        # JPEG: FF D8 FF
        if image_data[:3] == b'\xff\xd8\xff':
            return True
        # PNG: 89 50 4E 47
        if image_data[:4] == b'\x89PNG':
            return True
        # GIF: 47 49 46 38
        if image_data[:4] == b'GIF8':
            return True
        # BMP: 42 4D
        if image_data[:2] == b'BM':
            return True
        # WebP: RIFF...WEBP
        if image_data[:4] == b'RIFF' and len(image_data) > 8 and image_data[8:12] == b'WEBP':
            return True
        
        return False
    
    @staticmethod
    def download_from_line(message_id: str, access_token: str) -> Optional[bytes]:
        """
        從 LINE 下載圖片
        
        Args:
            message_id: 圖片訊息 ID
            access_token: LINE Access Token
        
        Returns:
            Optional[bytes]: 圖片資料
        """
        line_client = LINEAPIClient(access_token)
        return line_client.download_image(message_id)
    
    @staticmethod
    def download_from_url(image_url: str, timeout: int = 30) -> Optional[bytes]:
        """
        從 URL 下載圖片
        
        Args:
            image_url: 圖片網址
            timeout: 請求超時時間（秒）
        
        Returns:
            Optional[bytes]: 圖片資料，失敗返回 None
        """
        try:
            response = requests.get(image_url, timeout=timeout)
            response.raise_for_status()
            
            # 檢查是否為圖片
            content_type = response.headers.get('Content-Type', '')
            if not content_type.startswith('image/'):
                print(f"警告: 下載的內容不是圖片 (Content-Type: {content_type})")
            
            image_data = response.content
            return image_data
        except Exception as e:
            print(f"從網址下載圖片失敗: {str(e)}")
            return None
    
    @staticmethod
    def convert_to_base64(image_data: bytes) -> str:
        """
        轉換圖片為 Base64
        
        公式: base64_data = base64.b64encode(image_data).decode('utf-8')
        
        Args:
            image_data: 圖片資料（bytes）
        
        Returns:
            str: Base64 編碼的字串
        """
        return base64.b64encode(image_data).decode('utf-8')
    
    @staticmethod
    def resize_image(image_data: bytes, max_size: Tuple[int, int] = (1024, 1024)) -> bytes:
        """
        調整圖片大小（可選）
        
        Args:
            image_data: 原始圖片資料
            max_size: 最大尺寸 (width, height)
        
        Returns:
            bytes: 調整後的圖片資料
        """
        try:
            image = Image.open(io.BytesIO(image_data))
            image.thumbnail(max_size, Image.Resampling.LANCZOS)
            
            # 轉換回 bytes
            output = io.BytesIO()
            image_format = image.format or 'JPEG'
            image.save(output, format=image_format)
            return output.getvalue()
        except Exception as e:
            print(f"圖片調整失敗: {str(e)}")
            return image_data  # 返回原始圖片


class MessageFlowController:
    """控制訊息流程"""
    
    def __init__(self, dify_client: DifyAPIClient, line_client: LINEAPIClient):
        """
        初始化訊息流程控制器
        
        Args:
            dify_client: Dify API 客戶端
            line_client: LINE API 客戶端
        """
        self.dify_client = dify_client
        self.line_client = line_client
        # 儲存使用者的對話 ID（維持上下文）
        self.conversations = {}
        # 圖片事件緩衝區（按使用者分組）
        self.image_buffer = defaultdict(list)
        # 緩衝區鎖（線程安全）
        self.buffer_lock = threading.Lock()
        # 等待時間（秒）- 收集多張圖片的時間窗口
        self.buffer_wait_time = 1.5
        # 追蹤每個使用者的定時器
        self.user_timers = {}
    
    def process_line_image(self, event: Dict) -> bool:
        """
        處理單張 LINE 圖片事件（帶緩衝機制）
        
        流程：
        1. 將圖片事件加入緩衝區
        2. 等待一段時間收集更多圖片
        3. 批次處理所有圖片
        
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
        
        with self.buffer_lock:
            # 將事件加入緩衝區
            self.image_buffer[user_id].append(event)
            buffer_size = len(self.image_buffer[user_id])
            
            # 如果有 imageSet 資訊，檢查是否已收集完整
            if image_set:
                total = image_set.get('total')
                if total and buffer_size >= total:
                    # 已收集完整，立即處理
                    events = self.image_buffer[user_id].copy()
                    self.image_buffer[user_id].clear()
                    if user_id in self.user_timers:
                        self.user_timers[user_id].cancel()
                        del self.user_timers[user_id]
                    # 在背景線程中處理，避免阻塞
                    threading.Thread(target=self._process_buffered_images, 
                                    args=(user_id, events), daemon=True).start()
                    return True
            
            # 取消舊的定時器（如果存在）
            if user_id in self.user_timers:
                self.user_timers[user_id].cancel()
            
            # 設置新的定時器
            timer = threading.Timer(self.buffer_wait_time, 
                                   self._process_buffered_images_timeout, 
                                   args=(user_id,))
            timer.start()
            self.user_timers[user_id] = timer
        
        return True
    
    def _process_buffered_images_timeout(self, user_id: str):
        """
        定時器觸發：處理緩衝區中的圖片
        
        Args:
            user_id: 使用者 ID
        """
        with self.buffer_lock:
            if user_id in self.image_buffer and len(self.image_buffer[user_id]) > 0:
                events = self.image_buffer[user_id].copy()
                self.image_buffer[user_id].clear()
                if user_id in self.user_timers:
                    del self.user_timers[user_id]
                # 在背景線程中處理
                threading.Thread(target=self._process_buffered_images, 
                                args=(user_id, events), daemon=True).start()
    
    def _process_buffered_images(self, user_id: str, events: List[Dict]):
        """
        處理緩衝區中的圖片事件（實際處理邏輯）
        
        Args:
            user_id: 使用者 ID
            events: 圖片事件列表
        """
        try:
            self.process_line_images(events)
        except Exception as e:
            print(f"處理緩衝圖片失敗: {str(e)}")
            import traceback
            traceback.print_exc()
    
    def process_line_images(self, events: List[Dict]) -> bool:
        """
        處理單張或多張 LINE 圖片事件（批次處理）
        
        流程：
        1. 從事件中提取訊息 ID
        2. 並行下載所有圖片
        3. 批次轉發到 Dify
        4. 發送結果到 LINE
        
        Args:
            events: LINE 圖片事件列表（單張或多張）
        
        Returns:
            bool: 是否成功處理
        """
        try:
            total_start_time = time.time()  # 總開始時間
            print(f"[時間記錄] ========== 開始處理 {len(events)} 張圖片 ==========")
            
            if not events:
                print("錯誤: 沒有圖片事件")
                return False
            
            user_id = events[0].get('user_id')
            reply_token = events[0].get('reply_token')
            
            if not user_id:
                print("錯誤: 缺少使用者 ID")
                return False
            
            # 步驟 1: 並行下載所有圖片（優化：使用 ThreadPoolExecutor）
            download_start = time.time()
            print(f"[時間記錄] [1/4] 開始並行下載圖片...")
            
            def download_single_image(event: Dict) -> Optional[bytes]:
                """下載單張圖片（用於並行處理）"""
                message_id = event.get('message_id')
                if not message_id:
                    return None
                
                img_start = time.time()
                # image_data = ImageProcessor.download_from_line(message_id, LINE_CHANNEL_ACCESS_TOKEN)
                # 改用 self.line_client.download_image 以復用 Session 連接
                image_data = self.line_client.download_image(message_id)
                img_elapsed = time.time() - img_start
                if image_data:
                    print(f"[時間記錄] 下載圖片完成，耗時: {img_elapsed:.2f}秒")
                
                if not image_data:
                    return None
                
                # 驗證檔案格式是否為圖片
                if not ImageProcessor.is_valid_image(image_data):
                    return None
                
                # 可選 - 調整圖片大小（如果太大）
                if len(image_data) > 5 * 1024 * 1024:  # 5MB
                    image_data = ImageProcessor.resize_image(image_data, max_size=(1024, 1024))
                
                return image_data
            
            # 並行下載所有圖片
            image_data_list = []
            with ThreadPoolExecutor(max_workers=min(len(events), 5)) as executor:
                download_futures = {
                    executor.submit(download_single_image, event): i 
                    for i, event in enumerate(events)
                }
                
                # 收集結果（保持順序）
                temp_image_list = [None] * len(events)
                for future in as_completed(download_futures):
                    index = download_futures[future]
                    try:
                        image_data = future.result()
                        temp_image_list[index] = image_data
                    except Exception as e:
                        print(f"下載圖片 {index+1} 失敗: {str(e)}")
                
                image_data_list = [img for img in temp_image_list if img is not None]
            
            download_elapsed = time.time() - download_start
            print(f"[時間記錄] [1/4] 下載完成，總耗時: {download_elapsed:.2f}秒")
            
            if not image_data_list:
                error_msg = "抱歉，無法下載圖片，請稍後再試。"
                if reply_token:
                    self.line_client.reply_message(reply_token, error_msg)
                return False
            
            # 檢查是否有無效圖片
            if len(image_data_list) < len(events):
                error_msg = "上傳格式錯誤，請重新上傳。"
                if reply_token:
                    self.line_client.reply_message(reply_token, error_msg)
                else:
                    self.line_client.send_text_message(user_id, error_msg)
                return False
            
            # 步驟 2: 發送圖片到 Dify（添加時間記錄）
            dify_start = time.time()
            print(f"[時間記錄] [2/4] 開始 Dify 處理...")
            
            conversation_id = self.conversations.get(user_id)
            if len(image_data_list) == 1:
                query_text = "請分析這張圖片並提供詳細說明"
            else:
                query_text = f"請分析這 {len(image_data_list)} 張圖片並提供詳細說明"
            
            dify_response = self.dify_client.send_image(
                image_data=image_data_list if len(image_data_list) > 1 else image_data_list[0],
                conversation_id=conversation_id,
                user_id=user_id,
                query=query_text
            )
            
            dify_elapsed = time.time() - dify_start
            print(f"[時間記錄] [2/4] Dify 處理完成，耗時: {dify_elapsed:.2f}秒")
            
            if not dify_response:
                error_msg = "抱歉，圖片處理失敗，請稍後再試。"
                self._send_message_with_fallback(user_id, reply_token, 'text', error_msg)
                return False
            
            # 步驟 5: 提取 Dify 回應（從 data.outputs 提取所有變數）
            data = dify_response.get('data', {})
            outputs = data.get('outputs', {}) if isinstance(data, dict) else {}
            
            # 提取所有變數
            text = outputs.get('text', '') if isinstance(outputs, dict) else ''
            picture_1 = outputs.get('picture_1', '') if isinstance(outputs, dict) else ''
            picture_2 = outputs.get('picture_2', '') if isinstance(outputs, dict) else ''
            picture_3 = outputs.get('picture_3', '') if isinstance(outputs, dict) else ''
            dish_1 = outputs.get('dish_1', '') if isinstance(outputs, dict) else ''
            dish_2 = outputs.get('dish_2', '') if isinstance(outputs, dict) else ''
            dish_3 = outputs.get('dish_3', '') if isinstance(outputs, dict) else ''
            
            # 將 \n 轉換為實際換行符號
            if text:
                text = text.replace('\\n', '\n')
            
            # 驗證 text 是否有效
            if not text or not text.strip():
                print("[錯誤] text 為空或無效")
                text = "抱歉，無法取得回應內容。"
            
            # 工作流通常不支援 conversation_id，但保留以備不時之需
            new_conversation_id = dify_response.get('conversation_id')
            
            # 儲存對話 ID
            if new_conversation_id:
                self.conversations[user_id] = new_conversation_id
            
            # 存儲 text 和食譜數據（用於 Postback 事件）
            global user_recipe_storage, recipe_storage_lock, user_text_storage
            with recipe_storage_lock:
                user_recipe_storage[user_id] = {
                    'dish_1': dish_1,
                    'dish_2': dish_2,
                    'dish_3': dish_3
                }
                user_text_storage[user_id] = text
                
                # 設置清理定時器（30分鐘後刪除）
                def cleanup_recipe():
                    time.sleep(1800)  # 30分鐘
                    with recipe_storage_lock:
                        if user_id in user_recipe_storage:
                            del user_recipe_storage[user_id]
                        if user_id in user_text_storage:
                            del user_text_storage[user_id]
                
                threading.Thread(target=cleanup_recipe, daemon=True).start()
            
            # 步驟 6: 從 Dify 回應中提取圖片並創建 Image Carousel
            if picture_1 and picture_2 and picture_3:
                image_gen_start = time.time()
                print(f"[時間記錄] [3/4] 開始處理 Dify 回傳的圖片...")
                
                # 輔助函數：處理 Dify 回傳的圖片資料（支援 URL 和 base64）
                def parse_dify_image(picture_data: any, index: int) -> Optional[str]:
                    """從 Dify 回傳的資料中提取圖片 URL"""
                    if not picture_data:
                        print(f"[錯誤] picture_{index+1} 為空")
                        return None
                    
                    try:
                        # case 1: 如果是列表（新格式），直接提取 URL
                        if isinstance(picture_data, list):
                            if len(picture_data) > 0:
                                item = picture_data[0]
                                if isinstance(item, dict) and 'url' in item:
                                    url = item['url']
                                    print(f"[成功] picture_{index+1} 取得 URL: {url}")
                                    return url
                            print(f"[錯誤] picture_{index+1} 是列表但格式不符")
                            return None

                        # case 2: 如果是字串，嘗試解析 JSON
                        if isinstance(picture_data, str):
                            try:
                                parsed_data = json.loads(picture_data)
                                # 如果解析後是列表（JSON 字串格式的新格式）
                                if isinstance(parsed_data, list):
                                    if len(parsed_data) > 0:
                                        item = parsed_data[0]
                                        if isinstance(item, dict) and 'url' in item:
                                            url = item['url']
                                            print(f"[成功] picture_{index+1} 取得 URL (from JSON): {url}")
                                            return url
                                
                                # 舊格式處理 (base64)
                                picture_data = parsed_data
                            except json.JSONDecodeError:
                                # 如果不是 JSON，假設它就是 URL (如果看起來像的話)
                                if picture_data.startswith('http'):
                                    return picture_data
                                pass

                        # 舊格式：處理 dict 中的 predictions -> bytesBase64Encoded
                        if isinstance(picture_data, dict):
                            # 提取 predictions 陣列中的 bytesBase64Encoded
                            if "predictions" in picture_data and len(picture_data["predictions"]) > 0:
                                prediction = picture_data["predictions"][0]
                                if "bytesBase64Encoded" in prediction:
                                    image_base64 = prediction["bytesBase64Encoded"]
                                    
                                    # 將 base64 轉換為 bytes
                                    try:
                                        image_bytes = base64.b64decode(image_base64)
                                        print(f"[成功] picture_{index+1} base64 解碼成功，圖片大小: {len(image_bytes)} bytes")
                                        
                                        # 保存到臨時存儲並獲取 URL
                                        global temp_image_counter
                                        with temp_image_lock:
                                            temp_image_counter += 1
                                            image_id = f"img_{int(time.time())}_{temp_image_counter}"
                                            temp_image_storage[image_id] = image_bytes
                                            
                                            base_url = os.getenv('BASE_URL', '')
                                            if not base_url:
                                                host = os.getenv('HOST', '0.0.0.0')
                                                port = os.getenv('PORT', '5000')
                                                protocol = 'https' if os.getenv('HTTPS', '').lower() == 'true' else 'http'
                                                if host == '0.0.0.0':
                                                    host = 'localhost'
                                                base_url = f"{protocol}://{host}:{port}"
                                            
                                            image_url = f"{base_url}/temp_image/{image_id}"
                                            
                                            # 設置清理定時器
                                            def cleanup_image(img_id):
                                                time.sleep(1800)
                                                with temp_image_lock:
                                                    if img_id in temp_image_storage:
                                                        del temp_image_storage[img_id]
                                            
                                            threading.Thread(target=cleanup_image, args=(image_id,), daemon=True).start()
                                            return image_url
                                    except Exception as decode_error:
                                        print(f"[錯誤] picture_{index+1} base64 解碼失敗: {str(decode_error)}")
                                        return None
                            
                            # 如果 dict 中有 url 字段 (可能是解析後的單個對象)
                            if 'url' in picture_data:
                                return picture_data['url']

                        print(f"[錯誤] picture_{index+1} 格式無法識別或沒有 URL/Image 數據")
                        return None

                    except Exception as e:
                        print(f"[錯誤] picture_{index+1} 處理失敗: {str(e)}")
                        import traceback
                        traceback.print_exc()
                        return None
                
                # 並行處理三張圖片
                picture_data_list = [picture_1, picture_2, picture_3]
                image_urls = [None, None, None]  # 預先分配，保持順序
                
                with ThreadPoolExecutor(max_workers=3) as executor:
                    # 提交三個任務
                    future_to_index = {
                        executor.submit(parse_dify_image, picture_data, i): i 
                        for i, picture_data in enumerate(picture_data_list)
                    }
                    
                    # 收集結果（保持順序）
                    for future in as_completed(future_to_index):
                        index = future_to_index[future]
                        try:
                            image_url = future.result()
                            image_urls[index] = image_url
                        except Exception as e:
                            print(f"處理圖片 {index+1} 失敗: {str(e)}")
                            image_urls[index] = None
                
                image_gen_elapsed = time.time() - image_gen_start
                print(f"[時間記錄] [3/4] Dify 圖片處理完成，總耗時: {image_gen_elapsed:.2f}秒")
                
                # 提取菜肴名稱（從 dish_1, dish_2, dish_3 中提取）
                import re
                dish_names = []
                for dish in [dish_1, dish_2, dish_3]:
                    if dish and isinstance(dish, str):
                        # 使用正則表達式提取 "### 菜餚名稱：XXX" 或 "菜餚名稱：XXX" 中的 XXX
                        match = re.search(r'(?:###\s*)?菜餚名稱[：:]\s*(.+)', dish)
                        if match:
                            dish_name = match.group(1).strip().split('\n')[0]  # 只取第一行
                            dish_names.append(dish_name)
                        else:
                            # 如果找不到，使用前20個字符作為名稱
                            dish_names.append(dish[:20] if len(dish) > 20 else dish)
                    else:
                        dish_names.append("未知菜餚")
                
                # 創建 Image Carousel columns
                columns = []
                for i, (image_url, dish_name) in enumerate(zip(image_urls, dish_names), 1):
                    if image_url:
                        columns.append({
                            'imageUrl': image_url,
                            'action': {
                                'type': 'postback',
                                'label': '查看更多',
                                'data': f'recipe_select={i}'
                            }
                        })
                
                # 步驟 7: 發送結果（添加時間記錄，使用 fallback 機制處理 reply_token 過期）
                send_start = time.time()
                print(f"[時間記錄] [4/4] 開始發送結果...")
                
                # 發送 Image Carousel（使用 fallback 機制，reply_token 過期時自動使用 push）
                if len(columns) >= 3:
                    success = self._send_message_with_fallback(user_id, reply_token, 'image_carousel', columns)
                    
                    send_elapsed = time.time() - send_start
                    print(f"[時間記錄] [4/4] 發送完成，耗時: {send_elapsed:.2f}秒")
                    
                    total_elapsed = time.time() - total_start_time
                    print(f"[時間記錄] ========== 總耗時: {total_elapsed:.2f}秒 ==========")
                    print(f"[時間記錄] 時間分布: 下載={download_elapsed:.1f}s, Dify={dify_elapsed:.1f}s, 圖片處理={image_gen_elapsed:.1f}s, 發送={send_elapsed:.1f}s")
                    
                    return success
                elif len(columns) > 0:
                    # 如果圖片不足3張，仍然發送
                    success = self._send_message_with_fallback(user_id, reply_token, 'image_carousel', columns)
                    
                    send_elapsed = time.time() - send_start
                    print(f"[時間記錄] [4/4] 發送完成，耗時: {send_elapsed:.2f}秒")
                    
                    total_elapsed = time.time() - total_start_time
                    print(f"[時間記錄] ========== 總耗時: {total_elapsed:.2f}秒 ==========")
                    
                    return success
                else:
                    # 如果無法生成 Image Carousel，至少發送 text
                    success = self._send_message_with_fallback(user_id, reply_token, 'text', text)
                    
                    send_elapsed = time.time() - send_start
                    print(f"[時間記錄] [4/4] 發送完成，耗時: {send_elapsed:.2f}秒")
                    
                    total_elapsed = time.time() - total_start_time
                    print(f"[時間記錄] ========== 總耗時: {total_elapsed:.2f}秒 ==========")
                    
                    return success
            else:
                # 如果無法生成 Image Carousel，至少發送 text
                send_start = time.time()
                print(f"[時間記錄] [4/4] 開始發送結果...")
                
                success = self._send_message_with_fallback(user_id, reply_token, 'text', text)
                
                send_elapsed = time.time() - send_start
                print(f"[時間記錄] [4/4] 發送完成，耗時: {send_elapsed:.2f}秒")
                
                total_elapsed = time.time() - total_start_time
                print(f"[時間記錄] ========== 總耗時: {total_elapsed:.2f}秒 ==========")
                print(f"[時間記錄] 時間分布: 下載={download_elapsed:.1f}s, Dify={dify_elapsed:.1f}s, 發送={send_elapsed:.1f}s")
                
                return success
                
        except Exception as e:
            total_elapsed = time.time() - total_start_time if 'total_start_time' in locals() else 0
            print(f"[時間記錄] 處理失敗，總耗時: {total_elapsed:.2f}秒")
            print(f"處理圖片事件失敗: {str(e)}")
            import traceback
            traceback.print_exc()
            
            # 發送錯誤訊息給用戶（使用 fallback 機制）
            try:
                user_id = events[0].get('user_id') if events else None
                reply_token = events[0].get('reply_token') if events else None
                if user_id:
                    error_msg = "處理圖片時發生錯誤，請稍後再試。"
                    self._send_message_with_fallback(user_id, reply_token, 'text', error_msg)
            except:
                pass  # 如果發送錯誤訊息也失敗，忽略
            
            return False
    
    def _send_message_with_fallback(self, user_id: str, reply_token: Optional[str], 
                                    message_type: str, content: any) -> bool:
        """
        發送訊息，優先使用 reply_token，失敗則回退到 push message
        
        Args:
            user_id: 使用者 ID
            reply_token: 回覆 Token（可能為 None 或已過期）
            message_type: 訊息類型 ('text', 'image_carousel')
            content: 訊息內容（文字或 columns）
        
        Returns:
            bool: 是否成功發送
        """
        if message_type == 'text':
            # 發送文字訊息
            if reply_token:
                # 優先嘗試 reply，如果失敗則使用 push
                success = self.line_client.reply_message(reply_token, content)
                if not success:
                    print(f"[提示] reply_token 可能已過期，改用 push message")
                    success = self.line_client.send_text_message(user_id, content)
                return success
            else:
                return self.line_client.send_text_message(user_id, content)
        elif message_type == 'image_carousel':
            # 發送 Image Carousel
            if reply_token:
                # 優先嘗試 reply，如果失敗則使用 push
                success = self.line_client.reply_image_carousel(reply_token, content)
                if not success:
                    print(f"[提示] reply_token 可能已過期，改用 push message")
                    success = self.line_client.send_image_carousel(user_id, content)
                return success
            else:
                return self.line_client.send_image_carousel(user_id, content)
        else:
            print(f"[錯誤] 不支援的訊息類型: {message_type}")
            return False
    
    def forward_to_dify(self, image_data: bytes, user_id: str) -> Optional[str]:
        """
        轉發圖片到 Dify
        
        Args:
            image_data: 圖片資料
            user_id: 使用者 ID
        
        Returns:
            Optional[str]: Dify 回應文字
        """
        conversation_id = self.conversations.get(user_id)
        response = self.dify_client.send_image(
            image_data=image_data,
            conversation_id=conversation_id,
            user_id=user_id
        )
        
        if response:
            new_conversation_id = response.get('conversation_id')
            if new_conversation_id:
                self.conversations[user_id] = new_conversation_id
            return response.get('answer')
        
        return None
    
    def send_result_to_line(self, user_id: str, result: str) -> bool:
        """
        發送結果到 LINE
        
        Args:
            user_id: 使用者 ID
            result: 結果文字
        
        Returns:
            bool: 是否成功發送
        """
        return self.line_client.send_text_message(user_id, result)


# 初始化客戶端
webhook_handler = LINEWebhookHandler(LINE_CHANNEL_SECRET)
line_client = LINEAPIClient(LINE_CHANNEL_ACCESS_TOKEN)
dify_client = DifyAPIClient(DIFY_API_KEY, DIFY_API_ENDPOINT)
flow_controller = MessageFlowController(dify_client, line_client)


@app.route('/webhook', methods=['POST'])
def webhook():
    """
    LINE Webhook 端點
    
    處理流程：
    1. 驗證 Webhook 簽名
    2. 解析事件
    3. 處理圖片事件
    4. 回覆 200 OK
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
        
        # 收集同一批次的所有圖片事件（按使用者分組）
        image_events_by_user = {}
        other_events = []
        
        for event in events:
            image_event = webhook_handler.handle_image_event(event)
            if image_event:
                user_id = image_event.get('user_id')
                if user_id:
                    if user_id not in image_events_by_user:
                        image_events_by_user[user_id] = []
                    image_events_by_user[user_id].append(image_event)
            else:
                other_events.append(event)
        
        # 批次處理每個使用者的圖片事件
        for user_id, image_events in image_events_by_user.items():
            print(f"收到使用者 {user_id} 的 {len(image_events)} 張圖片事件")
            if len(image_events) == 1:
                # 單張圖片，使用單張處理方法
                flow_controller.process_line_image(image_events[0])
            else:
                # 多張圖片，使用批次處理方法
                flow_controller.process_line_images(image_events)
        
        # 處理其他事件（文字訊息、影片、文件等）
        for event in other_events:
            message_event = webhook_handler.handle_message_event(event)
            if message_event:
                message_type = message_event['message_type']
                user_id = message_event.get('user_id')
                reply_token = message_event.get('reply_token')
                
                # 檢查是否為不支援的格式（影片、文件等）
                unsupported_types = ['video', 'file', 'audio']
                if message_type in unsupported_types:
                    error_msg = "上傳格式錯誤，請重新上傳。"
                    if reply_token:
                        line_client.reply_message(reply_token, error_msg)
                    else:
                        line_client.send_text_message(user_id, error_msg)
                elif message_type == 'text':
                    text = message_event['message'].get('text', '')
                    # 可以選擇將文字訊息也轉發到 Dify
                    if reply_token:
                        line_client.reply_message(reply_token, 
                            "請上傳圖片，我會幫您分析。")
        
        return 'OK', 200
        
    except Exception as e:
        print(f"處理 Webhook 失敗: {str(e)}")
        import traceback
        traceback.print_exc()
        abort(500)


@app.route('/temp_image/<image_id>', methods=['GET'])
def get_temp_image(image_id: str):
    """
    提供臨時圖片訪問
    
    Args:
        image_id: 圖片 ID
    
    Returns:
        圖片內容或 404 錯誤
    """
    with temp_image_lock:
        if image_id in temp_image_storage:
            image_data = temp_image_storage[image_id]
            # 返回圖片（根據實際格式設置 Content-Type）
            return app.response_class(
                image_data,
                mimetype='image/png',
                headers={
                    'Content-Disposition': f'inline; filename="generated_image_{image_id}.png"'
                }
            )
        else:
            return 'Image not found', 404


@app.route('/health', methods=['GET'])
def health():
    """健康檢查端點"""
    return {'status': 'ok', 'service': 'LINE-Dify Integration'}, 200


@app.route('/', methods=['GET'])
def index():
    """首頁"""
    return '''
    <h1>LINE OA 與 Dify 整合系統</h1>
    <p>Webhook 端點: /webhook</p>
    <p>健康檢查: /health</p>
    <p>狀態: 運行中</p>
    '''


def main():
    """主函數"""
    import argparse
    
    parser = argparse.ArgumentParser(description='LINE OA 與 Dify 整合系統')
    parser.add_argument('--host', type=str, default='0.0.0.0',
                       help='伺服器主機 (預設: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=5000,
                       help='伺服器埠號 (預設: 5000)')
    parser.add_argument('--debug', action='store_true',
                       help='啟用除錯模式')
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("LINE OA 與 Dify 整合系統")
    print("=" * 60)
    print(f"Dify API Base URL: {DIFY_API_ENDPOINT}")
    print(f"工作流模式: 已啟用")
    print(f"LINE Channel Secret: {LINE_CHANNEL_SECRET[:20]}...")
    print(f"Webhook URL: http://{args.host}:{args.port}/webhook")
    print("=" * 60)
    print("\n伺服器啟動中...")
    print("注意: LINE Webhook 需要 HTTPS，本地測試請使用 ngrok")
    print("\n")
    
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == '__main__':
    main()

