"""
LINE Bot 中繼器程式
作為功能路由中轉站，根據用戶輸入路由到不同的功能模組
"""

import os
import re
import time
import base64
from typing import Dict, Optional, List
from flask import Flask, request, abort
from dotenv import load_dotenv
from datetime import datetime, timedelta

# 載入環境變數
# override=False: Cloud Run 環境變數優先，本地開發時如果 .env 文件存在也會載入
load_dotenv('LINE.env', override=False)

# 導入 Line2Dify 模組的類和函數
from Line2Dify import (
    LINEWebhookHandler,
    LINEAPIClient,
    DifyAPIClient,
    MessageFlowController,
    ImageProcessor,
    temp_image_storage,
    temp_image_lock,
    user_recipe_storage,
    user_text_storage,
    recipe_storage_lock
)

# 導入 record.py 模組的函數和類（用於記錄功能和查看功能）
from record import add_image_to_buffer, DatabaseManager, MYSQL_CONFIG, get_user_profile, db_manager

app = Flask(__name__)

# 1x1 透明 PNG（最小的有效PNG，用於404響應以減少資源消耗）
# Base64編碼的1x1透明PNG（約70字節）
EMPTY_PNG_B64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='
EMPTY_PNG_DATA = base64.b64decode(EMPTY_PNG_B64)

# 從環境變數讀取設定
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
DIFY_API_KEY = os.getenv('DIFY_API_KEY')
DIFY_API_ENDPOINT = os.getenv('DIFY_API_ENDPOINT', 'https://api.dify.ai')

# 初始化 LINE 和 Dify 客戶端
webhook_handler = LINEWebhookHandler(LINE_CHANNEL_SECRET)
line_client = LINEAPIClient(LINE_CHANNEL_ACCESS_TOKEN)
dify_client = DifyAPIClient(DIFY_API_KEY, DIFY_API_ENDPOINT)

# 初始化食譜功能控制器
recipe_flow_controller = MessageFlowController(dify_client, line_client)

# 使用 record.py 中的 db_manager 實例（共享同一個資料庫連接）
# 確保連接已建立
if not db_manager.connection:
    if not db_manager.connect():
        print("警告: 資料庫連接失敗，查看功能可能無法使用")

# 用戶功能狀態管理（追蹤每個用戶當前使用的功能）
# 格式: {user_id: 'function_name'}
user_function_state = {}

# 追蹤用戶是否已發送"請稍等"消息（避免重複發送）
# 格式: {user_id: timestamp} - 記錄發送時間，10秒內不再發送
user_wait_message_sent = {}

# 用戶刪除記錄映射（追蹤每個用戶的記錄編號對應的記錄ID）
# 格式: {user_id: {編號: {'id': record_id, 'food_name': ..., 'quantity': ..., 'storage_time': ...}}}
user_delete_records_mapping = {}

# 功能關鍵字映射
FUNCTION_KEYWORDS = {
    'recipe': ['食譜功能', '食譜', 'recipe', 'Recipe', 'RECIPE', '開始食譜', '使用食譜', '食譜模式'],
    'record': ['記錄功能', '記錄', 'record', 'Record', 'RECORD', '開始記錄', '使用記錄', '記錄模式'],
    'view': ['查看功能', '查看', 'view', 'View', 'VIEW', '查詢', '查詢功能', '我的記錄', '記錄查詢'],
    'delete': ['刪除功能', '刪除', 'delete', 'Delete', 'DELETE', '消耗', '消耗功能', '使用'],
    'help': ['幫助', 'help', 'Help', '功能', '選單', 'menu', 'Menu', '說明'],
    'exit': ['退出', 'exit', 'Exit', '結束', '取消', 'cancel', 'Cancel']
}


def query_user_food_records(user_id: str) -> List[Dict]:
    """
    查詢指定用戶的食物記錄（使用 user_id）
    
    Args:
        user_id: 使用者 ID（LINE user_id）
        
    Returns:
        List[Dict]: 食物記錄列表，每個記錄包含 food_name, quantity, storage_time
    """
    try:
        # 確保資料庫連接
        if not db_manager.connection:
            if not db_manager.connect():
                print("錯誤: 無法連接到資料庫")
                return []
        
        with db_manager.connection.cursor() as cursor:
            # 查詢該用戶的所有記錄（使用 user_id）
            sql = """
            SELECT 
                id,
                food_name AS 食品,
                quantity AS 數量,
                storage_time AS 購買時間
            FROM foods
            WHERE username = %s
            ORDER BY storage_time ASC
            """
            cursor.execute(sql, (user_id,))
            results = cursor.fetchall()
            
            # 轉換為字典列表
            records = []
            for row in results:
                records.append({
                    'id': row[0],  # id
                    'food_name': row[1],  # 食品
                    'quantity': row[2],   # 數量
                    'storage_time': row[3]  # 購買時間
                })
            
            print(f"✓ 查詢到 {len(records)} 筆記錄（user_id: {user_id}）")
            return records
            
    except Exception as e:
        print(f"✗ 查詢資料庫失敗: {e}")
        import traceback
        traceback.print_exc()
        return []


def query_food_records_by_name(username: str, food_name: str) -> List[Dict]:
    """
    查詢指定用戶的特定食品記錄（按時間升序，最舊的在前）
    
    Args:
        username: 使用者名稱
        food_name: 食品名稱
        
    Returns:
        List[Dict]: 食物記錄列表，每個記錄包含 id, food_name, quantity, storage_time
    """
    try:
        # 確保資料庫連接
        if not db_manager.connection:
            if not db_manager.connect():
                print("錯誤: 無法連接到資料庫")
                return []
        
        with db_manager.connection.cursor() as cursor:
            # 查詢該用戶的特定食品記錄，按時間升序（最舊的在前）
            sql = """
            SELECT 
                id,
                food_name,
                quantity,
                storage_time
            FROM foods
            WHERE username = %s AND food_name = %s
            ORDER BY storage_time ASC
            """
            cursor.execute(sql, (username, food_name))
            results = cursor.fetchall()
            
            # 轉換為字典列表
            records = []
            for row in results:
                records.append({
                    'id': row[0],
                    'food_name': row[1],
                    'quantity': row[2],
                    'storage_time': row[3]
                })
            
            print(f"✓ 查詢到 {len(records)} 筆 {food_name} 記錄（使用者: {username}）")
            return records
            
    except Exception as e:
        print(f"✗ 查詢資料庫失敗: {e}")
        import traceback
        traceback.print_exc()
        return []


def deduct_food_quantity(username: str, food_name: str, deduct_amount: float) -> Dict:
    """
    扣除食品數量（從最舊的記錄開始）
    
    Args:
        username: 使用者名稱
        food_name: 食品名稱
        deduct_amount: 要扣除的數量
        
    Returns:
        Dict: 包含 success, remaining_amount, updated_records, deleted_records
    """
    try:
        # 確保資料庫連接
        if not db_manager.connection:
            if not db_manager.connect():
                print("錯誤: 無法連接到資料庫")
                return {'success': False, 'message': '資料庫連接失敗'}
        
        # 查詢該食品的所有記錄（按時間升序）
        records = query_food_records_by_name(username, food_name)
        
        if not records:
            return {
                'success': False,
                'message': f'找不到 {food_name} 的記錄',
                'remaining_amount': deduct_amount,
                'updated_records': [],
                'deleted_records': []
            }
        
        remaining_amount = deduct_amount
        updated_records = []
        deleted_records = []
        
        with db_manager.connection.cursor() as cursor:
            for record in records:
                if remaining_amount <= 0:
                    break
                
                record_id = record['id']
                # 確保數量轉換為浮點數
                current_quantity = float(record['quantity']) if record['quantity'] is not None else None
                
                # 如果數量為 NULL，跳過
                if current_quantity is None:
                    continue
                
                if current_quantity <= remaining_amount:
                    # 當前記錄的數量不足或剛好，刪除這筆記錄
                    delete_sql = "DELETE FROM foods WHERE id = %s"
                    cursor.execute(delete_sql, (record_id,))
                    deleted_records.append({
                        'id': record_id,
                        'food_name': food_name,
                        'quantity': float(current_quantity)
                    })
                    remaining_amount -= current_quantity
                    print(f"✓ 刪除記錄 ID {record_id}: {food_name} {current_quantity}")
                else:
                    # 當前記錄的數量足夠，更新數量
                    new_quantity = float(current_quantity - remaining_amount)
                    update_sql = "UPDATE foods SET quantity = %s WHERE id = %s"
                    cursor.execute(update_sql, (new_quantity, record_id))
                    updated_records.append({
                        'id': record_id,
                        'food_name': food_name,
                        'old_quantity': float(current_quantity),
                        'new_quantity': float(new_quantity),
                        'deducted': float(remaining_amount)
                    })
                    print(f"✓ 更新記錄 ID {record_id}: {food_name} {current_quantity} -> {new_quantity} (扣除 {remaining_amount})")
                    remaining_amount = 0
            
            db_manager.connection.commit()
        
        return {
            'success': True,
            'remaining_amount': remaining_amount,
            'updated_records': updated_records,
            'deleted_records': deleted_records,
            'message': f'成功處理 {food_name} 的扣除'
        }
        
    except Exception as e:
        print(f"✗ 扣除數量失敗: {e}")
        import traceback
        traceback.print_exc()
        if db_manager.connection:
            db_manager.connection.rollback()
        return {
            'success': False,
            'message': f'扣除數量時發生錯誤: {str(e)}',
            'remaining_amount': deduct_amount
        }


def delete_food_record_by_id(record_id: int) -> Dict:
    """
    根據記錄 ID 刪除食物記錄
    
    Args:
        record_id: 記錄 ID
    
    Returns:
        Dict: 包含 success, message, deleted_record
    """
    try:
        # 確保資料庫連接
        if not db_manager.connection:
            if not db_manager.connect():
                print("錯誤: 無法連接到資料庫")
                return {'success': False, 'message': '資料庫連接失敗'}
        
        with db_manager.connection.cursor() as cursor:
            # 先查詢記錄信息（用於返回）
            select_sql = "SELECT id, food_name, quantity, storage_time FROM foods WHERE id = %s"
            cursor.execute(select_sql, (record_id,))
            record = cursor.fetchone()
            
            if not record:
                return {
                    'success': False,
                    'message': f'找不到 ID {record_id} 的記錄'
                }
            
            # 刪除記錄
            delete_sql = "DELETE FROM foods WHERE id = %s"
            cursor.execute(delete_sql, (record_id,))
            db_manager.connection.commit()
            
            deleted_record = {
                'id': record[0],
                'food_name': record[1],
                'quantity': record[2],
                'storage_time': record[3]
            }
            
            print(f"✓ 刪除記錄 ID {record_id}: {deleted_record['food_name']}")
            
            return {
                'success': True,
                'message': f'成功刪除記錄 ID {record_id}',
                'deleted_record': deleted_record
            }
            
    except Exception as e:
        print(f"✗ 刪除記錄失敗: {e}")
        import traceback
        traceback.print_exc()
        if db_manager.connection:
            db_manager.connection.rollback()
        return {
            'success': False,
            'message': f'刪除記錄時發生錯誤: {str(e)}'
        }


def remove_markdown_headers(text: str) -> str:
    """
    移除 Markdown 標題標記（# 符號）
    
    Args:
        text: 包含 Markdown 標記的文字
        
    Returns:
        str: 清理後的文字
    """
    # 使用正則表達式移除行首的 # 符號
    return re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)


def parse_consumption_input(text: str) -> List[Dict]:
    """
    解析用戶輸入的消耗信息
    
    Args:
        text: 用戶輸入的文字，例如："蘋果 2個\n橘子 1個"
        
    Returns:
        List[Dict]: 消耗項目列表，每個項目包含 'food_name' 和 'quantity'
    """
    consumption_items = []
    
    # 按換行符分割
    lines = text.strip().split('\n')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # 使用正則表達式匹配：食物名稱 + 數量
        # 匹配模式：食物名稱（中文、英文、數字）+ 數量（數字 + 單位）
        pattern = r'([^\d\s]+?)\s*(\d+(?:\.\d+)?)\s*(個|件|包|盒|瓶|罐|條|根|片|塊|斤|公斤|克|kg|g)?'
        match = re.search(pattern, line)
        
        if match:
            food_name = match.group(1).strip()
            quantity_str = match.group(2)
            
            try:
                quantity = float(quantity_str)
                consumption_items.append({
                    'food_name': food_name,
                    'quantity': quantity
                })
            except ValueError:
                print(f"警告: 無法解析數量 '{quantity_str}'")
    
    return consumption_items


class FunctionRouter:
    """功能路由器"""
    
    def __init__(self):
        self.functions = {
            'recipe': self.handle_recipe_function,
            'record': self.handle_record_function,
            'view': self.handle_view_function,
            'delete': self.handle_delete_function,
            'help': self.handle_help_function,
            'exit': self.handle_exit_function
        }
    
    def detect_function(self, text: str) -> Optional[str]:
        """
        檢測文字訊息中的功能關鍵字
        
        Args:
            text: 用戶輸入的文字
            
        Returns:
            Optional[str]: 功能名稱，如果未檢測到則返回 None
        """
        text_lower = text.strip().lower()
        
        for func_name, keywords in FUNCTION_KEYWORDS.items():
            for keyword in keywords:
                if keyword.lower() in text_lower:
                    return func_name
        
        return None
    
    def handle_recipe_function(self, user_id: str, reply_token: Optional[str]) -> bool:
        """
        處理食譜功能啟用
        
        Args:
            user_id: 用戶 ID
            reply_token: 回覆 Token
            
        Returns:
            bool: 是否成功處理
        """
        # 設定用戶功能狀態為食譜模式
        user_function_state[user_id] = 'recipe'
        
        guide_message = (
            "🍳 食譜功能已啟用！\n\n"
            "📸 請上傳您想要製作的食物圖片，我會為您：\n"
            "• 分析圖片中的食材\n"
            "• 提供詳細的食譜步驟\n"
            "• 建議烹飪方法和技巧\n\n"
            "請直接上傳食物圖片即可開始！\n\n"
            "💡 提示：\n"
            "• 輸入其他功能關鍵字可切換功能\n"
            "• 輸入「退出」可結束食譜功能"
        )
        
        if reply_token:
            return line_client.reply_message(reply_token, guide_message)
        else:
            return line_client.send_text_message(user_id, guide_message)
    
    def handle_record_function(self, user_id: str, reply_token: Optional[str]) -> bool:
        """
        處理記錄功能啟用
        
        Args:
            user_id: 用戶 ID
            reply_token: 回覆 Token
            
        Returns:
            bool: 是否成功處理
        """
        # 設定用戶功能狀態為記錄模式
        user_function_state[user_id] = 'record'
        
        guide_message = (
            "📝 記錄功能已啟用！\n\n"
            "📸 請上傳您想要記錄的食物圖片，我會為您：\n"
            "• 記錄食物名稱\n"
            "• 記錄入庫時間\n"
            "• 保存到資料庫\n\n"
            "請直接上傳食物圖片即可開始記錄！\n\n"
            "💡 提示：\n"
            "• 輸入其他功能關鍵字可切換功能\n"
            "• 輸入「退出」可結束記錄功能"
        )
        
        if reply_token:
            return line_client.reply_message(reply_token, guide_message)
        else:
            return line_client.send_text_message(user_id, guide_message)
    
    def handle_view_function(self, user_id: str, reply_token: Optional[str]) -> bool:
        """
        處理查看功能
        
        Args:
            user_id: 用戶 ID
            reply_token: 回覆 Token
            
        Returns:
            bool: 是否成功處理
        """
        try:
            # 獲取用戶資訊（用於顯示）
            user_profile = get_user_profile(user_id)
            username = user_profile.get('displayName', '未知用戶') if user_profile else '未知用戶'
            print(f"查看功能：使用者 {user_id} (USERNAME: {username})")
            
            # 查詢資料庫（使用 user_id）
            records = query_user_food_records(user_id)
            
            if not records:
                # 沒有記錄
                message = (
                    f"📋 {username} 的記錄\n\n"
                    "目前沒有任何記錄。\n"
                    "使用「記錄功能」來記錄食物吧！"
                )
            else:
                # 有記錄，格式化列表
                message = f"📋 {username} 的記錄\n\n"
                message += f"共 {len(records)} 筆記錄：\n\n"
                
                for i, record in enumerate(records, 1):
                    food_name = record.get('food_name', '未知')
                    quantity = record.get('quantity')
                    storage_time = record.get('storage_time', '')
                    
                    # 格式化數量
                    quantity_str = f"{quantity}" if quantity is not None else "未指定"
                    
                    # 格式化時間和計算已購買時間
                    time_str = "未指定"
                    elapsed_str = "無法計算"
                    
                    if storage_time:
                        # 將 storage_time 轉換為 datetime 對象
                        purchase_datetime = None
                        
                        if isinstance(storage_time, datetime):
                            purchase_datetime = storage_time
                            time_str = storage_time.strftime("%Y-%m-%d %H:%M:%S")
                        elif isinstance(storage_time, str):
                            # 嘗試解析字符串時間（多種格式）
                            time_str = storage_time
                            for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"]:
                                try:
                                    purchase_datetime = datetime.strptime(storage_time, fmt)
                                    time_str = purchase_datetime.strftime("%Y-%m-%d %H:%M:%S")
                                    break
                                except:
                                    continue
                        
                        # 計算已購買時間（從購買時間到現在的距離）
                        if purchase_datetime:
                            now = datetime.now()
                            elapsed = now - purchase_datetime
                            
                            # 格式化時間差
                            if elapsed.days > 0:
                                elapsed_str = f"{elapsed.days} 天"
                            elif elapsed.seconds >= 3600:
                                hours = elapsed.seconds // 3600
                                elapsed_str = f"{hours} 小時"
                            elif elapsed.seconds >= 60:
                                minutes = elapsed.seconds // 60
                                elapsed_str = f"{minutes} 分鐘"
                            else:
                                elapsed_str = "剛剛"
                    
                    message += f"{i}. {food_name}\n"
                    message += f"   數量: {quantity_str}\n"
                    message += f"   購買時間: {time_str}\n"
                    message += f"   已購買時間: {elapsed_str}"
                    # 如果不是最後一條記錄，添加兩個換行符；最後一條只添加一個換行符
                    if i < len(records):
                        message += "\n\n"
                    else:
                        message += "\n"
            
            # 查看功能執行完後，清除用戶狀態（回到初始狀態）
            if user_id in user_function_state:
                del user_function_state[user_id]
                print(f"✓ 查看功能執行完畢，已清除用戶 {user_id} 的功能狀態")
            
            # 發送訊息
            print(f"[查看功能] 準備發送訊息給用戶 {user_id}，訊息長度: {len(message)}")
            if reply_token:
                result = line_client.reply_message(reply_token, message)
                print(f"[查看功能] 使用 reply_token 發送結果: {result}")
                return result
            else:
                result = line_client.send_text_message(user_id, message)
                print(f"[查看功能] 使用 push 訊息發送結果: {result}")
                return result
                
        except Exception as e:
            print(f"處理查看功能失敗: {e}")
            import traceback
            traceback.print_exc()
            # 即使出錯，也清除用戶狀態
            if user_id in user_function_state:
                del user_function_state[user_id]
            error_msg = "查詢記錄時發生錯誤，請稍後再試。"
            if reply_token:
                return line_client.reply_message(reply_token, error_msg)
            else:
                return line_client.send_text_message(user_id, error_msg)
    
    def handle_delete_function(self, user_id: str, reply_token: Optional[str]) -> bool:
        """
        處理刪除功能啟用
        
        Args:
            user_id: 用戶 ID
            reply_token: 回覆 Token
            
        Returns:
            bool: 是否成功處理
        """
        try:
            # 獲取用戶資訊（用於顯示）
            user_profile = get_user_profile(user_id)
            username = user_profile.get('displayName', '未知用戶') if user_profile else '未知用戶'
            print(f"刪除功能：使用者 {user_id} (USERNAME: {username})")
            
            # 設定用戶功能狀態為刪除模式
            user_function_state[user_id] = 'delete'
            
            # 查詢資料庫，顯示記錄清單（使用 user_id）
            records = query_user_food_records(user_id)
            
            if not records:
                # 沒有記錄
                message = (
                    f"🗑️ 刪除功能已啟用！\n\n"
                    f"📋 {username} 的記錄\n\n"
                    "目前沒有任何記錄。\n"
                    "使用「記錄功能」來記錄食物吧！"
                )
            else:
                # 有記錄，格式化列表並保存編號映射
                # 清除舊的映射（如果存在）
                if user_id in user_delete_records_mapping:
                    del user_delete_records_mapping[user_id]
                
                # 創建新的映射
                user_delete_records_mapping[user_id] = {}
                
                message = f"🗑️ 刪除功能已啟用！\n\n"
                message += f"📋 {username} 的記錄\n\n"
                message += f"共 {len(records)} 筆記錄：\n\n"
                
                for i, record in enumerate(records, 1):
                    food_name = record.get('food_name', '未知')
                    quantity = record.get('quantity')
                    storage_time = record.get('storage_time', '')
                    record_id = record.get('id')
                    
                    # 保存編號到記錄ID的映射
                    if record_id:
                        user_delete_records_mapping[user_id][i] = {
                            'id': record_id,
                            'food_name': food_name,
                            'quantity': quantity,
                            'storage_time': storage_time
                        }
                    
                    # 格式化數量
                    quantity_str = f"{quantity}" if quantity is not None else "未指定"
                    
                    # 格式化時間
                    time_str = "未指定"
                    if storage_time:
                        if isinstance(storage_time, datetime):
                            time_str = storage_time.strftime("%Y-%m-%d %H:%M:%S")
                        elif isinstance(storage_time, str):
                            time_str = storage_time
                    
                    message += f"{i}. {food_name} - 數量: {quantity_str} - 時間: {time_str}\n"
                
                message += "\n刪除方式：\n"
                message += "1️⃣ 按編號刪除：輸入編號即可刪除該記錄\n"
                message += "   例如：3 （刪除編號 3 的記錄）\n"
                message += "   或：3 1 （刪除編號 3 的記錄，消耗數量 1）\n\n"
                message += "2️⃣ 按食品名稱刪除：輸入食品名稱和數量\n"
                message += "   例如：蘋果 2個\n"
                message += "   系統會從最舊的記錄開始扣除。\n\n"
                message += "💡 提示：\n"
                message += "• 輸入其他功能關鍵字可切換功能\n"
                message += "• 輸入「退出」可結束刪除功能"
            
            # 發送訊息
            print(f"[刪除功能] 準備發送訊息給用戶 {user_id}，訊息長度: {len(message)}")
            if reply_token:
                result = line_client.reply_message(reply_token, message)
                print(f"[刪除功能] 使用 reply_token 發送結果: {result}")
                return result
            else:
                result = line_client.send_text_message(user_id, message)
                print(f"[刪除功能] 使用 push 訊息發送結果: {result}")
                return result
                
        except Exception as e:
            print(f"處理刪除功能失敗: {e}")
            import traceback
            traceback.print_exc()
            error_msg = "啟用刪除功能時發生錯誤，請稍後再試。"
            if reply_token:
                return line_client.reply_message(reply_token, error_msg)
            else:
                return line_client.send_text_message(user_id, error_msg)
    
    def handle_help_function(self, user_id: str, reply_token: Optional[str]) -> bool:
        """
        處理幫助功能
        
        Args:
            user_id: 用戶 ID
            reply_token: 回覆 Token
            
        Returns:
            bool: 是否成功處理
        """
        help_message = (
            "📋 可用功能列表：\n\n"
            "🍳 食譜功能 - 輸入「食譜功能」或「食譜」\n"
            "   上傳食物圖片，獲得詳細食譜和烹飪建議\n"
            "   （持續模式：可持續上傳圖片）\n\n"
            "📝 記錄功能 - 輸入「記錄功能」或「記錄」\n"
            "   上傳食物圖片，記錄食物名稱和入庫時間\n"
            "   （持續模式：可持續上傳圖片）\n\n"
            "🔍 查看功能 - 輸入「查看功能」或「查看」\n"
            "   查看您的食物記錄列表\n"
            "   （執行完後自動返回初始狀態）\n\n"
            "🗑️ 刪除功能 - 輸入「刪除功能」或「刪除」\n"
            "   記錄食品消耗，從最舊的記錄開始扣除\n"
            "   （持續模式：可持續輸入消耗信息）\n\n"
            "💡 功能切換：\n"
            "   在任何持續模式下，輸入其他功能關鍵字即可切換功能\n\n"
            "❓ 幫助 - 輸入「幫助」或「help」\n"
            "   查看此功能列表\n\n"
            "❌ 退出 - 輸入「退出」或「exit」\n"
            "   結束當前功能模式，返回初始狀態"
        )
        
        if reply_token:
            return line_client.reply_message(reply_token, help_message)
        else:
            return line_client.send_text_message(user_id, help_message)
    
    def handle_exit_function(self, user_id: str, reply_token: Optional[str]) -> bool:
        """
        處理退出功能
        
        Args:
            user_id: 用戶 ID
            reply_token: 回覆 Token
            
        Returns:
            bool: 是否成功處理
        """
        if user_id in user_function_state:
            current_function = user_function_state[user_id]
            del user_function_state[user_id]
            
            # 清除記錄映射（如果是在刪除模式下）
            if current_function == 'delete' and user_id in user_delete_records_mapping:
                del user_delete_records_mapping[user_id]
            
            function_name_map = {
                'recipe': '食譜',
                'record': '記錄',
                'view': '查看',
                'delete': '刪除'
            }
            function_name = function_name_map.get(current_function, current_function)
            exit_message = f"已退出 {function_name} 功能模式。\n\n輸入「幫助」查看可用功能。"
        else:
            exit_message = "您目前沒有啟用任何功能模式。\n\n輸入「幫助」查看可用功能。"
        
        if reply_token:
            return line_client.reply_message(reply_token, exit_message)
        else:
            return line_client.send_text_message(user_id, exit_message)
    
    def route_message(self, user_id: str, text: str, reply_token: Optional[str]) -> bool:
        """
        路由文字訊息到對應功能
        
        Args:
            user_id: 用戶 ID
            text: 文字訊息
            reply_token: 回覆 Token
            
        Returns:
            bool: 是否成功處理
        """
        # 檢測功能關鍵字（優先檢查是否要切換功能）
        function_name = self.detect_function(text)
        
        # 檢查用戶當前功能狀態
        current_function = user_function_state.get(user_id)
        
        # 如果檢測到退出功能，優先處理退出
        if function_name == 'exit':
            return self.functions['exit'](user_id, reply_token)
        
        # 如果檢測到功能關鍵字，則切換功能
        if function_name and function_name in self.functions:
            # 如果用戶在持續模式下，且輸入的是其他功能關鍵字，則切換功能
            if current_function and current_function != function_name:
                # 切換到新功能
                return self.functions[function_name](user_id, reply_token)
            elif not current_function:
                # 用戶不在任何模式下，啟用新功能
                return self.functions[function_name](user_id, reply_token)
            else:
                # 用戶已在該模式下，重新顯示提示（可選）
                return self.functions[function_name](user_id, reply_token)
        
        # 如果用戶在持續模式下，處理該模式的輸入
        if current_function == 'delete':
            # 用戶在刪除模式下，處理消耗輸入
            return self.handle_delete_consumption(user_id, text, reply_token)
        elif current_function in ['recipe', 'record']:
            # 食譜和記錄功能持續模式，但文字輸入應該提示上傳圖片
            # 如果輸入的是功能關鍵字，上面已經處理了
            # 這裡處理其他文字輸入
            guide_message = (
                f"您目前在「{self._get_function_name(current_function)}」模式下。\n\n"
                "請上傳圖片以使用該功能，或輸入其他功能關鍵字切換功能。\n"
                "輸入「退出」可結束當前功能模式。"
            )
            if reply_token:
                return line_client.reply_message(reply_token, guide_message)
            else:
                return line_client.send_text_message(user_id, guide_message)
        
        # 未識別的功能，顯示幫助訊息
        unknown_message = (
            "❓ 未識別的功能指令。\n\n"
            "請輸入「幫助」查看可用功能列表。"
        )
        if reply_token:
            return line_client.reply_message(reply_token, unknown_message)
        else:
            return line_client.send_text_message(user_id, unknown_message)
    
    def _get_function_name(self, function_key: str) -> str:
        """獲取功能的中文名稱"""
        function_name_map = {
            'recipe': '食譜',
            'record': '記錄',
            'view': '查看',
            'delete': '刪除'
        }
        return function_name_map.get(function_key, function_key)
    
    def handle_delete_consumption(self, user_id: str, text: str, reply_token: Optional[str]) -> bool:
        """
        處理刪除模式下的消耗輸入
        
        Args:
            user_id: 用戶 ID
            text: 用戶輸入的消耗信息
            reply_token: 回覆 Token
            
        Returns:
            bool: 是否成功處理
        """
        # 先檢查是否輸入功能關鍵字（允許在刪除模式下切換功能）
        function_name = self.detect_function(text)
        if function_name and function_name in self.functions:
            # 如果是退出功能，直接處理
            if function_name == 'exit':
                return self.functions['exit'](user_id, reply_token)
            # 如果是其他功能關鍵字，切換功能
            elif function_name != 'delete':
                return self.functions[function_name](user_id, reply_token)
        
        try:
            # 獲取用戶資訊（USERNAME）
            user_profile = get_user_profile(user_id)
            username = user_profile.get('displayName', '未知用戶') if user_profile else '未知用戶'
            print(f"處理消耗輸入：使用者 {user_id} (USERNAME: {username})")
            print(f"輸入內容: {text}")
            
            # 檢查是否為按編號刪除（格式：純數字 或 "數字 數字"）
            text_stripped = text.strip()
            
            # 匹配純數字（例如：3）或 "數字 數字"（例如：3 1）
            number_pattern = r'^(\d+)(?:\s+(\d+(?:\.\d+)?))?$'
            number_match = re.match(number_pattern, text_stripped)
            
            if number_match:
                # 按編號刪除
                record_number = int(number_match.group(1))
                deduct_amount = float(number_match.group(2)) if number_match.group(2) else None
                
                # 檢查用戶是否有記錄映射
                if user_id not in user_delete_records_mapping:
                    error_msg = (
                        "❌ 找不到記錄映射。\n\n"
                        "請重新輸入「刪除功能」查看記錄列表。"
                    )
                    if reply_token:
                        return line_client.reply_message(reply_token, error_msg)
                    else:
                        return line_client.send_text_message(user_id, error_msg)
                
                # 獲取記錄映射
                record_mapping = user_delete_records_mapping[user_id]
                
                if record_number not in record_mapping:
                    error_msg = f"❌ 找不到編號 {record_number} 的記錄。\n\n請重新輸入「刪除功能」查看記錄列表。"
                    if reply_token:
                        return line_client.reply_message(reply_token, error_msg)
                    else:
                        return line_client.send_text_message(user_id, error_msg)
                
                # 獲取記錄信息
                record_info = record_mapping[record_number]
                record_id = record_info['id']
                food_name = record_info['food_name']
                current_quantity = record_info.get('quantity')
                
                # 如果指定了數量，檢查是否可以部分刪除
                if deduct_amount is not None and current_quantity is not None:
                    current_quantity_float = float(current_quantity)
                    
                    if deduct_amount >= current_quantity_float:
                        # 完全刪除記錄
                        result = delete_food_record_by_id(record_id)
                        if result['success']:
                            # 從映射中移除
                            del record_mapping[record_number]
                            success_msg = f"✅ 已刪除編號 {record_number} 的記錄：{food_name}\n"
                            if reply_token:
                                return line_client.reply_message(reply_token, success_msg)
                            else:
                                return line_client.send_text_message(user_id, success_msg)
                        else:
                            error_msg = f"❌ 刪除失敗：{result.get('message', '未知錯誤')}"
                            if reply_token:
                                return line_client.reply_message(reply_token, error_msg)
                            else:
                                return line_client.send_text_message(user_id, error_msg)
                    else:
                        # 部分扣除：更新數量
                        new_quantity = current_quantity_float - deduct_amount
                        try:
                            if not db_manager.connection:
                                if not db_manager.connect():
                                    raise Exception("資料庫連接失敗")
                            
                            with db_manager.connection.cursor() as cursor:
                                update_sql = "UPDATE foods SET quantity = %s WHERE id = %s"
                                cursor.execute(update_sql, (new_quantity, record_id))
                                db_manager.connection.commit()
                                
                                # 更新映射中的數量
                                record_info['quantity'] = new_quantity
                                
                                success_msg = (
                                    f"✅ 已更新編號 {record_number} 的記錄：{food_name}\n"
                                    f"   數量：{current_quantity} -> {new_quantity} (扣除 {deduct_amount})"
                                )
                                if reply_token:
                                    return line_client.reply_message(reply_token, success_msg)
                                else:
                                    return line_client.send_text_message(user_id, success_msg)
                        except Exception as e:
                            print(f"更新記錄失敗: {e}")
                            error_msg = f"❌ 更新記錄失敗：{str(e)}"
                            if reply_token:
                                return line_client.reply_message(reply_token, error_msg)
                            else:
                                return line_client.send_text_message(user_id, error_msg)
                else:
                    # 沒有指定數量，完全刪除記錄
                    result = delete_food_record_by_id(record_id)
                    if result['success']:
                        # 從映射中移除
                        del record_mapping[record_number]
                        success_msg = f"✅ 已刪除編號 {record_number} 的記錄：{food_name}"
                        if reply_token:
                            return line_client.reply_message(reply_token, success_msg)
                        else:
                            return line_client.send_text_message(user_id, success_msg)
                    else:
                        error_msg = f"❌ 刪除失敗：{result.get('message', '未知錯誤')}"
                        if reply_token:
                            return line_client.reply_message(reply_token, error_msg)
                        else:
                            return line_client.send_text_message(user_id, error_msg)
            
            # 如果不是編號格式，按原來的邏輯處理（食品名稱 + 數量）
            # 解析消耗信息
            consumption_items = parse_consumption_input(text)
            
            if not consumption_items:
                # 無法解析消耗信息
                error_msg = (
                    "❌ 無法解析消耗信息。\n\n"
                    "刪除方式：\n"
                    "1️⃣ 按編號刪除：輸入編號（例如：3）\n"
                    "2️⃣ 按食品名稱刪除：輸入食品名稱 數量（例如：蘋果 2個）"
                )
                if reply_token:
                    return line_client.reply_message(reply_token, error_msg)
                else:
                    return line_client.send_text_message(user_id, error_msg)
            
            # 處理每個消耗項目
            result_messages = []
            all_success = True
            
            for item in consumption_items:
                food_name = item['food_name']
                deduct_amount = item['quantity']
                
                print(f"處理消耗：{food_name} {deduct_amount}")
                
                # 扣除數量
                result = deduct_food_quantity(username, food_name, deduct_amount)
                
                if result['success']:
                    # 構建結果訊息
                    item_message = f"✅ {food_name} - 扣除 {float(deduct_amount)}\n"
                    
                    # 顯示更新的記錄
                    if result['updated_records']:
                        for record in result['updated_records']:
                            old_qty = float(record['old_quantity'])
                            new_qty = float(record['new_quantity'])
                            item_message += f"  更新：記錄 ID {record['id']} ({old_qty} -> {new_qty})\n"
                    
                    # 顯示刪除的記錄
                    if result['deleted_records']:
                        for record in result['deleted_records']:
                            qty = float(record['quantity'])
                            item_message += f"  刪除：記錄 ID {record['id']} (數量: {qty})\n"
                    
                    # 如果還有剩餘數量無法扣除
                    if result['remaining_amount'] > 0:
                        remaining = float(result['remaining_amount'])
                        item_message += f"  ⚠️ 警告：還需要扣除 {remaining}，但庫存不足\n"
                        all_success = False
                    
                    result_messages.append(item_message)
                else:
                    # 處理失敗
                    error_msg = f"❌ {food_name} - {result.get('message', '處理失敗')}\n"
                    result_messages.append(error_msg)
                    all_success = False
            
            # 組合所有結果訊息
            if all_success:
                final_message = "✅ 消耗記錄完成！\n\n"
            else:
                final_message = "⚠️ 消耗記錄處理完成（部分項目可能有問題）\n\n"
            
            final_message += "\n".join(result_messages)
            final_message += "\n\n輸入「查看功能」查看更新後的記錄。"
            
            # 發送訊息
            if reply_token:
                return line_client.reply_message(reply_token, final_message)
            else:
                return line_client.send_text_message(user_id, final_message)
                
        except Exception as e:
            print(f"處理消耗輸入失敗: {e}")
            import traceback
            traceback.print_exc()
            error_msg = "處理消耗信息時發生錯誤，請稍後再試。"
            if reply_token:
                return line_client.reply_message(reply_token, error_msg)
            else:
                return line_client.send_text_message(user_id, error_msg)


# 初始化路由器
router = FunctionRouter()


@app.route('/webhook', methods=['POST'])
def webhook():
    """
    LINE Webhook 端點（主入口）
    
    處理流程：
    1. 驗證 Webhook 簽名
    2. 解析事件
    3. 根據事件類型和用戶狀態路由到對應功能
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
        
        # 收集圖片事件和其他事件
        image_events_by_user = {}
        text_events = []
        postback_events = []
        other_events = []
        
        for event in events:
            # 處理 Postback 事件
            if event.get('type') == 'postback':
                postback_data = event.get('postback', {}).get('data', '')
                user_id = event.get('source', {}).get('userId', '')
                reply_token = event.get('replyToken', '')
                
                if postback_data.startswith('recipe_select='):
                    postback_events.append({
                        'user_id': user_id,
                        'reply_token': reply_token,
                        'data': postback_data
                    })
                    continue
            
            # 處理圖片事件
            image_event = webhook_handler.handle_image_event(event)
            if image_event:
                user_id = image_event.get('user_id')
                if user_id:
                    if user_id not in image_events_by_user:
                        image_events_by_user[user_id] = []
                    image_events_by_user[user_id].append(image_event)
            else:
                # 處理文字訊息
                message_event = webhook_handler.handle_message_event(event)
                if message_event:
                    message_type = message_event['message_type']
                    if message_type == 'text':
                        text_events.append(message_event)
                    else:
                        other_events.append(message_event)
                else:
                    other_events.append(event)
        
        # 處理 Postback 事件（食譜選擇）
        for postback_event in postback_events:
            user_id = postback_event['user_id']
            data = postback_event['data']
            
            print(f"收到 Postback 事件（用戶: {user_id}, data: {data}）")
            
            try:
                # 解析選擇的食譜編號
                recipe_num = int(data.split('=')[1])
                
                with recipe_storage_lock:
                    if user_id in user_recipe_storage:
                        recipes = user_recipe_storage[user_id]
                        recipe_key = f'dish_{recipe_num}'
                        
                        if recipe_key in recipes:
                            recipe_content = recipes[recipe_key]
                            
                            # 去除食譜中的 # 標記
                            cleaned_recipe = remove_markdown_headers(recipe_content)
                            
                            # 獲取 text（碳足跡計算結果）
                            text_content = user_text_storage.get(user_id, '')
                            
                            # 組合消息：text + 分隔符 + 清理後的食譜內容
                            if text_content:
                                message = text_content + "\n" + "=" * 25 + "\n" + cleaned_recipe
                            else:
                                message = cleaned_recipe
                            
                            # 使用 push message 發送（reply_token 已經用於發送 Image Carousel）
                            line_client.send_text_message(user_id, message)
                            print(f"已發送食譜 {recipe_num} 給用戶 {user_id}")
                        else:
                            error_msg = f"找不到編號 {recipe_num} 的食譜"
                            line_client.send_text_message(user_id, error_msg)
                            print(f"[錯誤] {error_msg}")
                    else:
                        error_msg = "食譜數據已過期，請重新上傳圖片"
                        line_client.send_text_message(user_id, error_msg)
                        print(f"[錯誤] 用戶 {user_id} 的食譜數據不存在")
            except (ValueError, IndexError) as e:
                print(f"解析 Postback 數據失敗: {e}")
                error_msg = "處理請求時發生錯誤，請重新上傳圖片"
                line_client.send_text_message(user_id, error_msg)
        
        # 處理文字訊息（功能路由）
        for text_event in text_events:
            user_id = text_event.get('user_id')
            reply_token = text_event.get('reply_token')
            text = text_event['message'].get('text', '').strip()
            
            print(f"收到文字訊息（用戶: {user_id}, 內容: {text}）")
            
            # 路由到對應功能
            router.route_message(user_id, text, reply_token)
        
        # 處理圖片事件（根據用戶功能狀態路由）
        for user_id, image_events in image_events_by_user.items():
            current_function = user_function_state.get(user_id)
            
            print(f"收到用戶 {user_id} 的 {len(image_events)} 張圖片（當前功能: {current_function}）")
            
            if current_function == 'recipe':
                # 路由到食譜功能
                # 只在第一次收到圖片時發送"請稍等"訊息（避免重複發送）
                current_time = time.time()
                last_sent_time = user_wait_message_sent.get(user_id, 0)
                
                # 如果10秒內沒有發送過，則發送
                if current_time - last_sent_time > 10:
                    wait_message = "請稍等，正在處理您的圖片..."
                    line_client.send_text_message(user_id, wait_message)
                    user_wait_message_sent[user_id] = current_time
                    print(f"已發送「請稍等」訊息給用戶 {user_id}")
                
                # 處理圖片（保留 reply_token 給後續結果發送使用）
                if len(image_events) == 1:
                    recipe_flow_controller.process_line_image(image_events[0])
                else:
                    recipe_flow_controller.process_line_images(image_events)
            elif current_function == 'record':
                # 路由到記錄功能（調用 record.py）
                for image_event in image_events:
                    # 調用 record.py 的 add_image_to_buffer 函數
                    # 該函數會自動處理緩衝和變數設定（freshrecord="True"）
                    add_image_to_buffer(image_event)
            else:
                # 未啟用功能，提示用戶
                guide_message = (
                    "📸 您上傳了圖片，但尚未啟用任何功能。\n\n"
                    "請先輸入「食譜功能」或「記錄功能」來啟用對應功能，\n"
                    "或輸入「幫助」查看所有可用功能。"
                )
                reply_token = image_events[0].get('reply_token')
                if reply_token:
                    line_client.reply_message(reply_token, guide_message)
                else:
                    line_client.send_text_message(user_id, guide_message)
        
        # 處理其他事件（影片、文件等）
        for event in other_events:
            # 如果 event 已經是處理過的 message_event（字典格式），直接使用
            # 否則嘗試解析原始事件
            if isinstance(event, dict) and 'message_type' in event:
                message_event = event
            else:
                message_event = webhook_handler.handle_message_event(event)
            
            if message_event:
                message_type = message_event.get('message_type')
                user_id = message_event.get('user_id')
                reply_token = message_event.get('reply_token')
                
                print(f"[除錯] 處理其他事件：類型={message_type}, 用戶={user_id}, reply_token={'有' if reply_token else '無'}")
                
                unsupported_types = ['video', 'file', 'audio']
                if message_type in unsupported_types:
                    error_msg = "目前不支援此格式，請上傳圖片。"
                    print(f"[除錯] 發送錯誤訊息給用戶 {user_id}: {error_msg}")
                    if reply_token:
                        success = line_client.reply_message(reply_token, error_msg)
                        print(f"[除錯] 使用 reply_token 發送結果: {success}")
                    elif user_id:
                        success = line_client.send_text_message(user_id, error_msg)
                        print(f"[除錯] 使用 push 訊息發送結果: {success}")
                    else:
                        print(f"[警告] 無法發送訊息：缺少 user_id 和 reply_token")
            else:
                print(f"[警告] 無法解析事件: {type(event)}")
        
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
        圖片內容或輕量級404響應（減少資源消耗）
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
            # 返回輕量級1x1透明PNG，並設置長期緩存頭
            # 這樣可以減少資源消耗，客戶端會緩存這個響應
            # 減少 Cloud Run 的 CPU、內存和帶寬使用
            return app.response_class(
                EMPTY_PNG_DATA,
                mimetype='image/png',
                headers={
                    'Cache-Control': 'public, max-age=31536000, immutable',  # 緩存1年
                    'Content-Length': str(len(EMPTY_PNG_DATA))
                }
            )


@app.route('/health', methods=['GET'])
def health():
    """健康檢查端點"""
    return {'status': 'ok', 'service': 'LINE Bot Router', 'functions': list(router.functions.keys())}, 200


@app.route('/', methods=['GET'])
def index():
    """首頁"""
    return '''
    <h1>LINE Bot 中繼器系統</h1>
    <p>Webhook 端點: /webhook</p>
    <p>健康檢查: /health</p>
    <p>狀態: 運行中</p>
    <h2>已註冊功能：</h2>
    <ul>
        <li>🍳 食譜功能 (recipe)</li>
    </ul>
    '''


def main():
    """主函數"""
    import argparse
    
    parser = argparse.ArgumentParser(description='LINE Bot 中繼器系統')
    # Cloud Run 會設置 PORT 環境變數，優先使用它
    port = int(os.getenv('PORT', 5000))
    parser.add_argument('--host', type=str, default='0.0.0.0',
                       help='伺服器主機 (預設: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=port,
                       help='伺服器埠號 (預設: 從 PORT 環境變數或 5000)')
    parser.add_argument('--debug', action='store_true',
                       help='啟用除錯模式')
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("LINE Bot 中繼器系統")
    print("=" * 60)
    print(f"LINE Channel Secret: {LINE_CHANNEL_SECRET[:20]}...")
    print(f"Webhook URL: http://{args.host}:{args.port}/webhook")
    print(f"已註冊功能: {', '.join(router.functions.keys())}")
    print("=" * 60)
    print("\n伺服器啟動中...")
    print("注意: LINE Webhook 需要 HTTPS，本地測試請使用 ngrok")
    print("\n")
    
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == '__main__':
    main()
