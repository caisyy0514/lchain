#!/usr/bin/env bash
# 服务器端一次性安装：检查 Python → 建 .venv → 装依赖 → 生成 .env → 导入自检。
#
# 用法（在项目根目录）：
#   bash scripts/install_server.sh
#   PYTHON=python3.12 PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple bash scripts/install_server.sh
#
# 说明：
#   - 需要 Python >= 3.11（推荐 3.12）；系统自带 3.9/3.10 时先装 3.12 再用 PYTHON=python3.12 重跑
#   - 国内服务器建议加 PIP_INDEX_URL 走镜像；公司内网有私有源就填私有源
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}

echo "== 1/5 检查 Python"
"$PY" --version >/dev/null 2>&1 || { echo "❌ 找不到 $PY，请安装 Python 3.11+"; exit 1; }
OK=$("$PY" -c 'import sys; print(sys.version_info[:2] >= (3, 11))' 2>/dev/null || echo False)
if [ "$OK" != "True" ]; then
  echo "❌ Python 版本过低：$("$PY" --version 2>&1)"
  echo "   安装 3.12 后重跑：PYTHON=python3.12 bash scripts/install_server.sh"
  exit 1
fi
echo "   $("$PY" --version) ✓"

echo "== 2/5 创建虚拟环境 .venv"
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv
fi
.venv/bin/python -m pip install -U pip >/dev/null

echo "== 3/5 安装依赖"
if [ -n "${PIP_INDEX_URL:-}" ]; then
  echo "   使用镜像：$PIP_INDEX_URL"
  .venv/bin/pip install -r requirements.txt -i "$PIP_INDEX_URL"
else
  .venv/bin/pip install -r requirements.txt
fi

echo "== 4/5 生成配置"
if [ ! -f .env ]; then
  cp deploy/server/.env.server.example .env
  echo "   已生成 .env —— 请填写 LLM_API_KEY；MySQL 默认指向 127.0.0.1:13306（可用 deploy/mysql 启动）"
else
  echo "   .env 已存在，保持不变"
fi

echo "== 5/5 导入自检"
PYTHONPATH=. .venv/bin/python -c "import app.main; print('   import app.main OK ✓')"

cat <<'EOF'

安装完成。接下来：

  1) 编辑 .env（必填 LLM_API_KEY；其余按需）
  2) 启动数据库（二选一）：
       cd deploy/mysql && docker compose up -d          # 自带 MySQL，127.0.0.1:13306
       # 或把 .env 的 MYSQL_URL 改成服务器已有 MySQL
  3) 可选：初始化 Langfuse 提示词（不装 Langfuse 也能跑）
       make init-prompts
  4) 启动服务：
       deploy/server/run.sh start
  5) 自检：
       PYTHONPATH=. .venv/bin/python scripts/smoke_check.py --llm
  6) 浏览器打开：http://<服务器IP>:8000/wecom

EOF
