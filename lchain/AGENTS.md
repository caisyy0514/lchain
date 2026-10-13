# AGENTS.md

LChain Agent：基于 **LangChain v1 / LangGraph + FastAPI + Langfuse** 的 AI Agent 工程，包含两部分：

1. **通用对话 Agent**（`app/agent/`）：`create_agent` + 工具（联网搜索、Skills），配套提示词调试台（`app/prompts/`）
2. **企业微信患者服务群回复 Agent**（`app/wecom/`，主要业务）：LangGraph 工作流 + MySQL 存储，支持打断合并、checkpoint 断点续跑、重启不丢不重；部署形态是多个 web 节点 + 一个独立的定时任务进程

面向用户的完整说明见 `README.md`；本文件记录 AI 编码助手修改代码时需要知道的约定。**与用户交流、代码注释、文档一律使用中文。**

## 环境与常用命令

```bash
make install        # 创建 .venv 并安装依赖（新增依赖也可以：uv pip install --python .venv/bin/python <pkg>）
make mysql-up       # MySQL 8.4：127.0.0.1:13306，账号 lchain/lchain，库 lchain（企微功能需要）
make langfuse-up    # 本地 Langfuse：http://localhost:3000（make langfuse-password 查看账号）
make dev            # web 服务（run.py，带热重载）
make scheduler      # 定时任务进程（只部署一个；web 节点不跑任何定时任务）
make init-prompts   # 把本地默认提示词同步到 Langfuse（已存在的跳过；--force 才会发布新版本）
make consult-test   # 问诊工作流自动化测试（桩判断节点，秒级）
make consult-smoke  # 问诊真实模型端到端冒烟（消耗 token）
make rural-test     # 山区问诊工作流自动化测试（桩判断节点，秒级）
make rural-smoke    # 山区问诊真实模型端到端冒烟（消耗 token）
make crash-test     # 企微重启恢复测试（真实杀进程，需先停掉 make dev）
```

| 地址 | 内容 |
| --- | --- |
| http://127.0.0.1:8000 | 对话页（顶部导航：💬 对话 / 🩺 问诊 / 🏔 山区问诊 / 👥 企微群 / 🧪 调试） |
| http://127.0.0.1:8000/?scenario=consult | 结构化问诊场景（与企微群同级） |
| http://127.0.0.1:8000/?scenario=rural | 山区远程问诊场景（面向偏远山区村民本人） |
| http://127.0.0.1:8000/wecom | 企微群回复测试页（模拟群聊） |
| http://127.0.0.1:8000/playground | 提示词调试台 |
| http://127.0.0.1:8000/docs | API 文档 |

- Python 3.12，虚拟环境 `.venv/`；运行项目内脚本用 `PYTHONPATH=. .venv/bin/python ...`
- **日志统一写在 `LOG_DIR`（默认项目下的 `logs/`）**：web 服务写 `app.log`（全部）/ `error.log`（WARNING 及以上）/ `access.log`（HTTP 访问），定时任务进程写 `scheduler.log` / `scheduler-error.log`，按天切分。不要把日志重定向到 `/tmp` 等其他位置，查问题直接看 `logs/`
- 重启服务（控制台输出与日志文件相同，不需要另存）：
  - web：`pkill -f run.py; pkill -f multiprocessing.spawn; nohup .venv/bin/python run.py > /dev/null 2>&1 &`
  - 定时任务：`pkill -f app.scheduler; nohup .venv/bin/python -m app.scheduler > /dev/null 2>&1 &`
- Docker 运行时是 Colima（`make docker`），不是 Docker Desktop
- 大模型：DeepSeek（OpenAI 兼容协议），`.env` 中 `LLM_API_KEY=${DEEPSEEK_API_KEY}`。`deepseek-flash` 支持图片输入（用于文件解读），`deepseek-v4-pro` 不支持

## 目录结构

```
app/
├── main.py            # FastAPI 入口（web 节点）；lifespan 里初始化 MySQL、checkpointer、心跳，恢复本节点未完成的任务
├── scheduler.py       # 独立的定时任务进程（python -m app.scheduler），MySQL 命名锁保证单例
├── api/routes.py      # 通用接口：对话 SSE、提示词、Playground、反馈、搜索调试
├── agent/             # 通用对话 Agent
│   ├── agent.py       # create_agent + ToolCallLimitMiddleware + InMemorySaver（对话记忆不持久化）
│   ├── tools.py       # BASIC_TOOLS / build_tools()
│   ├── search.py      # web_search / fetch_webpage；PROVIDERS = ddgs / tavily / searxng
│   ├── skills.py      # skills/ 目录加载 + load_skill 工具
│   └── report_tools.py / report_styles.py / imagegen.py / ppt_workflow.py / ppt_media.py  # 报告工具；自定义样式；PPT 流程门禁 + 图文素材与渲染验证
├── eval/              # 评估用例（一个用例一个 .md，新增/改用例看 eval/README.md）：consult/<科室>/<序号-主诉-疾病>.md、rural/<处置层级>/<序号-主诉-疾病>.md、wecom/<场景>/<序号-描述>.md
├── consult/           # 结构化问诊工作流（与 app/wecom 平级）
│   ├── graph.py       # LangGraph StateGraph：流程由图决定，模型只输出 action（经 clamp_action 钳制）
│   ├── prompts.py     # 7 个 consult-* 提示词（判断类 JSON / 生成类带 max_chars）
│   ├── llm.py         # call_json（判断）/ stream_text（生成，流式）——通用实现在 app/core/prompt_llm.py
│   ├── gates.py       # 10 项要素、轮次门槛、clamp_action、limit_questions、安全边界
│   ├── state.py       # consult.json 读写（含对话记录，重启不丢）+ 会话列表
│   ├── render.py      # 分诊/诊断/结束语渲染（免责声明与急诊提示由代码保证）
│   ├── cases.py       # 从 eval/consult/**/*.md 加载用例（不再把用例写在代码里）
│   ├── eval.py        # 用例跑批：真实模型 + 通过判定 + 统计 + DeepSeek 分析（机制在 app/core/workflow_eval.py）
│   └── routes.py      # /api/consult/*（状态、会话列表、跑批与报告、AI 分析）
├── rural/             # 山区远程问诊工作流（与 app/consult 平级，服务对象不同：偏远山区村民本人）
│   ├── graph.py       # 与 consult 同一套图结构，多一个 ask_access 分支（问就医条件）
│   ├── prompts.py     # 8 个 rural-* 提示词（老人友好：一次只问 1 个、更短更口语）
│   ├── gates.py       # 复用 consult 的 10 项要素 + 就医条件要素、处置层级、clamp_action、急救要点表
│   ├── render.py      # 就医建议/诊断/结束语渲染（就医路径、急救要点、无剂量由代码保证）
│   ├── state.py       # rural.json 读写（含对话记录与就医条件）+ 会话列表
│   ├── cases.py       # 从 eval/rural/**/*.md 加载用例（目录 = 期望处置层级）
│   ├── eval.py        # 山区问诊跑批：8 项判定（处置层级/就医条件/急救要点…）+ 报告
│   ├── llm.py         # 绑定 RURAL_* 配置的 LLM 调用（实现同 app/core/prompt_llm.py）
│   └── routes.py      # /api/rural/*（状态、会话列表、删除、跑批与报告）
├── prompts/
│   ├── manager.py     # resolve_prompt：override > Langfuse(label/version) > DEFAULT_PROMPTS（含 wecom-*）
│   └── playground.py  # 多变体并行对比
├── core/              # config.py / llm.py / prompt_llm.py（工作流通用 LLM 调用）/ workflow_eval.py（跑批通用机制）/ db.py（SQLAlchemy async）/ langfuse_client.py / logging_setup.py（日志）/ cluster.py（心跳、MySQL 命名锁）
├── wecom/             # 企微群回复 Agent，见下文
└── web/               # 原生 HTML/JS 前端，无需构建：index.html / wecom.html / wecom-eval.html / eval.html / playground.html / icons.js（图标）/ common.js / style.css / assets（内置图片，如找医生卡片头像 doctor-avatar.png）
skills/                # 每个子目录一个 SKILL.md（web-research、news-briefing）
scripts/               # init_prompts.py / run_experiment.py / crash_test.py / mock_webhook.py / consult_smoke.py / rural_smoke.py / langfuse_init.sh
deploy/                # langfuse/、mysql/ 的 docker-compose
logs/                  # 运行日志（LOG_DIR，已加入 .gitignore）
```

## 企微群回复 Agent（`app/wecom/`）

工作流（`graph.py`）：

```
need_reply → record_pending → parse_docs → load_context → plan
  → (web_search ∥ history_search) → assemble → (clarify ∥ intent) → respond → finalize
```

| 文件 | 作用 |
| --- | --- |
| `graph.py` | 节点、路由 `_route()`、`build_graph()`；入口 `run_stream()`（新执行）/ `resume_stream()`（从 checkpoint 续跑） |
| `dispatcher.py` | 每群一个 `GroupWorker`：同群串行（跨节点靠群锁）；回复发出前来新消息则打断、合并后重跑；员工发言视为人工接手；`recover_tasks()` 恢复指定节点遗留的任务 |
| `checkpoint.py` | `langgraph-checkpoint-mysql` 的 `AIOMySQLSaver`，`thread_id = run_id` |
| `outbound.py` | 发件箱：落库（`delivery=pending`）→ 原子认领（`sending`）→ 投递 webhook → `sent`/`failed`；`idem_key` 幂等；`redeliver_pending()` 补投递；`crash_point()` 崩溃演练 |
| `prompts.py` | 14 个 `wecom-*` 提示词初版；`PROMPT_TIER` 决定模型档位（fast / doc / reply） |
| `llm.py` | `call_json()`（判断类，JSON 模式）/ `call_text()`（回复，注入 `max_chars`） |
| `documents.py` | 图片 / PDF / docx / txt 解析，交给 `WECOM_DOC_MODEL` 解读 |
| `compress.py` | 聊天记录压缩（LangChain `SummarizationMiddleware`），跨节点用 MySQL 压缩锁 |
| `jobs.py` | 定时任务（只在 `app/scheduler.py` 进程里跑）：超时通知、补投递、接管失联节点、清理 checkpoint |
| `store.py` / `models.py` | 数据访问 / ORM（`wecom_groups`、`wecom_messages`、`wecom_documents`、`wecom_pending_replies`、`wecom_agent_runs`、`wecom_nodes`）；`list_groups_page()` 是测试页「会话历史」（分页 / 搜索 / 最后一条消息），`delete_groups()` 连消息 + 执行记录 + checkpoint 一起删 |
| `cases.py` | 评测用例加载：从 `eval/wecom/**/*.md` 读（目录 = 测试集合），坏文件跳过并报错 |
| `eval.py` | 评测跑批：临时群跑真实一轮 → 逐项判定 → 统计 → 报告（`data/report_workspace/evals/wecom-*.json`）→ DeepSeek 分析 |
| `routes.py` | `/api/wecom/*`，主接口 `POST /api/wecom/messages` |
| `templates.py` | 固定话术（引导语、通知模板） |

### 必须遵守的约定

- **发送消息只能通过 `graph._emit(state, key=...)`**，不要直接写消息表或调用 webhook。每个动作要有稳定的幂等键：回复、卡片、通知按 `run_id:动作`；引导语用 `key="guide:xxx"`（按这批消息的第一条锚定）。否则 checkpoint 续跑、打断合并、重启恢复时会重复发送。
- **`respond` 节点发正式回复前必须调用 `_commit(config)`**：此后本轮不再被新消息打断。
- **节点必须可以安全地重复执行**（会被自动重试、续跑时重新执行）：有副作用的操作先用幂等键查一下是否已经做过。
- 新增节点：在 `build_graph()` 的节点列表里注册（会自动加上重试策略和崩溃演练点），并返回 `_step()` 记录，测试页右侧面板和 `wecom_agent_runs.steps` 依赖它（面板刷新 / 切群时用 `GET /api/wecom/groups/{id}/runs` 从库里恢复最近几次执行）。
- 新增回复分支：同时修改 `_route()`、`node_respond()`、`INTENTS` / `INTENT_LABEL`，并在 `prompts.py` 中添加 `wecom-reply-*`（config 带 `max_chars`）；`wecom-intent` 提示词里也要加上新意图。
- 回复风格（产品要求）：口语化、简短、2~4 句、不列点、不用 Markdown；每个分支有 `max_chars` 字数上限。
- 医保报销 / 药价 / 办事流程（`public_info`）：联网搜到结果就直接回复，搜不到才转非医疗分支 @病历经理。
- **找医生卡片**：文字、描述、URL、头像、医生名全部来自 `WECOM_DOCTOR_CARD_*` 配置，在 `node_respond` 里组装成 `card` dict 随 `extra` 落库，并原样推给发送端（`card.avatar` 必须能在企微里访问）。默认头像用仓库内置的 `app/web/assets/doctor-avatar.png`（走 `/static`，测试页直接可见）；换头像只改 `.env`，不要写死在页面里。
- **评测临时群**：群 id 以 `store.EVAL_GROUP_PREFIX`（`eval-wecom-`）开头 = 本地评测群，`outbound._deliver` 按前缀**只落库、不外发**（评测绝不能把消息推到真实患者群）；跑完一条用例由 `eval._run_case` 整组删除。用例只写在 `eval/wecom/**/*.md`，**不要另建一份**：测试、跑批、页面都读它。
- 群里所有消息（包括病历经理、医生的发言）都会进入这个流程；员工发言不触发回复，但会关闭待回复记录、取消未发出的回复。

> 对外介绍「业务 Agent 怎么开发」看 `docs/business-agent-guide.md`（以问诊为例的方法论文档，改动流程/评测方式时同步更新它）。

## 结构化问诊场景（`app/consult/`，与 `app/wecom/` 平级）

顶部导航「🩺 问诊」进入（`/?scenario=consult`）。**这是显式工作流，不是通用 Agent 的工具场景**：
一次运行 = 患者发一条消息 → 回一条，`graph.py` 的 StateGraph 决定流程，判断提示词决定「这轮做什么」，
安全文本由 `render.py` 渲染。旧版的 `consult_*` 工具与 `ConsultMiddleware` 已删除，不要再用那套思路改。

- **流程由图决定，模型只说 action**：`extract`（患者说了什么）→ `assess`（这轮做什么）→ 分支节点 → `save_state`。
  模型给出的 action 一律过 `gates.clamp_action` 钳制后才执行；改流程改 `_route()` 与 `NODES`。
- **`extract` 与 `assess` 是并发的**（`load_state` 同时扇出到两个节点，再由纯代码节点 `decide` 汇合）：
  两个判断节点占单轮耗时的 ~73%，并发后单轮 3.99s → 2.62s。代价是 `assess` 看到的是「合并前」状态
  （可能少 1 项要素、不知道本轮的新变化），这两项偏差由 `decide` 调 `clamp_action` 时传入的
  `fresh_change` / `wants_to_stop` 补回来。**不要把 `extract` 的合并逻辑挪回它自己**（那样 `decide` 就看不到新鲜状态了），
  也不要改成串行——`test_extract_and_assess_run_concurrently` 会拦。
- **钳制规则在 `gates.clamp_action`**（模型判断错了也走不歪）：要素不全不能分诊；**要素齐 + 轮次够时不允许再 `ask_history`**（否则永远停在采集阶段）；
  鉴别轮次不够不能诊断；**没有诊断不能 `close`**（除非患者明确要去线下）；变化已追问过时 `ask_followup` 会改成 `re_diagnose`（防无限追问）。
  新增 action 要同时改 `ACTIONS`、`_route()`、`BRANCHES`、`prompts.py` 里的 `consult-assess` 规则。
- **提示词在 `app/consult/prompts.py`**（7 个 `consult-*`），运行时优先读 Langfuse：
  **改完必须 `python -m scripts.init_prompts --force` 才生效**。判断类（`consult-extract`/`consult-assess`）是 JSON 模式、
  提示词里必须出现 “JSON” 字样；生成类用 `config.max_chars` 限字数（`{{max_chars}}` 注入）。
- **回复风格是产品要求**：**问题直接给**——不带编号、不带「好的/再问两个」这类过渡语、不带任何铺垫，
  **每轮 ≤2 个问题且写在同一行**。`gates.limit_questions()` 按**问号**计数（不是按行：按行判断会漏掉「一行写了 3 个问题」的情况）、
  `gates.one_line()` 把多行合并成一行，两者都在 `_stream_reply` 里做，修正后发 `replace` 事件让前端重渲染。改提示词时不要把这几条弄丢。
- **安全边界不靠模型**：分诊/诊断的文本由 `render.py` 拼装（免责声明、急诊提示、无药物剂量）；患者提过危险信号但这一步给了常规就诊时，
  代码把 `urgency` 抬到 `urgent`。改完跑 `make consult-test`。
- **状态**在 `data/report_workspace/threads/consult-<会话id>/consult.json`（含对话记录，服务重启不丢）；
  对话记录在状态里而不是内存 checkpointer，所以判断节点能跨重启看到之前的问答。同一会话有进程内 asyncio 锁。
- **新增病例/改测试**：`tests/agent/test_consult.py` 的 `CASES` 是**唯一病例库**（10 个常见病例），
  `scripts/consult_smoke.py` 复用它跑真实模型。测试用「理想判断节点」的桩替换 LLM，测的是流程而不是模型能力。
  改完跑 `make consult-test`（秒级）和 `make consult-smoke`（消耗 token，可选）。
- **入口**：`app/agent/agent.py` 的 `stream_chat` 里 `scenario == "consult"` 时派发到 `consult.graph.run_stream`，
  SSE 事件格式（meta/token/replace/queued/tool_start/tool_end/done）与通用对话一致。
- **执行步骤的展示**：`app/web/index.html` 的 `makeSteps/updateSteps` 把节点渲染成「默认收起的一块」；
  `PARALLEL_OF` 声明哪些节点是并发的（目前 `extract`/`assess`）——**改了并发结构要同步这里**，
  否则并行节点会各占一行。实时流与历史回看都走这一套，节点名对应 `CONSULT_NODE_LABELS`（改节点名要同步）。
- **同一会话必须串行**（状态文件是读-改-写，`run_stream` 里按 session_id 加 asyncio 锁）；排队时先发 `queued` 事件，
  否则多标签页共用 sid 时用户会以为卡死。**不同会话是并发的**，压测用 `scripts/load_test.py`（默认不同 sid）；
  用浏览器开多标签页压测是错的——浏览器对同域名的 HTTP/1.1 并发上限是 6。
- **不要在异步路径里直接调同步阻塞的东西**：Langfuse 的 `get_prompt` / `auth_check` / `flush`、`requests`、大文件读写。
  提示词走 `resolve_prompt_async()`（线程池）+ `LANGFUSE_PROMPT_CACHE_TTL` 缓存；`flush()` 用 `asyncio.to_thread`。
  LLM 客户端由 `build_llm()` 按参数复用（共享 httpx 连接池），不要每次 new。改完跑一下 `scripts/load_test.py -n 50` 看 p99。
- `app/consult/__init__.py` 里 `graph`/`llm` 是**延迟导入**（`__getattr__`），因为 `app/prompts/manager.py` 会合并
  `app.consult.prompts`，急切导入会形成循环导入。
- **跑批机制是共用的，不要再写一套**：`app/core/workflow_eval.py::WorkflowEvaluator`（后台任务 + 报告读写 + 统计 + AI 分析）。
  `app/consult/eval.py` / `app/rural/eval.py` 只提供 `run_case`（AI 扮患者跑一轮）、`_check_case`（判定）与 `checks`（判定项 key），
  按 `prefix` 区分报告文件（`consult-*.json` / `rural-*.json`）与 `/evals` 列表。
- **跑批是后台任务，不跟着浏览器连接走**（`WorkflowEvaluator.start_run` → `asyncio.Task`，状态在实例的 `_current`）：
  前端只 **POST 启动 + 轮询** `/api/{consult|rural}/eval/run/status`，所以刷新页面不会中断跑批、进度也能恢复。
  **不要改回「用 SSE 跟着请求跑」**——那样用户一刷新就把任务取消了。同一时间只允许一轮（409），可显式取消。
- **跑批入口在对话页右栏**（两种问诊场景都有；山区问诊自动切成 `/api/rural/*`）：集合选择器读 `GET /api/{consult|rural}/eval/sets`
  （= `eval/<场景>/` 下的目录，**支持多选**）、过程实时输出，详细结果看 `/eval`（山区问诊是 `/eval?scenario=rural`）。
  **测试集合就是目录**——新增一个科室 / 处置层级目录，下拉里自动多一项，不需要改代码。
- **用例不写在代码里**：一个用例一个文件，在 `eval/consult/<期望分诊科室>/<序号-主诉-疾病>.md`
  （front matter = 结构化字段，正文 = 给维护者看的说明，模板见 `eval/README.md`）。
  `app/consult/cases.py` 是加载器：坏文件跳过并写 ERROR 日志（带路径），`make eval-check` 严格校验。
  需要的字段：`complaint/patient/department/urgency/red_flags/primary/updated/differentials/evidence/change/change_answer/history`。
  测试、跑批（`eval.py`）、冒烟脚本都从 `CASES` 取，**不要另建一份用例**。
- **接口在 `app/consult/routes.py`**：声明顺序有讲究——`/sessions`、`/evals*` 必须排在 `/{session_id}` 前面，
  否则会被当成 session_id 匹配掉。
- **会话删除**：`state.delete_sessions(ids)` 只走 `session_dir_name()` 洗过的 id 并校验解析后的路径仍在工作区 `threads/` 下
  （防路径穿越，删的是整个 `consult-<id>/` 目录）；前端多选删除后会刷新列表，删到当前会话时自动开新会话。
- **会话历史分页**：`state.list_sessions(limit, offset) -> (rows, total)` 先按目录 mtime 排序、只解析当前页
  （不要改回「全读再切片」，会话多了会变慢）；前端每页 10 条，翻页偏移是 `(page-1)*size`（曾经写成 `page*size` 跳过一页）。
- **测试报告页是 `/eval`**（`app/web/eval.html`，在 `app/main.py` 里注册；山区问诊用 `/eval?scenario=rural`）：
  对话页右侧只放最近 5 份概览，点卡片带场景参数新开页面直达那一份。页面含统计卡、逐例表格、
  点用例名展开完整对话记录、AI 分析按钮。**新增/改判定项要同步改两处**：`eval.html` 的 `CHECK` / `RURAL_CHECK`
  与对应后端的 `_check_case`（页面按 `?scenario=` 切换接口与中文名）。
- **跑批报告**在 `data/report_workspace/evals/*.json`；判定项在 `eval.py::_check_case`，改了判定记得同步
  README 里那张表。AI 分析（DeepSeek）在 `eval.py::analyze_report`，结论写回报告的 `analysis` 字段。
- **左侧栏只显示进度/历史，模型与提示词等配置默认收起**（`<details class="section">`）；顶部导航没有调试台入口，
  `/playground` 仍然可用但只能直接访问 URL。通用对话的会话落盘在 `app/agent/sessions.py`（`threads/chat-<sid>/session.json`），
  `get_history()` 优先读它，所以对话历史重启不丢。

## 山区远程问诊场景（`app/rural/`，与 `app/consult/` 平级）

顶部导航「🏔 山区问诊」进入（`/?scenario=rural`）。面向**偏远山区、医疗条件差地区的村民本人**（多为老人、低文化水平、
可能说方言、用语音转文字）。架构与 `app/consult/` **完全相同**（显式 LangGraph 工作流 + 判断提示词 + 代码钳制 + 代码渲染），
差别在服务对象，所以**千万不要把它合并回 consult 加开关**：要问的东西、要给的结论、说话方式都不一样。

```
load_state → (extract ∥ assess) → decide →
   ask_history | ask_access | triage | ask_differential | diagnose | ask_followup | close → save_state
```

- **比问诊多一问「就医条件」**：`gates.RURAL_FIELDS`（距离 / 交通 / 陪同人 / 当地可用条件）→ 决定 `disposition`
  处置层级（`home` / `village` / `township` / `county` / `emergency`）。`clamp_action` 里有一道门禁：
  现病史齐 + 轮次够但就医条件没问过 → 先 `ask_access`；**问过一轮仍不明确就按最坏情况给建议**（`MAX_ASK_ACCESS`，不让流程卡死）。
- **处置层级只升不降**：`gates.escalate(structured, ...)` 在患者说过危险信号（或 extract 到中毒/外伤现场）时
  把 `disposition` 抬到 `emergency`、`urgency` 抬到 `urgent`，并补上急救要点。不要为了“不过度转诊”而放开这条。
- **急救要点写死在 `gates.FIRST_AID`**（关键词 → 正确做法 + `check_any` 供评测比对）：蛇咬伤“别切开、别用嘴吸、别捆扎”、
  农药中毒“不要自行催吐”、摔伤“不要搬动复位”。这是模型最容易教错、最可能造成二次伤害的部分，
  **不要改成让模型自由发挥**；新增场景改这个表，关键词能命中 `found_red_flags` / `first_aid_scene` 即可。
- **一次只问一个问题**：`gates.MAX_QUESTIONS_PER_TURN = 1`，`_stream_reply` 里用 `limit_questions(text, 1)` + `one_line()` 兜底；
  生成类提示词的 `max_chars` 是 60~70。改提示词不要把这两条弄丢（老人一次问两个基本答不全）。
- **无药物剂量由代码过滤**：诊断/就医建议的 `local_management` / `local_actions` 先过 `gates.strip_dosage()`，
  命中剂量的条目直接剔除并记 WARNING（不是报错）。判提示词时不要绕过这一步。
- **文本由 `render.py` 拼装**：就医路径（去哪一级、怎么去、带什么）、急救要点、120 提示、免责声明都不靠模型；
  块过长时 `_assemble` 先丢“依据”这类可选段落（`TRIM_OVER_CHARS`），**就医方式/急救/急诊提示/免责声明永不丢**，
  所以急症块仍可能到 800 字左右（安全优先于精简，这是有意的）。
- **提示词在 `app/rural/prompts.py`**（8 个 `rural-*`），改完必须 `python -m scripts.init_prompts --force` 才生效；
  `rural-extract` / `rural-assess` 是 JSON 模式（提示词里必须有 “JSON” 字样），生成类靠 `config.max_chars` 限字数。
- **模型与当地条件配置**：`RURAL_MODEL` / `RURAL_FAST_MODEL` / `RURAL_FAST_REASONING_EFFORT` /
  `RURAL_EMERGENCY_NUMBER` / `RURAL_REFERRAL_TARGET` / `RURAL_COUNTY_HOSPITAL` / `RURAL_LOCAL_RESOURCES` / `RURAL_TRANSPORT_HINT`。
  换地区（另一个县）只改 `.env`，不要改代码。
- **状态**在 `data/report_workspace/threads/rural-<会话id>/rural.json`（含对话记录与就医条件，重启不丢），
  同会话进程内 asyncio 锁串行；接口 `/api/rural/*`（声明顺序同 consult：`/sessions*`、`/eval*`、`/evals*` 要排在 `/{session_id}` 之前）。
- **用例在 `eval/rural/<处置层级>/<序号-主诉-疾病>.md`**（目录 = 测试集合 = 期望处置层级，共 5 档）；
  比 consult 多两个必填字段：`disposition`、`access`；可选 `department_any`（可接受的科室方向列表，轻症用例写多个，
  避免因“全科 vs 呼吸内科”这种学名差异误判）。`app/rural/cases.py` 加载，`make eval-check` 校验。
- **跑批判定在 `app/rural/eval.py::_check_case`（8 项）**：处置层级（**完全一致才算过**；居家↔村卫生室算同档，
  低层=转诊不足、高层=过度转诊都记备注）/ 就医条件（≥2 项）/ 就医建议方向 / 诊断 / 变化更新 /
  **急救要点**（`gates.missing_first_aid`）/ 回复精简（**按问号数**，一轮 >1 个就超）/ 安全提示（含**全篇无剂量**）。
  改判定项要同步 `eval.html` 的 `RURAL_CHECK` 与 README 里那张表。
- **跑批机制与 consult 共用** `app/core/workflow_eval.py`（后台任务 / 报告 / 统计 / AI 分析）；新增工作流只要
  提供 `run_case` / `check_case` / `checks` 就能复用，**不要另写一套后台任务**。
- 测试 `tests/agent/test_rural.py`（桩判断节点跑流程 + 判定项 + 桩跑批，`make rural-test`）、
  冒烟 `scripts/rural_smoke.py`（`make rural-smoke CASE=4` 先跑急症用例）。
- **前端**：`index.html` 里 `isConsult()` / `isRural()` 决定接口、进度条、节点标签、跑批接口（`evalApi()`/`evalSetKey()`/`evalPageUrl()`）；
  新增场景要同时改 `renderScenario` / `refreshXxxFlow` / `enterXxx` / `openSession` / `refreshHistory` / `refreshEvals` / `newSession` / `init` 的 URL 分支，
  并在 `icons.js` 里加图标（山区问诊用 `mountain`）。

## 通用对话 Agent

- 新增工具：在 `app/agent/tools.py` 写 `@tool` 函数，加入 `BASIC_TOOLS` 或在 `build_tools()` 里新建分组
- 新增 Skill：在 `skills/<name>/SKILL.md` 中编写（front matter：`name` / `description` / `requires`），不需要改代码
- 新增搜索服务：在 `search.py` 写 provider 函数并加入 `PROVIDERS`
- 对话记忆是 `InMemorySaver`，服务重启后丢失（与企微 Agent 的 MySQL checkpoint 是两套机制）

## 报告类 Skill（html-report / word-report / ppt-report）

- 移植自 medex `storage/skills`，生成脚本在 `skills/<skill>/scripts/`，Agent 通过 `app/agent/report_tools.py` 的 4 个工具调用；工作区 `data/report_workspace` 相当于 medex 的 `/repo`
- **尽量不要改 medex 原脚本**，以便以后同步上游；需要适配时用环境变量（`REPORT_REPO_ROOT` / `REPORT_OUTPUT_DIR` / `PPT_REPORT_*`），改动点写在 README「报告类 Skill」一节
- 从 medex 同步新版本后：重新执行 `pytest tests/skills` 和 `python -m scripts.report_skills_smoke`，并确认 SKILL.md 顶部的 `requires` 与「在 LChain 中使用」说明还在
- **AI 生图**（`app/agent/imagegen.py`）：`generate_image` / `edit_image` 调用 `skills/imagegen/scripts/image_gen.py`（Codex 原版脚本，**不要修改**，需要适配时在 imagegen.py 里处理）。生图配置用 `IMAGEGEN_*`，只注入子进程环境，不要写进全局 `OPENAI_*`；未配置 `IMAGEGEN_API_KEY` 时不注册工具。`skills/imagegen/SKILL.md` 顶部的「在 LChain 项目中使用」说明和 `requires: [generate_image]` 在同步上游时要保留
- **自定义样式**（`app/agent/report_styles.py`）：spec 顶层 `style` 由 `report_run_generator` 解析为 `_style` 传给脚本；脚本只认 `_style` 的具体数值，不认预设名。新增预设改 `PRESETS`；脚本里所有颜色/字体都要走主题变量（HTML CSS 变量、Word `_T`、PPT 模块级颜色），不要再写死颜色。不写 style 时输出必须与 medex 原版一致（`pytest tests/skills` 保证）。样式测试：`tests/agent/test_report_styles.py`
- **PPT 制作流程**（`app/agent/ppt_workflow.py` + `ppt_media.py`）：需求 → 大纲确认（可多轮修改）→ 生成 → 渲染验证。`report_run_generator(skill="ppt-report")` 在对话中调用（有 runtime 会话 id）时必须存在用户确认过的最新大纲；脚本、测试直接调用（没有 runtime）不受影响。确认只接受大纲提出之后用户新发的确认消息（`classify_reply`）。不要绕开这些门禁，也不要让模型替用户确认。状态文件在 `threads/ppt-<会话>/workflow.json`
- 需要读取会话的工具使用 `runtime: ToolRuntime` 参数（不会出现在模型可见的 schema 中），通过 `runtime.config['configurable']['thread_id']` 获取会话 id
- PPT 流程测试：`PYTHONPATH=. .venv/bin/python -m pytest tests/agent -q`
- PPT 默认要求 Open XML SDK 校验通过（`tools/bin/medex-openxml-sdk-validator`，本机编译、不提交）；不要为了让生成通过而把 `PPT_SDK_VALIDATION` 改成 `optional`

## 代码风格

- 每个模块开头写中文 docstring，说明职责和关键设计；注释解释「为什么」
- 全部使用 async（FastAPI、SQLAlchemy async、aiomysql、`llm.ainvoke`）；阻塞调用用 `asyncio.to_thread`
- 配置统一放 `app/core/config.py`（`Settings` 字段 + 行尾注释），并同步更新 `.env.example`；新增的企微配置用 `WECOM_` 前缀
- 数据库操作放 `store.py`，用 `session_scope()`；不要在节点里直接写 SQL
- 日志用 `logger = logging.getLogger(__name__)`，不要用 `print`，也不要在模块里调用 `logging.basicConfig`（统一由 `app/core/logging_setup.py` 配置）
- 提示词使用 Langfuse 模板语法 `{{variable}}`；判断类提示词里必须出现 “JSON” 字样（开启了 `response_format=json_object`）；`wecom-summary` 里的 `{messages}` 是 SummarizationMiddleware 的占位符，不要再写其他花括号
- 前端是原生 JS，不引入构建工具；`$` / `api` / `streamSSE` / `esc` / `md` / `paintRange` 等公共函数在 `common.js`
- **图标统一走 SVG，不要再写 emoji**：`icons.js` 里 `icon(name, {cls, size})` 返回内联 `<svg class="ic">`（`currentColor` + `1.7` 描边），
  静态标记用 `<i class="ic" data-icon="chat"></i>`（DOM ready 时自动替换），JS 拼字符串用 `${icon("play")}`。
  **动态插入的 DOM（模板克隆、innerHTML 追加）要手动调一次 `paintIcons(node)`**，否则 `data-icon` 不会被替换；
  新增图标在 `icons.js` 的 `P` 里加一条 path 即可（24×24 viewBox、只写描边）。
  颜色/圆角/阴影/动效一律用 `style.css` 顶部的设计令牌（`--r`、`--sh-2`、`--dur` 等），不要再散写十六进制颜色。

## 验证方式

项目没有单元测试框架（除 `tests/agent/`、`tests/skills/` 下的 pytest，以及问诊病例测试），改完代码按以下方式验证：

- Python：`PYTHONPATH=. .venv/bin/python -c "import app.main"` 确认能导入；开发服务是热重载，看 `logs/error.log` 有没有 Traceback
- 前端：抽出 `<script>` 内容后用 `node --check` 检查语法
- 问诊场景：`make eval-check`（用例文件格式）→ `make consult-test`（流程+门禁，秒级）；改了流程/提示词后到 `/eval` 页跑一轮真实模型
- 山区问诊：`make eval-check` → `make rural-test`（流程 + 就医条件门禁 + 转诊决策 + 急救要点，秒级）→ `make rural-smoke CASE=4`（真实模型，消耗 token；改流程/提示词后先跑急症用例）
- 企微功能：`curl -X POST localhost:8000/api/wecom/reply ...`（同步返回每个节点的结果）或 `/wecom` 测试页
- 企微批量评测：`make wecom-test`（用例加载 + 判定 + 桩跑批链路，秒级、不花钱）→ `/wecom-eval` 页跑真实模型一轮（用例改动后先 `make eval-check`）
- 涉及发送、续跑、调度的改动：跑 `make crash-test`
- 测试数据用独立的 `group_id`（例如加 `test-` 前缀），测完删除（消息、待回复、执行记录、checkpoint 表都要清）

## 容易踩的坑

- **问诊类场景的「会话状态」有三条硬规则**（改了进场逻辑要一起看）：
  1. **会话 id 读取必须有兜底**：`crypto.randomUUID()` 在**非安全上下文**（用 `http://<局域网IP>:8000` 访问）下不存在，
     `localStorage` 在隐私模式/配额满时会抛错——两处都要 try/fallback（`newSessionId()` / `lsGet()` / `lsSet()`），
     否则「新会话」会**静默失败**：id 不换、界面还留着上一个会话的状态（真实踩过）。
  2. **进场景要回填历史对话**（`fetchSceneTurns` + `renderSceneHistory`），不能只留一句「已恢复」——用户看不到之前问了什么。
  3. **已结束（stage=closed）的会话文案不能写成还在流程里**：要说「上一次问诊已经结束了 + 上次结论 + 要问新的就直接描述，
     我会从头重新采集」；进度条要全部标「已完成」并在末尾加「已结束」格子，**不要把最后一格高亮成当前阶段**
     （`refreshConsultFlow` / `refreshRuralFlow` 里的 `closed` 分支）。
- **不要用正则批量替换代码里的函数调用**：曾经把 `localStorage.getItem` 批改成 `lsGet`，结果把 `lsGet`/`lsSet` **自己的函数体**
  也换成自调用（无限递归被 try/catch 吞掉 → 永远读不到、写不进，页面每次加载都当新会话）。批量替换后要看一眼被替换函数的定义体。

- **读接口不要有副作用**：`GET /api/{consult|rural}/{session_id}` 只校验 id 并读文件（`state_path()` + `session_dir_name()`），
  **不要调 `session_dir()`**（它 `mkdir`）；写的时候才建目录（`save_state` 里 `f.parent.mkdir(...)`）。
  曾经因为读状态顺手建目录，每查一次就多一个空会话目录（真实踩过）。
- **前端不要用 `confirm()` / `alert()` 做确认**：浏览器勾了「阻止此页面创建更多对话框」（或有弹窗拦截插件）后，
  `confirm` 会**静默返回 false**，点击看起来毫无反应——跑批按钮真实踩过：点了「开始批量测试」没发出任何 `/eval/run` 请求。
  改成「点了就执行 + 页面内联反馈」（如 `#evalList` 里显示“正在启动/启动失败”），危险操作的确认用按钮二次点击而不是原生对话框。
- **下拉浮层不要盖住操作按钮**：右栏「测试集合」的 `.set-pop` 向下展开时会盖住「数量 / 开始批量测试」，
  用户点按钮实际点在浮层上（也表现得像“点了没反应”）。`.rb-run .set-pop` 已改成**向上展开**，
  并在 `index.html`/`eval.html` 里加了「点空白处自动收起」；新增类似下拉时照这个来。
- **山区问诊的处置层级只能往上抬**：`gates.escalate` 在识别到危险信号 / 中毒外伤现场时会把 `disposition` 抬到 `emergency`；
  改 `node_triage` / `node_diagnose` 里的字段处理不要绕过它，也不要为了“少转诊”放宽红线（山区最怕患者在家硬扛）。
- **`app/core/prompt_llm.py` 的档位配置字段名是参数化的**（`model_key` / `fast_model_key` / `fast_effort_key`）：
  新增工作流时绑定自己的 `Settings` 字段，别把 `consult_*` 写死进去（否则另一个工作流的模型配置会静默失效）。
- **改了 `app/wecom/prompts.py` 不会自动生效**：运行时优先用 Langfuse 的 `production` 版本。改完要在 Langfuse 发布新版本（`lf.create_prompt(..., labels=["production"])` 或 `python -m scripts.init_prompts --force`）。
- **数据库没有迁移工具**：`init_db()` 只会创建不存在的表，**不会给已有表加列**。给已有表加字段时，要同时手动执行 `ALTER TABLE`（`docker exec lchain-mysql mysql -ulchain -plchain lchain -e "..."`），把语句追加到 README「升级已有数据库」一节，并在回复里告诉用户。
- **`build_llm()` 的缓存键必须能无损还原参数**：额外参数序列化成 JSON 做键（`app/core/llm.py`），**不要用 repr 拼字符串再反解** —— repr 会给字符串加引号，`reasoning_effort="low"` 会变成 `"'low'"` 发给模型，网关严格校验枚举，整轮执行报 422（`tests/agent/test_llm_factory.py` 锁住这条）。
- **`reasoning_effort` 是严格枚举**（`none`/`minimal`/`low`/`medium`/`high`/`xhigh`/`ultra`/`max`）：`.env` 或 Langfuse 提示词 config 里写脏（带引号、大写、拼错）会让整轮执行 422，读取时统一过 `clean_reasoning_effort()`。
- **`pymysql` 必须 `<1.2`**：aiomysql 0.3.x 依赖 pymysql 1.2 已删除的 `escape_bytes_prefixed`，写入二进制数据会报 `'str' object is not callable`。
- checkpoint 表的排序规则必须是 `utf8mb4_0900_ai_ci`（`checkpoint.py` 启动时自动转换），否则报 `Illegal mix of collations`。
- ddgs 搜索不要用 `auto`（会选到国内连不上的 mojeek）；`SEARCH_DDGS_BACKEND` 默认 `yahoo,bing,yandex` 并行搜索，免费引擎偶尔会同时失败。
- **部署形态是「多个 web 节点 + 一个定时任务进程」**：
  - 不要在 web 进程里加定时任务或后台轮询，放到 `app/wecom/jobs.py` 并在 `app/scheduler.py` 的 `JOBS` 里注册。
  - 新加的定时任务必须幂等，先原子地标记或认领再执行，参考 `claim_pending_notify` 和 `outbound._claim`。
  - 每个 web 节点一个进程，不要用 `uvicorn --workers`；打断合并的状态在节点内存里，同一个群跨节点靠 MySQL 群锁串行。
  - 运行记录和消息都带 `node_id`，恢复逻辑只处理指定节点的任务，修改 `store.unfinished_runs` 等查询时保留节点过滤。
- 热重载期间逐个修改多个文件，服务可能加载到改了一半的代码而报错；大改之后建议干净重启一次。
- **静态资源已强制协商缓存**（`app/main.py` 的 `NoCacheStatic` + 页面 `Cache-Control: no-cache`）：以前没有缓存头，浏览器会按启发式规则长期用旧 CSS，
  出现「新 HTML + 旧 CSS」的错乱页面（图标变巨大）。改完前端如果用户还看到旧样式，让他硬刷新（⌘⇧R）一次；
  改 CSS/JS 后要立即生效也可以给资源加上 `?v=N`。
- `sse-starlette` 输出的换行是 `\r\n`，自己写 SSE 解析时要兼容。
- 捕获任务取消要用 `BaseException` / `asyncio.CancelledError`（打断合并靠取消任务实现），`except Exception` 捕获不到。

## 安全与测试开关

- `.env` 含 API Key，不要打印或提交；`deploy/langfuse/.env` 同理（且不要删除后重新 init，会导致数据库密码不一致）
- 以下开关只用于测试，**线上必须为空**，测试后要还原 `.env`：
  - `WECOM_FAULT_INJECT=<节点名>`：该节点抛异常（验证失败后续跑）
  - `WECOM_CRASH_AT=<崩溃点>`：在该处 `os._exit`（验证重启恢复）。崩溃点：`ingested`、`node:<节点>`、`emit_saved:<动作>`、`emit_sent:<动作>`、`run:graph_done`
- 联网抓取（`fetch_webpage`、附件 URL）经过 `_is_public_url` 做 SSRF 检查，不要绕过

## 尚未完成（不要假设已存在）

- **medex 未接入**：`node_history_search` 目前是 MySQL 关键词检索
- **企微发送端未对接**：只有 `WECOM_OUTBOUND_WEBHOOK`，发送端需要按 `message_id` 去重
- 医生卡片 URL（`WECOM_DOCTOR_CARD_URL`）是占位地址；卡片头像默认是内置 AI 插画（`app/web/assets/doctor-avatar.png`），线上要换成公网可访问的真实医生头像（或医生团队照片）
- 跨节点的打断合并（目前只在同一个节点内合并，建议网关按 `group_id` 做粘性路由）

## 交付要求

- 改完代码要实际验证（调接口 / 跑测试），不要只凭代码推断
- 功能变化同步更新 `README.md` 和本文件；新增配置同步 `.env.example`（`.env` 也要加上，保持两者配置项一致）；新增依赖同步 `requirements.txt` 和 `pyproject.toml`
- **修改大文件（尤其是 `README.md`）不要用 Python 脚本一次性读写整个文件做批量替换**：脚本中途出错可能把文件清空（本项目没有 git，无法恢复）。优先用编辑工具做精确替换，改完用 `wc -l` 确认文件行数正常
- 修改了 Langfuse 托管的提示词、执行了 `ALTER TABLE`、重启了服务，都要在回复里告诉用户
