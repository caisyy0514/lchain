"""企微群回复 Agent 的数据表。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME, MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

LongText = Text().with_variant(MEDIUMTEXT(), "mysql")


def now() -> datetime:
    return datetime.now()


class Group(Base):
    """群（一个患者服务群）+ 基础信息 + 聊天压缩摘要。"""
    __tablename__ = "wecom_groups"

    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    profile: Mapped[str] = mapped_column(Text, default="")                  # 基础信息：患者档案 / 服务包 / 备注（自由文本）
    case_manager_id: Mapped[str] = mapped_column(String(64), default="")
    case_manager_name: Mapped[str] = mapped_column(String(64), default="")
    summary: Mapped[str] = mapped_column(LongText, default="")               # 聊天压缩记录
    summary_until_id: Mapped[int] = mapped_column(BigInteger, default=0)     # 已压缩到的消息 id（含）
    summary_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    doctor_card_pushed_at: Mapped[datetime | None] = mapped_column(DateTime) # 医生卡片首次推送时间
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class Message(Base):
    """群消息（用户消息 / 员工消息 / 助手回复 / 系统消息 / 卡片）。"""
    __tablename__ = "wecom_messages"
    __table_args__ = (Index("ix_msg_group_id", "group_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(64))
    ext_msg_id: Mapped[str | None] = mapped_column(String(128), unique=True)  # 企微侧消息 id，用于幂等
    sender_id: Mapped[str] = mapped_column(String(64), default="")
    sender_name: Mapped[str] = mapped_column(String(64), default="")
    # patient / family / case_manager / doctor / assistant / system
    sender_role: Mapped[str] = mapped_column(String(32), default="patient")
    # text / image / file / card / system / notify / doctor_card
    msg_type: Mapped[str] = mapped_column(String(32), default="text")
    content: Mapped[str] = mapped_column(LongText, default="")
    attachments: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    mentions: Mapped[list[str] | None] = mapped_column(JSON)
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    run_id: Mapped[str | None] = mapped_column(String(64))                   # 助手消息对应的 agent run
    # 助手发出动作的幂等键（run_id:动作），checkpoint 续跑重放节点时不会重复发送
    idem_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    # 出站投递状态（发件箱）：none 不外发（用户消息 / 系统消息）/ pending 待投递 / sending 投递中（已被某个进程认领）/ sent 已投递 / failed 投递失败待重试
    delivery: Mapped[str] = mapped_column(String(16), default="none")
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    node_id: Mapped[str | None] = mapped_column(String(128))                 # 接收该用户消息的 web 节点
    created_at: Mapped[datetime] = mapped_column(DateTime().with_variant(MYSQL_DATETIME(fsp=3), "mysql"), default=now)


class Document(Base):
    """文件解析结果（流程图中的“同步到 repo”）。"""
    __tablename__ = "wecom_documents"
    __table_args__ = (Index("ix_doc_group", "group_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(64))
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    file_name: Mapped[str] = mapped_column(String(255), default="")
    mime_type: Mapped[str] = mapped_column(String(128), default="")
    file_path: Mapped[str] = mapped_column(String(512), default="")
    raw_text: Mapped[str] = mapped_column(LongText, default="")      # 抽取出的原始文本（PDF/Word）
    parsed_text: Mapped[str] = mapped_column(LongText, default="")   # 大模型解读结果
    status: Mapped[str] = mapped_column(String(16), default="ok")    # ok / failed / unsupported
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class PendingReply(Base):
    """待回复记录：需要回复的消息先记录，回复后移除；超时由 job 通知病历经理。"""
    __tablename__ = "wecom_pending_replies"
    __table_args__ = (Index("ix_pending_status", "status", "created_at"), Index("ix_pending_group", "group_id"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(64))
    message_id: Mapped[int] = mapped_column(BigInteger)
    run_id: Mapped[str | None] = mapped_column(String(64))
    # pending 待回复 / running 助手处理中 / replied 已回复 / handover 已转病历经理 / notified 超时已通知 / skipped 无需回复
    status: Mapped[str] = mapped_column(String(16), default="pending")
    reason: Mapped[str] = mapped_column(Text, default="")
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime)


class AgentRun(Base):
    """每次 Agent 执行的记录（各节点结果 / 最终动作 / trace），便于排查和评估。"""
    __tablename__ = "wecom_agent_runs"
    __table_args__ = (Index("ix_run_group", "group_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(64))
    message_id: Mapped[int] = mapped_column(BigInteger)                   # 本轮最后一条消息
    message_ids: Mapped[list[int] | None] = mapped_column(JSON)           # 本轮合并处理的全部消息
    # running / done / skipped / merged（被新消息打断，合并到下一轮）/ cancelled（人工接手）/ failed / interrupted（服务中断，可续跑）
    status: Mapped[str] = mapped_column(String(16), default="running")
    opts: Mapped[dict[str, Any] | None] = mapped_column(JSON)            # 执行参数（force_reply / prompt_overrides），续跑时复用
    node_id: Mapped[str | None] = mapped_column(String(128))            # 执行该任务的 web 节点（重启时只恢复自己的）
    resume_count: Mapped[int] = mapped_column(Integer, default=0)       # 从 checkpoint 续跑的次数
    need_reply: Mapped[bool | None] = mapped_column(Boolean)
    intent: Mapped[str | None] = mapped_column(String(32))
    steps: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    actions: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    context: Mapped[str | None] = mapped_column(LongText)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class Node(Base):
    """进程心跳：web 节点和定时任务进程定期更新 last_seen；定时任务据此发现失联的 web 节点。"""
    __tablename__ = "wecom_nodes"

    node_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), default="web")          # web / scheduler
    pid: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="online")     # online / stopped / dead
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=now)


class FdCase(Base):
    """家庭医生 Agent（VBC 分级诊疗）的病例与当前有效计划（一个群一份）。"""
    __tablename__ = "wecom_fd_cases"
    __table_args__ = (Index("ix_fd_case_group", "group_id"),)

    case_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(64), unique=True)
    plan_version: Mapped[int] = mapped_column(Integer, default=1)
    urgency: Mapped[str] = mapped_column(String(8), default="EU")
    care_mode: Mapped[str] = mapped_column(String(32), default="initial_navigation")
    state: Mapped[str] = mapped_column(String(32), default="DRAFT")
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSON)             # ActionablePlan 序列化
    consultation: Mapped[dict[str, Any] | None] = mapped_column(JSON)     # 多轮问诊状态（轮次/已确认信息/已问问题）
    release_stage: Mapped[str] = mapped_column(String(32), default="DEMO")
    last_idem_key: Mapped[str | None] = mapped_column(String(128))        # 同一 run 重放时不重复升版本
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class FdObservation(Base):
    """居家观察计划与复评记录（wecom_fd_cases 的从属；由定时任务触发主动追问）。"""
    __tablename__ = "wecom_fd_observations"
    __table_args__ = (Index("ix_fd_obs_group", "group_id", "state"),
                      Index("ix_fd_obs_due", "state", "next_review_at"))

    observation_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(64))
    case_id: Mapped[str] = mapped_column(String(64))
    idem_key: Mapped[str | None] = mapped_column(String(128), unique=True)  # run 重放时不重复创建观察任务
    plan_version: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(32), default="PROPOSED")
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSON)             # HomeObservationPlan 序列化
    maximum_end_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime)
    response_due_at: Mapped[datetime | None] = mapped_column(DateTime)
    reassessment_due_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime)
    contact_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
