"""全局配置：统一从环境变量 / .env 读取。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ---------- 应用 ----------
    app_name: str = "LChain Agent"
    app_env: str = "dev"
    app_host: str = "127.0.0.1"
    app_thread_pool_size: int = 64            # 事件循环默认线程池大小（to_thread 用；阻塞型任务多时调大）
    app_limit_concurrency: int = 0            # uvicorn 最大并发连接数，0=不限
    app_backlog: int = 2048                   # uvicorn 等待队列长度
    app_port: int = 8000
    app_public_url: str = ""                 # 对外可访问地址（如 https://agent.example.com），用于生成患者端 H5 卡片链接；空=不下发链接

    # ---------- 部署：多 web 节点 + 单独的定时任务进程 ----------
    node_id: str = ""                      # 节点 ID，空 = 主机名:端口；同一节点重启后必须保持不变（才能恢复自己未完成的任务）
    node_heartbeat_seconds: int = 10       # 节点心跳间隔
    node_dead_seconds: int = 60            # 超过该时间没有心跳视为节点失联，由定时任务接管其未完成的任务
    shutdown_drain_seconds: int = 20       # web 节点停止时等待进行中任务完成的最长时间

    # ---------- 日志 ----------
    log_dir: str = "logs"                  # 日志目录（相对路径按项目根目录解析），可设为绝对路径如 /var/log/lchain
    log_level: str = "INFO"
    log_retention_days: int = 14           # 按天切分，保留天数
    log_access_console: bool = False       # HTTP 访问日志是否也打印到控制台（始终写入 access.log）

    # ---------- LLM（任何兼容 OpenAI 协议的服务都可以用：OpenAI / DeepSeek / 通义 / 本地 vLLM ...） ----------
    llm_api_key: str = ""
    llm_base_url: str | None = None
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.7
    # 前端下拉框里可选的模型，逗号分隔
    llm_timeout: float = 120                 # 单次 LLM 请求超时（秒）
    llm_max_retries: int = 2                 # LLM 客户端自带重试（网络抖动）
    llm_model_options: str = ""

    # ---------- 联网搜索 ----------
    # auto：配置了 TAVILY_API_KEY 用 tavily，否则用 ddgs（免 Key，聚合 bing / brave 等）
    search_provider: str = "auto"          # auto / ddgs / tavily / searxng
    search_max_results: int = 5
    search_region: str = "cn-zh"           # ddgs 地区，例如 cn-zh / us-en / wt-wt
    search_ddgs_backend: str = "yahoo,bing,yandex"  # ddgs 使用的搜索引擎（逗号分隔），空=auto
    tavily_api_key: str = ""
    searxng_url: str = ""                   # 自建 SearXNG，例如 http://localhost:8888
    fetch_max_chars: int = 8000            # fetch_webpage 返回正文的最大字符数
    search_max_calls: int = 6              # 单轮对话最多调用 web_search 次数（防止无限搜索，控制耗时和成本）
    fetch_max_calls: int = 4               # 单轮对话最多调用 fetch_webpage 次数
    fetch_allow_private: bool = False      # 允许 fetch_webpage 访问内网地址（有 SSRF 风险，默认关闭）

    # ---------- Skills ----------
    skills_dir: str = "skills"
    # 报告类 Skill（html-report / word-report / ppt-report）的工作区，相当于 medex 沙箱里的 /repo
    report_workspace: str = "data/report_workspace"
    eval_dir: str = "eval"                   # eval 用例目录（每个功能一个子目录，见 eval/README.md）
    # PPT 严格校验用的 Open XML SDK 校验器（skills/ppt-report/validators/openxml-sdk 编译产物）
    ppt_openxml_sdk_validator: str = "tools/bin/medex-openxml-sdk-validator"
    ppt_sdk_validator_owner: str = "user"      # root：要求 root 所有（生产镜像）；user：允许当前用户所有（本地开发）
    ppt_sdk_validation: str = "required"       # required：缺校验器即失败；optional：缺校验器时跳过并在输出中注明
    ppt_soffice_path: str = ""                 # PPT 渲染验证用的 LibreOffice soffice 路径，空=自动查找
    ppt_cjk_font: str = ""                     # 示意图 / 渲染验证用的中文字体文件，空=自动查找（苹方/冬青黑体/Noto CJK 等）
    ppt_verify_vision_model: str = ""
    # ---------- AI 生图（skills/imagegen，OpenAI Images API 兼容服务） ----------
    imagegen_api_key: str = ""                 # 为空则不启用生图工具；只传给生图子进程（作为 OPENAI_API_KEY）
    imagegen_base_url: str = ""                # 兼容服务地址（作为 OPENAI_BASE_URL），空=OpenAI 官方
    imagegen_model: str = "gpt-image-2"        # 生图模型（需以 gpt-image- 开头，CLI 的限制）
    imagegen_timeout: int = 600                # 单次生图最长等待秒数（部分服务排队较久）
    imagegen_max_calls: int = 6                # 单轮对话最多生图/编辑次数（控制耗时与费用）          # 渲染后做版面视觉评审的多模态模型（如 deepseek-flash），空=只做自动检查

    # ---------- Langfuse ----------
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    # 从 Langfuse 拉取哪个 label 的提示词（production / staging / latest ...）
    langfuse_prompt_label: str = "production"
    # 提示词缓存秒数；调试阶段设成 0，改完提示词立刻生效
    langfuse_prompt_cache_ttl: int = 300  # 提示词缓存秒数；0=每次请求都同步拉 Langfuse（会阻塞事件循环）

    # ---------- MySQL（企微群回复业务存储） ----------
    mysql_url: str = "mysql+aiomysql://lchain:lchain@127.0.0.1:13306/lchain?charset=utf8mb4"
    mysql_pool_size: int = 20                # 连接池常驻连接数（多 web 节点各自一份）
    mysql_max_overflow: int = 20             # 峰值时可临时超出的连接数（并发上限 ≈ pool + overflow）

    # ---------- 企业微信群回复 Agent ----------
    wecom_bot_name: str = "小助手"
    wecom_model: str = ""                    # 回复生成用的模型，空=LLM_MODEL
    wecom_fast_model: str = ""               # 判断/分类类节点用的模型，空=LLM_MODEL
    wecom_doc_model: str = "deepseek-flash"  # 文件（图片/PDF/文档）解读模型，需支持图片输入
    wecom_fast_reasoning_effort: str = "low" # 判断类节点的推理强度（none/minimal/low/medium/high/xhigh/ultra/max），空=模型默认；非法值会被忽略
    wecom_recent_messages: int = 40          # 上下文带入最近 N 条消息（约 20 轮对话）
    wecom_summary_trigger: int = 20          # 最近窗口之外未压缩的消息达到该条数时触发压缩（后台执行）
    wecom_checkpoint_enabled: bool = True     # LangGraph checkpoint（MySQL）断点续跑
    wecom_resume_on_startup: bool = True      # 服务启动时自动续跑上次中断的执行
    wecom_resume_max_age_seconds: int = 600   # 超过该时长的中断执行不再自动续跑（交给超时通知兜底），0=不限
    wecom_checkpoint_ttl_days: int = 7        # 已完成执行的 checkpoint 保留天数
    wecom_node_max_attempts: int = 3          # 节点失败自动重试次数（LLM 超时/限流等）
    wecom_fault_inject: str = ""              # 测试用：指定节点名模拟失败（验证断点续跑），线上保持为空
    wecom_crash_at: str = ""                  # 测试用：在指定崩溃点杀掉进程（见 outbound.crash_point），线上保持为空
    wecom_pending_timeout_seconds: int = 120 # 超过该时间群里无人回复 -> 通知病历经理
    wecom_job_interval_seconds: int = 15     # 定时任务进程（python -m app.scheduler）的轮询间隔；web 节点不跑定时任务

    # ---------- 结构化问诊工作流 ----------
    consult_model: str = ""                 # 生成类节点（提问/分诊/诊断）用的模型，空=LLM_MODEL
    consult_fast_model: str = ""            # 判断类节点（提取/阶段判断）用的模型，空=LLM_MODEL
    consult_fast_reasoning_effort: str = "" # 判断类节点的推理强度（low/high），空=模型默认
    consult_recent_turns: int = 8            # 提示词里带入最近 N 轮对话
    consult_max_turns: int = 60              # 状态文件里保留的对话轮次上限（防止无限增长）
    consult_node_max_attempts: int = 3       # 节点失败自动重试次数（LLM 超时/限流等）

    # ---------- 山区远程问诊工作流（app/rural/，面向偏远山区村民本人） ----------
    rural_model: str = ""                   # 生成类节点（提问/就医建议/诊断）用的模型，空=LLM_MODEL
    rural_fast_model: str = ""              # 判断类节点（信息提取/本轮动作）用的模型，空=LLM_MODEL
    rural_fast_reasoning_effort: str = ""   # 判断类节点的推理强度（low/high），空=模型默认
    rural_recent_turns: int = 8              # 提示词里带入最近 N 轮对话
    rural_max_turns: int = 60                # 状态文件里保留的对话轮次上限（防止无限增长）
    rural_node_max_attempts: int = 3         # 节点失败自动重试次数（LLM 超时/限流等）
    rural_emergency_number: str = "120"      # 本地急救电话（写进就医建议与提示词）
    rural_referral_target: str = "乡镇卫生院"  # 最近的一级就医去处（写进转诊建议）
    rural_county_hospital: str = "县人民医院"   # 上一级医院（急症 / 需要检查时）
    # 当地条件清单：注入提示词，让建议只落在「做得到」的范围里
    rural_local_resources: str = (
        "村卫生室一般能做：测体温、量血压、测血糖、外伤清创包扎、口服常用药；"
        "一般做不了化验、X 光、B 超、CT，也没有抗蛇毒血清和手术条件。老人家里通常只有少量常用药。"
    )
    # 山区转运提示：山路 / 雨雪 / 夜间 / 交通工具，写进转诊建议
    rural_transport_hint: str = (
        "山路弯多、雨雪天路滑、夜间一般没有班车；能借到摩托、三轮农用车就先往乡镇卫生院送，"
        "紧急情况直接打 120，同时让人在村口等车、派人在路边引路。"
    )
    wecom_redeliver_grace_seconds: int = 60  # 待投递消息超过该时间仍未投递才由定时任务补投（避免和 web 节点正在进行的投递冲突）
    wecom_upload_dir: str = "data/uploads"
    wecom_max_file_mb: int = 15
    wecom_pdf_max_pages: int = 8             # PDF 最多解析页数（扫描件按图片识别）
    wecom_push_parse_guide: bool = True      # 收到文件时先推送“正在解读”引导语
    wecom_push_search_guide: bool = True     # 需要联网检索时先推送引导消息
    wecom_severe_notify_manager: bool = True # 严重情况同时 @病历经理
    wecom_outbound_webhook: str = ""         # 回复动作推送地址（对接企微发送端），空=只落库

    # ---------- 家庭医生 Agent（VBC 分级诊疗 / 就医路径导航） ----------
    wecom_fd_enabled: bool = True             # 在企微群里启用家庭医生 Agent（安全门控 + 分级诊疗 + 计划卡）
    wecom_fd_name: str = "家庭医生"             # 家庭医生在群里的显示名
    wecom_fd_release_stage: str = "DEMO"      # DEMO / SHADOW / CLINICIAN_ASSISTED / PRODUCTION_APPROVED
    wecom_fd_observation_enabled: bool = True  # 居家观察主动追问（需要定时任务进程在跑；关闭时不允许 self_care）
    wecom_fd_emergency_number: str = "120"    # 所在地急救电话（中国大陆）
    wecom_fd_model: str = ""                  # 分诊生成模型，空=LLM_MODEL
    wecom_fd_fast_model: str = ""             # 判断类节点模型，空=LLM_MODEL
    wecom_fd_plan_card: bool = True           # 回复时附带结构化计划卡（前端渲染）
    wecom_fd_min_rounds: int = 2             # 普通病例结论前的最少问诊轮次（急症/稳定复诊除外）
    wecom_fd_max_rounds: int = 4             # 最多问诊轮次，超过后按现有信息给保守结论
    wecom_doctor_card_title: str = "找医生 · 在线问诊"
    wecom_doctor_card_desc: str = "点击卡片，选择合适的医生为您进一步诊疗"
    wecom_doctor_card_url: str = "https://example.com/find-doctor"
    # 找医生卡片头像：默认用内置资源（/static/assets/doctor-avatar.png，测试页可直接显示）；
    # 对接真实企微发送端时要填可公网访问的图片地址（企微卡片缩略图不支持本地路径）
    wecom_doctor_card_avatar: str = "/static/assets/doctor-avatar.png"
    wecom_doctor_card_doctor_name: str = ""      # 找医生卡片上的医生名（团队 / 医生姓名，留空则不显示）

    def node_name(self, role: str = "web") -> str:
        if self.node_id:
            return self.node_id if role == "web" else f"{self.node_id}:{role}"
        import socket

        host = socket.gethostname()
        return f"{host}:{self.app_port}" if role == "web" else f"{host}:{role}"

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def resolved_search_provider(self) -> str:
        if self.search_provider != "auto":
            return self.search_provider
        return "tavily" if self.tavily_api_key else "ddgs"

    @property
    def model_options(self) -> list[str]:
        opts = [m.strip() for m in self.llm_model_options.split(",") if m.strip()]
        if self.llm_model not in opts:
            opts.insert(0, self.llm_model)
        return opts


@lru_cache
def get_settings() -> Settings:
    return Settings()
