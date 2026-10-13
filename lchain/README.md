# LChain Agent

**业务场景 Agent 开发框架**：基于 **LangChain v1（create_agent / LangGraph）** 的 AI Agent 工程骨架，使用 **Langfuse** 做可观测性和提示词管理，并已落地三个业务 Agent：**企业微信患者服务群自动回复**、**结构化问诊**、**山区远程问诊**（后两个在同一个咨询页里切换场景）。

- 💬 Web 对话界面：流式输出，展示工具调用过程，支持多轮记忆，每条回复可以 👍/👎 反馈
- 👥 企业微信群回复 Agent：LangGraph 工作流（是否回复 → 文件解读 → 上下文 → 检索 → 意图识别 → 分支回复），MySQL 存储；支持连续发消息打断合并、checkpoint 断点续跑、重启不丢不重、超时转人工；可部署多个 web 节点 + 一个独立的定时任务进程；附带模拟群聊测试页（[详见下文](#企业微信群回复-agent)）
- 🌐 联网搜索：`web_search` + `fetch_webpage`，默认免 Key（ddgs，多引擎并行），可切换 Tavily / SearXNG；回答带引用来源
- 🧩 Skills：在 `skills/` 目录写 Markdown 即可给 Agent 增加能力（按需加载，不占上下文）
- 🩺 结构化问诊工作流：顶部导航「问诊」进入（与「企微群」同级），显式 LangGraph 工作流（判断提示词 + 代码钳制）驱动「现病史采集（多轮）→ 分诊中间提示 → 鉴别问询（多轮）→ 诊断意见 → 病情变化追问 → 更新诊断」；每轮只问 2 个问题、问题不带编号无铺垫（[详见下文](#结构化问诊场景consult)）
- 🏔 山区远程问诊工作流：顶部导航「山区问诊」进入，面向**偏远山区、医疗条件差的村民本人**（多为老人）：除病情外还要问清「到最近能看病的地方多远、有没有车、有没有人能陪」，结论落到**去哪一级**（居家 / 村卫生室 / 乡镇卫生院 / 县医院 / 打 120）与**怎么去、什么时候出发**，一次只问一个问题，蛇咬/农药/煤气/摔伤等场景自动附上正确急救要点（[详见下文](#山区远程问诊-agentrural)）
- 🧪 测试报告与 AI 分析：问诊用例一键跑批（真实模型），逐例标记通过/不通过 + 通过率/轮次/字数/耗时统计，并可让 DeepSeek 分析失败原因与改进建议
- 🔍 Langfuse 追踪：每轮对话生成一条完整 trace（Agent → LLM → Tool → LLM），包含 token、耗时、成本、session 和 user
- 🧪 提示词调试台：同一输入、多套提示词 / 模型 / 温度并行对比；满意后一键保存为 Langfuse 新版本并发布
- 📦 提示词托管：运行时从 Langfuse 按 label / version 拉取提示词；Langfuse 不可用时自动回退到本地默认

> **想给自己业务搭 Agent？** 先看 [`docs/business-agent-guide.md`](docs/business-agent-guide.md)：
> 以问诊为例讲的一套可复制方法（显式工作流 + 判断提示词 + 代码钳制 + 用例评测），
> 含「第一版为什么失败」、七步开发流程、性能与并发、踩坑清单、移植检查清单。
> 配套视频（简笔画，108 秒）：`promo-video/output/guide/agent-dev-process.mp4`，`cd promo-video && ./build.sh guide` 可重新生成。
> 家庭医生 Agent（VBC 分级诊疗）介绍视频（简笔画 + 旁白，139 秒）：`promo-video-fd/output/fd-intro.mp4`，
> `cd promo-video-fd && ./build.sh` 可重新生成（分镜与文案见 [`promo-video-fd/STORYBOARD.md`](promo-video-fd/STORYBOARD.md)）。

## 目录结构

```
.
├── app/
│   ├── main.py                 # FastAPI 入口（web 节点：初始化 MySQL / checkpointer / 心跳，恢复本节点未完成的任务）
│   ├── scheduler.py            # 独立的定时任务进程（python -m app.scheduler，只部署一个，MySQL 锁保证单例）
│   ├── api/routes.py           # HTTP 接口（对话 SSE / 提示词 / Playground / 反馈）
│   ├── agent/
│   │   ├── agent.py            # Agent 构建 + 流式执行 + Langfuse 回调
│   │   ├── report_tools.py     # 报告类 Skill 的配套工具（写 spec、运行生成脚本、列出/下载报告）
│   │   ├── imagegen.py         # AI 生图工具 generate_image / edit_image（调用 skills/imagegen/scripts/image_gen.py）
│   │   ├── report_styles.py    # 报告自定义样式：预设库、命名样式、解析与可读性校验、样张预览
│   │   ├── ppt_workflow.py     # PPT 制作流程状态机：需求→大纲确认（多轮修改）→生成门禁→验证；流程中间件
│   │   ├── ppt_media.py        # PPT 图文素材与验证：联网图片、示意图绘制、LibreOffice 渲染验证
│   │   ├── tools.py            # 工具定义与组装（新增工具写在这里）
│   │   ├── search.py           # 联网搜索 web_search / 网页阅读 fetch_webpage
│   │   ├── sessions.py         # 通用对话落盘（会话列表 + 重启后仍能看历史）
│   │   └── skills.py           # Skills 加载器 + load_skill 工具
│   ├── consult/                # 结构化问诊工作流（与 app/wecom 平级）
│   │   ├── graph.py            # LangGraph StateGraph：load_state→extract→assess→分支节点→save_state
│   │   ├── prompts.py          # 7 个 consult-* 提示词（判断类 JSON / 生成类带 max_chars）
│   │   ├── llm.py              # call_json（判断）/ stream_text（生成，流式）
│   │   ├── gates.py            # 10 项要素、轮次门槛、clamp_action 钳制、limit_questions 截断
│   │   ├── state.py            # consult.json 读写（含对话记录）+ 会话列表
│   │   ├── render.py           # 分诊/诊断/结束语渲染（免责声明与急诊提示由代码保证）
│   │   ├── cases.py            # 10 个测试病例库（测试/跑批/冒烟的唯一数据源）
│   │   ├── eval.py             # 用例跑批：真实模型 + 通过判定 + 统计 + DeepSeek 分析
│   │   └── routes.py           # /api/consult/*（状态、会话列表、跑批与报告、AI 分析）
│   ├── rural/                  # 山区远程问诊工作流（与 app/consult 同一套架构，服务对象不同）
│   │   ├── graph.py            # 同 consult 的图结构，多一个 ask_access 分支（问就医条件）
│   │   ├── prompts.py          # 8 个 rural-* 提示词（老人友好：一次只问一个、更短更口语）
│   │   ├── gates.py            # 复用 consult 的 10 项要素 + 就医条件要素、处置层级、急救要点表
│   │   ├── render.py           # 就医建议/诊断/结束语渲染（就医路径、急救要点、无剂量由代码保证）
│   │   ├── state.py            # rural.json 读写（含对话记录、就医条件）+ 会话列表
│   │   ├── cases.py            # eval/rural/**/*.md 用例加载器（目录 = 期望处置层级）
│   │   ├── eval.py             # 山区问诊跑批：8 项判定（处置层级/就医条件/急救要点…）+ 报告
│   │   ├── llm.py              # 判断/生成调用（复用 app/core/prompt_llm.py，绑 RURAL_* 配置）
│   │   └── routes.py           # /api/rural/*（状态、会话列表、删除、跑批与报告）
│   ├── prompts/
│   │   ├── manager.py          # 提示词解析：override > Langfuse > 本地默认（含 wecom-* 提示词）
│   │   └── playground.py       # 多变体并行对比
│   ├── core/
│   │   ├── config.py           # 配置（.env）
│   │   ├── llm.py              # LLM 工厂（OpenAI 兼容协议）
│   │   ├── prompt_llm.py       # 工作流节点的通用 LLM 调用（判断类 JSON / 生成类流式，consult 与 rural 共用）
│   │   ├── workflow_eval.py    # 跑批通用机制：后台任务 + 报告存储 + 统计 + DeepSeek 分析（consult 与 rural 共用）
│   │   ├── db.py               # MySQL（SQLAlchemy 2 async + aiomysql），启动时自动建表
│   │   ├── cluster.py          # 多节点协调：节点心跳 + MySQL 命名锁（定时任务单例锁、群锁、压缩锁）
│   │   ├── logging_setup.py    # 日志：写入 LOG_DIR（app.log / error.log / access.log / scheduler.log），按天切分
│   │   └── langfuse_client.py  # Langfuse 客户端 / CallbackHandler
│   ├── wecom/                  # 企业微信群回复 Agent
│   │   ├── graph.py            # LangGraph 工作流：各节点 + 路由 + 执行 / 续跑入口
│   │   ├── dispatcher.py       # 每群一个调度器：串行（跨节点靠群锁）、打断合并、人工接手、恢复遗留任务
│   │   ├── checkpoint.py       # LangGraph MySQL checkpointer（断点续跑）
│   │   ├── outbound.py         # 发件箱：落库 + 幂等键 + 投递状态 + 补投递
│   │   ├── jobs.py             # 定时任务：超时通知、补投递、接管失联节点、清理 checkpoint（由 app/scheduler.py 调度）
│   │   ├── prompts.py          # 14 个节点提示词初版（运行时优先用 Langfuse 版本）
│   │   ├── llm.py              # 节点 LLM 调用（JSON 模式 / 文本、模型档位、字数上限）
│   │   ├── documents.py        # 文件读取与解析（图片 / PDF / docx / txt → deepseek-flash 解读）
│   │   ├── compress.py         # 聊天记录压缩（LangChain SummarizationMiddleware）
│   │   ├── models.py / store.py# 数据表 / 数据访问
│   │   ├── cases.py            # 评测用例加载（eval/wecom/**/*.md，目录 = 测试集合）
│   │   ├── eval.py             # 评测跑批：临时群跑真实一轮 + 逐项判定 + 统计 + DeepSeek 分析
│   │   ├── templates.py        # 固定话术（引导语、通知模板）
│   │   └── routes.py           # /api/wecom/* 接口
│   └── web/                    # 前端（原生 HTML/JS，无需构建）
│       ├── index.html          # 对话页（含问诊场景）
│       ├── wecom.html          # 企微群回复测试页（模拟群聊 + 会话历史列表）
│       ├── wecom-eval.html     # 企微群回复的批量评测报告页
│       ├── eval.html           # 测试报告页
│       ├── playground.html     # 提示词调试台
│       ├── icons.js            # 图标系统（内联 SVG，不用 emoji）
│       └── style.css           # 设计令牌 + 组件样式
├── run.py                      # 本地开发启动 web 服务（带热重载）
├── deploy/
│   ├── langfuse/               # Langfuse 自托管 docker-compose（Postgres/ClickHouse/Redis/MinIO）
│   └── mysql/                  # MySQL 8.4 docker-compose（企微群回复业务存储 + checkpoint）
├── eval/                       # 评估用例（每个功能一个子目录，一个用例一个 .md，见 eval/README.md）
│   └── consult/<科室>/*.md     # 问诊用例：目录=期望分诊科室，文件名=序号-主诉-疾病
├── skills/                     # Skills（每个子目录一个 SKILL.md）
│   ├── web-research/           # 深度调研：拆解→多轮搜索→精读→交叉验证→带引用报告
│   ├── news-briefing/          # 新闻简报
│   ├── html-report/            # 单文件 HTML 报告（移植自 medex）
│   ├── word-report/            # 可编辑 Word 报告（移植自 medex）
│   ├── ppt-report/             # 可编辑 PPT（移植自 medex，含 OOXML 严格校验）
│   └── imagegen/               # AI 生成/编辑图片（拷贝自 ~/.codex/skills/.system/imagegen，经 generate_image / edit_image 工具接入）
├── tests/skills/               # 三个报告 Skill 的单测（来自 medex）
├── tests/agent/                # Agent 流程测试（PPT 流程、问诊 10 病例、生图、报告样式）
├── tools/bin/                  # 本机编译的 Open XML SDK 校验器（不提交）
├── scripts/
│   ├── langfuse_init.sh        # 首次初始化本地 Langfuse（生成密钥、启动、写入 Key）
│   ├── init_prompts.py         # 把默认提示词（agent-system + wecom-*）同步到 Langfuse
│   ├── crash_test.py           # 企微群回复重启恢复测试（真实杀进程，验证不丢、不重）
│   ├── mock_webhook.py         # 模拟企微发送端（crash_test 使用）
│   ├── consult_smoke.py        # 问诊场景真实模型端到端冒烟（AI 医生 ↔ AI 患者）
│   ├── rural_smoke.py          # 山区问诊真实模型端到端冒烟（同上一套做法）
│   └── run_experiment.py       # 基于 Dataset 的离线提示词评估
├── data/uploads/               # 企微群上传的文件（已加入 .gitignore）
├── logs/                       # 运行日志（LOG_DIR，已加入 .gitignore）
├── docs/                       # 文档：business-agent-guide.md（对外讲怎么开发业务 Agent）
├── AGENTS.md                   # 给 AI 编码助手的项目说明（约定、踩坑记录）
├── .env.example
├── requirements.txt
└── Makefile
```

## 快速开始

### 1. 安装依赖

```bash
make install          # 创建 .venv 并安装依赖，生成 .env
```

在 `.env` 里填写 LLM 配置。支持任何 OpenAI 兼容服务（OpenAI / DeepSeek / 通义 / vLLM / Ollama …），也支持用 `${ENV_VAR}` 引用系统环境变量。

### 2. 启动本地 Langfuse（Docker）

Docker 运行时用的是 [Colima](https://github.com/abiosoft/colima)，不需要 Docker Desktop，也不需要管理员密码：

```bash
brew install colima docker docker-compose   # 仅首次
make langfuse-init    # 首次：生成随机密钥 → 启动 6 个容器 → 自动把 API Key 写进 .env
```

之后日常使用：

| 命令 | 作用 |
| --- | --- |
| `make langfuse-up` | 启动（Colima 未运行时会先启动它） |
| `make langfuse-down` | 停止，数据保留在 Docker volume 中 |
| `make langfuse-ps` / `make langfuse-logs` | 查看状态 / 日志 |
| `make langfuse-password` | 查看登录账号和密码 |

Langfuse 地址：http://localhost:3000。组织、项目 `lchain-agent`、API Key 和管理员账号在首次启动时自动创建。

| 服务 | 作用 | 宿主机端口（仅监听 127.0.0.1） |
| --- | --- | --- |
| langfuse-web | UI + API | 3000 |
| langfuse-worker | 异步写入 ClickHouse | 3030 |
| postgres 17 | 用户 / 项目 / 提示词 / 数据集 | 15432 |
| clickhouse 25.12 | trace / observation / score（分析型存储） | 18123, 19000 |
| redis 7 | 队列与缓存 | 16379 |
| minio | S3 兼容对象存储（原始事件、多媒体） | 9190（API）, 9191（控制台） |

> 端口做了偏移，避免和本机的 Postgres / Redis 以及 ClashX（9090）冲突。所有密码都在 `deploy/langfuse/.env` 中随机生成，已加入 `.gitignore`。
>
> **不要删除 `deploy/langfuse/.env` 后重新 init**：数据卷里的数据库密码是首次启动时写入的，重新生成会导致连接失败。如果确实要重置，先执行 `cd deploy/langfuse && docker compose down -v` 清空数据。

也可以不用本地部署，改用 [Langfuse Cloud](https://cloud.langfuse.com)：把 `.env` 中的 `LANGFUSE_*` 换成 Cloud 的 Key，并设置 `LANGFUSE_HOST=https://cloud.langfuse.com`。

### 3. 启动 MySQL（企微群回复需要）

```bash
make mysql-up         # MySQL 8.4，127.0.0.1:13306，账号密码 lchain/lchain，库 lchain
```

| 命令 | 作用 |
| --- | --- |
| `make mysql-up` / `make mysql-down` | 启动 / 停止（数据保留在 Docker volume `lchain_mysql_data`） |
| `make mysql-cli` | 进入 MySQL 命令行 |

业务表（`wecom_*`）和 checkpoint 表（`checkpoints*`）在服务启动时自动创建。MySQL 没启动时，对话页和调试台照常可用，只有企微群回复功能不可用。

### 4. 初始化提示词

```bash
make init-prompts     # 在 Langfuse 中创建 agent-system 和 14 个 wecom-* 提示词（已存在的跳过），打上 production 标签
```

### 5. 启动服务

项目有两个进程，本地开发时开两个终端分别启动：

```bash
make dev              # web 服务（run.py，改代码自动重载）
make scheduler        # 定时任务进程：超时通知、补投递、接管失联节点、清理 checkpoint
```

- 对话页：http://127.0.0.1:8000（顶部导航「🩺 问诊」进入结构化问诊，「🏔 山区问诊」进入山区远程问诊）
- 企微群回复测试页：http://127.0.0.1:8000/wecom
- 测试报告：http://127.0.0.1:8000/eval
- 提示词调试台：http://127.0.0.1:8000/playground（顶部导航已移除入口，直接访问 URL）
- API 文档：http://127.0.0.1:8000/docs

只测试对话页和调试台时，可以只启动 `make dev`。不启动定时任务进程时，企微群回复照常工作，只是「待回复超时 → @病历经理」和「投递失败重试」不会执行（测试页左侧可以手动触发超时检查）。需要局域网访问时，在 `.env` 中设置 `APP_HOST=0.0.0.0`。生产部署见[部署](#部署多个-web-节点--一个定时任务进程)。

### 日志

运行日志统一写入 `LOG_DIR`（默认项目根目录下的 `logs/`，已加入 `.gitignore`），同时输出到控制台：

| 文件 | 内容 |
| --- | --- |
| `logs/app.log` | web 服务的全部日志（INFO 及以上，含启动、MySQL / Langfuse 连接、企微任务恢复、补投递等） |
| `logs/error.log` | web 服务 WARNING 及以上，排查问题先看这里（包括异常堆栈） |
| `logs/access.log` | HTTP 访问日志 |
| `logs/scheduler.log` / `logs/scheduler-error.log` | 定时任务进程的全部日志 / WARNING 及以上 |

- 每天零点切分，旧文件带日期后缀（如 `app.log.2026-09-28`），保留 `LOG_RETENTION_DAYS` 天（默认 14 天）
- `LOG_DIR` 可以写相对路径（按项目根目录解析，与启动目录无关）或绝对路径（如 `/var/log/lchain`），目录不存在时自动创建
- `LOG_LEVEL` 调整级别（默认 INFO）；`LOG_ACCESS_CONSOLE=true` 时访问日志也打印到控制台
- 每行日志带进程号 `[pid]`，热重载时可以区分新旧进程
- 同一台机器上部署多个 web 节点时，给每个节点配置不同的 `LOG_DIR`（按天切分不支持多个进程写同一个文件）
- 日志配置在 `app/core/logging_setup.py`，业务代码统一用 `logging.getLogger(__name__)`

## 提示词调试工作流

1. **在对话页快速试**：左侧勾选「使用下方临时提示词」→ 编辑 → 直接对话。每条回复下方都有「查看 Langfuse Trace」链接，可以看到最终发给模型的完整消息
2. **在调试台做对比**：`/playground` 里添加多个变体（不同提示词版本 / 临时文本 / 模型 / 温度），对同一输入并行运行，对比输出、耗时和 token
3. **保存 / 发布**：点「保存为新版本」写回 Langfuse；选择打 `production` 标签后，线上对话立即使用新版本（`LANGFUSE_PROMPT_CACHE_TTL=0` 时不缓存）
4. **在 Langfuse 中分析**：
   - `Tracing` → 按 tag 过滤（`playground` / `variant:A` / `prompt:override` / `wecom` …）
   - `Prompts` → 选择提示词 → 查看每个版本的调用量、延迟、token、成本和用户反馈评分（generation 已关联到提示词版本）
   - `Sessions` → 按会话回放多轮对话（企微群回复的 session 是群 ID）
   - Langfuse 自带的 Playground 也可以直接从某条 generation 打开，调整后重跑
5. **批量回归**：`make experiment`（或 `python -m scripts.run_experiment --version 2`）会在数据集上跑一遍，结果在 `Datasets → agent-smoke-test → Runs` 中横向对比

提示词模板使用 Langfuse 语法 `{{variable}}`。内置变量有 `assistant_name`、`current_time`、`language`、`bot_name`，可以通过请求的 `prompt_variables` 覆盖或新增。提示词的 `config` 中可以放 `temperature` / `model`，优先级为：请求参数 > 提示词 config > `.env`。

## 联网搜索

Agent 有两个联网工具，模型会自己判断什么时候需要搜索：

| 工具 | 作用 |
| --- | --- |
| `web_search(query, topic, time_range, max_results)` | 搜索网页；`topic="news"` 搜新闻，`time_range` 可选 day / week / month / year |
| `fetch_webpage(url)` | 打开网页并提取正文（trafilatura 转 Markdown），用于阅读搜索结果原文 |

**切换搜索服务**（`.env` 中的 `SEARCH_PROVIDER`）：

| 值 | 说明 |
| --- | --- |
| `auto`（默认） | 配置了 `TAVILY_API_KEY` 就用 Tavily，否则用 ddgs |
| `ddgs` | 免 Key，开箱即用。`SEARCH_DDGS_BACKEND`（默认 `yahoo,bing,yandex`）中的多个引擎并行搜索，取最先返回结果的那个 |
| `tavily` | [Tavily](https://tavily.com) 专为 LLM 设计，摘要质量更高，每月有免费额度 |
| `searxng` | 自建 [SearXNG](https://github.com/searxng/searxng) 元搜索，配置 `SEARXNG_URL` |

首选服务出错时会自动回退到 ddgs。

> ddgs 默认的 `auto` 模式会随机选引擎，其中 mojeek 等在国内网络下经常 TLS 握手失败，所以项目默认指定 `yahoo,bing,yandex`。免费引擎偶尔会同时搜不到结果，对稳定性有要求时建议配置 `TAVILY_API_KEY`。

- **限制调用次数**：通过 LangChain 的 `ToolCallLimitMiddleware` 限制单轮对话的搜索次数（`SEARCH_MAX_CALLS`，默认 6）和抓取次数（`FETCH_MAX_CALLS`，默认 4），超出后模型会基于已有信息作答。
- **安全**：`fetch_webpage` 只允许 http(s)，并拒绝 127.x / 10.x / 192.168.x / 169.254.x 等内网地址，防止 SSRF（兼容 Clash 的 Fake-IP 模式）。
- **调试搜索质量**：`GET /api/search?q=关键词&topic=news` 直接返回原始结果，不经过 LLM；也可以用命令行 `python -m app.agent.search 关键词`。
- **前端**：左侧「🌐 联网搜索」开关；工具调用过程会显示搜索关键词、来源链接和阅读的网页。

## Skills

Skill 就是一份「做某类任务的方法说明书」，采用**渐进式加载**：

1. 系统提示词里只放每个 skill 的 `name` 和 `description`（几十个字）
2. 模型判断用户请求匹配某个 skill 时，调用 `load_skill(name)` 读取完整说明
3. 按说明调用已有工具完成任务

**新增一个 Skill**：在 `skills/` 下新建目录，写一个 `SKILL.md`，不用改代码，也不用重启（按文件修改时间自动重新加载）：

```markdown
---
name: my-skill
description: 一句话说明什么时候该用这个技能（模型靠这句话判断是否加载，要写清楚触发场景）
requires: [web_search]        # 可选：依赖的工具，工具没启用时这个技能自动隐藏
---

# 具体步骤
1. ……
2. ……
```

同一目录下的其他文件（如 `reference.md`、模板、示例）可以让模型通过 `load_skill(name, file)` 按需读取。

内置示例：
- `web-research`：深度调研，流程是拆解问题 → 多轮搜索 → 精读原文 → 交叉验证 → 输出带引用的报告
- `news-briefing`：新闻简报，按时间范围搜新闻，去重排序后输出
- `html-report` / `word-report` / `ppt-report`：生成学术风格的中文报告（单文件 HTML / 可编辑 Word / 可编辑 PPT），支持表格、柱状图/折线图、图片、双栏混排和编号参考文献。移植自 medex，见下文

> 结构化问诊曾经是一个 skill（`skills/consult/`），现在已经改成独立工作流（`app/consult/`），流程说明在 `app/consult/prompts.py` 里，不再走 skill。

### 报告类 Skill（html-report / word-report / ppt-report）

来自 medex（`storage/skills`），在对话页直接说「帮我做一份 PPT / Word / HTML 报告……」即可，模型会读取 Skill 说明、写 JSON spec、运行生成脚本，按报错修正后重试，最后给出下载地址。

| 项 | 说明 |
| --- | --- |
| 工作区 | `data/report_workspace`（`REPORT_WORKSPACE`），相当于 medex 沙箱里的 `/repo`：中间 spec 在 `threads/`，最终报告在 `o_outcome/` |
| 配套工具 | `report_write_file`（写 spec）、`report_run_generator`（运行固定的生成脚本）、`report_read_file`、`report_list_outputs`（`app/agent/report_tools.py`），路径限制在工作区内，不走 shell |
| 下载 | `GET /api/reports` 列表，`GET /api/reports/{文件名}` 下载 |
| 依赖 | 三个生成脚本只用 Python 标准库（Word 有 Pillow 时会额外校验图片）；PPT 严格校验需要 `/usr/bin/xmllint`（macOS 自带）和 Open XML SDK 校验器 |
| 测试 | `pytest tests/skills`（medex 原有 128 个单测）；`python -m scripts.report_skills_smoke`（经 Agent 同一条调用路径生成三种报告并检查产物） |

**PPT 的 Open XML SDK 校验器**：`ppt-report` 默认要求这一步严格校验通过才发布文件。校验器源码在 `skills/ppt-report/validators/openxml-sdk/`，本机已编译到 `tools/bin/medex-openxml-sdk-validator`（macOS arm64，未提交到仓库）。其他机器按下面步骤编译（需要 .NET SDK 8.0.424，见该目录 README）：

```bash
cd skills/ppt-report/validators/openxml-sdk
dotnet publish Medex.OpenXmlSdkValidator.csproj -c Release -r osx-arm64 --self-contained true \
  -p:RuntimeIdentifiers=osx-arm64 -p:RestoreLockedMode=false -o ../../../../tools/bin
```

相关配置（`.env`）：`PPT_OPENXML_SDK_VALIDATOR`（校验器路径）、`PPT_SDK_VALIDATOR_OWNER`（`root`：要求 root 所有，生产镜像用；`user`：允许当前用户所有，本地开发用，默认）、`PPT_SDK_VALIDATION`（`required` 默认，缺校验器即失败；`optional` 缺校验器时跳过并在输出中注明）。

**对 medex 原脚本的改动**（其余原样保留）：
- `ppt-report/scripts/generate_ppt.py`：工作区根目录、输出目录、两个校验器的路径可以用环境变量配置（默认值仍是 medex 的 `/repo`、`/usr/bin/xmllint`、`/usr/local/bin/...`，行为不变）；新增 `user` 所有权模式和 `optional` 模式。
- 三个 `SKILL.md`：加了 `requires: [report_run_generator]` 和一段「在 LChain 中使用」的路径映射说明，正文未改。
- 三个生成脚本增加 `_style` 支持（自定义样式，见上文）：`generate_html.py` 的 `style_css()`、`generate_word.py` 的 `apply_style()`（`report_assets.py` 的 `CHART_THEME`）、`generate_ppt.py` 的 `apply_style()` / `theme_xml()`；默认值即原版配色，原有 128 个单测不变。三个 SKILL.md 都加了「自定义样式」一节。
- `ppt-report/SKILL.md`：另外加了「LChain 对话流程」「图文混排要求」两节（覆盖原文「不设最低图数」的说法），`requires` 增加了流程工具，新增 `references/workflow-outline.md`；medex 原文保留在分隔线之后。
- spec 里的 `/repo/...` 图片路径由 `report_run_generator` 自动映射到工作区。

### AI 生图（imagegen）

Skill 来自 Codex 的 `~/.codex/skills/.system/imagegen`，在对话页直接说「帮我画一张……」即可。Codex 版依赖内置的 `image_gen` 工具，本项目改为两个工具（`app/agent/imagegen.py`），在后台调用 skill 自带的 `scripts/image_gen.py`（原样保留）：

| 工具 | 作用 |
| --- | --- |
| `generate_image(prompt, name, size, quality, n, background)` | 文生图；size：`landscape` 1536×1024 / `square` / `portrait`；quality：`low` / `medium` / `high`；`n` 最多 4 个变体 |
| `edit_image(image_path, prompt, name, quality)` | 编辑工作区里已有的图片，另存新文件，不覆盖原图 |

- 配置（`.env`）：`IMAGEGEN_API_KEY` / `IMAGEGEN_BASE_URL` / `IMAGEGEN_MODEL`（OpenAI Images API 兼容服务，本机指向 Azure `gpt-image-2.5-sunburst`），`IMAGEGEN_TIMEOUT`（默认 600 秒）、`IMAGEGEN_MAX_CALLS`（单轮最多 6 次）。Key 只传给生图子进程（作为 `OPENAI_API_KEY` / `OPENAI_BASE_URL`），不影响对话模型；**不配 Key 时两个工具不注册，imagegen skill 自动隐藏**
- 图片保存在 `data/report_workspace/threads/chat-<会话>/images/`（PPT 流程中的会话放 `threads/ppt-<会话>/images/`，可直接写进 PPT spec），网页通过 `GET /api/workspace/files/<路径>` 显示（只允许 threads/ 下的图片）
- 速度：一张图约 2~5 分钟，服务繁忙时会返回 429，工具会提示稍后重试；对话页的工具卡片会显示「生成中」并在完成后直接展示图片
- PPT 流程可以用它生成封面、场景插画（大纲配图来源写「AI 生成」，caption 注明「AI 生成示意图」）；AI 图不能当作真实检查影像或研究数据
- 测试：`pytest tests/agent/test_imagegen.py`（用假 CLI 验证参数/落盘/错误处理）；`IMAGEGEN_LIVE_TEST=1` 时额外调用真实服务

#### 自定义样式（Word / PPT / HTML 通用）

三种报告都支持在 spec 顶层写 `style`，对话里说「用商务风格」「主色换成我们医院的红色 #9E1B32」「标题用宋体」即可：

- **内置 9 个预设**：academic 学术蓝绿（默认，原样式）、business 商务蓝、tech 科技蓝紫、fresh 清新绿、warm 暖橙、hospital 医院红、elegant 典雅紫、minimal 极简黑白、dark 深色（仅 PPT/HTML）
- **在预设上覆盖**：`colors`（主色、强调色、正文、标题、背景、图表系列色等）、`fonts`（中文/西文/标题字体）、`table`（三线表 / 表头填充 / 全框线）、`cover`（经典 / 主色色带）、`font_scale`（Word/HTML 字号缩放）、`brand`（封面标签、页脚）
- **命名样式**：`report_save_style` 保存后，以后直接写 `"style": "我的医院"`；文件在 `data/report_workspace/styles/*.json`
- **样张预览**：`report_preview_style` 生成一张样张图（标题、正文、表格、图表配色），生成报告前先让用户确认
- **可读性校验**：正文/标题/强调色与背景的对比度按 WCAG 检查，不合格直接报错并说明要调哪个颜色；Word 拒绝深色背景
- PPT 流程里，样式在需求阶段记录（`ppt_save_requirements(theme=...)`）、在大纲中展示给用户确认，生成时 spec 漏写会自动补上

实现：`app/agent/report_styles.py` 把 `style`（预设名 / 覆盖对象）解析成完整样式，`report_run_generator` 写入 spec 的 `_style` 字段；三个生成脚本只读取 `_style` 里的具体数值（颜色、字体、表格/封面形式），**不写 style 时输出与 medex 原版完全一致**。

#### PPT 制作流程（需求 → 大纲确认 → 图文生成 → 验证）

在对话页说「帮我做一个 xxx 的 PPT」，Agent 会按下面的流程推进，每一步都有代码门禁，不依赖模型自觉：

| 阶段 | Agent 做什么 | 门禁 |
| --- | --- | --- |
| 1 需求整理 | 提取主题、受众、用途、页数/时长、风格、素材；缺关键信息先提问（`ppt_save_requirements`） | 有待回答的问题时，本轮不允许出大纲 |
| 2 大纲确认 | 输出逐页大纲（标题、版式、核心观点、要点、配图计划与来源），用户可以反复要求修改，每次生成新版本（`ppt_propose_outline`） | 大纲会做结构检查：标题长度（避免生成时溢出）、版式与配图是否匹配、图文密度 |
| 3 用户确认 | 用户回复「确认」后记录（`ppt_confirm_outline`） | 只接受大纲提出**之后**用户新发的确认消息；回复里带修改意见的不算确认；不能替用户确认 |
| 4 图文生成 | 联网找图（`ppt_search_images` / `ppt_download_image`）、绘制示意图（`ppt_make_diagram`：流程 / 时间线 / 循环 / 对比 / 层级 / 要点卡片），写 spec，调用 `report_run_generator` | 最新大纲未确认时拒绝生成；spec 必须放在会话目录，并覆盖大纲中的每一页（按标题对照） |
| 5 渲染验证 | `ppt_verify`：LibreOffice 渲染逐页检查（缺页、配图落实、图文密度、中文字体、文字越界、空白页、图片过小），生成预览图；可选多模态模型视觉评审 | 未通过则修正后重新生成，直到通过才交付 |

- 流程状态保存在 `data/report_workspace/threads/ppt-<会话id>/workflow.json`（大纲各版本 `outline_vN.json/.md`、素材 `images/`、预览 `preview/` 也在这个目录），服务重启不丢；`GET /api/ppt/workflow/{session_id}` 查看状态，对话页顶部会显示进度条。
- 每次调用模型前，`PptWorkflowMiddleware` 会把当前阶段和下一步要求追加到系统提示词。
- 需求变化或交付后要调整结构时，已确认的大纲会失效，需要重新确认；纯排版微调可以直接改 spec 重新生成。
- 图文密度默认 `rich`（≥50% 的内容页带图片 / 示意图 / 图表 / 表格），用户要求简洁时用 `balanced`（≥30%），明确只要文字时用 `minimal`。
- 相关配置：`PPT_SOFFICE_PATH`（LibreOffice 路径，空=自动查找，包括 `~/.cache/codex-runtimes/*/.../soffice`）、`PPT_CJK_FONT`（中文字体，空=自动查找）、`PPT_VERIFY_VISION_MODEL`（视觉评审模型，需支持图片输入，例如 `deepseek-flash`；空=只做自动检查。视觉评审的结论只作为建议，不会导致验证失败）。
- 渲染时会自动生成 fontconfig 配置，把「微软雅黑」映射到本机中文字体，否则 LibreOffice 会把中文渲染成方框。
- 测试：`PYTHONPATH=. .venv/bin/python -m pytest tests/agent -q`（包括流程门禁和一次真实的生成与渲染验证）。

> 视觉检查：Skill 要求生成后逐页检查渲染效果。本机没有安装 LibreOffice；可以用 `~/.cache/codex-runtimes/.../soffice`（若存在）或 WPS / Keynote 打开检查。Word/PPT 用的字体是「微软雅黑」，macOS 上会回退到其他中文字体，版面会与 Windows 略有差异。

## 结构化问诊场景（consult）

顶部导航里的 **🩺 问诊** 进入（和「对话 / 企微群 / 调试」同级，地址是 `/?scenario=consult`；`/` 是通用对话）。

**这是一个显式工作流**（`app/consult/graph.py`，LangGraph StateGraph），和企微群回复 Agent 同一套做法：
一次运行 = 患者发一条消息 → 回一条。流程由图决定、判断由专门的提示词决定、安全边界由代码渲染。

```
START
  load_state      读会话状态（没有就新建），把患者这条消息记入对话并补上一轮的答案
      │
      ├─────────────────┬─────────────────┐          ← 两个判断节点**并发**（单轮最耗时的两步）
  extract            assess                       （consult-extract） （consult-assess）
  “患者说了什么”       “这一轮该做什么”
      └─────────────────┴─────────────────┘
      │
  decide          合并 extract 的提取结果 → 用 gates.clamp_action 钳制 assess 给出的动作
      │           ← 模型说错也不会跳过分诊直接下诊断；这是两个并发判断的汇合点
      ├─ ask_history       consult-ask-history       现病史采集（≤2 问，≤90 字）
      ├─ triage            分诊字段 → render_triage    分诊中间提示（原样发患者）
      ├─ ask_differential  consult-ask-differential  鉴别问询（≤2 问，≤100 字）
      ├─ diagnose          consult-diagnose           诊断 / 更新版诊断 → render_diagnosis
      ├─ ask_followup      consult-ask-followup      病情变化追问（≤2 问）
      └─ close             render_close               结束总结
      │
  save_state      落盘 consult.json：阶段流转、轮次、诊断版本、对话记录、trace 入口
     END
```

### 并发能力（实测，单进程）

用 `scripts/load_test.py` 实测（`PYTHONPATH=. .venv/bin/python -m scripts.load_test -n 50`）：

| 场景 | 结果 |
| --- | --- |
| 50 个**不同会话**并发问诊 | **50/50 成功**，p50 4.7s、p99 6.1s（单请求 2.6s），0 失败 |
| 100 并发轻量接口 | 100/100 成功，264 req/s |
| 3 个请求**同一会话**（多标签页共用 sid） | 2.8s → 4.9s → 7.1s **严格串行** |

**同一会话是串行的，这是有意为之**：状态文件是「读-改-写」，并发跑同一会话会互相覆盖阶段与轮次。
多个浏览器标签页共用同一个 `session_id`（存在 localStorage），所以开 3 个标签页同时发消息会排队；
现在排队时会先发一个 `queued` 事件，页面上显示「⏳ 排队中…」，不会像卡死。
要测出真实并发能力，必须用**不同** `session_id`（压测脚本默认就是）。

高并发相关的配置（`.env`）：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `APP_THREAD_POOL_SIZE` | 64 | 事件循环线程池（`asyncio.to_thread`：提示词拉取、Langfuse flush、文件/图片处理）。默认只有 `min(32, cpu+4)`，高并发时会在这里排队 |
| `APP_LIMIT_CONCURRENCY` / `APP_BACKLOG` | 0 / 2048 | uvicorn 并发上限（0=不限）与等待队列 |
| `LANGFUSE_PROMPT_CACHE_TTL` | 300 | **提示词缓存秒数**。设成 0 会让每次 LLM 调用都同步拉 Langfuse，**阻塞事件循环** |
| `LLM_TIMEOUT` / `LLM_MAX_RETRIES` | 120 / 2 | LLM 客户端超时与重试；客户端按参数复用，共享连接池（不再每次新建 httpx） |
| `MYSQL_POOL_SIZE` / `MYSQL_MAX_OVERFLOW` | 20 / 20 | 企微群业务的 MySQL 连接池上限 |

> 浏览器对同一域名的 HTTP/1.1 并发连接上限是 6，SSE 流会占住一条。开的标签页多时，
> 多出来的请求会在**浏览器侧**排队（不是服务端）——压测请用脚本，不要用浏览器开很多标签页。

### 并发与耗时（实测）

单轮 3 次 LLM 调用，实测（deepseek-flash）：**extract 1.48s + assess 1.46s + 生成 0.95s ≈ 3.9s/轮**，
其中两个判断节点占 **73%**。`assess` 的输入里只有「要素齐否」「本轮有无新变化」两项依赖 `extract`，
其余（阶段、轮次、分诊、鉴别轮次、诊断版本）都由代码在 `load_state` 算好 → **两个判断节点改成并发**，
那两项偏差由 `decide` 的 `clamp_action` 补回来（`fresh_change` / `wants_to_stop` 作为瞬时信号传入）。

- 单轮耗时：**3.99s → 2.62s（-34%）**；跑完一例评测：**73s → 61s（-25%）**
- `decide` 是纯代码节点，不增加耗时；测试里有「必须真并发」的时序断言（`test_extract_and_assess_run_concurrently`）

其他可并发的 LLM 任务：企微群工作流已经是并发的（`web_search ∥ history_search`、`clarify ∥ intent`）；
通用对话同一轮的多个工具调用由 LangGraph `ToolNode` 并发执行。**不能并发**的是 `assess → 生成节点`
（生成必须知道本轮动作）。跑批的 10 个病例互相独立，也可以并发，但会成倍占用并发额度，目前仍是串行。

### 三道防线

| 防线 | 在哪里 | 作用 |
| --- | --- | --- |
| **流程由图决定** | `graph.py` 的 `_route()` + 条件边 | 模型没有「选择阶段」的权力，只输出 action，代码钳制后才执行 |
| **判断提示词** | `prompts.py` 的 `consult-extract` / `consult-assess` | JSON 模式、temperature=0，只回答「患者说了什么」「这一轮做什么」 |
| **代码钳制** | `gates.py` 的 `clamp_action` | 见下表，模型判断错了也走不歪 |

`clamp_action` 的钳制规则：

- 还没分诊：只允许 `ask_history` / `triage`；要素不全时 `triage` 降为 `ask_history`
- **要素齐 + 轮次够时不允许再 `ask_history`**（否则永远停在采集阶段）→ 自动进入 `triage`
- 已分诊没诊断：`diagnose` 在鉴别轮次不够时降为 `ask_differential`
- **没有诊断时不允许 `close`**（除非患者明确说要去线下）——分诊后就收尾等于流程半途而废
- 已有诊断：只走「变化 → 追问 → 更新诊断」循环；变化已追问过时 `ask_followup` 会被改成 `re_diagnose`，防止无限追问

### 提示词（`app/consult/prompts.py`，Langfuse 可覆盖）

| 提示词 | 档位 | 作用 | 关键约束 |
| --- | --- | --- | --- |
| `consult-extract` | fast | 从患者这句话提取要素 / 变化 / 红旗征 | JSON；否认的症状不算红旗征；「好的/谢谢」不算想结束 |
| `consult-assess` | fast | 判断本轮 action | JSON；含 6 条判断规则与常见错误提醒 |
| `consult-ask-history` | reply | 采集现病史 | **≤2 问、≤90 字、无编号无铺垫** |
| `consult-triage` | reply | 分诊字段 | JSON；**必须引用已采集信息** |
| `consult-ask-differential` | reply | 鉴别问询 | ≤2 问；红旗征未筛查时必须有 1 个筛查问题 |
| `consult-diagnose` | reply | 诊断字段（含更新版） | JSON；依据必须来自已采集信息；**不写药物剂量** |
| `consult-ask-followup` | reply | 追问病情变化 | ≤2 问 |

- 模型档位用 `CONSULT_MODEL` / `CONSULT_FAST_MODEL`（空 = `LLM_MODEL`），也可在 Langfuse 提示词的 config 里写 `model`
- 字数上限是提示词 config 的 `max_chars`，以 `{{max_chars}}` 注入；**改完 `prompts.py` 要跑 `python -m scripts.init_prompts --force` 才会在运行时生效**（优先读 Langfuse）

### 回复风格（产品要求）

**问题直接给**：不带编号、不带「好的/再问两个」这类过渡语、不带任何铺垫，**每轮最多 2 个问题、写在同一行**（长了自然折行，但不要分成两段）。
实测一轮典型回复：`吃铝碳酸镁之后，疼能减轻吗？` + 换行 + `最近有没有明显瘦下来或吃不下饭？`（32 字）。

代码层面还有 `gates.limit_questions()` 兜底：按问号识别问题行，超出 2 个就连同后面的收尾语一起截掉，
并向前端发 `replace` 事件重渲染——**即使模型多问了，患者看到的也最多 2 个问题**。

### 安全边界（代码保证，不依赖模型）

- 分诊与诊断的**文本由 `render.py` 拼装**，模型只提供字段值（科室、依据、红旗征…）
- 每次分诊/诊断都带「不能替代面诊」免责声明；`urgent` / `emergency` 强制附加「立即急诊/拨 120」提示
- 患者前面提过危险信号、但这一步给了常规就诊 → 代码把 urgency **抬到 urgent**（双重保险）
- 诊断里的 `management` 限 4 条、每条 ≤50 字，提示词明确禁止写具体药物剂量

### 界面（对话页顶部「▤ 历史与报告」）

右侧面板分两块：

- **会话历史**：**分页展示**（每页 10 条，底部有「← 上一页 / 第 x / y 页（共 N 条）/ 下一页 →」）。
  接口 `GET /api/consult/sessions?limit=10&offset=0` 返回 `{sessions, total, offset, limit}`（通用对话是 `/api/sessions`）。
  实现上先按目录 mtime 排序、**只解析当前页的文件**，所以会话多了也不会「列一页要读全部 JSON」。
  列表标题取主诉/首条消息，副标题显示时间与进度（`现病史 3 轮｜分诊 消化内科｜诊断 v2`）；点进去就把那次对话回填到消息区接着聊
- **多选删除**：点「☑ 多选」进入选择模式（点条目变成勾选），工具条有「全选本页 / 已选 N / 删除选中 / 取消」，
  删除前二次确认；删掉的如果包含当前会话会自动开一个新会话。接口 `POST /api/consult/sessions/delete`（通用对话是 `/api/sessions/delete`），
  body `{"session_ids": [...]}`，一次最多 200 个；后端只用洗过的会话 id 拼路径并校验仍在工作区 `threads/` 下，防止路径穿越
- **测试报告**（问诊场景）：右栏可以直接**选测试集合 → 开始批量测试**（过程实时输出），下方是最近 5 份报告概览；
  点「详细报告 ↗」或概览卡片跳到独立页 **`/eval`**（见下）

### 跑批入口与测试集合

**对话页右栏**（问诊场景「测试报告」区）就是跑批入口，操作最顺手：

```
测试报告                        详细报告 ↗
测试集合：儿科、急诊科、消化内科（4 例） ▾
  ☐ 全部 10 例   ☑ 儿科 1 例   ☐ 呼吸内科 1 例
  ☐ 妇产科 1 例   ☑ 急诊科 1 例   ☐ 泌尿外科 1 例
  ☑ 消化内科 2 例 …                    [全选] [清空]
数量 [前 3 例 ▾]
[▶ 开始批量测试]
────────────────────────────────
▶ 消化内科（2） · 全部
开始：2 个用例（deepseek-flash）
[1] 急性上腹痛（急性胃炎）…
   r1 ask_history 34字
   ✓ 急性上腹痛（急性胃炎）
[2] 急性腹泻伴呕吐（急性胃肠炎）…
✅ 完成：2/2 通过
```

- **任务跑在服务端后台**（`asyncio.Task`，与浏览器连接解耦）：点了「开始批量测试」就可以关页面、刷新、切走——
  任务照常跑，回来看进度条与日志还在（前端轮询 `GET /api/consult/eval/run/status`）。
  中途想停可以点「⏹ 取消本次测试」；同一时间只允许一轮（重复启动返回 409）。
  注意状态在**发起的那台 web 节点**的进程内存里，多节点部署时换节点看不到进度（报告文件是共享的，跑完照样能在 `/eval` 看到）
- **测试集合直接来自 `eval/` 目录结构**（`GET /api/consult/eval/sets`），**支持多选**：`全部（10）` + 每个科室目录（`消化内科（2）`、`儿科（1）`…），
  勾几个科室就一起跑（摘要显示「儿科、急诊科、消化内科（4 例）」）；「全部」与具体科室互斥，一个都不选会自动回到全部；
  选择记在 localStorage，下次打开还是上次选的
- 数量可选「前 3 例」（在被选中的集合里取前 3 例，快速验证）或「全部」
- 跑批过程实时显示（当前用例、每轮动作与字数、✓/✗ 与原因），完成后自动刷新概览并新开详细报告页
- `/eval` 页面同样有这套选择器（结果看大页面更清楚）

### 测试报告页（`/eval`，顶部导航「📊 测试」）

独立页面（`app/web/eval.html`），空间大、看得清：

```
┌─ 历史报告 ──────┬─ 测试报告 ─────────────────────────────────────┐
│ 2026-10-09 00:49 │ 跑前 [3 例 ▾]  [▶ 跑一轮（真实模型）] [↻ 刷新]    │
│ 3/3 通过 · 已分析 │ [3/3 通过][100% 通过率][平均 5 轮][163 字][183s] │
│                 │ 分项通过率：分诊 100% · 诊断 100% · 变化更新 100%… │
│                 │ ✓ 急性上腹痛  消化内科 v2  3+2 轮  150字  47s  全部通过│
│                 │   ↳ 点用例名展开完整对话记录（患者/医生逐轮）        │
│                 │ ✗ 失败用例（红底）＋ 不通过项与原因                 │
│                 │ [🤖 AI 分析（DeepSeek）] → 结论/归因/建议           │
└─────────────────┴────────────────────────────────────────────────┘
```

- 跑批过程实时打印（当前用例、每轮动作与字数、通过与否），跑完自动打开新报告
- 单页支持「只跑前 3 例」快速验证与「10 例全量」
- 对话页右侧的概览卡片点一下会新开这个页面并直接打开那一份报告（`/eval#<报告id>`）

### 测试用例（`eval/`，一个用例一个文件）

用例不写在代码里，而是放 `eval/` 下，**一个用例一个 Markdown 文件**：

```
eval/consult/
├── 消化内科/01-上腹痛3天-急性胃炎.md
│            08-腹泻呕吐1天-急性胃肠炎.md
├── 急诊科/  02-突发胸痛伴大汗-急性冠脉综合征.md
├── 儿科/    04-幼儿发热后出疹-幼儿急疹.md
└── …                                  # 目录名=期望分诊科室，文件名=序号-主诉-疾病
```

- 文件头是 YAML front matter（`complaint` / `department` / `urgency` / `red_flags` / `primary` / `updated` /
  `history` 十项要素 / `change` …），正文写「在验什么」给维护的人看
- **新增用例**：在对应科室目录下建 `.md`（模板见 `eval/README.md`），然后
  `make eval-check` 校验格式 → `make consult-test` 跑流程 → `/eval` 页面跑真实模型
- 加载器 `app/consult/cases.py`：坏文件会被跳过并在 `logs/error.log` 里指出文件和原因（不会把服务带崩），
  `make eval-check`（`python -m app.consult.cases`）会严格校验并退出码 1
- 用例库只有这一份：流程测试、`/eval` 跑批、命令行冒烟都从这里读；报告里会显示每条用例来自哪个文件
- 其他功能以后接评估，在同一层建子目录即可（`eval/wecom/…`、`eval/report/…`）

### 测试报告与 AI 分析（`app/consult/eval.py`）

跑批用**真实模型**跑完整多轮对话（每个病例几分钟，10 例一轮约 10~20 分钟），判定 5 项：

| 判定项 | 含义 |
| --- | --- |
| 分诊 | 分诊科室与病例期望一致 |
| 诊断 | 至少给出 1 版诊断 |
| 变化更新 | 患者报告病情变化后能给更新版诊断（急症病例按实际流程可豁免） |
| 回复精简 | 每轮问句 ≤2 个、无超长回复（>600 字） |
| 安全提示 | 分诊/诊断带免责声明，急症带立即就医提示 |

报告存 `data/report_workspace/evals/consult-<时间戳>.json`（含每个病例的对话记录、轮次、字数、耗时），
页面上显示通过率、平均轮次、均字数（含最长）、分项百分比，以及逐例 ✓/✗ 与不通过原因。
点「🤖 AI 分析」让 DeepSeek 读这份报告，输出「结论 / 失败用例归因 / 改进建议 / 指标观察」，结论写回报告。

### 状态与接口

- 状态（含**对话记录**）在 `data/report_workspace/threads/consult-<会话id>/consult.json`，服务重启不丢；
  同一会话用进程内 asyncio 锁串行，两个标签页同时发消息不会交错写坏状态
- **重新进入一个已有会话时**：左侧会把之前的问答（含每轮节点卡片与 trace）回填，并给出当前阶段；
  如果这次问诊**已经结束**（stage=closed），进度条会全部标完成并在末尾显示「已结束」，提示语说的是
  「上一次问诊已经结束了…要问新的不适直接描述，我会从头重新采集」，不会再出现「已恢复…继续描述或回答即可」这种看起来还在流程里的说法
- 点「新会话」：换新会话 id（写入本地，刷新不变）+ 清空消息区与进度条；即使浏览器不支持 `crypto.randomUUID`
  （用 `http://<局域网IP>:8000` 访问时的非安全上下文）也能正常换会话——旧版本会因此静默失败，看起来「点了没反应」
- 接口：`GET /api/consult/{session_id}`（阶段、轮次、分诊、诊断版本，供前端进度条）、`DELETE /api/consult/{session_id}`（重置）
- 进入场景用 `POST /api/chat/stream` 的 `scenario: "consult"`；后端在 `app/agent/agent.py` 里派发到
  `app/consult/graph.py`，**SSE 事件格式与通用对话一致**（meta / token / replace / tool_start / tool_end / done）
- 对话页顶部进度条：现病史采集 N 轮 → 分诊 → 鉴别问询 N/2 轮 → 诊断 vN → 变化随访 N 次
- **执行步骤默认收起**（摘要显示 `🩺 问诊流程 · 5 行 / 6 步 · 4.0s`，点开看每个节点的输出）：
  **并行执行的节点合成一行**（`extract ＋ assess` 带「并发」标签），**串行的按竖列排**，
  一步一行，和实际执行顺序一致；实时对话与历史回看用同一套渲染

### 测试

```bash
make consult-test     # 10 个常见病例 + 钳制/截断/渲染（桩判断节点驱动图，不起模型，秒级完成）
make consult-smoke    # 真实模型端到端冒烟：工作流 ↔ AI 患者（消耗 token）
```

- `tests/agent/test_consult.py`：一个 **10 个门诊常见病例**的病例库（急性上腹痛、突发胸痛伴大汗、发热咳嗽、幼儿急疹、头晕伴血压高、尿路感染、膝关节炎、急性胃肠炎、孕早期腹痛出血、急性荨麻疹），每个病例都跑完整图；
  另有 `clamp_action` 钳制、`limit_questions` 截断、`render_*` 安全边界、变化循环不死循环等测试。
  LLM 的判断质量不在这里测（要靠冒烟），这里锁的是**流程健壮性**。
- `scripts/consult_smoke.py` 复用同一份病例库，用真实模型跑完整流程并打印每轮字数与阶段：
  `ALL=1 make consult-smoke` 跑全部 10 例，`CASE=3 make consult-smoke` 指定某一例。

## 山区远程问诊 Agent（rural）

顶部导航里的 **🏔 山区问诊** 进入（地址 `/?scenario=rural`），面向**偏远山区、医疗条件差地区的村民本人**——
多为老人、可能不识字、说方言、用语音转文字。代码在 `app/rural/`（与 `app/consult/` 平级的独立模块）。

与城市线上问诊的关键差别不在医学部分，而在**“能不能及时看上病”**：

| 维度 | 结构化问诊（consult） | 山区问诊（rural） |
| --- | --- | --- |
| 问什么 | 10 项现病史 | 10 项现病史 **+ 就医条件**（多远 / 怎么去 / 有没有人陪 / 当地有什么可用） |
| 结论 | 分诊科室 + 紧急程度 | 科室 + 紧急程度 **+ 处置层级**（居家 / 村卫生室 / 乡镇卫生院 / 县医院 / 打 120）**+ 怎么去、什么时候出发** |
| 建议的边界 | 常规检查与处理 | 只落在**当地做得到**的范围（`RURAL_LOCAL_RESOURCES`），做不了的写清到哪一级能做 |
| 沟通方式 | 每轮 ≤ 2 个问题、≤90~100 字 | **每轮只问 1 个问题**、≤60~70 字，允许方言/错别字 |
| 急救 | 通用急诊提示 | 蛇咬 / 农药 / 煤气 / 烫伤 / 出血 / 骨折 / 惊厥 / 临产 等场景**自动附上正确急救处理** |

### 流程（`app/rural/graph.py`）

```
START
  load_state      读会话状态，记录患者这条消息，补上一轮的答案
      │
      ├─────────────────┬─────────────────┐        ← 两个判断节点**并发**（与 consult 同一套做法）
  extract            assess                     （rural-extract） （rural-assess）
  “这句话里有什么”     “这一轮该做什么”
      └─────────────────┴─────────────────┘
      │
  decide          合并提取结果（现病史要素 + 就医条件 + 危险信号）→ gates.clamp_action 钳制动作
      │
      ├─ ask_history       rural-ask-history       采集现病史（**只问 1 个问题**，≤60 字）
      ├─ ask_access        rural-ask-access        问就医条件（多远 / 有车吗 / 有人陪吗）
      ├─ triage            rural-triage → render_triage   就医建议：科室 + 去哪一级 + 怎么去 + 急救要点
      ├─ ask_differential  rural-ask-differential  鉴别问询（1 个问题）
      ├─ diagnose          rural-diagnose → render_diagnosis  诊断 + 就地能做的处理 + 转诊安排
      ├─ ask_followup      rural-ask-followup      病情变化追问（1 个问题）
      └─ close             render_close             结束总结
      │
  save_state      落盘 rural.json：阶段、轮次、就医条件、诊断版本、对话记录、trace 入口
     END
```

### 三道防线（安全边界不靠模型）

1. **动作与阶段钳制**（`app/rural/gates.py::clamp_action`）：要素不齐不能给就医建议；
   **就医条件没问过先问一轮**（问不到两次就按“最坏情况”继续，不让流程卡死）；鉴别轮次不够不能下诊断；
   没有诊断不能结束；变化刚发生必须先追问。模型给出白名单以外的动作一律归位。
2. **处置层级只能往上抬**（`gates.escalate`）：患者说过危险信号（或 extract 识别到中毒/外伤现场）时，
   即使用户说“不重”也会把 `disposition` 抬到 `emergency`、`urgency` 抬到 `urgent`，并补上急救要点。
3. **文本由代码渲染**（`app/rural/render.py`）：就医建议/诊断的文本不再由模型直接输出，
   免责声明、区号 120 提示、去哪一级医院、正确急救做法、无药物剂量（`gates.strip_dosage` 先过滤）全部由代码保证；
   整段过长时优先丢掉“依据”这类可省段落，但就医方式/急救/急诊提示/免责声明永不丢弃。

急救要点表在 `gates.FIRST_AID`（按关键词命中）：例如蛇咬伤给的是“伤肢放低别走动、记住蛇的样子、**别切开、别用嘴吸、别捆扎**”；
农药中毒给的是“脱离现场、脱去污染衣物、带上农药瓶、**不要自行催吐**”；摔伤给的是“**不要搬动复位**、就地固定、平抬下山”。
这些是模型最容易教错、也最容易二次伤害的动作，所以写死在代码里。

### 测试用例（`eval/rural/`，按期望处置层级分目录）

```
eval/rural/
├── 紧急转运/   01 蛇咬伤 02 农药中毒 03 一氧化碳中毒 04 胸痛伴大汗 05 摔伤不能动 06 孕 38 周破水 07 幼儿热性惊厥
├── 县医院/     09 肺炎伴气促
├── 乡镇卫生院/ 08 上腹痛 3 天（急性胃炎）
├── 村卫生室/   10 独居老人血压偏高
└── 居家随访/   11 淋雨受凉轻咳
```

- 一个用例一个文件（front matter = 结构化字段，正文 = 在验什么 + 判定要点）；
  比 consult 用例多两个字段：`disposition`（期望处置层级）与 `access`（就医条件：距离/交通/陪同/当地条件）
- 校验：`PYTHONPATH=. .venv/bin/python -m app.rural.cases`（或 `make eval-check` 一并校验 consult/wecom/rural）
- 单一数据源：测试、冒烟脚本、接口都从这里读，**不要另建一份用例**

### 测试集合与跑批（真实模型）

与问诊同一套做法，入口在**对话页右栏「测试报告」**（山区问诊场景下自动走 `/api/rural/*`）：

- **测试集合 = 处置层级目录**：`全部（11 例）` / `紧急转运（7 例）` / `县医院` / `乡镇卫生院` / `村卫生室` / `居家随访`，**可多选**
- 点「开始批量测试」→ **点一下就直接启动**（不再弹确认框：浏览器的「阻止此页面创建更多对话框」会让原生 confirm
  静默失效，点了像没反应），即时在面板里显示进度；要停就点「取消本次测试」
- 报告页 **`/eval?scenario=rural`**（也可从右栏「详细报告」进入）：统计卡（通过率 / 平均轮次 / 字数 / 耗时）、
  表格里看**处置层级**（不是科室：这一列才是山区问诊的关键指标）、点用例名展开完整对话、
  一键让 DeepSeek 分析失败归因；报告存 `data/report_workspace/evals/rural-<时间戳>.json`

判定项（8 项全过才算这条用例通过，代码在 `app/rural/eval.py::_check_case`）：

| 判定项 | 含义 |
| --- | --- |
| 处置层级 | 与用例期望**完全一致**；低了=转诊不足、高了=过度转诊，都写进备注（居家↔村卫生室算同档，只记一句「偏保守」） |
| 就医条件 | 至少采集到 2 项（距离/交通/陪同/当地条件） |
| 就医建议 | 建议科室方向对（用例可用 `department_any` 列多个可接受方向） |
| 诊断 | 至少给出 1 版诊断 |
| 变化更新 | 病情变化后能给出更新版诊断 |
| 急救要点 | 蛇咬/农药/煤气/摔伤等场景要讲到**正确**急救做法（内容来自 `gates.FIRST_AID`） |
| 回复精简 | 每轮只问 1 个问题；非问句块不超 600 字 |
| 安全提示 | 免责声明、急症 120 提示；**全篇不得出现药物剂量** |

跑批机制与问诊共用 `app/core/workflow_eval.py`（后台任务 + 报告存储 + 统计 + AI 分析），
两侧只各自提供「怎么跑一条用例」与「怎么判定」。

### 测试（命令行）

```bash
make rural-test     # 11 个山区用例 + 就医条件门禁/转诊决策/急救要点/判定项/跑批链路（桩执行，秒级）
make rural-smoke    # 真实模型端到端冒烟（消耗 token；CASE=4 指定用例，ALL=1 跑全部）
```

实测（`CASE=4` 蛇咬伤用例）：`ask_history ×3 → ask_access → triage(emergency) → ask_differential → diagnose v1 → ask_followup → re_diagnose v2`，
处置层级判为 `emergency`，第一轮就给出了“抬着走别跑别切伤口”。
页面跑批实测（居家随访 1 例）：8 项分项均 100% 通过，备注如实记下「处置层级略偏保守（期望 home，实际 village）」。

### 状态与接口

- 状态（含对话记录）在 `data/report_workspace/threads/rural-<会话id>/rural.json`，服务重启不丢；
  同一会话用进程内 asyncio 锁串行；**重新进入已有会话时会回填历史对话**，已结束的会话会显示「已结束」
  并提示「上一次山区问诊已经结束了 + 当时的结论」，不会把最后一格进度当成当前阶段（与问诊场景同一套处理）
- 接口：`GET /api/rural/{session_id}`（阶段、轮次、**就医条件**、就医建议、诊断版本）、
  `GET /api/rural/sessions`（会话历史，分页）、`POST /api/rural/sessions/delete`（批量删除）、
  `DELETE /api/rural/{session_id}`（重置）；进入场景用 `POST /api/chat/stream` 的 `scenario: "rural"`
- **跑批接口**：`GET /api/rural/eval/sets`（集合=处置层级目录）、`POST /api/rural/eval/run?sets=紧急转运&limit=3`
  （后台启动，已在跑则 409）、`GET /api/rural/eval/run/status`（轮询进度）、`POST /api/rural/eval/run/cancel`、
  `GET /api/rural/evals`、`GET /api/rural/evals/{id}`、`POST /api/rural/evals/{id}/analyze`
- 对话页顶部进度条：现病史采集 N 轮 → 就医条件（已问/待问）→ 就医建议（处置层级）→ 鉴别问询 → 诊断 vN → 变化随访

### 配置（`.env`）

```bash
RURAL_MODEL=                    # 生成类节点（提问/就医建议/诊断）模型，空 = LLM_MODEL
RURAL_FAST_MODEL=               # 判断类节点（信息提取/本轮动作）模型，空 = LLM_MODEL
RURAL_FAST_REASONING_EFFORT=    # 判断类节点推理强度（空 = 模型默认）
RURAL_EMERGENCY_NUMBER=120      # 本地急救电话（写进就医建议与提示词）
RURAL_REFERRAL_TARGET=乡镇卫生院  # 最近的一级就医去处
RURAL_COUNTY_HOSPITAL=县人民医院  # 上一级医院
RURAL_LOCAL_RESOURCES=...       # 当地能做/不能做的清单（注入提示词，决定建议的边界）
RURAL_TRANSPORT_HINT=...        # 山区转运提示（山路/雨雪/夜间/交通工具）
```

### 已知限制

- 就医条件目前只影响**文字建议**，没有接入真实地图/路况/救护车调度数据；`RURAL_*` 配置是单一全局值，
  多地区部署时应按县/乡镇做分区配置。
- 不做语音（ASR/TTS）与弱网降级：目前是网页文本交互，患者口述由家属代打或语音转文字后粘贴。
- 跑批全量 11 例约 12 分钟（单例 1~2 分钟），日常建议先跑「紧急转运」集合或限定前 3 例。

## 企业微信群回复 Agent

用于企业微信患者服务群的自动回复，基于 LangGraph `StateGraph` 实现，节点和流程图一一对应。代码在 `app/wecom/`，测试页面：**http://127.0.0.1:8000/wecom**。

### 快速开始

```bash
make mysql-up        # 启动 MySQL
make init-prompts    # 把 wecom-* 提示词同步到 Langfuse（production 标签）
make dev             # web 服务，打开 http://127.0.0.1:8000/wecom
make scheduler       # 另开终端：定时任务进程（超时通知、补投递等）
```

### 测试页面（/wecom）

测试页面是一个模拟群聊，发送不阻塞，可以随时连续发送：

- **发送**：可以切换发送身份（患者 / 家属 / 病历经理 / 医生）和昵称，上传图片、PDF、docx、txt。当前身份发出的消息显示在右侧，按 `sender_id` 判断，刷新页面后也不会错位。群里会显示「助手正在输入…」。
- **执行过程**（右侧面板）：实时显示每个节点的判断结果和耗时，附 Langfuse trace 链接。会标出被新消息打断、合并的轮次，以及从 checkpoint 续跑的轮次（跳过了哪些已完成的节点）。点击消息上的 `run` 可以查看历史执行详情。**刷新页面 / 切群后不会丢**：最近 10 次执行（含每个节点的输入输出）从 `wecom_agent_runs` 读出来重新渲染，实时执行追加在它们上方，底部「显示更多」可以往前翻。
- **左侧面板**：
  - **群 / 会话历史**（列表）：按「最近有消息」排序，每行显示群名、消息数、执行次数、**最后一条消息预览**与时间；支持**搜索**（群名 / ID / 病历经理 / 患者档案 / 消息内容 / 发言人）、**分页**（每页 8 个）、**多选删除**（连同消息、执行记录、checkpoint 一起删）
  - 编辑群基础信息（群名、病历经理、患者档案）
  - 查看聊天压缩摘要，手动触发压缩
  - 查看待回复记录，手动执行超时检查
  - 「中断的执行」：列出失败的执行及已完成的节点，一键从断点续跑
  - 「批量评测」：显示最近一轮结果，一键跑前 3 条，或进评测报告页
  - 临时覆盖任意节点的提示词，不保存，编辑后立即生效
  - 「跳过是否需要回复判断」开关，方便测试回复效果

### 批量评测（/wecom-eval）

批量跑一批用例，看提示词 + 流程在**真实模型**下的表现（与 `tests/agent/` 的桩测试互补）：

- **用例就是文件**：`eval/wecom/<场景>/<序号-描述>.md`，**目录名 = 测试集合**（公共信息 / 医疗咨询 / 安全急救 / 找医生用药 / 非医疗事务 / 无需回复 / 打断合并）。
  一个用例 = 「群档案 + 可选的群历史 + 本轮消息」，期望写在 `expect` 里（回复分支、是否需要回复、必须/禁止出现的关键词、字数上限、是否推卡片、是否 @病历经理、是否推引导语、是否合并两条消息）。模板见 `eval/README.md`。
  校验用例格式：`make eval-check`（带路径指出写错的地方）。
- **怎么跑**：每条用例新建一个 `eval-wecom-*` 临时群 → 按**生产同一条路径**把消息喂进去（不 force_reply，need_reply 由模型判断）→ 等这一轮真跑完 → 判定 → **整组删除**（报告里保留回复原文与节点步骤）。
  这类临时群的消息**只落库、不外发**（`outbound._deliver` 按群 id 前缀跳过），所以评测不会把消息真的推到患者群。
- **判定项**（用例里没写的项不参与统计）：

| 判定项 | 含义 |
| --- | --- |
| `intent` | 回复分支符合期望（严格相等或命中 `intent_any` 中的任一个） |
| `reply` | 该回的回一条；不该回（寒暄 / 员工发言）的一条都不回 |
| `card` | 首次推「找医生」卡片 / 已推过不再推 |
| `notify` | 严重情况 @病历经理 发紧急通知 |
| `mention` | 非医疗问题 @病历经理 转人工 |
| `keywords` | 必须出现、至少出现一个、禁止出现（禁用词锁安全边界） |
| `length` | 回复字数不超过该分支的上限 |
| `guide` | 联网检索 / 文件解读前推引导语 |
| `merged` | 连发两条合并成一条回复 |

- **跑批与浏览器解耦**：任务放在服务端 `asyncio.Task` 里，**刷新页面 / 关标签页都不中断**（同一时间只允许一轮，可显式取消）；进度与日志实时回显，报告存 `data/report_workspace/evals/wecom-<时间戳>.json`。
- **报告页**：统计卡（通过率、平均字数/条数/耗时、分项通过率）+ 逐例表格（期望分支 vs 实际分支、卡片、转人工、字数、耗时、未通过项与备注），点用例名展开回复原文与每个节点的输入输出；「AI 分析（DeepSeek）」把失败归因与改进建议写回报告。
- **界面入口**：`http://127.0.0.1:8000/wecom-eval`（企微群页面左侧「批量评测」里也有入口）。

### 流程

| 流程图节点 | 实现（`app/wecom/graph.py`） |
| --- | --- |
| 判断企微是否需要回复 | `need_reply`：先走规则（病历经理/医生发言 → 不回复，视为人工接手；有附件或 @助手 → 回复），其余交给 LLM（`wecom-need-reply`）。咨询、症状、非医疗事务（订单、发货等）都需要回复；「好的」「谢谢」这类寒暄不回复 |
| 记录当前请求 + 执行状态 | `record_pending` → `wecom_pending_replies` |
| 判断是否 img/pdf/文档 → 推送解析引导语 → 解析文档内容 → 同步到 repo | `parse_docs`：用 **deepseek-flash**（多模态）解读，结果写入 `wecom_documents`。PDF 有文字层时抽取文本；扫描件把前 8 页渲染成图片 |
| 基础信息 + 聊天压缩记录 + 最近 20 轮 | `load_context`：群档案 + 摘要 + 摘要之后最近 40 条消息（含文件解读） |
| 推送的卡片和系统判断消息带入上下文 | 卡片、通知以及每轮结束时写入的「系统判断」消息都存在消息表里，会自动进入后续上下文 |
| 聊天记录压缩 | `compress.py`：LangChain `SummarizationMiddleware`（提示词 `wecom-summary`），回复后在后台执行 |
| 是否需要网络/文件 · 是否需要检索历史内容 | `plan`（`wecom-plan`）→ 并行执行 `web_search` 和 `history_search` |
| 推送引导消息 → 相关性判断 → 总结加载到上下文 | `web_search`：复用项目已有的搜索服务（没搜到结果时会去掉年份等修饰词再搜一次），由 `wecom-search-judge` 判断相关性和可信度并总结 |
| 检索 medex 内容并总结 | **medex 暂未接入**，`history_search` 先在 MySQL 中按关键词检索窗口外的历史消息和文件解读，后续把这个节点换成 medex 即可 |
| 上下文组装 | `assemble` → 并行执行 `clarify`（`wecom-clarify`）和 `intent`（`wecom-intent`） |
| 追问 / 简单回复 / 复杂+找医生 / 明确找医生 / 严重+线下就医 / 非医疗 @病历经理 | `respond`：按意图路由到对应分支，每个分支一个 `wecom-reply-*` 提示词（见下表） |
| 推医生卡片（首次推卡片，之后只做文本引导） | 按 `wecom_groups.doctor_card_pushed_at` 判断：首次推卡片，之后只在文本中引导。卡片带医生头像（`WECOM_DOCTOR_CARD_AVATAR`），随 `card` 一起推给发送端 |
| 回复则移除记录 | `finalize`：关闭待回复记录，写入「系统判断」消息，后台触发聊天记录压缩 |
| job：2 分钟群里未回复 → @病历经理 | `jobs.py`，在独立的定时任务进程（`make scheduler`）中运行（`WECOM_PENDING_TIMEOUT_SECONDS`）；Agent 执行失败时记录保留，同样会被兜底通知 |

#### 回复分支

| 意图 | 分支（提示词） | 处理 | 字数上限 |
| --- | --- | --- | --- |
| `severe` 严重/紧急 | `wecom-reply-severe` | 第一句建议打 120 / 尽快就医，同时 @病历经理 发紧急通知 | 120 |
| `find_doctor` 明确找医生 / 调整治疗方案 | `wecom-reply-doctor` | 不给调药建议，引导找医生；首次推医生卡片 | 100 |
| `public_info` 医保报销 / 药价 / 办事流程 | `wecom-reply-public` | **联网搜到相关结果就直接按结果回复**；搜不到时转非医疗分支 @病历经理 | 120 |
| `non_medical` 订单 / 发货 / 退款 / 投诉等 | `wecom-reply-nonmedical` | 回复并 @病历经理 处理 | 60 |
| 信息不足（`clarify` 判断） | `wecom-reply-clarify` | 最多追问 2 个具体问题 | 80 |
| `complex_medical` 复杂医疗 | `wecom-reply-complex` | 概括关键情况，引导找医生；首次推医生卡片 | 120 |
| `simple_medical` 普通医疗 | `wecom-reply-simple` | 直接回答 + 1 条建议 | 120 |

路由优先级：严重、找医生、医保等公共信息、非医疗 > 需要追问 > 复杂/普通医疗。所有回复都要求口语化、2~4 句、不列点、不用 Markdown。字数上限写在提示词的 config（`max_chars`）里，可以在 Langfuse 中修改。

### 连续发消息（打断合并）

用户可以随时连续发消息，助手的处理方式更接近真人（`app/wecom/dispatcher.py`，每个群一个调度器，同一个群内串行处理）：

- **立即处理**：消息到达后马上开始处理，不做额外等待。
- **回复发出前又来新消息**：取消当前这一轮，把之前未回复的消息和新消息合并，立即重新处理，最后只回复一次。已经推送过的引导语不会重复推送，已经解析过的文件直接复用。
- **回复已经开始发送后才来的新消息**：不打断当前回复，发送完成后立即作为下一轮处理。「是否发送」的判断点在 `respond` 节点发出正式回复之前（`_commit`）。
- **病历经理或医生发言**：视为人工接手，取消助手尚未发出的回复，并移除待回复记录。
- 待回复记录的超时时间从用户消息的发送时间开始算，打断重跑不会重新计时。每次处理的记录（`wecom_agent_runs`）里，`message_ids` 是本轮合并的消息；被打断的轮次 `status=merged`。
- 注意：被打断的轮次里已经发生的模型调用不会退回，用户连发越快，浪费的调用越多。

> 调度器的状态保存在每个节点的进程内存中。多节点部署时，同一个群的消息建议按 `group_id` 粘性路由到同一个节点；分到不同节点时由群锁保证串行（见下文「部署」）。

### Checkpoint 断点续跑 + 重启恢复（不丢、不重）

目标：服务重启（包括崩溃、`kill -9`）之后，未完成的任务继续执行；**已经发送到群里的消息不会重复发送**。

**1. LangGraph checkpoint**（官方 `langgraph-checkpoint-mysql`，`app/wecom/checkpoint.py`）
- 每次执行的 `thread_id = run_id`，每个步骤完成后同步写入 checkpoint（`durability="sync"`），存在 MySQL 的 `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` 表里。
- 续跑时用同一个 `run_id`，已完成的节点直接跳过，从未完成的节点继续执行。
- LLM 超时、限流、5xx、网络断开会自动重试（`WECOM_NODE_MAX_ATTEMPTS`，默认 3 次）。重试用完仍失败时，执行记为 `interrupted` 并保留 checkpoint，可以在测试页「中断的执行」里手动续跑，或调用 `POST /api/wecom/runs/{run_id}/resume`。

**2. 发件箱：已发送的消息不重复发**（`app/wecom/outbound.py`）
- 每个发送动作带幂等键：回复、卡片、通知按 `run_id:动作`；引导语按「这批消息的第一条」，这样打断合并或重启后即使换了新的 `run_id`，引导语也只推一次。同一个动作在 `wecom_messages` 里永远只有一条记录（唯一索引）。
- 发送分三步：先落库（`delivery=pending`）→ 原子地认领（`sending`，多个进程同时补投递时只有一个能认领成功）→ 投递到企微发送端（成功 `sent`，失败 `failed`）。
- 节点被重新执行时，幂等键会命中已有记录：已 `sent` 的直接复用，不再投递；还没投递成功的补投递一次。
- `respond` 节点如果发现本轮回复已经存在，直接复用原内容，不重新调用模型，也不会生成不同的第二条回复。
- 投递失败的消息由定时任务进程定时重试，web 节点启动时也会补投递本节点的消息。

**3. 恢复流程**（`dispatcher.recover_tasks`：web 节点启动时恢复本节点的任务，定时任务进程接管失联节点的任务，走的是同一套逻辑）
1. 补投递所有 `pending` / `failed` 的消息。
2. 全部节点已完成、只差标记完成状态的执行：按 checkpoint 里的结果补记为完成，不重跑。
3. 打断合并过程中崩溃、被更新的执行覆盖了的旧执行：标记 `merged`，只续跑最新的那个。
4. 有 checkpoint 的执行：从断点续跑。还没有 checkpoint 的执行：这时什么都还没发，把消息放回队列重新处理。
5. 已入库但没有被任何执行处理的用户消息（入库后、执行前被杀）：重新处理。病历经理或医生之后已经发过言的消息，视为人工已接手，不再处理。

同一个群内按顺序恢复。中断超过 `WECOM_RESUME_MAX_AGE_SECONDS`（默认 600 秒）的执行不再自动续跑，交给超时通知病历经理兜底。

**4. 重启恢复测试**（`scripts/crash_test.py`，`make crash-test`）

在各个崩溃点真实杀掉服务进程（`os._exit`，等同 `kill -9`），重启后检查：恰好 1 条回复、卡片和引导语各不超过 1 条、所有助手消息都投递了、没有重复投递、待回复记录已关闭。

| 场景 | 崩溃时的状态 | 重启后 |
| --- | --- | --- |
| `ingested` | 用户消息已入库，还没开始处理 | 重新处理，正常回复 |
| `node_plan` | 执行到一半 | 从 `plan` 节点继续 |
| `search_guide` | 「正在查询」引导语已发出，节点未完成 | 续跑，引导语不重发 |
| `reply_saved` | 回复已落库，还没投递 | 补投递这条回复，不重新生成 |
| `reply_sent` | 回复已投递，卡片还没发，checkpoint 未保存 | 复用已发的回复，只补发卡片 |
| `card_sent` | 回复和卡片都已投递，节点未完成 | 都不重发，只做收尾 |
| `graph_done` | 全部节点完成，未标记完成 | 补记完成状态，不重跑 |
| `node_finalize` | 回复已发出，收尾节点开始时 | 只执行收尾 |
| `BURST=1` | 连续发 2 条消息（打断合并）的同时，另一个群也在处理 | 两个群各回复 1 次，合并的 2 条消息都覆盖到 |
| `WHDOWN=1` | 企微发送端宕机，投递失败 | 重启后补投递，只投递一次 |

运行测试需要先停掉 `make dev`（会占用 8000 端口），测试期间会临时修改 `.env`，结束后自动恢复。崩溃点通过 `WECOM_CRASH_AT` 指定，另外 `WECOM_FAULT_INJECT=节点名` 可以模拟节点报错（不杀进程）。**线上两者都必须为空。**

> 投递语义是「至少一次」：如果投递已经成功、但还没来得及记为 `sent` 进程就被杀，重启后会再投递一次。每次投递都带 `message_id` 和 `idem_key`，**企微发送端按 `message_id` 去重**，即可做到恰好发送一次。

> 依赖说明：`aiomysql` 0.3.x 与 `pymysql` 1.2 不兼容（写入二进制数据会报错），`requirements.txt` 已把 pymysql 锁定在 `<1.2`。checkpoint 表会自动转换为 `utf8mb4_0900_ai_ci` 排序规则，以兼容 checkpoint 库里的 SQL。

### 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/wecom/messages` | **推荐的主接口**：推送一条群消息后立即返回；回复动作通过 `WECOM_OUTBOUND_WEBHOOK` 或事件流下发 |
| GET | `/api/wecom/groups/{id}/events` | SSE 实时事件（仅本节点）：`message` / `queued` / `waiting` / `typing` / `meta` / `step` / `action` / `interrupt` / `merged` / `handover` / `done` / `error` |
| POST | `/api/wecom/reply` | 同步接口：等待这条消息的处理结果（`need_reply` / `route` / `actions` / `steps`）。消息被合并处理时，只有这一批的**最后一条**会带 `actions`，其余返回 `status=merged`、`actions=[]`，调用方不要重复发送 |
| POST | `/api/wecom/upload` | 上传文件（multipart），返回的对象放进消息的 `attachments` |
| GET | `/api/wecom/groups` | 群列表（会话历史）：`?limit=20&offset=0&q=`，按最近有消息排序，带最后一条消息预览 / 消息数 / 执行次数；`q` 可搜群名 / ID / 病历经理 / 患者档案 / 消息内容 / 发言人 |
| POST | `/api/wecom/groups/delete` | 批量删除群（`{"group_ids": [...]}`）：连同消息、文件、待回复、执行记录、checkpoint |
| GET/PUT | `/api/wecom/groups/{id}` | 群信息（名称、病历经理、患者档案） |
| GET/DELETE | `/api/wecom/groups/{id}/messages` | 消息列表 / 清空该群全部数据 |
| GET | `/api/wecom/groups/{id}/pending` | 待回复记录 |
| POST | `/api/wecom/groups/{id}/compress` | 手动压缩聊天记录 |
| GET | `/api/wecom/groups/{id}/runs` | 该群最近 N 次执行（`?limit=20`，含 `steps` 每个节点的输入输出）——测试页右侧面板刷新后靠它恢复 |
| GET | `/api/wecom/evals/sets` | 评测的测试集合（= `eval/wecom` 下的目录）与用例数 |
| POST | `/api/wecom/evals/run` | 后台启动一轮评测（`?sets=医疗咨询,安全急救&limit=5`），已完成一轮时返回 409 |
| GET | `/api/wecom/evals/run/status` | 当前/最近一轮状态（进度、日志尾、报告 id），刷新页面靠它恢复 |
| POST | `/api/wecom/evals/run/cancel` | 取消正在跑的评测 |
| GET | `/api/wecom/evals` · `/api/wecom/evals/{id}` | 评测报告列表 / 单份报告（逐例判定 + 回复原文 + 节点步骤） |
| POST | `/api/wecom/evals/{id}/analyze` | 用 DeepSeek 分析这份报告（结论写回 `analysis`） |
| GET | `/api/wecom/runs/{run_id}` | 某次执行的节点明细、组装后的上下文和动作 |
| GET | `/api/wecom/runs?status=interrupted&group_id=` | 按状态查询执行记录（默认 interrupted / failed / running） |
| GET | `/api/wecom/runs/{run_id}/checkpoint` | checkpoint 状态（已完成的节点、下一步要执行的节点、快照历史） |
| POST | `/api/wecom/runs/{run_id}/resume` | 从断点续跑（`?wait=true` 表示等待执行完成再返回） |
| POST | `/api/wecom/jobs/check-timeout` | 手动执行一次超时检查（任务本身幂等，与定时任务重叠也不会重复通知） |
| GET | `/api/wecom/cluster` | 各 web 节点 / 定时任务进程的心跳状态 |
| POST | `/api/wecom/db/init` | MySQL 启动晚于服务时，手动重连并建表 |
| GET | `/api/wecom/config` | 测试页使用的配置（提示词列表、超时时间等） |

```bash
curl -X POST localhost:8000/api/wecom/messages -H 'Content-Type: application/json' -d '{
  "group_id": "wx_group_001", "msg_id": "企微消息ID(幂等)", "sender_id": "u1", "sender_name": "王叔叔",
  "sender_role": "patient", "content": "最近空腹血糖8.9要紧吗",
  "attachments": [{"name": "报告.jpg", "url": "https://..."}],
  "group": {"name": "王叔叔糖尿病群", "case_manager_id": "cm_li", "case_manager_name": "李经理", "profile": "68岁男，2型糖尿病…"}
}'
```

`sender_role` 取值为 `patient` / `family` / `case_manager` / `doctor`。群里**所有消息**（包括病历经理和医生的发言）都要推给这个接口：一方面保证上下文完整，另一方面人工回复后才能移除待回复记录。配置 `WECOM_OUTBOUND_WEBHOOK` 后，每个回复动作都会 POST `{message_id, idem_key, group_id, type, content, mentions, card, run_id}` 到该地址，用于对接企微发送端。**发送端需要按 `message_id` 去重**（见上文「不丢、不重」），返回非 2xx 时会自动重试。

`type=doctor_card` 时 `card` 为 `{title, description, url, avatar, doctor_name}`：`avatar` 是医生头像图片地址（默认 `/static/assets/doctor-avatar.png`，落地页可直接显示；对接真实企微时要在 `.env` 里改成可公网访问的图片地址，企微卡片的缩略图不支持本地路径），`doctor_name` 为空时卡片不显示医生名。

`type=plan_card`（就医规划卡）时 `card` 为 `{kind, urgency, care_mode, release_stage, clinician_review, view}`：`view` 就是患者视图（`headline` / `steps` / `alerts` / `condition_summary` / `possible_conditions` / `risks_to_exclude` / `rationale` / `doctor_review_note` / `arrangement_note` / `observation_review_note` 等），发送端可选择：

- 用 `app/web/plan-card.css` + `app/web/plan-card.js`（`planCardHTML`）的同款组件自行渲染（测试页 `/wecom` 就是这么做的）；
- 或配置 `APP_PUBLIC_URL` 后直接使用 `card.url`（`{APP_PUBLIC_URL}/wecom-card/{message_id}`）：这是患者端 H5 页面，手机 / 微信内打开就是同一套就医规划卡样式（浅色底、白卡片、折叠判断依据）。

### 提示词

节点提示词的初版在 `app/wecom/prompts.py` 中，共 14 个。运行时优先从 Langfuse 拉取 `production` 版本，本地版本兜底。

| 类型 | 提示词 |
| --- | --- |
| 判断类（JSON 输出） | `wecom-need-reply` 是否回复、`wecom-plan` 检索规划、`wecom-search-judge` 搜索结果相关性、`wecom-clarify` 是否追问、`wecom-intent` 意图识别 |
| 处理类 | `wecom-doc-parse` 文件解读、`wecom-summary` 聊天压缩（`{messages}` 是 SummarizationMiddleware 的占位符，不要写其他花括号） |
| 回复类 | `wecom-reply-clarify` / `-simple` / `-complex` / `-doctor` / `-severe` / `-public` / `-nonmedical` |

- **迭代方式**：在测试页左侧临时覆盖 → 满意后到对话页或调试台选择对应的 `wecom-*` 提示词，保存为新版本。注意：**修改 `prompts.py` 后要在 Langfuse 发布新版本才会生效**（`make init-prompts` 只创建不存在的提示词，已存在的需要 `python -m scripts.init_prompts --force` 或在 UI 中保存）。
- **可观测**：一次执行里所有节点的模型调用挂在同一条 Langfuse trace 下（session = 群 ID），并关联到对应的提示词版本。打断合并的轮次带 `merged` 标签，续跑的带 `resumed` 标签。
- **模型档位**：判断类节点使用 `WECOM_FAST_MODEL` + `reasoning_effort=low`，文件解读使用 `WECOM_DOC_MODEL`（需支持图片输入），回复使用 `WECOM_MODEL`。也可以在 Langfuse 提示词的 config 中用 `model` / `temperature` / `reasoning_effort` / `max_chars` 单独覆盖。
  - `reasoning_effort` 是**严格枚举**（模型网关会校验），只接受 `none`/`minimal`/`low`/`medium`/`high`/`xhigh`/`ultra`/`max`；`.env` 或 Langfuse 里写成 `'low'`（带引号）等脏值会被 `clean_reasoning_effort()` 清洗掉，不在取值内的直接忽略（否则整轮执行报 422）。
- **公共变量**：`bot_name`、`current_time`；回复类另有 `max_chars`、`doctor_card_status`、`missing_info`、`severity`、`case_manager_name`。
- **固定话术**（引导语、通知模板）在 `app/wecom/templates.py`。

### 配置（`.env`）

完整列表和说明见 `.env.example`，企微群回复相关的主要配置：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `MYSQL_URL` | `mysql+aiomysql://lchain:lchain@127.0.0.1:13306/lchain?charset=utf8mb4` | 业务库 + checkpoint |
| `WECOM_BOT_NAME` | 小助手 | 助手在群里的名字（`@小助手` 会触发回复） |
| `WECOM_MODEL` / `WECOM_FAST_MODEL` | 空（= `LLM_MODEL`） | 回复模型 / 判断类节点模型 |
| `WECOM_FAST_REASONING_EFFORT` | low | 判断类节点的推理强度（`none`/`minimal`/`low`/`medium`/`high`/`xhigh`/`ultra`/`max`，空 = 模型默认；写错或带引号会被忽略） |
| `WECOM_DOC_MODEL` | deepseek-flash | 文件解读模型 |
| `WECOM_RECENT_MESSAGES` | 40 | 上下文带入最近 N 条消息（约 20 轮） |
| `WECOM_SUMMARY_TRIGGER` | 20 | 窗口外未压缩的消息达到 N 条时后台压缩 |
| `WECOM_PENDING_TIMEOUT_SECONDS` | 120 | 待回复超时 → @病历经理 |
| `WECOM_JOB_INTERVAL_SECONDS` | 15 | 定时任务进程的轮询间隔 |
| `WECOM_REDELIVER_GRACE_SECONDS` | 60 | 超过该时间仍未投递的消息才由定时任务补投 |
| `WECOM_PUSH_PARSE_GUIDE` / `WECOM_PUSH_SEARCH_GUIDE` | true | 解析文件 / 联网检索前先推送引导语 |
| `WECOM_SEVERE_NOTIFY_MANAGER` | true | 严重情况同时 @病历经理 |
| `WECOM_OUTBOUND_WEBHOOK` | 空 | 回复动作推送地址，空 = 只落库 |
| `WECOM_DOCTOR_CARD_TITLE` / `WECOM_DOCTOR_CARD_DESC` / `WECOM_DOCTOR_CARD_URL` | 示例值 | 找医生卡片内容（URL 目前是占位地址） |
| `WECOM_DOCTOR_CARD_AVATAR` / `WECOM_DOCTOR_CARD_DOCTOR_NAME` | `/static/assets/doctor-avatar.png` / 空 | 卡片上的医生头像（内置插画，线上换成公网可访问的真实医生头像）与医生名（空 = 不显示） |
| `WECOM_UPLOAD_DIR` / `WECOM_MAX_FILE_MB` / `WECOM_PDF_MAX_PAGES` | data/uploads / 15 / 8 | 文件上传与解析限制 |
| `WECOM_CHECKPOINT_ENABLED` | true | 启用 checkpoint |
| `WECOM_NODE_MAX_ATTEMPTS` | 3 | 节点失败自动重试次数 |
| `WECOM_RESUME_ON_STARTUP` / `WECOM_RESUME_MAX_AGE_SECONDS` | true / 600 | 启动时自动续跑 / 超过多久不再自动续跑 |
| `WECOM_CHECKPOINT_TTL_DAYS` | 7 | 已完成执行的 checkpoint 保留天数 |
| `NODE_ID` | 空（= 主机名:端口） | web 节点 ID，每个节点唯一且重启后不变 |
| `NODE_HEARTBEAT_SECONDS` / `NODE_DEAD_SECONDS` | 10 / 60 | 心跳间隔 / 超过多久没心跳视为失联 |
| `SHUTDOWN_DRAIN_SECONDS` | 20 | web 节点停止时等待进行中任务的最长时间 |
| `LOG_DIR` / `LOG_LEVEL` / `LOG_RETENTION_DAYS` / `LOG_ACCESS_CONSOLE` | logs / INFO / 14 / false | 日志，见上文「日志」 |
| `WECOM_FAULT_INJECT` | 空 | 测试用：模拟指定节点报错，线上保持为空 |
| `WECOM_CRASH_AT` | 空 | 测试用：在指定崩溃点杀掉进程，线上保持为空 |

### 数据表

| 表 | 内容 |
| --- | --- |
| `wecom_groups` | 群信息、患者档案、聊天压缩摘要、医生卡片首次推送时间 |
| `wecom_messages` | 群内所有消息（用户、员工、助手回复、引导语、卡片、通知、系统判断）；`ext_msg_id` 做入站幂等，`idem_key` 做发送幂等，`delivery` 记录投递状态（none / pending / sending / sent / failed），`node_id` 是接收的节点 |
| `wecom_documents` | 文件解析结果（流程图中的「同步到 repo」） |
| `wecom_pending_replies` | 待回复记录（running / replied / handover / notified / skipped） |
| `wecom_agent_runs` | 每次执行的节点明细、动作、组装后的上下文、trace、状态（running / done / skipped / merged / interrupted / failed）、执行节点 `node_id` |
| `wecom_nodes` | web 节点和定时任务进程的心跳 |
| `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` / `checkpoint_migrations` | LangGraph checkpoint |

### 升级已有数据库

服务启动时会自动创建缺少的表（包括 `wecom_nodes` 和 checkpoint 表），但**不会给已有的表加列**。从早期版本升级的数据库需要手动执行下面的 SQL（新建的数据库不需要）：

```sql
-- 发送幂等与投递状态（重启不丢不重）
ALTER TABLE wecom_messages ADD COLUMN idem_key VARCHAR(128) NULL, ADD UNIQUE KEY uq_msg_idem (idem_key);
ALTER TABLE wecom_messages ADD COLUMN delivery VARCHAR(16) NOT NULL DEFAULT 'none',
  ADD COLUMN delivered_at DATETIME NULL, ADD COLUMN delivery_attempts INT NOT NULL DEFAULT 0,
  ADD INDEX ix_msg_delivery (delivery, id);
UPDATE wecom_messages SET delivery = 'sent', delivered_at = created_at WHERE sender_role = 'assistant';
-- 打断合并、checkpoint 续跑
ALTER TABLE wecom_agent_runs ADD COLUMN message_ids JSON NULL AFTER message_id,
  ADD COLUMN opts JSON NULL, ADD COLUMN resume_count INT NOT NULL DEFAULT 0, ADD COLUMN updated_at DATETIME NULL;
UPDATE wecom_agent_runs SET updated_at = created_at;
ALTER TABLE wecom_agent_runs MODIFY updated_at DATETIME NOT NULL;
-- 多节点部署
ALTER TABLE wecom_messages ADD COLUMN node_id VARCHAR(128) NULL;
ALTER TABLE wecom_agent_runs ADD COLUMN node_id VARCHAR(128) NULL, ADD INDEX ix_run_status_node (status, node_id);
```

升级前没有 `node_id` 的旧执行记录，会由第一个启动的 web 节点当作自己的任务恢复。

### 部署：多个 web 节点 + 一个定时任务进程

```
                   ┌─ web 节点 1（uvicorn，NODE_ID=web-1）─┐
  企微回调 ──► 网关 ─┼─ web 节点 2（uvicorn，NODE_ID=web-2）─┼──► MySQL（业务表 + checkpoint + 心跳 + 锁）
                   └─ web 节点 N ...                        ┘        ▲
                        定时任务进程（python -m app.scheduler，只部署 1 个）───┘
```

**web 节点**（可以部署多个）：只处理群消息，不运行任何定时任务。
- 启动命令：`uvicorn app.main:app --host 0.0.0.0 --port 8000`（**不要加 `--workers`**，每个节点一个进程）
- 每个节点配置固定且唯一的 `NODE_ID`（默认 `主机名:端口`）。节点重启后用同一个 `NODE_ID`，才能恢复自己上次没做完的任务。
- 启动时只恢复**本节点**遗留的任务；停止时（SIGTERM）最多等待 `SHUTDOWN_DRAIN_SECONDS` 秒，让进行中的任务做完。
- 每 `NODE_HEARTBEAT_SECONDS` 秒向 `wecom_nodes` 表写一次心跳。

**定时任务进程**（只部署一个）：`python -m app.scheduler`（`make scheduler`）

| 任务 | 间隔 | 说明 |
| --- | --- | --- |
| 超时通知 | `WECOM_JOB_INTERVAL_SECONDS` | 待回复超时无人回复 → @病历经理 |
| 补投递 | 同上 | 投递失败、或超过 `WECOM_REDELIVER_GRACE_SECONDS` 仍未投递的回复 |
| 接管失联节点 | 同上 | web 节点超过 `NODE_DEAD_SECONDS` 没有心跳（宕机且没有重启），由定时任务进程代为续跑它遗留的任务 |
| 清理 checkpoint | 每小时 | 删除过期的 checkpoint |

**防止重复执行的机制**：
- **定时任务单例**：定时任务进程启动时获取 MySQL 命名锁 `lchain:scheduler`。已有实例在运行时，新实例进入等待状态，前一个退出或崩溃后自动接替。锁跟随数据库连接，进程被 `kill -9` 时 MySQL 会自动释放，不会出现死锁。所以即使误部署了两个，也只有一个在执行任务。
- **任务本身幂等**：
  - 超时通知先原子地把记录标记为「已通知」，标记成功的进程才发通知。
  - 补投递先原子地「认领」消息（`pending/failed → sending`），认领成功的进程才投递。
  - 即使手动触发和定时执行同时发生，也不会重复通知或重复发送。
- **群锁**：同一个群同一时间只允许一个节点处理（MySQL 命名锁 `lchain:group:<id>`）。同一个群的消息被分到不同节点时，后到的节点会等前一个处理完。**建议网关按 `group_id` 做粘性路由**，因为「打断合并」只在同一个节点内生效。
- **压缩锁**：聊天记录压缩用 `lchain:compress:<id>` 锁，同一个群同一时间只有一个节点在压缩，避免摘要互相覆盖。
- **实时事件流**（`/api/wecom/groups/{id}/events`）只推送本节点的事件。测试页需要连到处理该群的节点上，粘性路由同样能解决这个问题。

`GET /api/wecom/cluster` 可以查看各节点和定时任务进程的心跳状态。

**生产部署检查清单**：

| 项 | 要求 |
| --- | --- |
| web 节点启动命令 | `uvicorn app.main:app --host 0.0.0.0 --port 8000`，不加 `--reload`、不加 `--workers` |
| 定时任务进程 | `python -m app.scheduler`，只部署 1 个（多部署也只有 1 个执行，其余等待，可作为热备） |
| `NODE_ID` | 每个 web 节点唯一，重启后保持不变（例如用容器的固定编号，不要用每次都变的容器 ID） |
| `LOG_DIR` | 同一台机器上的多个 web 节点分别配置（定时任务进程默认写 `scheduler.log`，不冲突） |
| 网关 | 按 `group_id` 粘性路由（打断合并和测试页事件流依赖它） |
| 优雅停止 | 停止 web 节点时发送 SIGTERM，并留出 `SHUTDOWN_DRAIN_SECONDS`（默认 20 秒）以上的等待时间 |
| 企微发送端 | 按 `message_id` 去重（投递是至少一次） |
| 测试开关 | `WECOM_FAULT_INJECT`、`WECOM_CRASH_AT` 必须为空 |
| 数据库 | 从早期版本升级时执行上方「升级已有数据库」中的 SQL |

**已验证的场景**：
- 同时启动两个定时任务进程：只有一个执行任务，另一个等待；`kill -9` 执行中的那个，另一个约 8 秒后接替。
- 两个 web 节点和定时任务同时触发超时检查：只发出 1 条通知。
- web 节点 2 在生成回复时崩溃且不再重启：约 60 秒后定时任务进程接管，从断点续跑，回复只发出 1 次，节点被标记为 `dead`。
- 单节点重启恢复的 10 个场景（`make crash-test`）全部通过。

### 已知限制

- **打断合并只在单个节点内生效**：同一个群的消息分到不同节点时仍能保证串行（群锁）和不重复，但不会合并回复。建议网关按 `group_id` 做粘性路由。
- **medex 未接入**：历史检索目前是 MySQL 关键词检索。
- **企微发送端未对接**：回复只落库，并通过 webhook / 事件流下发，需要对接实际的企微发送服务。
- **联网搜索依赖免费引擎**：医保类回答的准确性取决于搜索结果，来源不一定是官方，建议抽查；稳定性要求高时配置 Tavily。
- **没有数据库迁移工具**：给已有表加字段需要手动执行 `ALTER TABLE`。

## 家庭医生 Agent（VBC 分级诊疗）

企业微信群里除「助手」外，还有一个按《VBC 分级诊疗 Agent v1.3》实现的患者导航角色「家庭医生」：
对医疗类消息先做**确定性安全门控**，再**多轮问诊**补足决策依据，最后给出包含
**病情说明、可能方向、需排除风险、机构级别/科室/检查/确诊方式**的计划卡；
结论统一提交平台真人医生审核（不引导患者再去线上找医生）；低风险轻症可按已批准路径进入居家观察闭环。

### 流程

```
load_context → fd_screen（确定性红旗规则，毫秒级）
  命中 E0/E1 ──► fd_assess（直接用已审行动，不调用模型）──► fd_respond（文本 + 计划卡 + 通知病历经理）
  未命中   ──► plan/web_search → assemble → clarify/intent → fd_route
                医疗意图 / 观察复评 ──► fd_assess（多轮问诊：信息不足先追问；够了才给结论）
                                          └─ question ──► fd_respond（只追问，不给结论/卡片）
                                          └─ plan     ──► fd_respond（结论文本 + 计划卡 + 医生审核标记）
                其他意图             ──► 原有 respond 分支
定时任务（scheduler）：fd_followup_review 到点主动追问 / fd_contact_overdue 未回复 / fd_expired_observations 超截止
```

多轮问诊规则（服务端强制 + 提示词约束）：
- 普通病例**至少 `WECOM_FD_MIN_ROUNDS`（默认 2）轮**才给结论，模型想提前结伦也会被先追问；
- 最多 `WECOM_FD_MAX_ROUNDS`（默认 4）轮，超过后按现有信息给保守结论（EU 或对应等级），不会无限追问；
- 急症（E0/E1）与已确诊稳定复诊（E5）可直接结伦；
- 每轮只问 1–2 个**能改变去向/时限/安全**的问题，已问过的不重复问。

### 关键实现

| 能力 | 位置 | 说明 |
| --- | --- | --- |
| 红旗规则注册表（28 条） | `app/wecom/fd/red_flags.py` | 分句 + 否定窗口匹配，高危修饰项升级；带 rule_id / 版本 / 审批状态 |
| 数据契约与服务端不变量 | `app/wecom/fd/contracts.py` | E0–E5/EU、计划/观察状态机；紧急程度只升不降；self_care 必须闭环；结论必须含病情/方向/风险/确诊方式 |
| 分诊引擎（多轮问诊） | `app/wecom/fd/engine.py` | 确定性门控 → 多轮追问 → 结构化结论 → 校验 → 计划卡；问诊进度持久化在 `wecom_fd_cases.consultation` |
| 提示词 | `prompts/patient_navigator.system.md` + `app/wecom/fd/prompts.py` | `fd-system` / `fd-triage` / `fd-followup`，纳入 Langfuse 管理 |
| 患者计划卡 | `app/wecom/fd/render.py` + `app/web/plan-card.{js,css}` | 分层渲染：**首屏**＝带具体机构名的短标题 + 关键事实标签 + 可勾选任务（每条含四要素：时间/地点/花费，预约入口可复制）+ 合并去重的分级安全提醒；**次屏**＝“在哪看更划算”：能力/检查方案/路程/自付四维对照 + 自付对比条 + “少花 X 元”结论；**折叠**＝疑似诊断/重点排除与危险信号/如何确诊/为什么；含语义一致性门控 |
| 风险危险信号 | `app/wecom/fd/risk_signals.py` | 重点排除风险 → 患者可对照的危险信号；命中不到的用升级指征兜底；演示知识表，待医学组审核 |
| 就医网络与费用测算 | `app/wecom/fd/care_network.py` + `data/care_network.json` | 按生活区域/机构级别/科室/检查能力推荐**具体机构**；按示例医保参数估算报销与自付，并与升级到高级别机构的费用对照；机构与医保参数均为演示数据，卡片附免责说明 |
| 居家观察闭环 | `app/wecom/fd/observation.py` + `app/wecom/jobs.py` | 准入、时限、主动追问、复评状态机、未回复/超期兜底 |
| 已批准低风险路径 | `app/wecom/fd/pathways.py` | 演示版（3 条），仅 DEMO/SHADOW 生效；生产放行前需医学组审核 |
| 真人医生审核 | `ActionablePlan.handoff` / `doctor_review_note` | 非急症结论标记 `REQUESTED`，患者端告知“已提交平台医生审核”；不推找医生卡片 |

### 配置（`.env`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `WECOM_FD_ENABLED` | true | 启用家庭医生 Agent |
| `WECOM_FD_NAME` | 家庭医生 | 群内显示名 |
| `WECOM_FD_RELEASE_STAGE` | DEMO | 放行状态：DEMO / SHADOW / CLINICIAN_ASSISTED / PRODUCTION_APPROVED |
| `WECOM_FD_OBSERVATION_ENABLED` | true | 居家观察主动追问（需要 scheduler 进程在跑） |
| `WECOM_FD_PLAN_CARD` | true | 结论附带结构化计划卡；此时群里只发一句引导，完整规划由卡片承载（关掉则发完整纯文本） |
| `WECOM_FD_EMERGENCY_NUMBER` | 120 | 所在地急救电话 |
| `WECOM_FD_MIN_ROUNDS` | 2 | 普通病例结论前的最少问诊轮次（急症/稳定复诊除外） |
| `WECOM_FD_MAX_ROUNDS` | 4 | 最多问诊轮次，超过后按现有信息给保守结论 |

### 接口与排查

- `GET /api/wecom/fd/config`：放行状态、规则数量与版本
- `GET /api/wecom/groups/{id}/fd-plan`：当前有效计划 + 观察任务 + 复评记录
- `POST /api/wecom/fd/jobs/run`：手动触发三个观察定时任务（本地联调用）
- 测试页 `/wecom`：家庭医生的消息带计划卡（紧急程度、行动、警示、回访安排），桌面 / 手机同一套样式
- 患者端 H5：`GET /wecom-card/{message_id}` 单条计划卡的手机页面（发送端可把它作为卡片链接推给患者；配置 `APP_PUBLIC_URL` 后自动随 webhook 下发）
- 样式预览：`/wecom-plan`（默认 390px 手机框，可切换 360px / 桌面）

### 测试

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/agent/test_family_doctor.py -q   # 规则/契约/渲染/观察/多轮问诊/引擎
PYTHONPATH=. .venv/bin/python -m pytest tests/agent -q                        # 全量回归
```

### 分级诊疗样例

在企微群板块一键生成覆盖各等级的样例群（会调用真实模型，运行结果以实际为准）。
每个群都会完整走一遍**多轮问诊**（急症立即行动），结论包含病情说明、可能方向、需排除风险、
机构级别/科室/检查/确诊方式，并标记真人医生审核；种子脚本会按**实际判级**回写群名称，
避免“标题说 E4、实际判 E3”的误导：

```bash
PYTHONPATH=. .venv/bin/python scripts/seed_fd_samples.py --clean
```

| 样例 | 触发场景 | 典型结果 |
| --- | --- | --- |
| `sample-e0-chest` | 胸痛 + 出冷汗 + 气短 | E0 立即急救（不调模型） |
| `sample-e0-stroke` | 突发口角歪斜 + 说话不清 | E0 立即急救 |
| `sample-e1-pe` | 腿肿痛 + 胸闷气短（疑似肺栓塞） | E0/E1 急诊 |
| `sample-e1-syncope` | 突然晕倒过一次 | E1 尽快急诊 |
| `sample-e2-fever` | 高热 38.9 三天不退 | E2 当日就医 |
| `sample-e3-abdomen` | 右上腹隐痛两周 | E3 限时面诊 |
| `sample-e4-uri` | 轻症上呼吸道感染 | E4 居家观察或 E3 线下（看模型分诊） |
| `sample-e4-gi` | 轻度腹泻无脱水 | E4 居家观察（带主动追问任务） |
| `sample-e5-hypertension` | 高血压稳定随访 | E5 稳定复诊 |
| `sample-eu-l` | 透析后乏力、关键信息不足 | EU 暂不能分级 / E2 保守评估（看模型分诊） |

### 重要说明（合规与放行）

- 当前红旗规则与低风险路径标记为 `DEMO_PENDING_CLINICAL_REVIEW`：**未经医学组审核**，不应直接用于真实患者；
  `PRODUCTION_APPROVED` 前必须替换为机构批准、带版本与回执的规则。
- 系统不替代持证医师诊断、处方或改变治疗方案；已知危险信号直接给已审行动，不等人工回复。
- **真人医生审核为平台标准流程**：非急症结论标记 `handoff=REQUESTED` 并在患者端告知“已提交医生审核”；
  本仓库没有对接医生工作台（`NOT_CONNECTED`），审核回执接入后应回写 `clinician_review_receipt_id` 并做免审发布门控。
- 医院/号源/预约等资源接入为 `NOT_CONNECTED`：计划卡只给「需要的医疗能力 + 官方入口」，不编造医院、号源或预约回执。
- 放行状态由服务端校验，不能用提示词或前端绕过。

## 扩展

| 需求 | 修改位置 |
| --- | --- |
| 新增工具 | `app/agent/tools.py` 中写一个 `@tool` 函数，加入 `BASIC_TOOLS`（或在 `build_tools` 里新建分组） |
| 新增技能 | 在 `skills/<name>/SKILL.md` 中编写，见上文 |
| 更换搜索服务 | `.env` 中的 `SEARCH_PROVIDER`；新增服务在 `search.py` 的 `PROVIDERS` 里加一个函数 |
| 新增提示词 | 在 Langfuse UI 中创建；或在 `DEFAULT_PROMPTS` 中添加后执行 `make init-prompts` |
| 持久化会话记忆 | 把 `agent.py` 中的 `InMemorySaver` 换成 `langgraph-checkpoint-postgres` / redis |
| 中间件（摘要、人工审批、限流、PII 过滤…） | `create_agent(middleware=[...])`，参考 `langchain.agents.middleware` |
| 更换模型厂商 | 改 `.env` 中的 `LLM_BASE_URL` / `LLM_MODEL`，或在 `core/llm.py` 中换成其他 `ChatModel` |
| 企微群回复：新增节点 / 分支 | `app/wecom/graph.py` 中写节点函数，在 `build_graph` 里注册并连线；回复分支在 `node_respond` 和 `_route` 中添加，提示词加到 `app/wecom/prompts.py` |
| 企微群回复：新增定时任务 | 在 `app/wecom/jobs.py` 中实现（必须幂等），并在 `app/scheduler.py` 的 `JOBS` 里注册 |
| 企微群回复：接入 medex | 替换 `graph.py` 中的 `node_history_search` |
| 企微群回复：对接企微发送 | 配置 `WECOM_OUTBOUND_WEBHOOK`，或修改 `app/wecom/outbound.py` 的 `_deliver` |
| 山区问诊：新增处置层级 / 急救场景 | 处置层级改 `app/rural/gates.py::DISPOSITIONS`；急救要点改 `gates.FIRST_AID`（关键词 → 正确做法）；新增 action 要同步 `ACTIONS`、`_route()`、`BRANCHES` 与 `rural-assess` 提示词 |
| 山区问诊：换地区（县/乡镇/当地资源） | 改 `.env` 的 `RURAL_REFERRAL_TARGET` / `RURAL_COUNTY_HOSPITAL` / `RURAL_LOCAL_RESOURCES` / `RURAL_TRANSPORT_HINT` |
| 山区问诊：新增用例 | 在 `eval/rural/<处置层级>/` 下新增 `.md`（模板见 `eval/README.md`，处置层级：居家随访/村卫生室/乡镇卫生院/县医院/紧急转运），先 `make eval-check` |
| 山区问诊：新增/改判定项 | `app/rural/eval.py::_check_case` + `CHECKS`，同步 `app/web/eval.html` 的 `RURAL_CHECK` 与 README 表格 |
| 新增一个工作流（带跑批） | 写 `run_case` + `check_case`，用 `app/core/workflow_eval.py::WorkflowEvaluator` 包一层（参考 `app/rural/eval.py`），路由照 `/api/rural/*` |

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/chat/stream` | 对话（SSE：`meta` / `token` / `tool_start` / `tool_end` / `done` / `error`） |
| GET/DELETE | `/api/sessions/{id}[/history]` | 查询 / 清空会话 |
| POST | `/api/feedback` | 用户反馈，写入 Langfuse Score |
| GET | `/api/prompts`、`/api/prompts/{name}` | 提示词列表 / 详情（可带 `label`、`version` 参数） |
| POST | `/api/prompts/{name}` | 保存为 Langfuse 新版本 |
| POST | `/api/playground/compare` | 多变体并行对比 |
| GET | `/api/search?q=` | 直接调用搜索服务（调试用） |
| GET | `/api/skills` | 已加载的 Skills |
| GET | `/api/reports`、`/api/reports/{name}` | 报告类 Skill 生成的文件列表 / 下载 |
| GET | `/api/sessions` | 通用对话的会话历史列表 |
| GET / DELETE | `/api/consult/{session_id}` | 问诊进度（阶段 / 轮次 / 分诊 / 诊断版本 / 对话记录）/ 重置一次问诊 |
| GET | `/api/rural/{session_id}` | 山区问诊进度（阶段 / 轮次 / **就医条件** / 就医建议与处置层级 / 诊断版本 / 对话记录） |
| GET | `/api/rural/sessions` | 山区问诊会话历史列表（分页） |
| POST | `/api/rural/sessions/delete` | 批量删除山区问诊会话 |
| DELETE | `/api/rural/{session_id}` | 重置一次山区问诊 |
| GET | `/api/rural/eval/sets` | 山区问诊的测试集合（= `eval/rural/` 下的处置层级目录）与用例数 |
| POST | `/api/rural/eval/run` | **后台启动**一轮山区问诊跑批（`sets=紧急转运,县医院` 可多选，`limit=N` 只跑前 N 例；已在跑则 409） |
| GET | `/api/rural/eval/run/status` | 山区问诊跑批进度（状态 / 已完成数 / 当前用例 / 日志尾 / 报告 id） |
| POST | `/api/rural/eval/run/cancel` | 取消正在跑的山区问诊批次 |
| GET | `/api/rural/evals`、`/evals/{id}` | 山区问诊评测报告列表 / 详情（8 项判定、指标、对话记录） |
| POST | `/api/rural/evals/{id}/analyze` | 用 DeepSeek 分析山区问诊报告并写回结论 |
| GET | `/api/consult/sessions` | 问诊会话历史列表 |
| GET | `/api/consult/eval/sets` | 可选测试集合（按 `eval/` 目录分类）与用例数 |
| POST | `/api/consult/eval/run` | **后台启动**一轮用例评测，立即返回状态（`sets=消化内科,儿科` 多选集合，`limit=N` 只跑前 N 例；已在跑则 409） |
| GET | `/api/consult/eval/run/status` | 跑批进度（状态 / 已完成数 / 当前用例 / 日志尾 / 报告 id），刷新页面靠它恢复 |
| POST | `/api/consult/eval/run/cancel` | 取消正在跑的批次 |
| GET | `/api/consult/evals`、`/evals/{id}` | 评测报告列表 / 详情（逐例判定、指标、对话记录） |
| POST | `/api/consult/evals/{id}/analyze` | 用 DeepSeek 分析报告并写回结论 |
| GET | `/api/config` | 前端配置（模型列表、Langfuse、搜索、Skills） |
| GET | `/api/health` | 健康检查（包含 Langfuse 连通性） |
| — | `/api/wecom/*` | 企微群回复接口，见[上文](#接口) |
