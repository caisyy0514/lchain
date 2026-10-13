# 迁移到服务器（在线 VS Code / 公司服务器）

本文档把本机开发环境里的 LChain Agent（含**家庭医生 Agent**）迁到一台服务器上运行。
服务器只要能开终端、能联网（或能接收离线包）即可；不要求 root、不要求 systemd。

- 代码仓库：`git@github.com:caisyy0514/lchain.git`（已包含全部改动，`.env` 不在仓库里）
- 推荐路径：服务器上 `git clone` → `bash scripts/install_server.sh` → 填 `.env` → 起 MySQL → `deploy/server/run.sh start`
- 服务器不能访问 GitHub 时：用 `bash scripts/make_bundle.sh` 生成离线包，上传解压即可（步骤见第 2 节）

---

## 0. 迁移清单（哪些要迁、哪些不用迁）

| 内容 | 是否迁移 | 说明 |
| --- | --- | --- |
| 代码（含 `app/wecom/fd`、`prompts/`、`tests/`、`scripts/`） | ✅ | 已在 git 仓库里，`git clone` 即可 |
| `.env` 配置（密钥） | ✅ 手动 | 被 `.gitignore` 忽略；用 `deploy/server/.env.server.example` 重建，或在服务器上重新填 |
| MySQL 数据 | ❌ | 服务器上是新库，启动时自动建表；样例群可用 `scripts/seed_fd_samples.py` 重新生成 |
| Langfuse 的提示词 / Trace | ❌ | 可重新 `make init-prompts`；不装 Langfuse 也能跑（程序回退到内置提示词） |
| `logs/`、`data/uploads/`、`deploy/langfuse/.env` | ❌ | 本机运行产物，服务器各自生成 |
| 视频工程（`promo-video*/`） | 可选 | 代码已入库；服务器上想生成视频需要额外装 ffmpeg + `promo-video` 的 venv |

**服务器最低要求**

| 项 | 要求 |
| --- | --- |
| 系统 | Linux（x86_64 / arm64 均可） |
| Python | **≥ 3.11**（推荐 3.12） |
| 内存 | ≥ 2 GB（不跑 Langfuse 时）；跑本地 Langfuse 建议 ≥ 8 GB |
| 磁盘 | ≥ 5 GB（不跑 Langfuse） |
| 网络 | 出网到 LLM 服务（DeepSeek 等）；企微网关能访问本服务的 HTTP 端口 |
| Docker | 可选。用于跑 MySQL / Langfuse；没有 Docker 就用服务器已有 MySQL |

---

## 1. 方式 A：从 git 拉取（推荐）

在服务器（在线 VS Code 的终端）里执行：

```bash
# 1) 拉代码（服务器访问 GitHub 慢/不通时先配代理，或改用第 2 节离线包）
git clone git@github.com:caisyy0514/lchain.git
# 没有配置 SSH key 的话改用 HTTPS：
# git clone https://github.com/caisyy0514/lchain.git

cd lchain

# 2) 一键安装（建 .venv + 装依赖 + 生成 .env）
bash scripts/install_server.sh
# 国内服务器建议走镜像：
# PYTHON=python3.12 PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple bash scripts/install_server.sh

# 3) 填配置（见第 3 节）
vi .env

# 4) 起数据库（见第 4 节）
cd deploy/mysql && docker compose up -d && cd ../..

# 5) 启动（web + 定时任务）
deploy/server/run.sh start

# 6) 自检
PYTHONPATH=. .venv/bin/python scripts/smoke_check.py --llm
```

## 2. 方式 B：离线迁移包（服务器访问不了 GitHub）

在**本机**执行：

```bash
cd "/Users/caisy/Documents/LC IDE/lchain"
bash scripts/make_bundle.sh v1.0.0        # 产物：dist/lchain-migration-v1.0.0.tar.gz
```

把 tar.gz 上传到服务器（在线 VS Code 直接拖拽上传 / `scp` / 公司网盘），然后在服务器上：

```bash
tar xzf lchain-migration-v1.0.0.tar.gz
cd lchain-migration-v1.0.0
cat QUICKSTART.txt          # 快速开始
bash scripts/install_server.sh
```

> 包内只有源码 + 配置模板 + 本迁移文档；`.env`、日志、虚拟环境、视频产物都不会进包。

---

## 3. 配置 `.env`（必读）

`install_server.sh` 会在没有 `.env` 时生成一份 `deploy/server/.env.server.example` 的副本。必须改的：

| 变量 | 说明 | 从哪里拿 |
| --- | --- | --- |
| `LLM_API_KEY` | **必填**，DeepSeek/OpenAI 兼容服务的 Key | 本地 `.env` 第 29 行，或向平台申请 |
| `LLM_BASE_URL` / `LLM_MODEL` | 默认 DeepSeek `https://api.deepseek.com/v1` / `deepseek-flash` | 一般不用改 |
| `MYSQL_URL` | 默认 `mysql+aiomysql://lchain:lchain@127.0.0.1:13306/lchain` | 用自带 Docker MySQL 不用改；用已有库改成实际地址 |
| `APP_HOST=0.0.0.0` | 服务器对外提供服务必须 0.0.0.0 | 模板已设好 |
| `NODE_ID` | 同一套 MySQL 内唯一，重启保持不变 | 例如 `site1-web-1` |
| `WECOM_OUTBOUND_WEBHOOK` | 企微发送端地址；空 = 只落库（测试页可用） | 对接方提供 |
| `WECOM_FD_RELEASE_STAGE` | 家庭医生放行状态 | 按机构审批：`DEMO → SHADOW → CLINICIAN_ASSISTED → PRODUCTION_APPROVED` |
| `LANGFUSE_*` | 可选 | 用本地/中心 Langfuse；留空也能跑 |

密钥安全：

- `.env` 只在服务器上保存，权限设 `chmod 600 .env`，**不要提交、不要发微信群**
- 生产建议用公司密钥管理（K8s Secret / 云 KMS / 凭据服务）注入环境变量
- `WECOM_FAULT_INJECT`、`WECOM_CRASH_AT` 生产必须为空（测试开关）

---

## 4. 数据库

**方案 1：用仓库自带的 Docker MySQL（简单）**

```bash
cd deploy/mysql && docker compose up -d
docker ps | grep lchain-mysql          # 看到 healthy 即可
```

**方案 2：用服务器已有 MySQL**

```sql
CREATE DATABASE lchain CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER 'lchain'@'%' IDENTIFIED BY '强密码';
GRANT ALL PRIVILEGES ON lchain.* TO 'lchain'@'%';
FLUSH PRIVILEGES;
```

然后把 `.env` 的 `MYSQL_URL` 改成 `mysql+aiomysql://lchain:强密码@数据库地址:3306/lchain?charset=utf8mb4`。
业务表和 checkpoint 表在服务启动时自动创建，无需手工建表。

## 5. Langfuse（可选）

| 场景 | 做法 |
| --- | --- |
| 不装 | `.env` 的 `LANGFUSE_*` 留空：程序用代码内置提示词，功能完整，只是没有提示词管理/Trace |
| 服务器本地装 | `make langfuse-init`（需要 Docker，会起 6 个容器，占内存较大） |
| 用中心 Langfuse | 在中心建项目，把 `LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST` 填到服务器的 `.env`，然后 `make init-prompts` 一次 |

初始化提示词（本地或中心 Langfuse 均可）：

```bash
make init-prompts        # 已存在的提示词会跳过；--force 会创建新版本
```

## 6. 启动与常驻

**方式 1：`run.sh`（无需 root，适合在线 VS Code 容器）**

```bash
deploy/server/run.sh start      # 启动 web + scheduler（nohup 常驻，PID 在 run/）
deploy/server/run.sh status     # 看状态 + /api/health
deploy/server/run.sh logs       # 看最近日志
deploy/server/run.sh restart
deploy/server/run.sh stop
```

> 在线 VS Code 的容器如果会随会话回收，建议放进 `tmux`：`tmux new -s lchain` 里再执行 `run.sh start`。

**方式 2：systemd（有 root 的正式服务器）**

```bash
sudo useradd -m lchain
sudo mkdir -p /opt/lchain && sudo chown lchain:lchain /opt/lchain
# 把项目放到 /opt/lchain（git clone 或解压），按上面步骤装好依赖、配好 .env
sudo cp deploy/server/lchain-web.service deploy/server/lchain-scheduler.service /etc/systemd/system/
sudo sed -i 's#/opt/lchain#/opt/lchain#' /etc/systemd/system/lchain-*.service   # 路径不同时自行修改
sudo systemctl daemon-reload
sudo systemctl enable --now lchain-web lchain-scheduler
sudo systemctl status lchain-web lchain-scheduler
```

> 定时任务进程**只部署一个**；部署多个也只有 1 个执行（MySQL 单例锁），其余作为热备。

## 7. 验证

```bash
PYTHONPATH=. .venv/bin/python scripts/smoke_check.py --llm
```

期望输出：

```
✅ web /api/health：200（Langfuse：未配置…）
✅ 家庭医生：放行=DEMO 红旗规则=28 条 低风险路径=3 条 急救电话=120
✅ MySQL：已连接（当前 0 个群）
✅ 页面 /wecom：200    ✅ 页面 /docs：200
✅ LLM（deepseek-flash）：正常
全部通过 ✅
```

再打开 `http://<服务器IP>:8000/wecom`，用「患者」身份发一句「昨晚开始胸痛，出冷汗」，应看到家庭医生回复「现在请拨打 120，不要等到明天」+ 计划卡。
也可以一键生成样例群（覆盖 E0–E5）：

```bash
PYTHONPATH=. .venv/bin/python scripts/seed_fd_samples.py --clean
```

## 8. 对外接入（企微）

```
企微消息回调 ──► 网关（按 group_id 粘性路由）──► web 节点 :8000
                                                   │
回复动作（落库）──────────────────► 企微发送端（WECOM_OUTBOUND_WEBHOOK，按 message_id 去重）
```

- 端口：`APP_HOST=0.0.0.0` + 防火墙放行 8000（或反向代理到域名，注意 SSE `/events` 不要开缓冲）
- 网关建议按 `group_id` 粘性路由（「连续发消息打断合并」和测试页事件流依赖同一节点）
- 发送端必须按 `message_id` 去重（投递是 at-least-once）
- 先用 `WECOM_FD_RELEASE_STAGE=SHADOW` 观察，再逐级放开；医生审核工作台未对接（`handoff=REQUESTED` 只落库），接入后应回写审核回执

## 9. 日常运维

| 操作 | 命令 |
| --- | --- |
| 看节点/定时任务心跳 | `curl http://127.0.0.1:8000/api/wecom/cluster` |
| 看日志 | `tail -f logs/app.log logs/error.log logs/scheduler.log` |
| 升级 | `git pull` → `deploy/server/run.sh restart`（依赖有变时先 `bash scripts/install_server.sh`） |
| 回滚 | `git checkout <上一个 commit>` → `restart`；数据库字段变更需手工 `ALTER TABLE` 回退 |
| 数据库备份 | `docker exec lchain-mysql mysqldump -ulchain -plchain lchain > backup_$(date +%F).sql` |
| 密钥轮换 | 改 `.env` 的 `LLM_API_KEY` → `restart` |
| 多 web 节点 | 同一台/多台启动多个 `uvicorn`，每个节点 `NODE_ID` 唯一、`LOG_DIR` 不同；`scheduler` 仍只 1 个 |

## 10. 安全与合规

- `.env` 与 `deploy/langfuse/.env` 含密钥，已被 git 忽略；不要打进任何交付包
- 家庭医生默认 `DEMO`：红旗规则与低风险路径标注 `DEMO_PENDING_CLINICAL_REVIEW`，**上线前必须由医学组审核并替换**，再逐级放开放行状态
- 患者数据留在服务器本地 MySQL；如需跨机构汇总，先过法务/合规评估
- 医院/号源/预约仍是 `NOT_CONNECTED`：计划卡只给「需要的医疗能力 + 官方入口」，不会编造医院或预约回执

## 11. 常见问题

| 现象 | 处理 |
| --- | --- |
| `install_server.sh` 报 Python 版本低 | 装 3.12：`apt install python3.12 python3.12-venv`，再 `PYTHON=python3.12 bash scripts/install_server.sh` |
| pip 安装很慢/超时 | 加镜像：`PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple` |
| 没有 Docker / 不能用 Docker | 用服务器已有 MySQL（第 4 节方案 2），Langfuse 可留空 |
| `/api/health` 不通 | 确认服务已启动、端口一致：`deploy/server/run.sh status`；服务器防火墙/安全组放行端口 |
| MySQL 连不上 | 看 `logs/error.log` 的 `MySQL 连接失败`；核对 `MYSQL_URL` 账号密码/端口；容器方式用 `docker exec lchain-mysql mysqladmin ping` |
| Langfuse 报连接失败 | 不影响主流程（提示词回退内置）；要启用就检查 `LANGFUSE_HOST` 是否容器内可访问（跨容器用宿主机 IP，不要写 `localhost`） |
| 页面/文件时间是 UTC | 容器设置 `TZ=Asia/Shanghai`（compose 已带） |
| 企微侧收不到回复 | 检查 `WECOM_OUTBOUND_WEBHOOK`；未配置时消息只落库（测试页能看到） |
