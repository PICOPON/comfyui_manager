# ComfyUI Multi-User Manager

[![Python 3.7+](https://img.shields.io/badge/python-3.7+-blue.svg)](https://www.python.org/downloads/)
[![Flask](https://img.shields.io/badge/Flask-2.3.3-green.svg)](https://flask.palletsprojects.com/)
[![NPU](https://img.shields.io/badge/Device-NPU-green.svg)](https://www.hiascend.com/)

🎨 **基于ComfyUI的高性能多用户资源管理系统**

一个专为AI绘画工具ComfyUI设计的企业级多用户管理平台，支持NPU资源调度、安全令牌访问、实时监控和自动化运维。

## ✨ 核心特性

### 🔐 安全管理
- **动态令牌验证**: 32位随机令牌自动生成，确保访问安全
- **Caddy集成**: 自动更新代理配置，实现令牌验证
- **多用户隔离**: 独立工作环境，数据安全隔离
- **会话管理**: 自动会话超时和资源释放

### 🚀 性能优化
- **数据库连接池**: 高效的SQLite连接池管理
- **队列检查缓存**: 0.5秒缓存机制，减少网络开销
- **线程池管理**: 限制并发数，提升系统稳定性
- **异步处理**: 非阻塞任务处理和错误重试机制

### 📊 实时监控
- **WebSocket推送**: 实时状态更新和进度监控
- **空闲检测**: 自动识别空闲用户并释放资源
- **进程监控**: 多层进程清理，确保NPU资源完全释放
- **系统负载**: 自适应调节和性能监控

### 🎯 资源调度
- **NPU卡管理**: 智能分配和调度NPU计算资源
- **端口复用**: 多用户共享端口机制
- **队列管理**: 1秒间隔队列检查，精确控制
- **自动清理**: 进程异常检测和自动恢复

## 📋 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                    Web管理界面 (8880)                        │
├─────────────────────────────────────────────────────────────┤
│                     Flask + SocketIO                        │
├─────────────────────────────────────────────────────────────┤
│  ComfyUI实例管理器  │  用户认证  │  NPU资源调度  │  令牌管理   │
├─────────────────────────────────────────────────────────────┤
│                      Caddy反向代理                          │
│   8001→9001(9081)  │  8002→9002(9082)  │  8003→9003(9083)  │
├─────────────────────────────────────────────────────────────┤
│            ComfyUI实例 (8001-8004) + NPU卡 (1-4)           │
└─────────────────────────────────────────────────────────────┘
```

### 端口映射表

| ComfyUI端口 | 外部访问端口 | 工作区端口 | NPU卡 |
|-------------|-------------|------------|-------|
| 8001        | 9001        | 9081       | 卡1   |
| 8002        | 9002        | 9082       | 卡2   |
| 8003        | 9003        | 9083       | 卡3   |
| 8004        | 9004        | 9084       | 卡4   |

## 🛠️ 系统要求

### 硬件要求
- **NPU**: 昇腾NPU卡 (推荐4卡配置)
- **内存**: 16GB+ RAM
- **存储**: 100GB+ 可用空间
- **网络**: 千兆网络

### 软件要求
- **操作系统**: Linux (CentOS 7+/Ubuntu 18.04+)
- **Python**: 3.7+
- **ComfyUI**: 最新版本
- **Caddy**: 2.0+
- **NPU驱动**: 昇腾CANN工具包

## 📦 安装部署

### 1. 环境准备

```bash
# 克隆项目
git clone https://github.com/yourusername/comfyui-multi-user-manager.git
cd comfyui-multi-user-manager

# 安装系统依赖
sudo yum install gcc python3-devel -y  # CentOS
# sudo apt install gcc python3-dev -y    # Ubuntu

# 安装Caddy
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy
```

### 2. 配置ComfyUI

```bash
# 确保ComfyUI已安装在 /mnt/sdb1/ComfyUI
# 如果路径不同，请修改 comfyui_manager.py 中的 COMFYUI_PATH 变量
ls -la /mnt/sdb1/ComfyUI/main.py  # 验证ComfyUI安装
```

### 3. 配置系统

```bash
# 修改配置文件中的外部IP地址
vim comfyui_manager.py
# 将 EXTERNAL_IP = "XXX.XX.XX.XXX" 改为您的实际服务器IP

# 确保NPU环境变量已设置
export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3
```

### 4. 启动系统

```bash
# 使用一键启动脚本
chmod +x start_manager.sh
./start_manager.sh start

# 或手动启动
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 comfyui_manager.py
```

### 5. 启动Caddy代理

```bash
# 在项目目录下启动Caddy
caddy start --config Caddyfile
```

## 🎯 使用指南

### 管理员操作

1. **访问管理界面**
   ```
   http://your-server-ip:8880
   默认账号: admin / admin123
   ```

2. **创建用户**
   - 登录管理界面
   - 点击"创建新用户"
   - 填写用户名、密码和分配端口
   - 系统自动配置NPU资源

3. **监控系统状态**
   - 实时查看端口状态和用户活动
   - 监控NPU卡使用情况
   - 查看空闲检测进度
   - 管理用户权限

### 用户操作

1. **登录系统**
   ```
   http://your-server-ip:8880
   使用管理员分配的账号密码
   ```

2. **启动ComfyUI**
   - 点击"启动ComfyUI"按钮
   - 等待系统分配资源和生成安全令牌
   - 系统自动更新Caddy配置

3. **安全访问工作区**
   - 启动完成后点击"打开安全工作区"
   - 系统自动跳转到含令牌的安全URL
   - 享受完整的ComfyUI功能

## ⚙️ 配置说明

### 核心配置参数

```python
# comfyui_manager.py 主要配置
EXTERNAL_IP = "YOUR_SERVER_IP"          # 服务器外部IP
COMFYUI_PATH = "/mnt/sdb1/ComfyUI"      # ComfyUI安装路径
MANAGER_PORT = 8880                     # 管理界面端口
IDLE_TIMEOUT = 600                      # 空闲超时时间(秒)
QUEUE_CHECK_INTERVAL = 1                # 队列检查间隔(秒)
MAX_WORKERS = 8                         # 最大工作线程数
```

### Caddyfile配置

系统自动管理Caddyfile中的令牌配置，格式如下：

```caddyfile
:8881 {
    @hasToken query token_8001=123456789  # 默认令牌
    # 启动后自动更新为: token_8001=a8F3xY9mD2pQ7wn5rT1u94OP4sf2fG8G
    
    handle @hasToken {
        reverse_proxy 127.0.0.1:8001
    }
    
    handle {
        respond "Access denied." 403
    }
}
```

### 数据库Schema

```sql
-- 用户表
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin BOOLEAN DEFAULT FALSE,
    assigned_port INTEGER,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 端口状态表
CREATE TABLE port_status (
    port INTEGER PRIMARY KEY,
    current_user_id INTEGER,
    current_username TEXT,
    process_id INTEGER,
    status TEXT DEFAULT 'idle',
    last_activity TIMESTAMP,
    last_queue_check TIMESTAMP,
    idle_count INTEGER DEFAULT 0
);
```

## 🔧 系统管理

### 启动停止管理

```bash
# 启动系统
./start_manager.sh start

# 停止系统
./start_manager.sh stop

# 重启系统
./start_manager.sh restart

# 查看状态
./start_manager.sh status
```

### 日志监控

```bash
# 查看系统日志
tail -f manager.log

# 查看特定端口日志
grep "端口 8001" manager.log

# 监控进程状态
ps aux | grep main.py
```

### 数据库管理

```bash
# 备份数据库
cp comfyui_manager.db comfyui_manager.db.backup

# 查看用户信息
sqlite3 comfyui_manager.db "SELECT * FROM users;"

# 重置管理员密码
sqlite3 comfyui_manager.db "UPDATE users SET password_hash='admin123_hash' WHERE username='admin';"
```

## 🚨 故障排除

### 常见问题

1. **端口被占用**
   ```bash
   # 查看端口占用
   lsof -i :8001
   
   # 终止占用进程
   kill -9 <PID>
   ```

2. **ComfyUI启动失败**
   ```bash
   # 检查NPU环境
   echo $ASCEND_RT_VISIBLE_DEVICES
   
   # 验证ComfyUI路径
   ls -la /mnt/sdb1/ComfyUI/main.py
   
   # 查看详细错误
   tail -100 manager.log
   ```

3. **Caddy配置更新失败**
   ```bash
   # 手动测试Caddy配置
   caddy validate --config Caddyfile
   
   # 重载配置
   caddy reload --config Caddyfile
   ```

4. **令牌验证失败**
   ```bash
   # 检查Caddyfile令牌配置
   grep "token_" Caddyfile
   
   # 手动重置令牌
   # 在管理界面重启对应端口
   ```

### 性能调优

1. **数据库优化**
   ```bash
   # 清理数据库
   sqlite3 comfyui_manager.db "VACUUM;"
   
   # 分析表结构
   sqlite3 comfyui_manager.db ".schema"
   ```

2. **内存监控**
   ```bash
   # 监控内存使用
   free -h
   
   # 查看进程内存
   ps aux --sort=-%mem | head
   ```

3. **NPU资源监控**
   ```bash
   # 查看NPU状态
   npu-smi info
   
   # 监控NPU使用率
   watch -n 1 npu-smi info
   ```

## 🔐 安全最佳实践

### 1. 访问控制
- 定期更换管理员密码
- 限制管理界面访问IP
- 启用HTTPS（生产环境）

### 2. 数据保护
- 定期备份数据库
- 设置文件权限（600）
- 启用日志轮转

### 3. 网络安全
- 配置防火墙规则
- 使用VPN访问管理界面
- 监控异常访问

## 📈 性能监控

### 关键指标

- **CPU使用率**: < 80%
- **内存使用率**: < 85%
- **NPU利用率**: 监控各卡使用情况
- **响应时间**: 队列检查 < 1秒
- **并发用户**: 支持4个同时在线

### 监控命令

```bash
# 系统资源监控
htop

# 网络连接监控
netstat -an | grep :800

# 磁盘空间监控
df -h

# 进程树查看
pstree -p | grep python
```

## 🤝 贡献指南

我们欢迎社区贡献！请遵循以下步骤：

1. **Fork项目**
2. **创建特性分支** (`git checkout -b feature/AmazingFeature`)
3. **提交更改** (`git commit -m 'Add some AmazingFeature'`)
4. **推送到分支** (`git push origin feature/AmazingFeature`)
5. **开启Pull Request**

### 开发规范

- 遵循PEP 8代码规范
- 添加适当的注释和文档
- 编写单元测试
- 更新README文档

## 📄 许可证

Code by Claude pro

## 🙏 致谢

- [ComfyUI](https://github.com/comfyanonymous/ComfyUI) - 强大的AI绘画工具
- [Flask](https://flask.palletsprojects.com/) - 轻量级Web框架
- [Caddy](https://caddyserver.com/) - 现代化Web服务器
- [昇腾AI](https://www.hiascend.com/) - NPU计算平台

## 📞 联系我们

- **Issues**: [GitHub Issues](https://github.com/yourusername/comfyui-multi-user-manager/issues)
- **讨论**: [GitHub Discussions](https://github.com/yourusername/comfyui-multi-user-manager/discussions)
- **邮箱**: 1322363294@qq.com

---

⭐ 如果这个项目对您有帮助，请给我们一个Star！

🐛 发现问题？欢迎提交Issue！

🚀 想要新功能？欢迎提交Feature Request！
