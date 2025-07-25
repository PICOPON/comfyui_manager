#!/bin/bash

yum install gcc python3-devel -y

# AI绘画工具多用户管理系统启动脚本
# 使用方法: ./start_manager.sh

set -e

# 配置变量
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMFYUI_PATH="/mnt/sdb1/ComfyUI"
MANAGER_SCRIPT="$SCRIPT_DIR/comfyui_manager.py"
REQUIREMENTS_FILE="$SCRIPT_DIR/requirements.txt"
VENV_PATH="$SCRIPT_DIR/venv"
LOG_FILE="$SCRIPT_DIR/manager.log"
PID_FILE="$SCRIPT_DIR/manager.pid"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 日志函数
log() {
    echo -e "${GREEN}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[$(date '+%Y-%m-%d %H:%M:%S')] WARNING:${NC} $1"
}

error() {
    echo -e "${RED}[$(date '+%Y-%m-%d %H:%M:%S')] ERROR:${NC} $1"
}

# 检查系统环境
check_environment() {
    log "检查系统环境..."
    
    # 检查Python版本
    if ! command -v python3 &> /dev/null; then
        error "Python3 未找到，请安装 Python 3.7 或更高版本"
        exit 1
    fi
    
    python_version=$(python3 --version | cut -d' ' -f2 | cut -d'.' -f1-2)
    log "Python版本: $python_version"
    
    # 检查ComfyUI路径
    if [ ! -d "$COMFYUI_PATH" ]; then
        error "ComfyUI路径不存在: $COMFYUI_PATH"
        exit 1
    fi
    
    if [ ! -f "$COMFYUI_PATH/main.py" ]; then
        error "ComfyUI主文件不存在: $COMFYUI_PATH/main.py"
        exit 1
    fi
    
    # 检查NPU环境
    if [ -z "$ASCEND_RT_VISIBLE_DEVICES" ]; then
        warn "ASCEND_RT_VISIBLE_DEVICES 环境变量未设置，将使用默认配置"
    fi
    
    log "环境检查完成"
}

# 创建虚拟环境
setup_virtualenv() {
    if [ ! -d "$VENV_PATH" ]; then
        log "创建Python虚拟环境..."
        python3 -m venv --clear "$VENV_PATH"
    fi
    
    log "激活虚拟环境..."
    source "$VENV_PATH/bin/activate"
    
    log "升级pip..."
    pip install --upgrade pip
    
    if [ -f "$REQUIREMENTS_FILE" ]; then
        log "安装依赖包..."
        pip install -r "$REQUIREMENTS_FILE"
    else
        log "安装基础依赖包..."
        pip install flask flask-socketio requests psutil
    fi
}

# 创建目录结构
create_directories() {
    log "创建必要目录..."
    
    # 创建实例目录
    mkdir -p "/mnt/sdb1/ComfyUI/instances"
    
    # 为每个端口创建独立的输出目录
    for port in 8001 8002 8003 8004; do
        mkdir -p "/mnt/sdb1/ComfyUI/instances/port_$port"
    done
    
    log "目录创建完成"
}

# 检查端口占用
check_ports() {
    log "检查端口占用状态..."
    
    ports=(8001 8002 8003 8004 8880)
    for port in "${ports[@]}"; do
        if lsof -Pi :$port -sTCP:LISTEN -t >/dev/null 2>&1; then
            warn "端口 $port 已被占用"
            if [ "$port" != "8880" ]; then
                # 工作端口被占用不是问题，管理系统会处理
                continue
            else
                # 管理端口被占用需要处理
                read -p "管理端口 8880 已被占用，是否终止占用进程? (y/n): " -n 1 -r
                echo
                if [[ $REPLY =~ ^[Yy]$ ]]; then
                    lsof -ti:8880 | xargs kill -9
                    log "已终止占用端口 8880 的进程"
                else
                    error "无法启动管理系统，端口 8880 被占用"
                    exit 1
                fi
            fi
        fi
    done
}

# 启动管理系统
start_manager() {
    log "启动AI绘画工具多用户管理系统..."
    
    # 检查是否已经在运行
    if [ -f "$PID_FILE" ]; then
        old_pid=$(cat "$PID_FILE")
        if kill -0 "$old_pid" 2>/dev/null; then
            warn "管理系统已在运行 (PID: $old_pid)"
            exit 0
        else
            rm -f "$PID_FILE"
        fi
    fi
    
    # 激活虚拟环境
    source "$VENV_PATH/bin/activate"
    
    # 启动管理系统
    nohup python3 "$MANAGER_SCRIPT" > "$LOG_FILE" 2>&1 &
    manager_pid=$!
    
    # 保存PID
    echo $manager_pid > "$PID_FILE"
    
    log "管理系统已启动 (PID: $manager_pid)"
    log "日志文件: $LOG_FILE"
    
    # 等待服务启动
    sleep 5
    
    # 检查服务状态
    if kill -0 "$manager_pid" 2>/dev/null; then
        log "管理系统启动成功!"
        log "访问地址: http://$(hostname -I | awk '{print $1}'):8880"
        log "默认管理员账号: admin / admin123"
    else
        error "管理系统启动失败，请检查日志文件: $LOG_FILE"
        exit 1
    fi
}

# 停止管理系统
stop_manager() {
    log "停止管理系统..."
    
    if [ -f "$PID_FILE" ]; then
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid"
            rm -f "$PID_FILE"
            log "管理系统已停止"
        else
            warn "管理系统未运行"
            rm -f "$PID_FILE"
        fi
    else
        warn "PID文件不存在，管理系统可能未运行"
    fi
    
    # 清理所有ComfyUI进程
    log "清理ComfyUI进程..."
    pkill -f "main.py.*--port" || true
    
    log "停止完成"
}

# 显示状态
show_status() {
    log "系统状态:"
    
    # 检查管理系统状态
    if [ -f "$PID_FILE" ]; then
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo -e "${GREEN}管理系统: 运行中 (PID: $pid)${NC}"
        else
            echo -e "${RED}管理系统: 已停止${NC}"
        fi
    else
        echo -e "${RED}管理系统: 未运行${NC}"
    fi
    
    # 检查工作端口状态
    echo -e "${BLUE}工作端口状态:${NC}"
    for port in 8001 8002 8003 8004; do
        if lsof -Pi :$port -sTCP:LISTEN -t >/dev/null 2>&1; then
            echo -e "  端口 $port: ${GREEN}运行中${NC}"
        else
            echo -e "  端口 $port: ${RED}空闲${NC}"
        fi
    done
    
    # 显示日志
    if [ -f "$LOG_FILE" ]; then
        echo -e "${BLUE}最近日志:${NC}"
        tail -n 10 "$LOG_FILE"
    fi
}

# 主函数
main() {
    case "${1:-start}" in
        start)
            check_environment
            setup_virtualenv
            create_directories
            check_ports
            start_manager
            ;;
        stop)
            stop_manager
            ;;
        restart)
            stop_manager
            sleep 2
            check_environment
            setup_virtualenv
            create_directories
            check_ports
            start_manager
            ;;
        status)
            show_status
            ;;
        *)
            echo "使用方法: $0 {start|stop|restart|status}"
            echo "  start   - 启动管理系统"
            echo "  stop    - 停止管理系统"
            echo "  restart - 重启管理系统"
            echo "  status  - 显示系统状态"
            exit 1
            ;;
    esac
}

# 信号处理
trap 'log "接收到中断信号，正在清理..."; stop_manager; exit 0' INT TERM

# 运行主函数
main "$@"
