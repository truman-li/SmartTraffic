#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"

# 默认配置
ENV_NAME="${TRAF_ENV_NAME:-traffic}"
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8000}"

PIDS=()
BACKEND_PID=""

RUN_TAG=""
RUN_LOG_DIR=""
BACKEND_LOG_FILE=""
BACKEND_RUNTIME_LOG_DIR=""
GRAPHRAG_REPORT_LOG_DIR=""

# 颜色与级别定义
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

log_info() { echo -e "${CYAN}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

usage() {
	cat <<'EOF'
用法:
	./start_service.sh [可选参数]

可选参数:
	--port PORT      后端端口 (默认: 8000)
	--reset          重置系统数据 (清空日志/缓存/上传文件等)
	-h, --help       显示帮助

示例:
	./start_service.sh
	./start_service.sh --port 9000
	./start_service.sh --reset
EOF
}

command_exists() {
	command -v "$1" >/dev/null 2>&1
}

bootstrap_env() {
	# 加载 .env 并导出变量
	if [[ -f "$PROJECT_ROOT/.env" ]]; then
		while IFS= read -r line || [[ -n "$line" ]]; do
			[[ "$line" =~ ^#.*$ ]] && continue
			[[ -z "$line" ]] && continue
			export "$line"
		done < "$PROJECT_ROOT/.env"
	fi
	
	# 统一 API_KEY
	local base="${API_KEY:-${DASHSCOPE_API_KEY:-${GRAPHRAG_API_KEY:-}}}"
	if [[ -n "$base" ]]; then
		export API_KEY="$base"
		export DASHSCOPE_API_KEY="$base"
		export GRAPHRAG_API_KEY="$base"
	fi
}

activate_conda() {
	if command_exists conda; then
		eval "$(conda shell.bash hook)"
		conda activate "$ENV_NAME" || log_warn "激活环境 $ENV_NAME 失败，尝试在该环境下运行..."
	else
		log_warn "未找到 conda，将使用当前环境。"
	fi
}

prepare_log_dir() {
	local base_dir="$PROJECT_ROOT/logs"
	local tag="$(date +"%Y%m%d_%H%M%S")"
	RUN_TAG="$tag"
	RUN_LOG_DIR="$base_dir/$tag"
	
	mkdir -p "$RUN_LOG_DIR/backend_runtime" "$RUN_LOG_DIR/graphrag"
	BACKEND_LOG_FILE="$RUN_LOG_DIR/backend.log"
	BACKEND_RUNTIME_LOG_DIR="$RUN_LOG_DIR/backend_runtime"
	GRAPHRAG_REPORT_LOG_DIR="$RUN_LOG_DIR/graphrag"
}

http_ready() {
	curl -fsS --max-time 2 "$1" >/dev/null 2>&1
}

wait_service_ready() {
	local url="$2"
	local pid="$3"
	local timeout_seconds="$4"
	local elapsed=0
	while [[ $elapsed -lt $timeout_seconds ]]; do
		if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then return 1; fi
		if http_ready "$url"; then return 0; fi
		sleep 1
		elapsed=$((elapsed + 1))
	done
	return 1
}

cleanup() {
	local exit_code=$?
	trap - INT TERM EXIT
	if [[ ${#PIDS[@]} -gt 0 ]]; then
		echo
		log_info "服务停止中..."
		for pid in "${PIDS[@]}"; do
			[[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
		done
	fi
	exit "$exit_code"
}

reset_system() {
	if http_ready "http://${BACKEND_HOST}:${BACKEND_PORT}/api/health"; then
		log_error "服务运行中，请先停止服务后再执行 --reset。"
		exit 1
	fi
	log_info "正在清理系统全部数据..."
	local paths=( "logs" "Module-1/vehicle_imgs" "Module-1/vehicles" "Module-1/module1.sqlite3" "Module-2/reports" "Module-2/videos" "Module-3/cache" "Module-3/input" "Module-3/input_sources" "Module-3/output" "Module-3/update_output" "Module-4/cache" "Module-4/input" "WebUI/backend/agent_history.sqlite3" )
	for p in "${paths[@]}"; do
		[[ -e "$PROJECT_ROOT/$p" ]] && rm -rf "$PROJECT_ROOT/$p"
	done

	# 清理项目中的 Python 字节码缓存目录
	find "$PROJECT_ROOT" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true

	log_success "重置完成。"
}

start_backend() {
	(
		cd "$PROJECT_ROOT/WebUI/backend"
		activate_conda
		export TRAFFIC_RUNTIME_LOG_DIR="$BACKEND_RUNTIME_LOG_DIR"
		export TRAFFIC_GRAPHRAG_REPORT_DIR="$GRAPHRAG_REPORT_LOG_DIR"
		export TRAFFIC_RUN_TAG="$RUN_TAG"
		python -m uvicorn app.main:app --host "$BACKEND_HOST" --port "$BACKEND_PORT" --reload
	) >"$BACKEND_LOG_FILE" 2>&1 &
	BACKEND_PID="$!"
	PIDS+=("$BACKEND_PID")
}

# --- Main Logic ---
RESET_FLAG=0
while [[ $# -gt 0 ]]; do
	case "$1" in
		--port) BACKEND_PORT="$2"; shift 2 ;;
		--reset) RESET_FLAG=1; shift ;;
		-h|--help) usage; exit 0 ;;
		*) log_error "未知参数: $1。"; exit 1 ;;
	esac
done

bootstrap_env

if [[ $RESET_FLAG -eq 1 ]]; then
	reset_system
	exit 0
fi

trap cleanup INT TERM EXIT
prepare_log_dir
log_info "服务启动中..."
start_backend

if ! wait_service_ready "Python 后端" "http://${BACKEND_HOST}:${BACKEND_PORT}/api/health" "$BACKEND_PID" 120; then
	log_error "启动失败，请检查日志：$BACKEND_LOG_FILE。"
	exit 1
fi

log_success "启动成功。"
echo
log_info "地址：http://${BACKEND_HOST}:${BACKEND_PORT}/frontend/index.html"
log_info "日志：logs/${RUN_TAG}。"
echo

wait "$BACKEND_PID"
