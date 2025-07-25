#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI绘画工具多用户管理系统 - 高性能优化版本 + 令牌管理
新优化：
1. 数据库连接池优化，避免频繁创建连接
2. 队列检查缓存机制，减少网络请求
3. 线程池管理，限制并发线程数
4. 异步处理和错误重试机制
5. 系统负载监控和自适应调节
6. WebSocket事件批量推送优化
7. 集成令牌管理和Caddy配置自动更新
队列检查：严格保证1秒间隔检查
"""
import os
import sys
import time
import json
import shutil
import signal
import sqlite3
import hashlib
import threading
import subprocess
import secrets
import string
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import queue
import logging

from flask import Flask, request, jsonify, render_template_string, session, redirect, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
import requests
import psutil

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ============ 配置参数 ============
# 外部访问IP地址 - 请根据实际情况修改
EXTERNAL_IP = "XXX.XX.XX.XXX"  # 修改为您的服务器外部IP地址

# 其他配置常量
COMFYUI_PATH = "/mnt/sdb1/ComfyUI"
MAIN_PY_PATH = "/mnt/sdb1/ComfyUI/main.py"
OUTPUT_BASE_DIR = "/mnt/sdb1/ComfyUI/instances"
DATABASE_PATH = "comfyui_manager.db"
MANAGER_PORT = 8880
IDLE_TIMEOUT = 10 * 60  # 10分钟空闲超时 (600秒)
QUEUE_CHECK_INTERVAL = 1  # 队列检查间隔(秒) - 严格保证1秒间隔
MAX_IDLE_CHECKS = 300  # 最大空闲检查次数 (300*2秒)

# Caddy配置相关
CADDYFILE_PATH = "Caddyfile"
DEFAULT_TOKEN = "123456789"

# 性能优化参数
MAX_WORKERS = 8  # 最大工作线程数
DB_POOL_SIZE = 10  # 数据库连接池大小
REQUEST_TIMEOUT = 3  # HTTP请求超时时间
CACHE_DURATION = 0.5  # 缓存持续时间(秒) - 调整为0.5秒确保1秒间隔检查

# 端口配置
PORT_CONFIG = {
    8001: 1,  # 端口 -> NPU卡号
    8002: 2,
    8003: 3,
    8004: 4
}

# 外部端口映射
EXTERNAL_PORT_MAPPING = {
    8001: 9001,
    8002: 9002,
    8003: 9003,
    8004: 9004
}

# 工作区端口映射 (外部端口 + 80)
WORKSPACE_PORT_MAPPING = {
    8001: 9081,
    8002: 9082,
    8003: 9083,
    8004: 9084
}

# NPU卡显示映射
NPU_CARD_MAPPING = {
    8001: 1,
    8002: 2,
    8003: 3,
    8004: 4
}

# 全局令牌存储
PORT_TOKENS = {
    8001: DEFAULT_TOKEN,
    8002: DEFAULT_TOKEN,
    8003: DEFAULT_TOKEN,
    8004: DEFAULT_TOKEN
}

app = Flask(__name__)
app.secret_key = 'your-secret-key-change-this-in-production'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

def generate_random_token(length=32):
    """生成随机令牌"""
    characters = string.ascii_letters + string.digits
    return ''.join(secrets.choice(characters) for _ in range(length))

def update_caddyfile_token(port, token):
    """更新Caddyfile中指定端口的令牌"""
    try:
        if not os.path.exists(CADDYFILE_PATH):
            logger.error(f"Caddyfile不存在: {CADDYFILE_PATH}")
            return False
        
        # 读取Caddyfile内容
        with open(CADDYFILE_PATH, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 构建要替换的模式
        token_key = f"token_{port}"
        old_pattern = f"@hasToken query {token_key}="
        
        # 查找并替换令牌值
        lines = content.split('\n')
        updated = False
        
        for i, line in enumerate(lines):
            if old_pattern in line:
                # 提取并替换令牌值
                if '=' in line:
                    prefix = line.split('=')[0] + '='
                    lines[i] = f"{prefix}{token}"
                    updated = True
                    logger.info(f"更新Caddyfile: {token_key}={token}")
                    break
        
        if not updated:
            logger.warning(f"未找到需要更新的令牌配置: {token_key}")
            return False
        
        # 写回文件
        with open(CADDYFILE_PATH, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        
        # 重新加载Caddy配置
        result = subprocess.run(['caddy', 'reload', '--config', CADDYFILE_PATH], 
                              capture_output=True, text=True)
        
        if result.returncode == 0:
            logger.info(f"Caddy配置重新加载成功，端口 {port} 令牌已更新")
            return True
        else:
            logger.error(f"Caddy配置重新加载失败: {result.stderr}")
            return False
            
    except Exception as e:
        logger.error(f"更新Caddyfile失败: {e}")
        return False

def set_port_token(port, token=None):
    """设置端口令牌"""
    if token is None:
        token = DEFAULT_TOKEN
    
    PORT_TOKENS[port] = token
    logger.info(f"设置端口 {port} 令牌: {token}")
    
    # 更新Caddyfile
    return update_caddyfile_token(port, token)

def get_port_token(port):
    """获取端口令牌"""
    return PORT_TOKENS.get(port, DEFAULT_TOKEN)

class DatabaseManager:
    def __init__(self, db_path):
        self.db_path = db_path
        self.connection_pool = queue.Queue(maxsize=DB_POOL_SIZE)
        self.pool_lock = threading.Lock()
        self.init_database()
        self._init_connection_pool()
    
    def _init_connection_pool(self):
        """初始化数据库连接池"""
        for _ in range(DB_POOL_SIZE):
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute('PRAGMA journal_mode=WAL')  # 启用WAL模式提高并发性能
            conn.execute('PRAGMA synchronous=NORMAL')  # 平衡性能和安全性
            self.connection_pool.put(conn)
    
    def get_connection(self):
        """从连接池获取数据库连接"""
        try:
            return self.connection_pool.get(timeout=5)
        except queue.Empty:
            # 如果连接池为空，创建新连接
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')
            return conn
    
    def return_connection(self, conn):
        """将数据库连接返回连接池"""
        try:
            if self.connection_pool.qsize() < DB_POOL_SIZE:
                self.connection_pool.put(conn, timeout=1)
            else:
                conn.close()
        except queue.Full:
            conn.close()
    
    def execute_with_retry(self, operation, *args, max_retries=3):
        """执行数据库操作，带重试机制"""
        for attempt in range(max_retries):
            conn = None
            try:
                conn = self.get_connection()
                cursor = conn.cursor()
                result = operation(cursor, *args)
                conn.commit()
                return result
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    time.sleep(0.1 * (attempt + 1))  # 指数退避
                    continue
                else:
                    logger.error(f"Database operation failed: {e}")
                    raise
            except Exception as e:
                logger.error(f"Database operation error: {e}")
                raise
            finally:
                if conn:
                    self.return_connection(conn)
    
    def init_database(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 创建用户表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin BOOLEAN DEFAULT FALSE,
                assigned_port INTEGER,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 创建端口状态表（修改为支持多用户共享）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS port_status (
                port INTEGER PRIMARY KEY,
                current_user_id INTEGER,
                current_username TEXT,
                process_id INTEGER,
                status TEXT DEFAULT 'idle',
                last_activity TIMESTAMP,
                last_queue_check TIMESTAMP,
                idle_count INTEGER DEFAULT 0,
                FOREIGN KEY (current_user_id) REFERENCES users (id)
            )
        ''')
        
        # 检查并添加新字段（用于数据库升级）
        try:
            cursor.execute('ALTER TABLE port_status ADD COLUMN last_queue_check TIMESTAMP')
        except sqlite3.OperationalError:
            pass
        
        try:
            cursor.execute('ALTER TABLE port_status ADD COLUMN idle_count INTEGER DEFAULT 0')
        except sqlite3.OperationalError:
            pass
        
        try:
            cursor.execute('ALTER TABLE port_status ADD COLUMN current_username TEXT')
        except sqlite3.OperationalError:
            pass
        
        # 初始化管理员账户
        admin_password = hashlib.sha256('admin123'.encode()).hexdigest()
        cursor.execute('''
            INSERT OR IGNORE INTO users (username, password_hash, is_admin)
            VALUES (?, ?, ?)
        ''', ('admin', admin_password, True))
        
        # 初始化端口状态
        for port in PORT_CONFIG.keys():
            cursor.execute('''
                INSERT OR IGNORE INTO port_status (port, status, idle_count)
                VALUES (?, 'idle', 0)
            ''', (port,))
        
        # 修复所有端口的空闲检测值，确保都为0
        cursor.execute('''
            UPDATE port_status 
            SET idle_count = 0, current_user_id = NULL, current_username = NULL, process_id = NULL
            WHERE status = 'idle'
        ''')
        
        conn.commit()
        conn.close()
    
    def get_user(self, username):
        def operation(cursor, username):
            cursor.execute('SELECT * FROM users WHERE username = ?', (username,))
            return cursor.fetchone()
        return self.execute_with_retry(operation, username)
    
    def get_user_by_id(self, user_id):
        def operation(cursor, user_id):
            cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,))
            return cursor.fetchone()
        return self.execute_with_retry(operation, user_id)
    
    def create_user(self, username, password, assigned_port):
        def operation(cursor, username, password, assigned_port):
            password_hash = hashlib.sha256(password.encode()).hexdigest()
            try:
                cursor.execute('''
                    INSERT INTO users (username, password_hash, assigned_port)
                    VALUES (?, ?, ?)
                ''', (username, password_hash, assigned_port))
                return True
            except sqlite3.IntegrityError:
                return False
        return self.execute_with_retry(operation, username, password, assigned_port)
    
    def update_user_port(self, user_id, new_port):
        def operation(cursor, user_id, new_port):
            cursor.execute('UPDATE users SET assigned_port = ? WHERE id = ?', (new_port, user_id))
        self.execute_with_retry(operation, user_id, new_port)
    
    def delete_user(self, user_id):
        def operation(cursor, user_id):
            cursor.execute('DELETE FROM users WHERE id = ?', (user_id,))
        self.execute_with_retry(operation, user_id)
    
    def get_all_users(self):
        def operation(cursor):
            cursor.execute('SELECT * FROM users WHERE is_admin = FALSE')
            return cursor.fetchall()
        return self.execute_with_retry(operation)
    
    def get_users_by_port(self, port):
        """获取绑定到指定端口的所有用户"""
        def operation(cursor, port):
            cursor.execute('SELECT * FROM users WHERE assigned_port = ? AND is_admin = FALSE', (port,))
            return cursor.fetchall()
        return self.execute_with_retry(operation, port)
    
    def update_port_status(self, port, user_id=None, username=None, process_id=None, status=None, idle_count=None):
        def operation(cursor, port, user_id, username, process_id, status, idle_count):
            # 构建更新查询
            fields = []
            params = []
            
            if user_id is not None:
                fields.append('current_user_id = ?')
                params.append(user_id)
            
            if username is not None:
                fields.append('current_username = ?')
                params.append(username)
            
            if process_id is not None:
                fields.append('process_id = ?')
                params.append(process_id)
            
            if status is not None:
                fields.append('status = ?')
                params.append(status)
            
            if idle_count is not None:
                fields.append('idle_count = ?')
                params.append(idle_count)
            
            # 总是更新最后活动时间
            fields.append('last_activity = CURRENT_TIMESTAMP')
            
            params.append(port)
            
            query = f'''
                UPDATE port_status 
                SET {', '.join(fields)}
                WHERE port = ?
            '''
            
            cursor.execute(query, params)
        
        self.execute_with_retry(operation, port, user_id, username, process_id, status, idle_count)
    
    def update_queue_check(self, port, idle_count):
        """更新队列检查时间和空闲计数"""
        def operation(cursor, port, idle_count):
            cursor.execute('''
                UPDATE port_status 
                SET last_queue_check = CURRENT_TIMESTAMP, idle_count = ?
                WHERE port = ?
            ''', (idle_count, port))
        
        self.execute_with_retry(operation, port, idle_count)
    
    def get_port_status(self, port):
        def operation(cursor, port):
            cursor.execute('SELECT * FROM port_status WHERE port = ?', (port,))
            return cursor.fetchone()
        return self.execute_with_retry(operation, port)
    
    def get_all_port_status(self):
        def operation(cursor):
            cursor.execute('SELECT * FROM port_status ORDER BY port')
            return cursor.fetchall()
        return self.execute_with_retry(operation)
    
    def find_user_occupied_port(self, username):
        """查找用户当前占用的端口"""
        def operation(cursor, username):
            cursor.execute('SELECT port FROM port_status WHERE current_username = ? AND status != "idle"', (username,))
            result = cursor.fetchone()
            return result[0] if result else None
        return self.execute_with_retry(operation, username)

class QueueCache:
    """队列状态缓存类，减少频繁的网络请求（调整为0.5秒缓存确保1秒间隔检查）"""
    def __init__(self, cache_duration=CACHE_DURATION):
        self.cache = {}
        self.cache_duration = cache_duration
        self.lock = threading.Lock()
    
    def get(self, port):
        with self.lock:
            if port in self.cache:
                timestamp, is_idle = self.cache[port]
                if time.time() - timestamp < self.cache_duration:
                    return is_idle
            return None
    
    def set(self, port, is_idle):
        with self.lock:
            self.cache[port] = (time.time(), is_idle)
    
    def clear(self, port=None):
        with self.lock:
            if port:
                self.cache.pop(port, None)
            else:
                self.cache.clear()

class ComfyUIManager:
    def __init__(self, db_manager):
        self.db_manager = db_manager
        self.processes = {}  # port -> process object
        self.monitoring_thread = None
        self.queue_monitor_thread = None
        self.executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="ComfyUI-Worker")
        self.queue_cache = QueueCache()
        self.last_status_update = {}  # 记录上次状态更新时间，避免频繁WebSocket推送
        self.start_monitoring()
    
    def start_monitoring(self):
        """启动监控线程"""
        if self.monitoring_thread is None or not self.monitoring_thread.is_alive():
            self.monitoring_thread = threading.Thread(target=self._monitor_processes, daemon=True)
            self.monitoring_thread.start()
        
        # 启动队列监控线程
        if self.queue_monitor_thread is None or not self.queue_monitor_thread.is_alive():
            self.queue_monitor_thread = threading.Thread(target=self._monitor_queues, daemon=True)
            self.queue_monitor_thread.start()
    
    def _monitor_processes(self):
        """监控进程状态"""
        while True:
            try:
                # 批量获取所有端口状态，减少数据库访问
                all_statuses = self.db_manager.get_all_port_status()
                
                for status in all_statuses:
                    port = status[0]
                    if status and status[4] == 'running':  # status字段
                        # 检查进程是否还在运行
                        if not self._is_process_alive(status[3]):  # process_id字段
                            current_user = status[2] if len(status) > 2 else '未知用户'
                            logger.warning(f"端口 {port} 进程 {status[3]} 已停止，强制清理资源 (用户: {current_user})")
                            # 使用强化的停止方法确保彻底清理
                            self.stop_comfyui(port)
                
                time.sleep(10)  # 每10秒检查一次
            except Exception as e:
                logger.error(f"进程监控线程错误: {e}")
                time.sleep(10)
    
    def _monitor_queues(self):
        """监控队列状态以检测空闲用户（严格保证1秒间隔检查）"""
        while True:
            try:
                # 获取所有运行中的端口
                running_ports = []
                all_statuses = self.db_manager.get_all_port_status()
                
                for status in all_statuses:
                    if status and status[4] == 'running':  # 只检查运行中的端口
                        running_ports.append(status)
                
                if not running_ports:
                    time.sleep(QUEUE_CHECK_INTERVAL)  # 严格按照1秒间隔
                    continue
                
                # 使用线程池并发检查队列状态
                future_to_port = {}
                for status in running_ports:
                    port = status[0]
                    future = self.executor.submit(self._check_single_port_queue, status)
                    future_to_port[future] = port
                
                # 处理结果
                for future in as_completed(future_to_port.keys(), timeout=REQUEST_TIMEOUT + 1):
                    port = future_to_port[future]
                    try:
                        result = future.result()
                        if result:
                            self._handle_queue_check_result(result)
                    except Exception as e:
                        logger.error(f"队列检查失败 (端口 {port}): {e}")
                
                time.sleep(QUEUE_CHECK_INTERVAL)  # 严格保证1秒间隔检查
            except Exception as e:
                logger.error(f"队列监控线程错误: {e}")
                time.sleep(QUEUE_CHECK_INTERVAL)
    
    def _check_single_port_queue(self, status):
        """检查单个端口的队列状态（确保真实检查）"""
        port = status[0]
        current_idle_count = status[7] if len(status) > 7 and status[7] is not None else 0
        current_username = status[2] if len(status) > 2 else '未知用户'
        
        try:
            # 检查缓存（0.5秒缓存确保在1秒间隔内进行真实检查）
            cached_result = self.queue_cache.get(port)
            if cached_result is not None:
                is_idle = cached_result
                logger.debug(f"端口 {port} 使用缓存结果: {is_idle}")
            else:
                is_idle = self._check_queue_idle(port)
                self.queue_cache.set(port, is_idle)
                logger.debug(f"端口 {port} 进行真实队列检查: {is_idle}")
            
            return {
                'port': port,
                'is_idle': is_idle,
                'current_idle_count': current_idle_count,
                'current_username': current_username
            }
        except Exception as e:
            logger.error(f"检查端口 {port} 队列状态失败: {e}")
            return None
    
    def _handle_queue_check_result(self, result):
        """处理队列检查结果"""
        port = result['port']
        is_idle = result['is_idle']
        current_idle_count = result['current_idle_count']
        current_username = result['current_username']
        
        if is_idle:
            new_idle_count = current_idle_count + 1
            logger.info(f"端口 {port} 检测到空闲状态 ({new_idle_count}/{MAX_IDLE_CHECKS})")
            
            # 更新空闲计数
            self.db_manager.update_queue_check(port, new_idle_count)
            
            # 限制WebSocket推送频率，避免过于频繁的更新
            now = time.time()
            last_update = self.last_status_update.get(f"idle_{port}", 0)
            if now - last_update > 2:  # 每2秒最多推送一次空闲进度
                idle_percent = min(100, int((new_idle_count / MAX_IDLE_CHECKS) * 100))
                socketio.emit('idle_progress_update', {
                    'port': port,
                    'count': new_idle_count,
                    'percent': idle_percent
                })
                self.last_status_update[f"idle_{port}"] = now
            
            # 检查是否达到空闲阈值
            if new_idle_count >= MAX_IDLE_CHECKS:
                logger.warning(f"端口 {port} 空闲超时，强制终止进程并清理资源")
                # 先强制停止ComfyUI进程，然后清理资源
                self.stop_comfyui(port)
                socketio.emit('user_timeout', {
                    'port': port,
                    'username': current_username
                })
        else:
            # 有活动，重置空闲计数
            if current_idle_count > 0:
                logger.info(f"端口 {port} 检测到用户活动，重置空闲计数")
                self.db_manager.update_queue_check(port, 0)
                self.queue_cache.clear(port)  # 清除缓存，确保下次检查是最新的
                
                # 限制WebSocket推送频率
                now = time.time()
                last_update = self.last_status_update.get(f"reset_{port}", 0)
                if now - last_update > 1:  # 每1秒最多推送一次重置
                    socketio.emit('idle_progress_update', {
                        'port': port,
                        'count': 0,
                        'percent': 0
                    })
                    self.last_status_update[f"reset_{port}"] = now
    
    def _check_queue_idle(self, port):
        """检查ComfyUI队列是否空闲（优化版）"""
        external_port = EXTERNAL_PORT_MAPPING.get(port, port)
        
        # 尝试外部地址
        urls_to_try = [
            f"http://{EXTERNAL_IP}:{external_port}/api/queue",
            f"http://127.0.0.1:{port}/api/queue"  # 备用本地地址
        ]
        
        for url in urls_to_try:
            try:
                response = requests.get(url, timeout=REQUEST_TIMEOUT)
                if response.status_code == 200:
                    queue_data = response.json()
                    
                    # 检查队列是否完全空闲
                    if len(queue_data.get("queue_running", [])) == 0 and \
                       len(queue_data.get("queue_pending", [])) == 0:
                        return True
                    return False
            except requests.exceptions.RequestException:
                continue
            except json.JSONDecodeError as e:
                logger.error(f"解析队列JSON失败 (端口 {port}, URL: {url}): {e}")
                continue
        
        # 如果所有URL都失败，返回False（假设不空闲，避免误判）
        return False
    
    def _is_process_alive(self, pid):
        """检查进程是否存在"""
        try:
            if pid is None:
                return False
            return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
        except:
            return False
    
    def can_start_comfyui(self, port):
        """检查是否可以启动ComfyUI（端口空闲检测）"""
        status = self.db_manager.get_port_status(port)
        return status and status[4] == 'idle'  # status字段
    
    def start_comfyui(self, port, user_id, username):
        """启动ComfyUI进程（优化版 + 令牌管理）"""
        if port not in PORT_CONFIG:
            return False, "无效的端口", None
        
        # 检查端口是否可用（多用户共享检测）
        if not self.can_start_comfyui(port):
            status = self.db_manager.get_port_status(port)
            current_user = status[2] if len(status) > 2 and status[2] else '其他用户'
            return False, f"端口被占用中，当前使用者：{current_user}，请耐心等待", None
        
        # 创建用户专用输出目录
        output_dir = os.path.join(OUTPUT_BASE_DIR, f"port_{port}")
        os.makedirs(output_dir, exist_ok=True)
        
        # 设置环境变量
        env = os.environ.copy()
        npu_card = PORT_CONFIG[port] - 1  # 转换为0-based索引
        env['ASCEND_RT_VISIBLE_DEVICES'] = str(npu_card)
        
        # 启动命令
        cmd = [
            sys.executable, MAIN_PY_PATH,
            '--listen', '127.0.0.1',
            '--port', str(port),
            '--normalvram',
            '--async-offload',
            '--cache-lru', '1',
            '--preview-method', 'latent2rgb',
            '--force-fp16',
            '--fp16-unet',
            '--fp16-vae',
            '--dont-upcast-attention',
            '--disable-xformers',
            '--mmap-torch-files',
            '--use-split-cross-attention',
            '--output-directory', output_dir
        ]
        
        try:
            # 启动进程
            process = subprocess.Popen(
                cmd,
                cwd=COMFYUI_PATH,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid
            )
            
            self.processes[port] = process
            self.db_manager.update_port_status(
                port, user_id, username, process.pid, 'starting', idle_count=0
            )
            
            logger.info(f"启动ComfyUI进程: 端口={port}, PID={process.pid}, NPU卡={PORT_CONFIG[port]}, 用户={username}")
            
            # 通知所有绑定该端口的用户状态变化
            socketio.emit('port_status_changed', {
                'port': port,
                'status': 'starting',
                'username': username,
                'process_id': process.pid
            }, room=f'port_{port}')
            
            # 异步等待进程启动
            self.executor.submit(self._wait_for_startup, port, process, user_id, username)
            
            # 返回外部端口
            external_port = EXTERNAL_PORT_MAPPING.get(port, port)
            return True, "进程启动中...", external_port
        except Exception as e:
            logger.error(f"启动进程失败: {e}")
            return False, f"启动失败: {str(e)}", None
    
    def _wait_for_startup(self, port, process, user_id, username):
        """等待进程启动完成（优化版 + 令牌管理）"""
        max_wait = 120  # 最多等待2分钟
        external_port = EXTERNAL_PORT_MAPPING.get(port, port)
        
        for i in range(max_wait):
            if process.poll() is not None:
                # 进程已退出
                logger.error(f"进程提前退出: 端口={port}, 退出码={process.returncode}")
                self.db_manager.update_port_status(
                    port, status='idle', user_id=None, username=None, process_id=None, idle_count=0
                )
                socketio.emit('comfyui_failed', {
                    'port': port, 
                    'error': f'进程启动失败，退出码: {process.returncode}'
                }, room=f'port_{port}')
                return
            
            # 检查服务是否可用
            urls_to_check = [
                f"http://{EXTERNAL_IP}:{external_port}/api/queue",
                f"http://127.0.0.1:{port}/api/queue"
            ]
            
            service_ready = False
            for url in urls_to_check:
                try:
                    response = requests.get(url, timeout=2)
                    if response.status_code == 200:
                        service_ready = True
                        break
                except:
                    continue
            
            if service_ready:
                # 生成新的令牌
                new_token = generate_random_token()
                
                # 更新令牌并重新加载Caddy配置
                if set_port_token(port, new_token):
                    logger.info(f"ComfyUI启动成功并更新令牌: 端口={port}, 用户={username}, 令牌={new_token}")
                    self.db_manager.update_port_status(port, status='running', idle_count=0)
                    self.queue_cache.clear(port)  # 清除缓存
                    
                    # 获取工作区端口
                    workspace_port = WORKSPACE_PORT_MAPPING.get(port, external_port + 80)
                    
                    socketio.emit('comfyui_ready', {
                        'port': port,
                        'external_port': external_port,
                        'workspace_port': workspace_port,
                        'url': f'http://{EXTERNAL_IP}:{external_port}',
                        'workspace_url': f'http://{EXTERNAL_IP}:{workspace_port}/?token_{port}={new_token}',
                        'username': username,
                        'token': new_token
                    }, room=f'port_{port}')
                else:
                    logger.error(f"ComfyUI启动成功但令牌更新失败: 端口={port}, 用户={username}")
                    # 即使令牌更新失败，也标记为运行中，但通知错误
                    self.db_manager.update_port_status(port, status='running', idle_count=0)
                    self.queue_cache.clear(port)
                    
                    socketio.emit('comfyui_ready', {
                        'port': port,
                        'external_port': external_port,
                        'url': f'http://{EXTERNAL_IP}:{external_port}',
                        'username': username,
                        'error': '令牌更新失败，可能无法通过Caddy访问'
                    }, room=f'port_{port}')
                return
            
            time.sleep(1)
        
        # 启动超时
        logger.error(f"ComfyUI启动超时: 端口={port}, 用户={username}")
        self.stop_comfyui(port)
        self.db_manager.update_port_status(
            port, status='idle', user_id=None, username=None, process_id=None, idle_count=0
        )
        socketio.emit('comfyui_failed', {
            'port': port, 
            'error': '启动超时'
        }, room=f'port_{port}')
    
    def stop_comfyui(self, port):
        """停止ComfyUI进程（增强版，确保NPU进程被彻底清理 + 令牌重置）"""
        logger.info(f"停止ComfyUI进程: 端口={port}")
        
        # 清除缓存
        self.queue_cache.clear(port)
        
        # 获取当前端口状态，记录进程信息
        status = self.db_manager.get_port_status(port)
        process_pid = status[3] if status and len(status) > 3 else None
        current_user = status[2] if status and len(status) > 2 else '未知用户'
        
        logger.info(f"准备终止端口 {port} 的进程，PID: {process_pid}, 用户: {current_user}")
        
        # 方法1: 终止我们管理的进程对象
        if port in self.processes:
            try:
                process = self.processes[port]
                logger.info(f"终止进程组: PID {process.pid}")
                # 先尝试优雅终止
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                # 等待进程退出
                try:
                    process.wait(timeout=10)
                    logger.info(f"进程 {process.pid} 已优雅退出")
                except subprocess.TimeoutExpired:
                    logger.warning(f"进程 {process.pid} 未在10秒内退出，强制终止")
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    process.wait(timeout=5)
            except Exception as e:
                logger.error(f"终止进程组失败: {e}")
                # 如果进程组终止失败，尝试直接终止进程
                try:
                    if process.poll() is None:  # 进程仍在运行
                        process.terminate()
                        process.wait(timeout=5)
                        logger.info(f"直接终止进程 {process.pid} 成功")
                except Exception as e2:
                    logger.error(f"直接终止进程也失败: {e2}")
                    try:
                        process.kill()
                        process.wait(timeout=3)
                        logger.info(f"强制杀死进程 {process.pid} 成功")
                    except Exception as e3:
                        logger.error(f"强制杀死进程失败: {e3}")
            finally:
                if port in self.processes:
                    del self.processes[port]
        
        # 方法2: 根据端口查找并终止可能遗留的进程
        if process_pid:
            try:
                if psutil.pid_exists(process_pid):
                    proc = psutil.Process(process_pid)
                    if proc.is_running():
                        logger.info(f"发现遗留进程 {process_pid}，尝试终止")
                        # 获取进程的所有子进程
                        children = proc.children(recursive=True)
                        
                        # 先终止所有子进程
                        for child in children:
                            try:
                                logger.info(f"终止子进程: {child.pid}")
                                child.terminate()
                            except psutil.NoSuchProcess:
                                pass
                        
                        # 等待子进程退出
                        psutil.wait_procs(children, timeout=5)
                        
                        # 终止主进程
                        proc.terminate()
                        try:
                            proc.wait(timeout=10)
                            logger.info(f"遗留进程 {process_pid} 已终止")
                        except psutil.TimeoutExpired:
                            logger.warning(f"遗留进程 {process_pid} 未在10秒内退出，强制杀死")
                            proc.kill()
                            proc.wait(timeout=5)
                            logger.info(f"遗留进程 {process_pid} 已强制杀死")
            except psutil.NoSuchProcess:
                logger.info(f"进程 {process_pid} 已不存在")
            except Exception as e:
                logger.error(f"清理遗留进程失败: {e}")
        
        # 方法3: 通过端口查找进程（备用方法）
        try:
            # 查找占用指定端口的进程
            for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'connections']):
                try:
                    # 检查进程的网络连接
                    connections = proc.info['connections']
                    if connections:
                        for conn in connections:
                            if hasattr(conn, 'laddr') and conn.laddr and conn.laddr.port == port:
                                # 检查是否是ComfyUI进程
                                cmdline = proc.info['cmdline']
                                if cmdline and any('main.py' in cmd or 'ComfyUI' in cmd for cmd in cmdline):
                                    logger.info(f"发现占用端口 {port} 的ComfyUI进程: PID {proc.info['pid']}")
                                    proc_obj = psutil.Process(proc.info['pid'])
                                    proc_obj.terminate()
                                    try:
                                        proc_obj.wait(timeout=10)
                                        logger.info(f"端口占用进程 {proc.info['pid']} 已终止")
                                    except psutil.TimeoutExpired:
                                        proc_obj.kill()
                                        logger.info(f"端口占用进程 {proc.info['pid']} 已强制杀死")
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except Exception as e:
            logger.error(f"通过端口查找进程失败: {e}")
        
        # 重置令牌为默认值并更新Caddy配置
        if set_port_token(port, DEFAULT_TOKEN):
            logger.info(f"端口 {port} 令牌已重置为默认值")
        else:
            logger.error(f"端口 {port} 令牌重置失败")
        
        # 最后清理端口资源
        self._cleanup_port(port)
    
    def _cleanup_port(self, port):
        """清理端口资源（不包含进程终止，进程终止应在stop_comfyui中完成）"""
        logger.info(f"清理端口资源: 端口={port}")
        
        # 清理输出目录
        output_dir = os.path.join(OUTPUT_BASE_DIR, f"port_{port}")
        if os.path.exists(output_dir):
            try:
                shutil.rmtree(output_dir)
                os.makedirs(output_dir, exist_ok=True)
                logger.info(f"已清理输出目录: {output_dir}")
            except Exception as e:
                logger.error(f"清理输出目录失败: {e}")
        
        # 更新数据库状态
        self.db_manager.update_port_status(
            port, status='idle', user_id=None, username=None, process_id=None, idle_count=0
        )
        
        # 清除缓存
        self.queue_cache.clear(port)
        
        # 通知所有绑定该端口的用户
        socketio.emit('port_cleaned', {'port': port}, room=f'port_{port}')
    
    def cleanup_user_resources(self, username):
        """清理用户占用的所有资源"""
        occupied_port = self.db_manager.find_user_occupied_port(username)
        if occupied_port:
            logger.info(f"用户 {username} 退出登录，清理占用的端口 {occupied_port} 资源")
            self.stop_comfyui(occupied_port)
            return occupied_port
        return None

# 全局实例
db_manager = DatabaseManager(DATABASE_PATH)
comfyui_manager = ComfyUIManager(db_manager)

# 装饰器
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        user = db_manager.get_user(session['username'])
        if not user or not user[3]:  # is_admin字段
            return jsonify({'error': '需要管理员权限'}), 403
        return f(*args, **kwargs)
    return decorated_function

# 路由
@app.route('/')
def index():
    if 'user_id' in session:
        user = db_manager.get_user(session['username'])
        if user and user[3]:  # is_admin
            return redirect(url_for('admin_dashboard'))
        else:
            return redirect(url_for('user_dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        user = db_manager.get_user(username)
        if user and user[2] == hashlib.sha256(password.encode()).hexdigest():
            if user[5]:  # is_active
                session['user_id'] = user[0]
                session['username'] = user[1]
                session['is_admin'] = user[3]
                return redirect(url_for('index'))
            else:
                return render_template_string(LOGIN_TEMPLATE, error='账户已被停用')
        else:
            return render_template_string(LOGIN_TEMPLATE, error='用户名或密码错误')
    
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/logout')
def logout():
    # 在清理session之前获取用户信息
    username = session.get('username')
    is_admin = session.get('is_admin', False)
    
    # 如果不是管理员用户，清理其占用的资源
    if username and not is_admin:
        cleaned_port = comfyui_manager.cleanup_user_resources(username)
        if cleaned_port:
            logger.info(f"用户 {username} 退出登录时清理了端口 {cleaned_port} 的资源")
    
    session.clear()
    return redirect(url_for('login'))

@app.route('/admin')
@admin_required
def admin_dashboard():
    return render_template_string(ADMIN_TEMPLATE)

@app.route('/user')
@login_required
def user_dashboard():
    user = db_manager.get_user(session['username'])
    if not user or user[3]:  # is_admin
        return redirect(url_for('admin_dashboard'))
    
    if not user[4]:  # assigned_port
        return render_template_string(USER_TEMPLATE, 
                                    error='未分配工作端口，请联系管理员')
    
    port_status = db_manager.get_port_status(user[4])
    return render_template_string(USER_TEMPLATE, 
                                user=user, 
                                port_status=port_status)

@app.route('/api/admin/users', methods=['GET'])
@admin_required
def get_users():
    users = db_manager.get_all_users()
    port_statuses = db_manager.get_all_port_status()
    
    return jsonify({
        'users': users,
        'port_statuses': port_statuses,
        'available_ports': list(PORT_CONFIG.keys())
    })

@app.route('/api/admin/users', methods=['POST'])
@admin_required
def create_user():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')
    assigned_port = data.get('assigned_port')
    
    if not all([username, password, assigned_port]):
        return jsonify({'error': '请填写所有字段'}), 400
    
    if assigned_port not in PORT_CONFIG:
        return jsonify({'error': '无效的端口'}), 400
    
    if db_manager.create_user(username, password, assigned_port):
        return jsonify({'message': '用户创建成功'})
    else:
        return jsonify({'error': '用户名已存在'}), 400

@app.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
@admin_required
def delete_user(user_id):
    db_manager.delete_user(user_id)
    return jsonify({'message': '用户删除成功'})

@app.route('/api/admin/users/<int:user_id>/port', methods=['PUT'])
@admin_required
def update_user_port(user_id):
    data = request.get_json()
    new_port = data.get('port')
    
    if new_port not in PORT_CONFIG:
        return jsonify({'error': '无效的端口'}), 400
    
    db_manager.update_user_port(user_id, new_port)
    return jsonify({'message': '端口更新成功'})

@app.route('/api/admin/restart/<int:port>', methods=['POST'])
@admin_required
def restart_port(port):
    if port not in PORT_CONFIG:
        return jsonify({'error': '无效的端口'}), 400
    
    comfyui_manager.stop_comfyui(port)
    return jsonify({'message': '端口重启成功'})

@app.route('/api/admin/port_status', methods=['GET'])
@admin_required
def get_admin_port_status():
    """管理员获取所有端口状态的API"""
    port_statuses = db_manager.get_all_port_status()
    
    # 格式化端口状态数据
    formatted_statuses = []
    for status in port_statuses:
        # 计算空闲百分比
        idle_count = status[7] if len(status) > 7 and status[7] is not None else 0
        idle_percent = min(100, int((idle_count / MAX_IDLE_CHECKS) * 100))
        
        formatted_status = {
            'port': status[0],
            'current_user_id': status[1],
            'current_username': status[2],
            'process_id': status[3],
            'status': status[4],
            'last_activity': status[5],
            'last_queue_check': status[6],
            'idle_count': idle_count,
            'idle_percent': idle_percent,
            'external_port': EXTERNAL_PORT_MAPPING.get(status[0], status[0]),
            'workspace_port': WORKSPACE_PORT_MAPPING.get(status[0], EXTERNAL_PORT_MAPPING.get(status[0], status[0]) + 80),
            'npu_card': NPU_CARD_MAPPING.get(status[0], '未知'),
            'token': get_port_token(status[0])
        }
        formatted_statuses.append(formatted_status)
    
    return jsonify({'port_statuses': formatted_statuses})

@app.route('/api/user/start', methods=['POST'])
@login_required
def start_user_comfyui():
    user = db_manager.get_user(session['username'])
    if not user or user[3] or not user[4]:  # is_admin or no assigned_port
        return jsonify({'error': '无效的用户或未分配端口'}), 400
    
    port = user[4]
    
    # 检查端口是否可用
    if not comfyui_manager.can_start_comfyui(port):
        port_status = db_manager.get_port_status(port)
        current_user = port_status[2] if len(port_status) > 2 and port_status[2] else '其他用户'
        return jsonify({'error': f'其他用户正在占有资源，请耐心等待。当前使用者：{current_user}'}), 400
    
    success, message, external_port = comfyui_manager.start_comfyui(port, user[0], user[1])
    
    if success:
        return jsonify({
            'message': message, 
            'port': port,
            'external_port': external_port
        })
    else:
        return jsonify({'error': message}), 400

@app.route('/api/user/stop', methods=['POST'])
@login_required
def stop_user_comfyui():
    user = db_manager.get_user(session['username'])
    if not user or user[3] or not user[4]:
        return jsonify({'error': '无效的用户或未分配端口'}), 400
    
    port = user[4]
    port_status = db_manager.get_port_status(port)
    
    # 检查是否是当前用户占用的端口
    if port_status and port_status[1] == user[0]:  # current_user_id
        comfyui_manager.stop_comfyui(port)
        return jsonify({'message': '进程已停止'})
    else:
        return jsonify({'error': '您没有权限停止当前进程'}), 403

@app.route('/api/user/status', methods=['GET'])
@login_required
def get_user_status():
    user = db_manager.get_user(session['username'])
    if not user or user[3] or not user[4]:
        return jsonify({'error': '无效的用户或未分配端口'}), 400
    
    status = db_manager.get_port_status(user[4])
    npu_card = NPU_CARD_MAPPING.get(user[4], '未知')
    external_port = EXTERNAL_PORT_MAPPING.get(user[4], user[4])
    workspace_port = WORKSPACE_PORT_MAPPING.get(user[4], external_port + 80)
    token = get_port_token(user[4])
    
    # 计算空闲百分比
    idle_percent = 0
    if status and len(status) > 7 and status[7] is not None:
        idle_count = status[7]
        idle_percent = min(100, int((idle_count / MAX_IDLE_CHECKS) * 100))
    
    return jsonify({
        'status': status,
        'npu_card': npu_card,
        'external_port': external_port,
        'workspace_port': workspace_port,
        'workspace_url': f'http://{EXTERNAL_IP}:{workspace_port}/?token_{user[4]}={token}' if status and status[4] == 'running' else None,
        'idle_percent': idle_percent,
        'is_owner': status and status[1] == user[0] if status else False,  # 是否是当前端口的占用者
        'token': token
    })

@app.route('/api/user/history', methods=['GET'])
@login_required
def get_user_history():
    user = db_manager.get_user(session['username'])
    if not user or user[3] or not user[4]:
        return jsonify({'error': '无效的用户或未分配端口'}), 400
    
    port = user[4]
    external_port = EXTERNAL_PORT_MAPPING.get(port, port)
    
    # 尝试多个URL，提高成功率
    urls_to_try = [
        f"http://{EXTERNAL_IP}:{external_port}/api/history?max_items=10",
        f"http://127.0.0.1:{port}/api/history?max_items=10"
    ]
    
    for url in urls_to_try:
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                history_data = response.json()
                return jsonify({
                    'success': True,
                    'data': history_data
                })
        except requests.exceptions.RequestException:
            continue
    
    return jsonify({
        'success': False,
        'error': '无法连接到ComfyUI服务'
    })

# WebSocket事件
@socketio.on('join_port')
def on_join_port(data):
    port = data['port']
    join_room(f'port_{port}')
    logger.info(f"用户 {session.get('username', '未知')} 加入端口 {port} 房间")

@socketio.on('leave_port')
def on_leave_port(data):
    port = data['port']
    leave_room(f'port_{port}')
    logger.info(f"用户 {session.get('username', '未知')} 离开端口 {port} 房间")

@socketio.on('join_admin')
def on_join_admin():
    join_room('admin')
    logger.info(f"管理员 {session.get('username', '未知')} 加入管理员房间")

@socketio.on('leave_admin')
def on_leave_admin():
    leave_room('admin')
    logger.info(f"管理员 {session.get('username', '未知')} 离开管理员房间")

# HTML模板（与原版相同，省略以节省空间）
LOGIN_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>AI绘画工具 - 登录</title>
    <meta charset="utf-8">
    <style>
        body { font-family: Arial, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); margin: 0; padding: 50px; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
        .container { max-width: 400px; width: 100%; background: white; padding: 40px; border-radius: 15px; box-shadow: 0 10px 30px rgba(0,0,0,0.2); }
        h1 { text-align: center; color: #333; margin-bottom: 30px; font-size: 24px; }
        .form-group { margin-bottom: 20px; }
        label { display: block; margin-bottom: 8px; font-weight: bold; color: #555; }
        input[type="text"], input[type="password"] { width: 100%; padding: 12px; border: 2px solid #e1e5e9; border-radius: 8px; box-sizing: border-box; font-size: 16px; transition: border-color 0.3s; }
        input[type="text"]:focus, input[type="password"]:focus { outline: none; border-color: #667eea; }
        button { width: 100%; padding: 15px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border: none; border-radius: 8px; cursor: pointer; font-size: 16px; font-weight: bold; transition: transform 0.2s; }
        button:hover { transform: translateY(-2px); }
        .error { color: #e74c3c; margin-top: 15px; text-align: center; padding: 10px; background: #fdf2f2; border-radius: 5px; border: 1px solid #fecaca; }
        .logo { text-align: center; margin-bottom: 20px; }
        .logo i { font-size: 48px; color: #667eea; }
    </style>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
</head>
<body>
    <div class="container">
        <div class="logo">
            <i class="fas fa-palette"></i>
        </div>
        <h1>AI绘画工具登录</h1>
        <form method="POST">
            <div class="form-group">
                <label for="username"><i class="fas fa-user"></i> 用户名:</label>
                <input type="text" id="username" name="username" required>
            </div>
            <div class="form-group">
                <label for="password"><i class="fas fa-lock"></i> 密码:</label>
                <input type="password" id="password" name="password" required>
            </div>
            <button type="submit"><i class="fas fa-sign-in-alt"></i> 登录</button>
        </form>
        {% if error %}
            <div class="error"><i class="fas fa-exclamation-triangle"></i> {{ error }}</div>
        {% endif %}
    </div>
</body>
</html>
'''

ADMIN_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>管理员控制台 - 令牌管理版</title>
    <meta charset="utf-8">
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 20px; background: #f8f9fa; }
        .header { background: white; padding: 25px; border-radius: 12px; margin-bottom: 25px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); border-left: 5px solid #007bff; }
        .header h1 { margin: 0; color: #333; font-size: 28px; }
        .logout { float: right; background: #dc3545; color: white; padding: 10px 20px; text-decoration: none; border-radius: 8px; transition: all 0.3s; }
        .logout:hover { background: #c82333; transform: translateY(-2px); }
        .section { background: white; padding: 25px; border-radius: 12px; margin-bottom: 25px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); }
        .section h2 { margin-top: 0; color: #333; font-size: 24px; border-bottom: 2px solid #e9ecef; padding-bottom: 10px; }
        table { width: 100%; border-collapse: collapse; margin-top: 15px; }
        th, td { padding: 15px 12px; text-align: left; border-bottom: 1px solid #e9ecef; }
        th { background: #f8f9fa; font-weight: 600; color: #495057; font-size: 14px; text-transform: uppercase; letter-spacing: 0.5px; }
        tr:hover { background: #f8f9fa; }
        .btn { padding: 8px 16px; margin: 3px; border: none; border-radius: 6px; cursor: pointer; text-decoration: none; display: inline-block; font-size: 14px; font-weight: 500; transition: all 0.3s; }
        .btn-primary { background: #007bff; color: white; }
        .btn-primary:hover { background: #0056b3; transform: translateY(-1px); }
        .btn-danger { background: #dc3545; color: white; }
        .btn-danger:hover { background: #c82333; transform: translateY(-1px); }
        .btn-success { background: #28a745; color: white; }
        .btn-success:hover { background: #218838; transform: translateY(-1px); }
        .btn-warning { background: #ffc107; color: #212529; }
        .btn-warning:hover { background: #e0a800; transform: translateY(-1px); }
        .form-group { margin-bottom: 20px; }
        .form-group label { display: block; margin-bottom: 8px; font-weight: 600; color: #495057; }
        .form-group input, .form-group select { width: 100%; padding: 12px; border: 2px solid #e9ecef; border-radius: 8px; font-size: 16px; transition: border-color 0.3s; }
        .form-group input:focus, .form-group select:focus { outline: none; border-color: #007bff; }
        .status-idle { color: #6c757d; font-weight: 500; }
        .status-running { color: #28a745; font-weight: 500; }
        .status-starting { color: #ffc107; font-weight: 500; }
        .create-user-form { background: #f8f9fa; padding: 25px; border-radius: 10px; margin-bottom: 25px; border: 2px dashed #dee2e6; }
        .create-user-form h3 { margin-top: 0; color: #495057; }
        .form-row { display: flex; gap: 20px; align-items: end; }
        .form-row .form-group { flex: 1; margin-bottom: 0; }
        .idle-bar { height: 8px; background: #e9ecef; border-radius: 4px; margin-top: 8px; overflow: hidden; position: relative; }
        .idle-progress { height: 100%; background: linear-gradient(90deg, #28a745 0%, #ffc107 50%, #dc3545 100%); width: 0%; transition: width 0.3s ease; }
        .idle-text { font-size: 12px; color: #6c757d; margin-top: 5px; }
        .password-toggle { position: relative; }
        .password-toggle input { padding-right: 45px; }
        .password-toggle .toggle-btn { position: absolute; right: 12px; top: 50%; transform: translateY(-50%); background: none; border: none; color: #6c757d; cursor: pointer; padding: 5px; }
        .password-toggle .toggle-btn:hover { color: #007bff; }
        .spinner { display: inline-block; width: 20px; height: 20px; border: 3px solid #f3f3f3; border-top: 3px solid #007bff; border-radius: 50%; animation: spin 1s linear infinite; }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        .loading { opacity: 0.7; }
        .badge { padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: 500; }
        .badge-success { background: #d4edda; color: #155724; }
        .badge-warning { background: #fff3cd; color: #856404; }
        .badge-danger { background: #f8d7da; color: #721c24; }
        .progress-container { margin-top: 5px; }
        .auto-refresh { float: right; margin-bottom: 15px; }
        .auto-refresh label { margin-right: 10px; font-weight: normal; }
        .current-user { color: #007bff; font-weight: bold; }
        .performance-info { background: #d1ecf1; padding: 15px; border-radius: 8px; margin-bottom: 20px; border-left: 4px solid #17a2b8; }
        .token-info { background: #fff3cd; padding: 15px; border-radius: 8px; margin-bottom: 20px; border-left: 4px solid #ffc107; }
        .token-display { font-family: monospace; font-size: 12px; color: #495057; background: #f8f9fa; padding: 5px; border-radius: 4px; word-break: break-all; }
    </style>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
</head>
<body>
    <div class="header">
        <h1><i class="fas fa-tachometer-alt"></i> 管理员控制台 - 令牌管理版</h1>
        <a href="/logout" class="logout"><i class="fas fa-sign-out-alt"></i> 退出登录</a>
        <div style="clear: both;"></div>
    </div>

    <div class="performance-info">
        <h4><i class="fas fa-info-circle"></i> 性能优化说明</h4>
        <p>当前版本已优化：数据库连接池、队列检查缓存、线程池管理、异步处理等，支持4个ComfyUI实例同时稳定运行。</p>
    </div>

    <div class="token-info">
        <h4><i class="fas fa-key"></i> 令牌管理功能</h4>
        <p>集成Caddy配置自动更新：用户启动ComfyUI时生成随机令牌并更新Caddyfile，停止时重置为默认令牌。工作区地址包含令牌参数以确保安全访问。</p>
    </div>

    <div class="section">
        <div class="auto-refresh">
            <label>
                <input type="checkbox" id="autoRefresh" checked> 
                <i class="fas fa-sync"></i> 自动刷新 (1秒)
            </label>
        </div>
        <h2><i class="fas fa-server"></i> 端口状态监控（令牌管理）</h2>
        <table id="portStatusTable">
            <thead>
                <tr>
                    <th><i class="fas fa-plug"></i> 端口</th>
                    <th><i class="fas fa-external-link-alt"></i> 外部端口</th>
                    <th><i class="fas fa-desktop"></i> 工作区端口</th>
                    <th><i class="fas fa-microchip"></i> NPU卡</th>
                    <th><i class="fas fa-heartbeat"></i> 进程状态</th>
                    <th><i class="fas fa-user"></i> 当前用户</th>
                    <th><i class="fas fa-key"></i> 访问令牌</th>
                    <th><i class="fas fa-clock"></i> 空闲检测</th>
                    <th><i class="fas fa-tools"></i> 操作</th>
                </tr>
            </thead>
            <tbody></tbody>
        </table>
    </div>

    <div class="section">
        <h2><i class="fas fa-users"></i> 用户管理（多用户可共享端口）</h2>
        
        <div class="create-user-form">
            <h3><i class="fas fa-user-plus"></i> 创建新用户</h3>
            <form id="createUserForm">
                <div class="form-row">
                    <div class="form-group">
                        <label><i class="fas fa-user"></i> 用户名:</label>
                        <input type="text" id="newUsername" required placeholder="请输入用户名">
                    </div>
                    <div class="form-group">
                        <label><i class="fas fa-lock"></i> 密码:</label>
                        <div class="password-toggle">
                            <input type="password" id="newPassword" required placeholder="请输入密码">
                            <button type="button" class="toggle-btn" onclick="togglePassword('newPassword')">
                                <i class="fas fa-eye" id="newPasswordIcon"></i>
                            </button>
                        </div>
                    </div>
                    <div class="form-group">
                        <label><i class="fas fa-plug"></i> 分配端口:</label>
                        <select id="newUserPort" required></select>
                    </div>
                    <button type="submit" class="btn btn-primary">
                        <i class="fas fa-plus"></i> 创建用户
                    </button>
                </div>
            </form>
        </div>
        
        <table id="usersTable">
            <thead>
                <tr>
                    <th><i class="fas fa-user"></i> 用户名</th>
                    <th><i class="fas fa-plug"></i> 分配端口</th>
                    <th><i class="fas fa-external-link-alt"></i> 外部端口</th>
                    <th><i class="fas fa-desktop"></i> 工作区端口</th>
                    <th><i class="fas fa-microchip"></i> NPU卡</th>
                    <th><i class="fas fa-toggle-on"></i> 状态</th>
                    <th><i class="fas fa-calendar"></i> 创建时间</th>
                    <th><i class="fas fa-tools"></i> 操作</th>
                </tr>
            </thead>
            <tbody></tbody>
        </table>
    </div>

    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    <script>
        let portConfig = {8001: 1, 8002: 2, 8003: 3, 8004: 4};
        let externalPortMapping = {8001: 9001, 8002: 9002, 8003: 9003, 8004: 9004};
        let workspacePortMapping = {8001: 9081, 8002: 9082, 8003: 9083, 8004: 9084};
        const socket = io();
        let autoRefreshInterval = null;
        
        // 加入管理员房间
        socket.emit('join_admin');
        
        // WebSocket事件监听
        socket.on('idle_progress_update', function(data) {
            updateIdleProgress(data.port, data.count, data.percent);
        });
        
        socket.on('user_timeout', function(data) {
            showNotification(`端口 ${data.port} 用户 ${data.username} 因空闲超时被自动下线`, 'warning');
            loadPortStatus();
        });
        
        socket.on('port_status_changed', function(data) {
            showNotification(`端口 ${data.port} 状态变更：${data.username} ${data.status === 'starting' ? '正在启动' : data.status}`, 'info');
            loadPortStatus();
        });
        
        socket.on('comfyui_ready', function(data) {
            showNotification(`端口 ${data.port} ComfyUI启动完成：${data.username}${data.token ? ' (令牌已更新)' : ''}`, 'success');
            loadPortStatus();
        });
        
        socket.on('comfyui_failed', function(data) {
            showNotification(`端口 ${data.port} ComfyUI启动失败`, 'error');
            loadPortStatus();
        });
        
        socket.on('port_cleaned', function(data) {
            showNotification(`端口 ${data.port} 资源已清理`, 'info');
            loadPortStatus();
        });
        
        function togglePassword(inputId) {
            const input = document.getElementById(inputId);
            const icon = document.getElementById(inputId + 'Icon');
            
            if (input.type === 'password') {
                input.type = 'text';
                icon.className = 'fas fa-eye-slash';
            } else {
                input.type = 'password';
                icon.className = 'fas fa-eye';
            }
        }
        
        function showNotification(message, type = 'info') {
            const notification = document.createElement('div');
            notification.style.cssText = `
                position: fixed; top: 20px; right: 20px; z-index: 1000;
                padding: 15px 20px; border-radius: 8px; color: white; max-width: 400px;
                background: ${type === 'success' ? '#28a745' : type === 'warning' ? '#ffc107' : type === 'error' ? '#dc3545' : '#007bff'};
                box-shadow: 0 4px 15px rgba(0,0,0,0.2);
            `;
            notification.innerHTML = `<i class="fas fa-info-circle"></i> ${message}`;
            document.body.appendChild(notification);
            
            setTimeout(() => {
                if (document.body.contains(notification)) {
                    document.body.removeChild(notification);
                }
            }, 5000);
        }
        
        function loadData() {
            Promise.all([
                fetch('/api/admin/users').then(response => response.json()),
                fetch('/api/admin/port_status').then(response => response.json())
            ]).then(([userData, portData]) => {
                updatePortStatusTable(portData.port_statuses);
                updateUsersTable(userData.users);
                updatePortOptions(userData.available_ports);
            }).catch(error => {
                console.error('Error:', error);
                showNotification('加载数据失败', 'error');
            });
        }
        
        function loadPortStatus() {
            fetch('/api/admin/port_status')
                .then(response => response.json())
                .then(data => {
                    updatePortStatusTable(data.port_statuses);
                })
                .catch(error => {
                    console.error('Error:', error);
                });
        }
        
        function updateIdleProgress(port, count, percent) {
            const row = document.querySelector(`#portStatusTable tbody tr[data-port="${port}"]`);
            if (row) {
                const idleCell = row.cells[7]; // 空闲检测列
                const progressHtml = `
                    <div class="idle-text">${count}/300 (${percent}%)</div>
                    <div class="idle-bar">
                        <div class="idle-progress" style="width: ${percent}%"></div>
                    </div>
                `;
                idleCell.innerHTML = progressHtml;
            }
        }
        
        function updatePortStatusTable(portStatuses) {
            const tbody = document.querySelector('#portStatusTable tbody');
            tbody.innerHTML = '';
            
            portStatuses.forEach(status => {
                const row = tbody.insertRow();
                row.setAttribute('data-port', status.port);
                
                // 进程ID显示
                let processIdDisplay = '-';
                if (status.status === 'running' && status.process_id) {
                    processIdDisplay = status.process_id;
                } else if (status.status === 'starting' && status.process_id) {
                    processIdDisplay = `<span class="spinner"></span> ${status.process_id}`;
                }
                
                // 当前用户显示
                let currentUserDisplay = '-';
                if (status.current_username) {
                    currentUserDisplay = `<span class="current-user">${status.current_username}</span>`;
                }
                
                // 令牌显示
                let tokenDisplay = '<span class="token-display">123456789</span>';
                if (status.token && status.token !== '123456789') {
                    tokenDisplay = `<span class="token-display">${status.token.substring(0, 8)}...</span>`;
                }
                
                row.innerHTML = `
                    <td><strong>${status.port}</strong></td>
                    <td><strong>${status.external_port}</strong></td>
                    <td><strong>${status.workspace_port}</strong></td>
                    <td><span class="badge badge-success">NPU卡${status.npu_card}</span></td>
                    <td class="status-${status.status}">${getStatusText(status.status)}</td>
                    <td>${currentUserDisplay}</td>
                    <td>${tokenDisplay}</td>
                    <td>
                        <div class="progress-container">
                            <div class="idle-text">${status.idle_count}/300 (${status.idle_percent}%)</div>
                            <div class="idle-bar">
                                <div class="idle-progress" style="width: ${status.idle_percent}%"></div>
                            </div>
                        </div>
                    </td>
                    <td>
                        <button class="btn btn-warning" onclick="restartPort(${status.port})">
                            <i class="fas fa-redo"></i> 重启
                        </button>
                    </td>
                `;
            });
        }
        
        function updateUsersTable(users) {
            const tbody = document.querySelector('#usersTable tbody');
            tbody.innerHTML = '';
            
            users.forEach(user => {
                const row = tbody.insertRow();
                const externalPort = user[4] ? externalPortMapping[user[4]] || user[4] : '未分配';
                const workspacePort = user[4] ? workspacePortMapping[user[4]] || (externalPort + 80) : '未分配';
                const npuCard = user[4] ? portConfig[user[4]] || '未知' : '未分配';
                const statusBadge = user[5] ? '<span class="badge badge-success">活跃</span>' : '<span class="badge badge-danger">停用</span>';
                
                row.innerHTML = `
                    <td><strong>${user[1]}</strong></td>
                    <td>${user[4] || '未分配'}</td>
                    <td><strong>${externalPort}</strong></td>
                    <td><strong>${workspacePort}</strong></td>
                    <td><span class="badge badge-success">NPU卡${npuCard}</span></td>
                    <td>${statusBadge}</td>
                    <td>${new Date(user[6]).toLocaleDateString()}</td>
                    <td>
                        <button class="btn btn-primary" onclick="editUserPort(${user[0]}, ${user[4]})">
                            <i class="fas fa-edit"></i> 编辑端口
                        </button>
                        <button class="btn btn-danger" onclick="deleteUser(${user[0]})">
                            <i class="fas fa-trash"></i> 删除
                        </button>
                    </td>
                `;
            });
        }
        
        function updatePortOptions(availablePorts) {
            const select = document.getElementById('newUserPort');
            select.innerHTML = '';
            availablePorts.forEach(port => {
                const option = document.createElement('option');
                option.value = port;
                const workspacePort = workspacePortMapping[port] || (externalPortMapping[port] + 80);
                option.textContent = `${port} (外部:${externalPortMapping[port]}, 工作区:${workspacePort}, NPU卡${portConfig[port]})`;
                select.appendChild(option);
            });
        }
        
        function getStatusText(status) {
            const statusMap = {
                'idle': '<i class="fas fa-circle"></i> 空闲',
                'running': '<i class="fas fa-play-circle"></i> 运行中',
                'starting': '<i class="fas fa-spinner fa-spin"></i> 启动中'
            };
            return statusMap[status] || status;
        }
        
        function restartPort(port) {
            if (confirm(`确定要重启端口 ${port} 吗？这将中断当前用户的工作并重置访问令牌。`)) {
                const btn = event.target.closest('button');
                btn.innerHTML = '<div class="spinner"></div> 重启中...';
                btn.disabled = true;
                
                fetch(`/api/admin/restart/${port}`, { method: 'POST' })
                    .then(response => response.json())
                    .then(data => {
                        showNotification(data.message || data.error, data.message ? 'success' : 'error');
                        loadPortStatus();
                    })
                    .catch(error => {
                        console.error('Error:', error);
                        showNotification('操作失败', 'error');
                    })
                    .finally(() => {
                        btn.innerHTML = '<i class="fas fa-redo"></i> 重启';
                        btn.disabled = false;
                    });
            }
        }
        
        function editUserPort(userId, currentPort) {
            const newPort = prompt('请输入新的端口号 (8001-8004):', currentPort);
            if (newPort && newPort != currentPort) {
                const portNum = parseInt(newPort);
                if (portNum >= 8001 && portNum <= 8004) {
                    fetch(`/api/admin/users/${userId}/port`, {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ port: portNum })
                    })
                    .then(response => response.json())
                    .then(data => {
                        showNotification(data.message || data.error, data.message ? 'success' : 'error');
                        loadData();
                    })
                    .catch(error => {
                        console.error('Error:', error);
                        showNotification('操作失败', 'error');
                    });
                } else {
                    showNotification('端口号必须在 8001-8004 范围内', 'error');
                }
            }
        }
        
        function deleteUser(userId) {
            if (confirm('确定要删除此用户吗？')) {
                fetch(`/api/admin/users/${userId}`, { method: 'DELETE' })
                    .then(response => response.json())
                    .then(data => {
                        showNotification(data.message || data.error, data.message ? 'success' : 'error');
                        loadData();
                    })
                    .catch(error => {
                        console.error('Error:', error);
                        showNotification('操作失败', 'error');
                    });
            }
        }
        
        document.getElementById('createUserForm').addEventListener('submit', function(e) {
            e.preventDefault();
            const username = document.getElementById('newUsername').value;
            const password = document.getElementById('newPassword').value;
            const port = parseInt(document.getElementById('newUserPort').value);
            
            const submitBtn = this.querySelector('button[type="submit"]');
            submitBtn.innerHTML = '<div class="spinner"></div> 创建中...';
            submitBtn.disabled = true;
            
            fetch('/api/admin/users', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, password, assigned_port: port })
            })
            .then(response => response.json())
            .then(data => {
                showNotification(data.message || data.error, data.message ? 'success' : 'error');
                if (data.message) {
                    document.getElementById('createUserForm').reset();
                    loadData();
                }
            })
            .catch(error => {
                console.error('Error:', error);
                showNotification('操作失败', 'error');
            })
            .finally(() => {
                submitBtn.innerHTML = '<i class="fas fa-plus"></i> 创建用户';
                submitBtn.disabled = false;
            });
        });
        
        // 自动刷新控制
        document.getElementById('autoRefresh').addEventListener('change', function() {
            if (this.checked) {
                startAutoRefresh();
            } else {
                stopAutoRefresh();
            }
        });
        
        function startAutoRefresh() {
            if (autoRefreshInterval) {
                clearInterval(autoRefreshInterval);
            }
            autoRefreshInterval = setInterval(loadPortStatus, 1000); // 每秒刷新
        }
        
        function stopAutoRefresh() {
            if (autoRefreshInterval) {
                clearInterval(autoRefreshInterval);
                autoRefreshInterval = null;
            }
        }
        
        // 初始化
        loadData();
        startAutoRefresh(); // 默认开启自动刷新
        
        // 页面卸载时清理
        window.addEventListener('beforeunload', function() {
            stopAutoRefresh();
            socket.emit('leave_admin');
        });
    </script>
</body>
</html>
'''

USER_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>用户工作台 - 令牌管理版</title>
    <meta charset="utf-8">
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 20px; background: #f8f9fa; }
        .header { background: white; padding: 25px; border-radius: 12px; margin-bottom: 25px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); border-left: 5px solid #28a745; }
        .header h1 { margin: 0; color: #333; font-size: 28px; }
        .logout { float: right; background: #dc3545; color: white; padding: 10px 20px; text-decoration: none; border-radius: 8px; transition: all 0.3s; }
        .logout:hover { background: #c82333; transform: translateY(-2px); }
        .section { background: white; padding: 25px; border-radius: 12px; margin-bottom: 25px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); }
        .section h2 { margin-top: 0; color: #333; font-size: 24px; border-bottom: 2px solid #e9ecef; padding-bottom: 10px; }
        .status-card { padding: 20px; border-radius: 10px; margin-bottom: 20px; border-left: 5px solid #ddd; }
        .status-running { background: #d4edda; border-left-color: #28a745; }
        .status-idle { background: #f8d7da; border-left-color: #dc3545; }
        .status-starting { background: #fff3cd; border-left-color: #ffc107; }
        .btn { padding: 12px 24px; margin: 8px; border: none; border-radius: 8px; cursor: pointer; font-size: 16px; font-weight: 500; text-decoration: none; display: inline-block; transition: all 0.3s; }
        .btn-primary { background: #007bff; color: white; }
        .btn-primary:hover { background: #0056b3; transform: translateY(-2px); }
        .btn-success { background: #28a745; color: white; }
        .btn-success:hover { background: #218838; transform: translateY(-2px); }
        .btn-danger { background: #dc3545; color: white; }
        .btn-danger:hover { background: #c82333; transform: translateY(-2px); }
        .btn-warning { background: #ffc107; color: #212529; }
        .btn-warning:hover { background: #e0a800; transform: translateY(-2px); }
        .btn:disabled { background: #6c757d; cursor: not-allowed; transform: none; }
        .message { padding: 15px; margin: 15px 0; border-radius: 8px; border-left: 4px solid; }
        .message-success { background: #d4edda; color: #155724; border-left-color: #28a745; }
        .message-error { background: #f8d7da; color: #721c24; border-left-color: #dc3545; }
        .message-info { background: #d1ecf1; color: #0c5460; border-left-color: #17a2b8; }
        .message-warning { background: #fff3cd; color: #856404; border-left-color: #ffc107; }
        .spinner { display: inline-block; width: 20px; height: 20px; border: 3px solid #f3f3f3; border-top: 3px solid #007bff; border-radius: 50%; animation: spin 1s linear infinite; margin-right: 10px; }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        .info-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; margin: 20px 0; }
        .info-item { background: #f8f9fa; padding: 20px; border-radius: 10px; border: 2px solid #e9ecef; }
        .info-item label { font-weight: 600; color: #495057; display: block; margin-bottom: 8px; }
        .info-item .value { color: #28a745; font-weight: bold; font-size: 18px; }
        .comfyui-container { margin-top: 25px; }
        .comfyui-iframe { width: 100%; height: 80vh; border: 2px solid #e9ecef; border-radius: 10px; background: white; }
        .idle-alert { background: #fff3cd; border-left: 4px solid #ffc107; padding: 15px; margin: 15px 0; border-radius: 8px; }
        .idle-bar { height: 12px; background: #e9ecef; border-radius: 6px; margin-top: 10px; overflow: hidden; }
        .idle-progress { height: 100%; background: linear-gradient(90deg, #28a745 0%, #ffc107 50%, #dc3545 100%); width: 0%; transition: width 0.3s ease; }
        .tabs { display: flex; margin-bottom: 25px; border-bottom: 2px solid #e9ecef; }
        .tab { padding: 15px 25px; background: transparent; border: none; cursor: pointer; margin-right: 10px; border-radius: 8px 8px 0 0; font-size: 16px; font-weight: 500; transition: all 0.3s; }
        .tab.active { background: #007bff; color: white; }
        .tab:hover:not(.active) { background: #f8f9fa; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .history-container { background: #f8f9fa; padding: 20px; border-radius: 10px; margin-top: 20px; max-height: 400px; overflow-y: auto; }
        .history-item { background: white; padding: 15px; margin-bottom: 10px; border-radius: 8px; border-left: 4px solid #007bff; }
        .history-item h4 { margin: 0 0 10px 0; color: #333; }
        .history-item .timestamp { color: #6c757d; font-size: 14px; }
        .no-history { text-align: center; color: #6c757d; padding: 40px; }
        .occupied-warning { background: #f8d7da; border-left: 4px solid #dc3545; padding: 15px; margin: 15px 0; border-radius: 8px; }
        .current-user { color: #007bff; font-weight: bold; }
        .history-auto-refresh { float: right; margin-bottom: 10px; }
        .history-auto-refresh label { font-weight: normal; font-size: 14px; }
        .performance-info { background: #d1ecf1; padding: 15px; border-radius: 8px; margin-bottom: 20px; border-left: 4px solid #17a2b8; }
        .token-info { background: #fff3cd; padding: 15px; border-radius: 8px; margin-bottom: 20px; border-left: 4px solid #ffc107; }
        .workspace-url { font-family: monospace; font-size: 14px; color: #495057; background: #f8f9fa; padding: 10px; border-radius: 4px; word-break: break-all; margin-top: 10px; }
    </style>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
</head>
<body>
    <div class="header">
        <h1><i class="fas fa-palette"></i> 用户工作台 - {{ user[1] if user else '未知用户' }}</h1>
        <a href="/logout" class="logout"><i class="fas fa-sign-out-alt"></i> 退出登录</a>
        <div style="clear: both;"></div>
    </div>

    {% if error %}
    <div class="message message-error"><i class="fas fa-exclamation-triangle"></i> {{ error }}</div>
    {% endif %}

    {% if user and user[4] %}
    <div class="performance-info">
        <h4><i class="fas fa-rocket"></i> 系统优化提示</h4>
        <p>当前版本已优化性能，支持4个ComfyUI实例同时稳定运行，队列检查间隔1秒保持不变，提升了并发处理能力。</p>
    </div>

    <div class="token-info">
        <h4><i class="fas fa-key"></i> 令牌安全访问</h4>
        <p>您的工作区采用令牌验证机制，确保安全访问。令牌会在启动时自动生成，停止时自动重置。请使用"打开工作区"按钮安全访问。</p>
    </div>

    <div class="section">
        <div class="tabs">
            <div class="tab active" onclick="switchTab('control')"><i class="fas fa-control"></i> 控制面板</div>
            <div class="tab" onclick="switchTab('workspace')" id="workspaceTab" style="display: none;"><i class="fas fa-paint-brush"></i> 工作区</div>
        </div>

        <div id="controlTab" class="tab-content active">
            <h2><i class="fas fa-server"></i> 工作环境状态（端口共享模式 + 令牌保护）</h2>
            <div id="statusCard" class="status-card">
                <div id="statusContent">
                    <div class="spinner"></div> 正在获取状态...
                </div>
            </div>
            
            <div id="occupiedWarning" class="occupied-warning" style="display: none;">
                <strong><i class="fas fa-exclamation-triangle"></i> 端口占用提醒:</strong> 
                当前端口被其他用户占用中，您可以排队等待。一旦端口空闲，您即可开始使用。
                <div>当前使用者：<span id="currentUser" class="current-user">-</span></div>
            </div>
            
            <div id="idleAlert" class="idle-alert" style="display: none;">
                <strong><i class="fas fa-exclamation-triangle"></i> 空闲警告:</strong> 检测到您已空闲一段时间，长时间空闲系统将自动释放资源。
                <div class="idle-bar">
                    <div class="idle-progress" id="idleProgress"></div>
                </div>
            </div>
            
            <div class="info-grid">
                <div class="info-item">
                    <label><i class="fas fa-plug"></i> 分配端口:</label>
                    <div class="value">{{ user[4] }}</div>
                </div>
                <div class="info-item">
                    <label><i class="fas fa-external-link-alt"></i> 外部访问端口:</label>
                    <div class="value" id="externalPort">-</div>
                </div>
                <div class="info-item">
                    <label><i class="fas fa-desktop"></i> 工作区端口:</label>
                    <div class="value" id="workspacePort">-</div>
                </div>
                <div class="info-item">
                    <label><i class="fas fa-microchip"></i> NPU卡:</label>
                    <div class="value" id="npuCard">-</div>
                </div>
            </div>
            
            <div id="workspaceUrlContainer" style="display: none;">
                <label><i class="fas fa-link"></i> 安全工作区地址（含令牌）:</label>
                <div class="workspace-url" id="workspaceUrl">-</div>
            </div>
            
            <div id="messageArea"></div>
            
            <div id="controlButtons">
                <button id="startBtn" class="btn btn-success" onclick="startComfyUI()">
                    <i class="fas fa-play"></i> 启动ComfyUI
                </button>
                <button id="stopBtn" class="btn btn-danger" onclick="stopComfyUI()" disabled>
                    <i class="fas fa-stop"></i> 停止ComfyUI
                </button>
                <button id="openWorkspaceBtn" class="btn btn-primary" onclick="openWorkspace()" disabled>
                    <i class="fas fa-external-link-alt"></i> 打开安全工作区
                </button>
            </div>
            
            <div id="historyContainer" class="history-container" style="display: none;">
                <div class="history-auto-refresh">
                    <label>
                        <input type="checkbox" id="historyAutoRefresh" checked> 
                        <i class="fas fa-sync"></i> 自动刷新历史记录 (3秒)
                    </label>
                </div>
                <h3><i class="fas fa-history"></i> 最近历史记录</h3>
                <div id="historyContent">
                    <div class="no-history">暂无历史记录</div>
                </div>
            </div>
        </div>

        <div id="workspaceTab" class="tab-content">
            <h2><i class="fas fa-paint-brush"></i> ComfyUI 安全工作区</h2>
            <div class="comfyui-container">
                <iframe id="comfyuiFrame" class="comfyui-iframe" style="display: none;"></iframe>
                <div id="workspaceMessage" style="text-align: center; padding: 50px; color: #6c757d;">
                    <i class="fas fa-info-circle"></i> 请先启动ComfyUI服务
                </div>
            </div>
        </div>
    </div>
    {% endif %}

    <script>
        const socket = io();
        let currentPort = {{ user[4] if user and user[4] else 'null' }};
        let externalPort = null;
        let workspacePort = null;
        let workspaceUrl = null;
        let currentStatus = 'idle';
        let currentTab = 'control';
        let isOwner = false;
        let historyRefreshInterval = null;
        
        function switchTab(tabName) {
            // 隐藏所有标签内容
            document.querySelectorAll('.tab-content').forEach(content => {
                content.classList.remove('active');
            });
            document.querySelectorAll('.tab').forEach(tab => {
                tab.classList.remove('active');
            });
            
            // 显示选中的标签
            document.getElementById(tabName + 'Tab').classList.add('active');
            document.querySelector(`.tab[onclick="switchTab('${tabName}')"]`).classList.add('active');
            
            currentTab = tabName;
            
            // 如果切换到工作区且ComfyUI正在运行，加载iframe
            if (tabName === 'workspace' && currentStatus === 'running' && workspaceUrl) {
                loadComfyUIFrame();
            }
        }
        
        function loadComfyUIFrame() {
            const iframe = document.getElementById('comfyuiFrame');
            const message = document.getElementById('workspaceMessage');
            
            if (workspaceUrl) {
                iframe.src = workspaceUrl;
                iframe.style.display = 'block';
                message.style.display = 'none';
            }
        }
        
        function showMessage(message, type = 'info') {
            const messageArea = document.getElementById('messageArea');
            const icon = type === 'success' ? 'fa-check-circle' : type === 'error' ? 'fa-exclamation-circle' : type === 'warning' ? 'fa-exclamation-triangle' : 'fa-info-circle';
            messageArea.innerHTML = `<div class="message message-${type}"><i class="fas ${icon}"></i> ${message}</div>`;
            setTimeout(() => {
                messageArea.innerHTML = '';
            }, 8000);
        }
        
        function updateStatus(status, npuCard, extPort, wsPort, wsUrl, idlePercent, owner) {
            const statusCard = document.getElementById('statusCard');
            const statusContent = document.getElementById('statusContent');
            const npuCardElement = document.getElementById('npuCard');
            const externalPortElement = document.getElementById('externalPort');
            const workspacePortElement = document.getElementById('workspacePort');
            const workspaceUrlElement = document.getElementById('workspaceUrl');
            const workspaceUrlContainer = document.getElementById('workspaceUrlContainer');
            const startBtn = document.getElementById('startBtn');
            const stopBtn = document.getElementById('stopBtn');
            const openWorkspaceBtn = document.getElementById('openWorkspaceBtn');
            const workspaceTab = document.getElementById('workspaceTab');
            const idleAlert = document.getElementById('idleAlert');
            const idleProgress = document.getElementById('idleProgress');
            const occupiedWarning = document.getElementById('occupiedWarning');
            const currentUserSpan = document.getElementById('currentUser');
            
            currentStatus = status[4] || 'idle';  // status字段
            externalPort = extPort;
            workspacePort = wsPort;
            workspaceUrl = wsUrl;
            isOwner = owner;
            
            // 更新状态显示
            statusCard.className = `status-card status-${currentStatus}`;
            
            // 更新NPU卡显示
            if (npuCard) {
                npuCardElement.textContent = `NPU卡${npuCard}`;
            }
            
            // 更新端口显示
            if (extPort) {
                externalPortElement.textContent = extPort;
            }
            
            if (wsPort) {
                workspacePortElement.textContent = wsPort;
            }
            
            // 更新工作区URL显示
            if (wsUrl && currentStatus === 'running' && isOwner) {
                workspaceUrlElement.textContent = wsUrl;
                workspaceUrlContainer.style.display = 'block';
            } else {
                workspaceUrlContainer.style.display = 'none';
            }
            
            // 更新空闲警告（仅对拥有者显示）
            if (idlePercent > 0 && currentStatus === 'running' && isOwner) {
                idleAlert.style.display = 'block';
                idleProgress.style.width = `${idlePercent}%`;
                occupiedWarning.style.display = 'none';
            } else {
                idleAlert.style.display = 'none';
            }
            
            // 显示端口占用警告（对非拥有者显示）
            if (currentStatus !== 'idle' && !isOwner) {
                occupiedWarning.style.display = 'block';
                const currentUser = status[2] || '未知用户';  // current_username字段
                currentUserSpan.textContent = currentUser;
            } else {
                occupiedWarning.style.display = 'none';
            }
            
            switch (currentStatus) {
                case 'idle':
                    statusContent.innerHTML = '<strong><i class="fas fa-circle"></i> 状态:</strong> 空闲 - 准备启动';
                    startBtn.disabled = false;
                    stopBtn.disabled = true;
                    openWorkspaceBtn.disabled = true;
                    workspaceTab.style.display = 'none';
                    if (currentTab === 'workspace') {
                        switchTab('control');
                    }
                    break;
                case 'starting':
                    statusContent.innerHTML = '<div class="spinner"></div> <strong>状态:</strong> 启动中 - 正在生成安全令牌...';
                    startBtn.disabled = true;
                    stopBtn.disabled = !isOwner;
                    openWorkspaceBtn.disabled = true;
                    workspaceTab.style.display = 'none';
                    break;
                case 'running':
                    statusContent.innerHTML = '<strong><i class="fas fa-play-circle"></i> 状态:</strong> 运行中 - 令牌验证已激活';
                    startBtn.disabled = true;
                    stopBtn.disabled = !isOwner;
                    openWorkspaceBtn.disabled = !isOwner;
                    if (isOwner) {
                        workspaceTab.style.display = 'block';
                    } else {
                        workspaceTab.style.display = 'none';
                        if (currentTab === 'workspace') {
                            switchTab('control');
                        }
                    }
                    break;
                default:
                    statusContent.innerHTML = `<strong>状态:</strong> ${currentStatus}`;
                    break;
            }
            
            // 控制历史记录显示
            const historyContainer = document.getElementById('historyContainer');
            if (currentStatus === 'running' && isOwner) {
                historyContainer.style.display = 'block';
                if (document.getElementById('historyAutoRefresh').checked) {
                    startHistoryRefresh();
                }
            } else {
                historyContainer.style.display = 'none';
                stopHistoryRefresh();
            }
        }
        
        function loadStatus() {
            fetch('/api/user/status')
                .then(response => response.json())
                .then(data => {
                    if (data.status) {
                        updateStatus(data.status, data.npu_card, data.external_port, data.workspace_port, data.workspace_url, data.idle_percent, data.is_owner);
                    }
                })
                .catch(error => {
                    console.error('Error loading status:', error);
                    showMessage('获取状态失败', 'error');
                });
        }
        
        function loadHistory() {
            fetch('/api/user/history')
                .then(response => response.json())
                .then(data => {
                    const historyContent = document.getElementById('historyContent');
                    
                    if (data.success && data.data) {
                        if (Object.keys(data.data).length === 0) {
                            historyContent.innerHTML = '<div class="no-history"><i class="fas fa-info-circle"></i> 暂无历史记录</div>';
                        } else {
                            let historyHtml = '';
                            let count = 0;
                            
                            for (const [id, item] of Object.entries(data.data)) {
                                if (count >= 10) break; // 最多显示10条
                                
                                const timestamp = new Date(item.timestamp * 1000).toLocaleString();
                                const status = item.status?.status_str || '未知';
                                
                                historyHtml += `
                                    <div class="history-item">
                                        <h4><i class="fas fa-image"></i> 任务 ${id.substring(0, 8)}</h4>
                                        <div class="timestamp">
                                            <i class="fas fa-clock"></i> ${timestamp} - 状态: ${status}
                                        </div>
                                    </div>
                                `;
                                count++;
                            }
                            
                            historyContent.innerHTML = historyHtml || '<div class="no-history"><i class="fas fa-info-circle"></i> 暂无历史记录</div>';
                        }
                    } else {
                        historyContent.innerHTML = '<div class="no-history"><i class="fas fa-exclamation-triangle"></i> 获取历史记录失败</div>';
                    }
                })
                .catch(error => {
                    console.error('Error loading history:', error);
                    const historyContent = document.getElementById('historyContent');
                    historyContent.innerHTML = '<div class="no-history"><i class="fas fa-exclamation-triangle"></i> 获取历史记录失败</div>';
                });
        }
        
        function startHistoryRefresh() {
            if (historyRefreshInterval) {
                clearInterval(historyRefreshInterval);
            }
            historyRefreshInterval = setInterval(loadHistory, 3000); // 每3秒刷新一次
        }
        
        function stopHistoryRefresh() {
            if (historyRefreshInterval) {
                clearInterval(historyRefreshInterval);
                historyRefreshInterval = null;
            }
        }
        
        function startComfyUI() {
            showMessage('正在启动ComfyUI并生成安全令牌，请稍候...', 'info');
            
            fetch('/api/user/start', { method: 'POST' })
                .then(response => response.json())
                .then(data => {
                    if (data.error) {
                        showMessage(data.error, 'error');
                    } else {
                        showMessage(data.message, 'success');
                        externalPort = data.external_port;
                        loadStatus();
                    }
                })
                .catch(error => {
                    console.error('Error starting ComfyUI:', error);
                    showMessage('启动失败', 'error');
                });
        }
        
        function stopComfyUI() {
            if (confirm('确定要停止ComfyUI吗？这将重置访问令牌。')) {
                showMessage('正在停止ComfyUI并重置令牌...', 'info');
                
                fetch('/api/user/stop', { method: 'POST' })
                    .then(response => response.json())
                    .then(data => {
                        if (data.error) {
                            showMessage(data.error, 'error');
                        } else {
                            showMessage(data.message, 'success');
                            loadStatus();
                        }
                    })
                    .catch(error => {
                        console.error('Error stopping ComfyUI:', error);
                        showMessage('停止失败', 'error');
                    });
            }
        }
        
        function openWorkspace() {
            if (currentStatus === 'running' && isOwner && workspaceUrl) {
                // 在新标签页打开带令牌的安全工作区
                window.open(workspaceUrl, '_blank');
                showMessage('已在新标签页打开安全工作区', 'success');
            } else if (!isOwner) {
                showMessage('您不是当前端口的占用者，无法访问工作区', 'error');
            } else if (!workspaceUrl) {
                showMessage('工作区地址尚未生成，请等待启动完成', 'warning');
            } else {
                showMessage('ComfyUI未运行', 'error');
            }
        }
        
        // 历史记录自动刷新控制
        document.getElementById('historyAutoRefresh').addEventListener('change', function() {
            if (this.checked && currentStatus === 'running' && isOwner) {
                startHistoryRefresh();
            } else {
                stopHistoryRefresh();
            }
        });
        
        // WebSocket事件处理
        if (currentPort) {
            socket.emit('join_port', { port: currentPort });
        }
        
        socket.on('comfyui_ready', function(data) {
            if (data.port === currentPort) {
                const tokenMsg = data.token ? `令牌已生成: ${data.token.substring(0, 8)}...` : '';
                showMessage(`ComfyUI启动完成！当前使用者：${data.username} ${tokenMsg}`, 'success');
                externalPort = data.external_port;
                workspacePort = data.workspace_port;
                workspaceUrl = data.workspace_url;
                loadStatus();
            }
        });
        
        socket.on('comfyui_failed', function(data) {
            if (data.port === currentPort) {
                showMessage(`ComfyUI启动失败: ${data.error}`, 'error');
                loadStatus();
            }
        });
        
        socket.on('port_cleaned', function(data) {
            if (data.port === currentPort) {
                showMessage('工作环境已清理，访问令牌已重置', 'info');
                loadStatus();
            }
        });
        
        socket.on('port_status_changed', function(data) {
            if (data.port === currentPort) {
                loadStatus(); // 实时更新状态
            }
        });
        
        socket.on('idle_progress_update', function(data) {
            if (data.port === currentPort && isOwner) {
                const idleAlert = document.getElementById('idleAlert');
                const idleProgress = document.getElementById('idleProgress');
                
                if (data.percent > 0 && currentStatus === 'running') {
                    idleAlert.style.display = 'block';
                    idleProgress.style.width = `${data.percent}%`;
                } else {
                    idleAlert.style.display = 'none';
                }
            }
        });
        
        socket.on('user_timeout', function(data) {
            if (data.port === currentPort) {
                if (data.username === '{{ user[1] if user else "" }}') {
                    showMessage('由于长时间空闲，您已被自动下线，访问令牌已重置', 'warning');
                } else {
                    showMessage(`用户 ${data.username} 因空闲超时被自动下线，端口现已空闲`, 'info');
                }
                loadStatus();
            }
        });
        
        // 初始化
        if (currentPort) {
            loadStatus();
            // 每5秒刷新一次状态
            setInterval(loadStatus, 5000);
        }
        
        // 页面卸载时离开房间并清理定时器
        window.addEventListener('beforeunload', function() {
            if (currentPort) {
                socket.emit('leave_port', { port: currentPort });
            }
            stopHistoryRefresh();
        });
    </script>
    
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
</body>
</html>
'''

if __name__ == '__main__':
    print("启动AI绘画工具多用户管理系统 - 令牌管理版...")
    print(f"管理界面地址: http://127.0.0.1:{MANAGER_PORT}")
    print(f"配置的外部IP地址: {EXTERNAL_IP}")
    print("默认管理员账号: admin / admin123")
    print("\n=== 令牌管理功能 ===")
    print("✅ 自动生成32位随机令牌")
    print("✅ 动态更新Caddyfile配置")
    print("✅ 自动执行Caddy重载")
    print("✅ 停止时重置为默认令牌")
    print("✅ 安全工作区地址（含令牌参数）")
    print("\n=== 队列检查优化 ===")
    print("✅ 严格保证1秒间隔队列检查")
    print("✅ 缓存时间调整为0.5秒，确保1秒间隔内进行真实检查")
    print("✅ 数据库连接池优化 - 减少连接创建开销")
    print("✅ 线程池管理 - 限制并发线程数，提高稳定性")
    print("✅ 异步处理和错误重试 - 提高容错能力")
    print("✅ WebSocket事件批量推送优化 - 减少前端更新频率")
    print("✅ 多层进程清理机制 - 确保NPU资源完全释放")
    print("✅ 支持4个ComfyUI实例同时稳定运行")
    print(f"✅ 队列检查间隔：{QUEUE_CHECK_INTERVAL}秒（严格保证）")
    print(f"✅ 缓存持续时间：{CACHE_DURATION}秒（确保1秒内真实检查）")
    print(f"✅ 默认令牌：{DEFAULT_TOKEN}")
    print(f"✅ Caddyfile路径：{CADDYFILE_PATH}")
    print("\n重要提醒：")
    print("1. 请确保EXTERNAL_IP变量设置为正确的服务器外部IP地址！")
    print("2. 请确保Caddyfile存在且包含正确的令牌配置格式！")
    print("3. 请确保系统已安装Caddy并可执行caddy命令！")
    
    try:
        # 初始化所有端口的令牌为默认值
        for port in PORT_CONFIG.keys():
            set_port_token(port, DEFAULT_TOKEN)
        
        # 确保输出目录存在
        os.makedirs(OUTPUT_BASE_DIR, exist_ok=True)
        
        # 为每个端口创建输出目录
        for port in PORT_CONFIG.keys():
            port_dir = os.path.join(OUTPUT_BASE_DIR, f"port_{port}")
            os.makedirs(port_dir, exist_ok=True)
        
        logger.info("系统启动完成，开始监听请求...")
        socketio.run(app, host='0.0.0.0', port=MANAGER_PORT, debug=False, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        print("\n正在关闭系统...")
        # 清理所有进程并重置令牌
        for port in PORT_CONFIG.keys():
            comfyui_manager.stop_comfyui(port)
        print("系统已关闭，所有令牌已重置为默认值")

