#!/usr/bin/env bash
# 生成离线迁移包（服务器不能访问 GitHub 时使用）。
#
# 用法：
#   bash scripts/make_bundle.sh                # 包名带时间戳
#   bash scripts/make_bundle.sh v1.0.0         # 指定版本号
#
# 产物：dist/lchain-migration-<版本>.tar.gz（含源码 + 服务器配置模板 + 迁移文档 + 校验和）
# 说明：只打包 git 已提交的文件；.env / 日志 / 虚拟环境 / 视频渲染产物不会进包。
set -euo pipefail
cd "$(dirname "$0")/.."
TAG=${1:-$(date +%Y%m%d-%H%M)}
NAME="lchain-migration-$TAG"
OUT="dist/$NAME"
rm -rf "$OUT" "dist/$NAME.tar.gz"
mkdir -p "$OUT"

echo "== 导出源码（git archive HEAD）"
git archive --format=tar HEAD | tar -x -C "$OUT"
[ -f "$OUT/.env.server.example" ] || cp deploy/server/.env.server.example "$OUT/.env.server.example" 2>/dev/null || true
cp docs/migrate-to-server.md "$OUT/MIGRATION.md"

cat > "$OUT/QUICKSTART.txt" <<'EOF'
服务器迁移包 · 快速开始
========================
1. 解压：tar xzf lchain-migration-*.tar.gz && cd lchain-migration-*
2. 安装：bash scripts/install_server.sh
3. 配置：编辑 .env（必填 LLM_API_KEY）
4. 数据库：cd deploy/mysql && docker compose up -d
   （或把 .env 的 MYSQL_URL 改成服务器已有 MySQL）
5. 启动：deploy/server/run.sh start
6. 自检：PYTHONPATH=. .venv/bin/python scripts/smoke_check.py --llm
7. 打开：http://<服务器IP>:8000/wecom

完整说明见 MIGRATION.md
EOF

echo "== 打包"
tar -czf "dist/$NAME.tar.gz" -C dist "$NAME"
if command -v shasum >/dev/null 2>&1; then
  shasum -a 256 "dist/$NAME.tar.gz" | tee "dist/$NAME.tar.gz.sha256"
else
  sha256sum "dist/$NAME.tar.gz" | tee "dist/$NAME.tar.gz.sha256"
fi

echo
echo "完成：dist/$NAME.tar.gz"
echo "上传到服务器后："
echo "  tar xzf $NAME.tar.gz && cd $NAME && bash scripts/install_server.sh"
