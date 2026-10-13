#!/usr/bin/env bash
# 首次初始化本地 Langfuse：生成随机密钥 -> 启动容器 -> 把 API Key 写入项目 .env
# 已存在 deploy/langfuse/.env 时不会重新生成（避免数据卷里的密码和配置对不上）。
set -euo pipefail
cd "$(dirname "$0")/.."
LF_DIR=deploy/langfuse
LF_ENV=$LF_DIR/.env

rand() { openssl rand -hex "${1:-16}"; }

if [[ ! -f $LF_ENV ]]; then
  echo "» 生成 ${LF_ENV}（随机密码 / 密钥）"
  PG=$(rand 12); MI=$(rand 12)
  sed -e "s#^NEXTAUTH_SECRET=.*#NEXTAUTH_SECRET=$(rand 32)#" \
      -e "s#^SALT=.*#SALT=$(rand 32)#" \
      -e "s#^ENCRYPTION_KEY=.*#ENCRYPTION_KEY=$(rand 32)#" \
      -e "s#^POSTGRES_PASSWORD=.*#POSTGRES_PASSWORD=$PG#" \
      -e "s#^DATABASE_URL=.*#DATABASE_URL=postgresql://postgres:$PG@postgres:5432/postgres#" \
      -e "s#^CLICKHOUSE_PASSWORD=.*#CLICKHOUSE_PASSWORD=$(rand 12)#" \
      -e "s#^REDIS_AUTH=.*#REDIS_AUTH=$(rand 12)#" \
      -e "s#^MINIO_ROOT_PASSWORD=.*#MINIO_ROOT_PASSWORD=$MI#" \
      -e "s#^\(LANGFUSE_S3_[A-Z_]*_SECRET_ACCESS_KEY\)=.*#\1=$MI#" \
      -e "s#^LANGFUSE_INIT_PROJECT_PUBLIC_KEY=.*#LANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-lf-$(rand 16)#" \
      -e "s#^LANGFUSE_INIT_PROJECT_SECRET_KEY=.*#LANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-$(rand 16)#" \
      -e "s#^LANGFUSE_INIT_USER_PASSWORD=.*#LANGFUSE_INIT_USER_PASSWORD=$(openssl rand -base64 12 | tr -d '/+=' | cut -c1-14)#" \
      -e "s#^\# ===== .*#\# ===== Langfuse 自托管（本机开发）。由 scripts/langfuse_init.sh 随机生成，勿提交 =====#" \
      "$LF_DIR/.env.example" > "$LF_ENV"
  chmod 600 "$LF_ENV"
else
  echo "» 已存在 ${LF_ENV}，沿用原有密钥"
fi

echo "» 启动 Langfuse 容器"
(cd "$LF_DIR" && docker compose up -d)

echo -n "» 等待 Langfuse 就绪"
for _ in $(seq 1 120); do
  curl -sf --noproxy '*' http://localhost:3000/api/public/health >/dev/null && break
  echo -n "."; sleep 3
done
echo

get() { grep "^$1=" "$LF_ENV" | cut -d= -f2-; }
PK=$(get LANGFUSE_INIT_PROJECT_PUBLIC_KEY); SK=$(get LANGFUSE_INIT_PROJECT_SECRET_KEY)

[[ -f .env ]] || cp .env.example .env
sed -i.bak -e "s#^LANGFUSE_PUBLIC_KEY=.*#LANGFUSE_PUBLIC_KEY=$PK#" \
           -e "s#^LANGFUSE_SECRET_KEY=.*#LANGFUSE_SECRET_KEY=$SK#" \
           -e "s#^LANGFUSE_HOST=.*#LANGFUSE_HOST=http://localhost:3000#" .env && rm -f .env.bak
echo "» 已把 API Key 写入 .env"

cat <<EOF

  Langfuse:  http://localhost:3000
  账号:      $(get LANGFUSE_INIT_USER_EMAIL)
  密码:      $(get LANGFUSE_INIT_USER_PASSWORD)

下一步: make init-prompts && make dev
EOF
