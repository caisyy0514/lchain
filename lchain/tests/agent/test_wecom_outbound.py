"""企微发送端投递契约测试：计划卡必须把新版患者视图完整下发。

覆盖：
- type=plan_card → card 带 view（任务/安全分组/费用测算等）与紧急程度；
- 配置 APP_PUBLIC_URL 时附带患者端 H5 链接；
- doctor_card 原样透传；其他消息不为空发 card。
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.wecom import outbound


def _msg(msg_type: str, extra: dict[str, Any], mid: int = 42) -> Any:
    return SimpleNamespace(id=mid, msg_type=msg_type, extra=extra)


_VIEW = {
    "case_id": "c1", "plan_version": 2, "rendered_at": "2026-10-12T10:00:00",
    "urgency_word": "今天内", "headline": "今天内到示例市第三医院·呼吸内科就诊。",
    "time_window": "今天内",
    "tasks": [{"kind": "book", "status": "todo", "title": "先完成预约挂号", "detail": "官方渠道"}],
    "conditions": [{"name": "细菌性肺炎", "likelihood": "需优先排除", "evidence": "发热3天伴黄痰"}],
    "risk_signals": [{"name": "细菌性肺炎", "signals": ["持续高热不退"], "action": "立即急诊"}],
    "safety_groups": [{"level": "emergency", "action": "出现以下任一情况，立即拨打120或去最近急诊",
                       "signals": ["呼吸困难、口唇发紫"]}],
    "care_options": {"region": "示例市朝阳区", "options": [{"name": "示例市第三医院", "recommended": True}],
                     "saving": 129},
    "alerts": ["如果出现呼吸困难，请立即拨打120"],
}


def test_plan_card_payload_carries_full_view(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbound, "get_settings", lambda: SimpleNamespace(app_public_url=""))
    card = outbound.outbound_card(_msg("plan_card", {
        "kind": "plan_card", "urgency": "E2", "care_mode": "in_person",
        "release_stage": "DEMO", "clinician_review": False, "view": _VIEW,
    }))
    assert card is not None
    assert card["kind"] == "plan_card" and card["urgency"] == "E2" and card["care_mode"] == "in_person"
    view = card["view"]
    for field in ("tasks", "safety_groups", "conditions", "risk_signals", "care_options", "time_window"):
        assert field in view, f"投递卡片必须包含 {field}"
    assert view["safety_groups"][0]["level"] == "emergency"
    assert view["care_options"]["saving"] == 129
    assert "url" not in card, "未配置 APP_PUBLIC_URL 时不下发链接"


def test_plan_card_payload_includes_h5_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbound, "get_settings",
                        lambda: SimpleNamespace(app_public_url="https://agent.example.com/"))
    card = outbound.outbound_card(_msg("plan_card", {"kind": "plan_card", "view": _VIEW}, mid=1402))
    assert card is not None
    assert card["url"] == "https://agent.example.com/wecom-card/1402"


def test_doctor_card_and_plain_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbound, "get_settings", lambda: SimpleNamespace(app_public_url=""))
    doctor = outbound.outbound_card(_msg("doctor_card", {"card": {"title": "找医生"}}))
    assert doctor == {"title": "找医生"}
    assert outbound.outbound_card(_msg("text", {"route": "simple"})) is None
    assert outbound.outbound_card(_msg("plan_card", {"kind": "plan_card"})) is None, "没有 view 不构造空卡片"
