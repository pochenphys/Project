"""
LINE OA 與 Dify 整合系統
"""

import os
import json
import base64
import hmac
import hashlib
import threading
import time
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from flask import Flask, request, abort
import requests
from dotenv import load_dotenv
from PIL import Image
import io


# 載入環境變數
load_dotenv()

app = Flask(__name__)

# 從環境變數讀取設定
DIFY_API_KEY = os.getenv('DIFY_API_KEY')
DIFY_API_ENDPOINT = os.getenv('DIFY_API_ENDPOINT', 'https://api.dify.ai')
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')

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
    
    def download_image(self, message_id: str) -> Optional[bytes]:
        """
        從 LINE 下載圖片
        
        API: GET https://api-data.line.me/v2/bot/message/{message_id}/content
        
        Args:
            message_id: 圖片訊息 ID
        
        Returns:
            Optional[bytes]: 圖片資料，失敗返回 None
        """
        try:
            url = LINE_CONTENT_URL.format(message_id=message_id)
            headers = {
                'Authorization': f'Bearer {self.access_token}'
            }
            
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            
            return response.content
        except Exception as e:
            print(f"下載圖片失敗: {str(e)}")
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
            
            response = requests.post(url, headers=self.headers, json=payload, timeout=10)
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
            
            response = requests.post(url, headers=self.headers, json=payload, timeout=10)
            response.raise_for_status()
            return True
        except Exception as e:
            print(f"回覆訊息失敗: {str(e)}")
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
            
            response = requests.post(url, headers=self.headers, json=payload, timeout=10)
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
            
            response = requests.post(url, headers=self.headers, json=payload, timeout=30)
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
            
            response = requests.post(url, headers=self.headers, json=payload, timeout=30)
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
            
            # 打印調試信息（只打印關鍵部分，避免過長）
            print(f"[除錯] Image Carousel columns 數量: {len(columns)}")
            for i, col in enumerate(columns):
                print(f"[除錯] Column {i+1}: imageUrl={col.get('imageUrl', '')[:50]}..., action={col.get('action', {}).get('type', 'N/A')}")
            
            response = requests.post(url, headers=self.headers, json=payload, timeout=30)
            
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
                print(f"錯誤響應內容: {e.response.text}")
                try:
                    error_data = e.response.json()
                    print(f"錯誤詳情: {json.dumps(error_data, ensure_ascii=False, indent=2)}")
                except:
                    pass
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
            
            # 打印調試信息
            print(f"[除錯] Image Carousel columns 數量: {len(columns)}")
            for i, col in enumerate(columns):
                print(f"[除錯] Column {i+1}: imageUrl={col.get('imageUrl', '')[:50]}..., action={col.get('action', {}).get('type', 'N/A')}")
            
            response = requests.post(url, headers=self.headers, json=payload, timeout=30)
            
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
                print(f"錯誤響應內容: {e.response.text}")
                try:
                    error_data = e.response.json()
                    print(f"錯誤詳情: {json.dumps(error_data, ensure_ascii=False, indent=2)}")
                except:
                    pass
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
        try:
            url = f'{self.endpoint}/chat-messages'
            payload = {
                'inputs': {},
                'query': text,
                'response_mode': 'blocking',
                'user': user_id
            }
            
            if conversation_id:
                payload['conversation_id'] = conversation_id
            
            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
            response.raise_for_status()
            
            return response.json()
        except Exception as e:
            print(f"Dify API 請求失敗: {str(e)}")
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
            
            # 步驟 1: 上傳所有圖片取得 file_id 列表
            file_ids = []
            for i, img_data in enumerate(image_data_list):
                print(f"正在上傳圖片 {i+1}/{len(image_data_list)} (大小: {len(img_data)} bytes)...")
                file_id = self._upload_file(img_data, user_id)
                if not file_id:
                    print(f"❌ 圖片 {i+1} 上傳失敗")
                    return None
                file_ids.append(file_id)
                print(f"✓ 圖片 {i+1} 上傳成功，file_id: {file_id}")
            
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
            
            print(f"準備發送工作流請求（{len(file_ids)} 張圖片）...")
            print(f"API Key: {self.api_key[:20]}...")
            print(f"端點: {url}")
            print(f"輸入變數: {user_variable}={user_id}, {text_variable}={query[:50] if query else 'N/A'}...")
            print(f"圖片變數: {image_variable} (file_ids: {file_ids})")
            
            response = requests.post(url, headers=self.headers, json=payload, timeout=120)
            
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
                print(f"\n❌ Dify API 錯誤狀態碼: {response.status_code}")
                print(f"錯誤回應: {response.text}")
                try:
                    error_data = response.json()
                    print(f"錯誤詳情: {json.dumps(error_data, ensure_ascii=False, indent=2)}")
                except:
                    pass
                response.raise_for_status()
            
            result = response.json()
            # 提取 'data.outputs.text' 欄位（工作流回應格式）
            data = result.get('data', {})
            print(f"[除錯] data: {data}")
            print(f"[除錯] data 類型: {type(data)}")
            
            outputs = data.get('outputs', {}) if isinstance(data, dict) else {}
            print(f"[除錯] outputs: {outputs}")
            print(f"[除錯] outputs 類型: {type(outputs)}")
            
            answer = outputs.get('text', '') if isinstance(outputs, dict) else ''
            print(f"[除錯] 提取的 answer: {answer}")
            print(f"[除錯] answer 類型: {type(answer)}, 長度: {len(str(answer)) if answer else 0}")
            
            # 將 \n 轉換為實際換行符號
            if answer:
                answer = answer.replace('\\n', '\n')
                print(f"[除錯] 轉換後的 answer 長度: {len(str(answer))}")
            
            print(f"✓ Dify 工作流執行成功 (回應長度: {len(str(answer))} 字元)")
            if answer:
                print(f"回應預覽: {str(answer)[:200]}...")
            else:
                print("[警告] 回應為空")
                print(f"[除錯] 完整 result: {json.dumps(result, ensure_ascii=False, indent=2)}")
            return result
            
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
            import mimetypes
            
            url = f"{self.base_url}/v1/files/upload"
            headers = {
                'Authorization': f'Bearer {self.api_key}'
            }
            data = {
                'user': user_id
            }
            
            # 判斷檔案類型（簡單判斷）
            mime_type = 'image/jpeg'
            filename = 'image.jpg'
            if file_data[:4] == b'\x89PNG':
                mime_type = 'image/png'
                filename = 'image.png'
            elif file_data[:2] == b'\xff\xd8':
                mime_type = 'image/jpeg'
                filename = 'image.jpg'
            
            # 使用 requests 的 files 參數上傳
            files = {
                'file': (filename, file_data, mime_type)
            }
            
            response = requests.post(url, headers=headers, data=data, files=files, timeout=30)
            
            if response.status_code in (200, 201):
                body = response.json()
                file_id = body.get('id')
                if file_id:
                    return file_id
                else:
                    print(f"[錯誤] 上傳成功但未取得 file_id，回應: {body}")
                    return None
            else:
                print(f"[錯誤] 檔案上傳失敗，狀態碼: {response.status_code}")
                print(f"錯誤回應: {response.text}")
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
    """與 Google Gemini API 通訊（用於圖片生成）"""
    
    def __init__(self, api_key: str):
        """
        初始化 Gemini API 客戶端
        
        Args:
            api_key: Google Gemini API Key
        """
        self.api_key = api_key
        # Gemini 2.5 Flash Image 使用 Google Generative AI API
        # 嘗試多個可能的模型名稱
        self.possible_models = [
            "gemini-2.5-flash-image-preview",
            "gemini-2.5-flash-image",
            "gemini-2.0-flash-exp-image-generation-001",
            "gemini-2.5-flash-exp-image-generation",
            "imagen-3",
            "imagen-4"
        ]
        # 預設使用第一個模型
        self.base_url = f'https://generativelanguage.googleapis.com/v1beta/models/{self.possible_models[0]}:generateContent'
        self.headers = {
            'Content-Type': 'application/json'
        }
        self.current_model = self.possible_models[0]
    
    def list_available_models(self) -> List[str]:
        """
        列出可用的圖像生成模型
        
        Returns:
            List[str]: 可用的模型名稱列表
        """
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={self.api_key}"
            response = requests.get(url, timeout=30)
            if response.status_code == 200:
                models_data = response.json()
                models = models_data.get('models', [])
                # 過濾出圖像生成相關的模型
                image_models = []
                for model in models:
                    name = model.get('name', '')
                    supported_methods = model.get('supportedGenerationMethods', [])
                    # 檢查是否支持 generateContent 且名稱包含 image
                    if 'generateContent' in supported_methods and ('image' in name.lower() or 'imagen' in name.lower()):
                        image_models.append(name.replace('models/', ''))
                print(f"[除錯] 找到 {len(image_models)} 個可用的圖像生成模型:")
                for m in image_models:
                    print(f"  - {m}")
                return image_models
            else:
                print(f"[警告] 無法列出模型: {response.status_code}")
                return []
        except Exception as e:
            print(f"[警告] 列出模型時發生錯誤: {str(e)}")
            return []
    
    def generate_image(self, prompt: str, size: str = "1024x1024", model: str = None) -> Optional[bytes]:
        """
        使用 Google Gemini API 生成圖片
        
        API: POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
        
        Args:
            prompt: 圖片描述提示詞
            size: 圖片尺寸（例如: "1024x1024"），Gemini API 可能使用不同的尺寸格式
            model: 使用的模型（如果為 None，則自動嘗試可用的模型）
        
        Returns:
            Optional[bytes]: 生成的圖片資料（bytes），失敗返回 None
        """
        try:
            # 如果沒有指定模型，使用當前模型
            if model is None:
                model = self.current_model
            else:
                self.current_model = model
            
            # 更新 base_url 以使用指定的模型
            self.base_url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
            
            # Gemini API 請求格式（圖像生成）
            payload = {
                "contents": [
                    {
                        "parts": [
                            {
                                "text": prompt
                            }
                        ]
                    }
                ],
                "generationConfig": {
                    "temperature": 1.0,
                    "topK": 32,
                    "topP": 1.0,
                    "maxOutputTokens": 4096,
                    "responseModalities": ["IMAGE"],  # 指定生成圖像
                    "imageConfig": {
                        "aspectRatio": "1:1"  # 預設使用 1:1，可以根據 size 參數調整
                    }
                }
            }
            
            # 根據 size 參數調整寬高比（簡化處理）
            # Gemini API 使用寬高比而不是具體像素尺寸
            if "1024x1024" in size or "1:1" in size:
                payload["generationConfig"]["imageConfig"]["aspectRatio"] = "1:1"
            elif "16:9" in size or "1024x576" in size:
                payload["generationConfig"]["imageConfig"]["aspectRatio"] = "16:9"
            elif "4:3" in size:
                payload["generationConfig"]["imageConfig"]["aspectRatio"] = "4:3"
            elif "9:16" in size:
                payload["generationConfig"]["imageConfig"]["aspectRatio"] = "9:16"
            
            print(f"正在調用 Gemini 圖片生成 API...")
            print(f"Prompt: {prompt[:100]}...")
            print(f"Model: {model}")
            
            # 在 URL 中添加 API key
            url = f"{self.base_url}?key={self.api_key}"
            
            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
            
            # 檢查回應狀態
            if response.status_code not in (200, 201):
                print(f"❌ Gemini API 錯誤狀態碼: {response.status_code}")
                print(f"錯誤回應: {response.text}")
                try:
                    error_data = response.json()
                    print(f"錯誤詳情: {json.dumps(error_data, ensure_ascii=False, indent=2)}")
                    
                    # 處理速率限制錯誤 (429)
                    if response.status_code == 429:
                        retry_after = response.headers.get('Retry-After', '5')
                        try:
                            wait_time = int(retry_after) if retry_after.isdigit() else 5
                        except:
                            wait_time = 5
                        print(f"[警告] 觸發速率限制，等待 {wait_time} 秒後重試...")
                        time.sleep(wait_time)
                        # 重試一次
                        response = requests.post(url, headers=self.headers, json=payload, timeout=60)
                        if response.status_code not in (200, 201):
                            print(f"❌ 重試後仍然失敗: {response.status_code}")
                            # 繼續嘗試其他模型，需要重新解析錯誤
                            try:
                                error_data = response.json()
                            except:
                                error_data = {}
                        # 如果重試成功，response.status_code 會是 200/201，會跳出這個 if 塊繼續處理
                    
                    # 如果模型不存在，嘗試其他可能的模型
                    if response.status_code not in (200, 201) and (error_data.get('error', {}).get('message', '').find('not found') != -1 or error_data.get('error', {}).get('code') == 404):
                        print(f"[提示] 模型 '{model}' 不存在，嘗試其他可用的模型...")
                        # 嘗試列表中的其他模型
                        for fallback_model in self.possible_models:
                            if fallback_model == model:
                                continue
                            print(f"[嘗試] 使用模型: {fallback_model}")
                            fallback_url = f"https://generativelanguage.googleapis.com/v1beta/models/{fallback_model}:generateContent?key={self.api_key}"
                            try:
                                # 添加短暫延遲避免速率限制
                                time.sleep(1)
                                fallback_response = requests.post(fallback_url, headers=self.headers, json=payload, timeout=60)
                                if fallback_response.status_code in (200, 201):
                                    print(f"✓ 成功使用模型: {fallback_model}")
                                    self.current_model = fallback_model
                                    self.base_url = fallback_url.split('?')[0]  # 更新 base_url
                                    response = fallback_response
                                    break
                                elif fallback_response.status_code == 429:
                                    # 如果備用模型也遇到速率限制，等待後重試
                                    print(f"[警告] 模型 {fallback_model} 也觸發速率限制，等待後重試...")
                                    time.sleep(5)
                                    fallback_response = requests.post(fallback_url, headers=self.headers, json=payload, timeout=60)
                                    if fallback_response.status_code in (200, 201):
                                        print(f"✓ 重試後成功使用模型: {fallback_model}")
                                        self.current_model = fallback_model
                                        self.base_url = fallback_url.split('?')[0]
                                        response = fallback_response
                                        break
                                else:
                                    print(f"✗ 模型 {fallback_model} 也失敗: {fallback_response.status_code}")
                            except Exception as e:
                                print(f"✗ 模型 {fallback_model} 請求失敗: {str(e)}")
                                continue
                        else:
                            # 如果所有模型都失敗，列出可用模型
                            if response.status_code not in (200, 201):
                                print("[錯誤] 所有嘗試的模型都失敗，正在列出可用的模型...")
                                available_models = self.list_available_models()
                                if available_models:
                                    print(f"[建議] 請嘗試使用以下模型之一: {', '.join(available_models[:3])}")
                                response.raise_for_status()
                    elif response.status_code not in (200, 201):
                        # 對於其他錯誤，也嘗試其他模型
                        print(f"[提示] 遇到錯誤，嘗試其他可用的模型...")
                        for fallback_model in self.possible_models:
                            if fallback_model == model:
                                continue
                            print(f"[嘗試] 使用模型: {fallback_model}")
                            fallback_url = f"https://generativelanguage.googleapis.com/v1beta/models/{fallback_model}:generateContent?key={self.api_key}"
                            try:
                                time.sleep(1)  # 添加延遲
                                fallback_response = requests.post(fallback_url, headers=self.headers, json=payload, timeout=60)
                                if fallback_response.status_code in (200, 201):
                                    print(f"✓ 成功使用模型: {fallback_model}")
                                    self.current_model = fallback_model
                                    self.base_url = fallback_url.split('?')[0]
                                    response = fallback_response
                                    break
                            except Exception as e:
                                print(f"✗ 模型 {fallback_model} 請求失敗: {str(e)}")
                                continue
                        else:
                            if response.status_code not in (200, 201):
                                response.raise_for_status()
                except:
                    if response.status_code not in (200, 201):
                        response.raise_for_status()
            
            result = response.json()
            print(f"[除錯] Gemini API 回應: {json.dumps(result, ensure_ascii=False, indent=2)}")
            
            # 提取圖片數據（Gemini API 回應格式）
            # 回應格式可能為: {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": "base64..."}}]}}]}
            candidates = result.get('candidates', [])
            if not candidates or len(candidates) == 0:
                print("[錯誤] Gemini API 回應中沒有 candidates 欄位")
                return None
            
            first_candidate = candidates[0]
            content = first_candidate.get('content', {})
            parts = content.get('parts', [])
            
            if not parts or len(parts) == 0:
                print("[錯誤] Gemini API 回應中沒有 parts 欄位")
                return None
            
            # 查找包含圖片數據的 part
            image_data_base64 = None
            for part in parts:
                inline_data = part.get('inlineData')
                if inline_data:
                    image_data_base64 = inline_data.get('data')
                    if image_data_base64:
                        break
            
            if not image_data_base64:
                print("[警告] Gemini API 回應中沒有找到圖片數據")
                print(f"[除錯] 完整回應結構: {json.dumps(result, ensure_ascii=False, indent=2)}")
                return None
            
            # 將 base64 字符串解碼為 bytes
            try:
                image_data = base64.b64decode(image_data_base64)
                print(f"✓ 成功解碼 base64 圖片 (大小: {len(image_data)} bytes)")
                return image_data
            except Exception as e:
                print(f"❌ Base64 解碼失敗: {str(e)}")
                return None
            
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if hasattr(e, 'response') and e.response else 'N/A'
            print(f"\n❌ Gemini 圖片生成失敗 (HTTP {status_code}): {str(e)}")
            if hasattr(e, 'response') and hasattr(e.response, 'text'):
                print(f"錯誤回應內容: {e.response.text}")
            return None
        except Exception as e:
            print(f"\n❌ Gemini 圖片生成失敗: {str(e)}")
            import traceback
            traceback.print_exc()
            return None


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
            print(f"正在從網址下載圖片: {image_url}")
            response = requests.get(image_url, timeout=timeout)
            response.raise_for_status()
            
            # 檢查是否為圖片
            content_type = response.headers.get('Content-Type', '')
            if not content_type.startswith('image/'):
                print(f"警告: 下載的內容不是圖片 (Content-Type: {content_type})")
            
            image_data = response.content
            print(f"✓ 圖片下載成功 (大小: {len(image_data)} bytes)")
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
        self.buffer_wait_time = 2.0
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
            
            print(f"圖片事件已加入緩衝區（使用者: {user_id}, 目前: {buffer_size} 張）")
            
            # 如果有 imageSet 資訊，檢查是否已收集完整
            if image_set:
                total = image_set.get('total')
                if total and buffer_size >= total:
                    # 已收集完整，立即處理
                    print(f"已收集完整圖片組（{buffer_size}/{total}），立即處理")
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
            print(f"開始處理緩衝區中的 {len(events)} 張圖片（使用者: {user_id}）")
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
        2. 下載所有圖片
        3. 批次轉發到 Dify
        4. 發送結果到 LINE
        
        Args:
            events: LINE 圖片事件列表（單張或多張）
        
        Returns:
            bool: 是否成功處理
        """
        try:
            if not events:
                print("錯誤: 沒有圖片事件")
                return False
            
            user_id = events[0].get('user_id')
            reply_token = events[0].get('reply_token')
            
            if not user_id:
                print("錯誤: 缺少使用者 ID")
                return False
            
            # 步驟 1: 下載所有圖片
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
                
                # 驗證檔案格式是否為圖片
                if not ImageProcessor.is_valid_image(image_data):
                    print(f"錯誤: 檔案 {i+1} 不是有效的圖片格式")
                    error_msg = "上傳格式錯誤，請重新上傳。"
                    if reply_token:
                        self.line_client.reply_message(reply_token, error_msg)
                    else:
                        self.line_client.send_text_message(user_id, error_msg)
                    return False
                
                print(f"✓ 圖片 {i+1} 下載成功 (大小: {len(image_data)} bytes)")
                
                # 可選 - 調整圖片大小（如果太大）
                if len(image_data) > 5 * 1024 * 1024:  # 5MB
                    print(f"圖片 {i+1} 過大，正在調整大小...")
                    image_data = ImageProcessor.resize_image(image_data, max_size=(1024, 1024))
                
                image_data_list.append(image_data)
            
            if not image_data_list:
                error_msg = "抱歉，無法下載圖片，請稍後再試。"
                if reply_token:
                    self.line_client.reply_message(reply_token, error_msg)
                return False
            
            print(f"共下載 {len(image_data_list)} 張圖片")
            
            # 步驟 2: 取得或創建對話 ID
            conversation_id = self.conversations.get(user_id)
            
            # 步驟 3: 發送圖片到 Dify（單張或多張）
            if len(image_data_list) == 1:
                print(f"正在將圖片傳送至 Dify...")
                query_text = "請分析這張圖片並提供詳細說明"
            else:
                print(f"正在將 {len(image_data_list)} 張圖片傳送至 Dify...")
                query_text = f"請分析這 {len(image_data_list)} 張圖片並提供詳細說明"
            
            dify_response = self.dify_client.send_image(
                image_data=image_data_list if len(image_data_list) > 1 else image_data_list[0],
                conversation_id=conversation_id,
                user_id=user_id,
                query=query_text
            )
            
            if not dify_response:
                error_msg = "抱歉，圖片處理失敗，請稍後再試。"
                if reply_token:
                    self.line_client.reply_message(reply_token, error_msg)
                return False
            
            # 步驟 5: 提取 Dify 回應（從 data.outputs 提取所有變數）
            data = dify_response.get('data', {})
            print(f"[除錯] data: {data}")
            print(f"[除錯] data 類型: {type(data)}")
            
            outputs = data.get('outputs', {}) if isinstance(data, dict) else {}
            print(f"[除錯] outputs: {outputs}")
            print(f"[除錯] outputs 類型: {type(outputs)}")
            
            # 提取所有變數
            text = outputs.get('text', '') if isinstance(outputs, dict) else ''
            picture_1 = outputs.get('picture_1', '') if isinstance(outputs, dict) else ''
            picture_2 = outputs.get('picture_2', '') if isinstance(outputs, dict) else ''
            picture_3 = outputs.get('picture_3', '') if isinstance(outputs, dict) else ''
            dish_1 = outputs.get('dish_1', '') if isinstance(outputs, dict) else ''
            dish_2 = outputs.get('dish_2', '') if isinstance(outputs, dict) else ''
            dish_3 = outputs.get('dish_3', '') if isinstance(outputs, dict) else ''
            
            print(f"[除錯] 提取的 text: {text[:100] if text else 'None'}...")
            print(f"[除錯] 提取的 picture_1: {picture_1[:50] if picture_1 else 'None'}...")
            print(f"[除錯] 提取的 dish_1: {dish_1[:50] if dish_1 else 'None'}...")
            
            # 將 \n 轉換為實際換行符號
            if text:
                text = text.replace('\\n', '\n')
            
            # 驗證 text 是否有效
            if not text or not text.strip():
                print("[錯誤] text 為空或無效")
                print(f"[除錯] 完整 dify_response: {json.dumps(dify_response, ensure_ascii=False, indent=2)}")
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
                        print(f"[除錯] 已清理用戶 {user_id} 的食譜數據和 text")
                
                threading.Thread(target=cleanup_recipe, daemon=True).start()
            
            # 步驟 6: 生成圖片並創建 Image Carousel
            if GEMINI_API_KEY and picture_1 and picture_2 and picture_3:
                print(f"正在生成圖片並創建 Image Carousel...")
                gemini_client = GeminiImageAPIClient(GEMINI_API_KEY)
                image_urls = []
                
                # 生成三張圖片
                for i, prompt in enumerate([picture_1, picture_2, picture_3], 1):
                    if prompt and isinstance(prompt, str) and prompt.strip():
                        # 如果不是第一張圖片，添加延遲避免速率限制
                        if i > 1:
                            wait_time = 2  # 等待 2 秒
                            print(f"[等待] 避免速率限制，等待 {wait_time} 秒...")
                            time.sleep(wait_time)
                        
                        print(f"正在生成圖片 {i}...")
                        generated_image_data = gemini_client.generate_image(
                            prompt=prompt.strip(),
                            size="1024x1024",
                            model=None  # 使用預設模型，如果失敗會自動嘗試其他模型
                        )
                        
                        if generated_image_data:
                            # 保存到臨時存儲並獲取 URL
                            global temp_image_counter
                            with temp_image_lock:
                                temp_image_counter += 1
                                image_id = f"img_{int(time.time())}_{temp_image_counter}"
                                temp_image_storage[image_id] = generated_image_data
                                
                                base_url = os.getenv('BASE_URL', '')
                                if not base_url:
                                    host = os.getenv('HOST', '0.0.0.0')
                                    port = os.getenv('PORT', '5000')
                                    protocol = 'https' if os.getenv('HTTPS', '').lower() == 'true' else 'http'
                                    if host == '0.0.0.0':
                                        host = 'localhost'
                                    base_url = f"{protocol}://{host}:{port}"
                                
                                image_url = f"{base_url}/temp_image/{image_id}"
                                image_urls.append(image_url)
                                
                                # 設置清理定時器
                                def cleanup_image(img_id):
                                    time.sleep(1800)
                                    with temp_image_lock:
                                        if img_id in temp_image_storage:
                                            del temp_image_storage[img_id]
                                
                                threading.Thread(target=cleanup_image, args=(image_id,), daemon=True).start()
                        else:
                            print(f"[警告] 圖片 {i} 生成失敗")
                            image_urls.append(None)
                    else:
                        print(f"[警告] picture_{i} 為空或無效")
                        image_urls.append(None)
                
                # 提取菜肴名稱（從 dish_1, dish_2, dish_3 中提取）
                # import re
                # dish_names = []
                # for dish in [dish_1, dish_2, dish_3]:
                #     if dish and isinstance(dish, str):
                #         # 使用正則表達式提取 "### 菜餚名稱：XXX" 中的 XXX
                #         match = re.search(r'###\s*菜餚名稱[：:]\s*(.+)', dish)
                #         if match:
                #             dish_name = match.group(1).strip().split('\n')[0]  # 只取第一行
                #             dish_names.append(dish_name)
                #         else:
                #             # 如果找不到，使用前20個字符作為名稱
                #             dish_names.append(dish[:20] if len(dish) > 20 else dish)
                #     else:
                #         dish_names.append("未知菜餚")
                
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
                
                # 發送 Image Carousel（使用 reply_token，只使用一次）
                if len(columns) >= 3:
                    print(f"正在發送 Image Carousel...")
                    print(f"[除錯] columns 數量: {len(columns)}")
                    # 打印每個 column 的信息
                    for i, col in enumerate(columns, 1):
                        img_url = col.get('imageUrl', 'N/A')
                        action_data = col.get('action', {}).get('data', 'N/A')
                        print(f"[除錯] Column {i}: imageUrl={img_url}, action.data={action_data}")
                    
                    if reply_token:
                        success = self.line_client.reply_image_carousel(reply_token, columns)
                    else:
                        success = self.line_client.send_image_carousel(user_id, columns)
                    
                    if success:
                        print("Image Carousel 已成功發送")
                        return True
                    else:
                        print("Image Carousel 發送失敗")
                        return False
                elif len(columns) > 0:
                    # 如果圖片不足3張，仍然發送
                    print(f"[警告] 圖片數量不足，只發送 {len(columns)} 張")
                    if reply_token:
                        success = self.line_client.reply_image_carousel(reply_token, columns)
                    else:
                        success = self.line_client.send_image_carousel(user_id, columns)
                    return success
                else:
                    print(f"[警告] 無法創建 Image Carousel：圖片生成失敗")
                    # 如果無法生成 Image Carousel，至少發送 text
                    if reply_token:
                        success = self.line_client.reply_message(reply_token, text)
                    else:
                        success = self.line_client.send_text_message(user_id, text)
                    return success
            else:
                print(f"[警告] 無法生成 Image Carousel：缺少 GEMINI_API_KEY 或提示詞")
                # 如果無法生成 Image Carousel，至少發送 text
                if reply_token:
                    success = self.line_client.reply_message(reply_token, text)
                else:
                    success = self.line_client.send_text_message(user_id, text)
                return success
                
        except Exception as e:
            print(f"處理圖片事件失敗: {str(e)}")
            import traceback
            traceback.print_exc()
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

