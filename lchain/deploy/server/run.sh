#!/usr/bin/env bash
# 在服务器上启动/停止 LChain（web + scheduler），不需要 systemd / root。
# 适合「在线 VS Code + 后台服务器」这类环境：进程用 nohup 常驻，PID 记在 run/ 目录。
#
# 用法：
#   deploy/server/run.sh start|stop|restart|status|logs
#   APP_PORT=8000 deploy/server/run.sh start        # 覆盖端口
#   PY=/path/to/python deploy/server/run.sh start   # 覆盖解释器（默认 .venv/bin/python）
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT=$(pwd)
[ -f .env ] && APP_PORT=$(grep -E '^APP_PORT=' .env | cut -d= -f2) && LOG_DIR_ENV=$(grep -E '^LOG_DIR=' .env | cut -d= -f2) || true
APP_PORT=${APP_PORT:-8000}
PY=${PY:-$ROOT/.venv/bin/python}
RUN_DIR=${RUN_DIR:-$ROOT/run}
LOGS=${LOG_DIR:-${LOG_DIR_ENV:-$ROOT/logs}}
mkdir -p "$RUN_DIR" "$LOGS"

if [ ! -x "$PY" ]; then
  echo "❌ 找不到虚拟环境：$PY"
  echo "   先在项目根目录执行：bash scripts/install_server.sh"
  exit 1
fi

start_one() {  # name command...
  local name=$1; shift
  local pidfile=$RUN_DIR/$name.pid
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "$name 已在运行（pid $(cat "$pidfile")）"; return
  fi
  nohup "$@" > "$LOGS/$name.out" 2>&1 &
  echo $! > "$pidfile"
  echo "$name 已启动（pid $(cat "$pidfile")，日志 $LOGS/$name.out）"
}

stop_one() {
  local name=$1 pidfile=$RUN_DIR/$name.pid pid
  [ -f "$pidfile" ] || { echo "$name 未在运行"; return; }
  pid=$(cat "$pidfile")
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid"    # SIGTERM：web 会等待进行中的任务（SHUTDOWN_DRAIN_SECONDS）后退出
    for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" || true
  fi
  rm -f "$pidfile"
  echo "$name 已停止"
}

case "${1:-}" in
  start)
    start_one web "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port "$APP_PORT"
    start_one scheduler "$PY" -m app.scheduler
    ;;
  stop)
    stop_one web; stop_one scheduler
    ;;
  restart)
    "$0" stop; sleep 1; "$0" start
    ;;
  status)
    for n in web scheduler; do
      f=$RUN_DIR/$n.pid
      if [ -f "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null; then
        echo "$n: running（pid $(cat "$f")）"
      else
        echo "$n: stopped"
      fi
    done
    echo -n "健康检查："
    curl -fsS --max-time 5 "http://127.0.0.1:$APP_PORT/api/health" && echo || echo "（web 未响应）"
    ;;
  logs)
    tail -n 60 "$LOGS"/web.out "$LOGS"/scheduler.out 2>/dev/null || true
    ;;
  *)
    echo "用法：$0 start|stop|restart|status|logs"
    exit 1
    ;;
esac
