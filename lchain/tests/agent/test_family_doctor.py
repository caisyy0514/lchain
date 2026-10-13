"""家庭医生 Agent（VBC 分级诊疗 / 就医路径导航）的测试。

覆盖（对应《VBC…v1.3.md》第 5 / 16–18 / 23 / 24 节）：
1. 确定性红旗门控：命中、否定、既往描述、跨分句时序、高危修饰项
2. 数据契约与服务端不变量：紧急程度只升不降、self_care 必须闭环、FEASIBLE 核验、未知时间要有理由
3. 患者计划卡渲染与语义一致性门控（E0 必须给急救行动、不出现内部代码）
4. 居家观察：时间窗顺序、继续观察不重置最长截止、延长需医师批准、未回复不推断好转
5. 引擎端到端（桩 LLM + 桩持久化）：急救不调用模型；非急救按已批准路径创建观察任务

    PYTHONPATH=. .venv/bin/python -m pytest tests/agent/test_family_doctor.py -q
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import pytest

from app.wecom.fd import engine as fd_engine
from app.wecom.fd import observation as obs_mod
from app.wecom.fd import pathways
from app.wecom.fd import red_flags
from app.wecom.fd import render as fd_render
from app.wecom.fd import store as fd_store
from app.wecom.fd.contracts import (
    ActionablePlan,
    HomeObservationPlan,
    ObservationState,
    PatientPlanView,
    Urgency,
    clamp_urgency,
    make_eu_plan,
    validate_plan,
    worst_urgency,
)


def _screen(text: str, profile: str = "") -> red_flags.ScreenResult:
    return red_flags.screen(text, profile=profile, emergency_number="120")


# ---------------------------------------------------------------- 1. 红旗门控
class TestRedFlags:
    def test_registry_not_empty_and_versioned(self) -> None:
        assert red_flags.rule_count() >= 20
        assert red_flags.RED_FLAG_REGISTRY_VERSION.startswith("fd-redflags-")
        assert red_flags.REVIEW_STATUS  # 必须有审批状态标记

    def test_chest_pain_acs(self) -> None:
        res = _screen("昨晚开始胸痛，出冷汗，喘不上气")
        assert res.emergency and res.urgency_floor == "E0"
        assert any(m["rule_id"] == "RF-CARD-ACS" for m in res.matched)

    def test_negation_does_not_trigger(self) -> None:
        res = _screen("没有胸痛，就是有点胃胀")
        assert not res.matched and not res.emergency

    def test_past_history_does_not_trigger(self) -> None:
        res = _screen("以前有过胸痛，现在没有")
        assert not res.matched

    def test_stroke(self) -> None:
        res = _screen("今天早上突然口角歪斜，说话不清")
        assert any(m["rule_id"] == "RF-NEU-STROKE" for m in res.matched)
        assert res.urgency_floor == "E0"

    def test_tia_recovered_still_urgent(self) -> None:
        """症状已恢复也不能降级（跨分句时序匹配）。"""
        res = _screen("昨天说话不清、手脚麻木，几分钟就好了")
        assert res.emergency
        assert res.urgency_floor in ("E0", "E1")

    def test_infant_fever_requires_modifier(self) -> None:
        assert _screen("3个月宝宝发烧38.5度，不吃奶").emergency
        assert not any(m["rule_id"] == "RF-INF-INFANT" for m in _screen("大人发烧38.5度").matched)

    def test_anticoagulant_head_injury(self) -> None:
        res = _screen("摔到头了，有点头晕", profile="长期吃华法林")
        assert any(m["rule_id"] == "RF-TRAUMA-HEAD-ANTICOAG" for m in res.matched)
        assert res.emergency

    def test_suicide_intent(self) -> None:
        res = _screen("我不想活了，已经想好怎么做了")
        assert res.urgency_floor == "E0"

    def test_dka(self) -> None:
        res = _screen("糖尿病人，血糖18，呕吐肚子疼，呼吸很快")
        assert res.urgency_floor == "E0"

    def test_obstetric_bleeding(self) -> None:
        res = _screen("怀孕6周，今天肚子疼还有阴道出血")
        assert res.urgency_floor == "E0"

    def test_obstetric_dizziness_alone_not_triggered(self) -> None:
        """孕 20 周仅头晕（无出血/腹痛/晕厥）不能触发产科红旗，避免过度急救。"""
        res = _screen("我怀孕 20 周，最近两天有点头晕，血压 110/70，没有头痛也没有视物模糊")
        assert not any(m["rule_id"] == "RF-OB-BLEED" for m in res.matched)

    def test_eye_vision_loss(self) -> None:
        assert _screen("突然右眼看不见了").emergency

    def test_anticoagulant_escalates_gi_bleed(self) -> None:
        res = _screen("黑便两天，站起来头晕", profile="房颤，吃利伐沙班")
        assert res.urgency_floor == "E0"

    def test_low_risk_not_triggered(self) -> None:
        res = _screen("嗓子疼，流鼻涕，有点低烧")
        assert not res.matched and not res.emergency

    def test_negative_symptoms_do_not_trigger(self) -> None:
        """回答“没有 X、Y”不能因分句/窗口误判成阳性红旗（样例回访发现的问题）。"""
        assert not _screen("没有呼吸困难、胸痛，精神还可以").matched
        assert not _screen("没有血便黑便，也没有剧烈腹痛和呕吐").matched
        assert not _screen("没有胸痛和呼吸困难").matched
        assert not _screen("没有胸痛、呼吸困难").matched

    def test_positive_combination_still_triggers(self) -> None:
        assert _screen("胸痛、呼吸困难").emergency

    def test_pathway_exclusion_respects_negation(self) -> None:
        """低风险路径排除项也要看否定：“没有血便黑便”不能把腹泻路径排除掉。"""
        text = ("拉肚子两天了，一天三四次，没有发烧，还能正常喝水吃饭，"
                "没有血便黑便，也没有剧烈腹痛和呕吐，没有呼吸困难、胸痛")
        pw = pathways.find_pathway(text, release_stage="DEMO")
        assert pw is not None and pw.pathway_id == "PATHWAY-DEMO-GASTRO-MILD"
        assert pathways.find_pathway("拉肚子两天，今天有血便", release_stage="DEMO") is None
        uri = pathways.find_pathway("嗓子疼、流鼻涕、低烧 37.8，没有呼吸困难、胸痛，没有基础病",
                                    release_stage="DEMO")
        assert uri is not None and uri.pathway_id == "PATHWAY-DEMO-URI-MILD"

    def test_modifier_detection(self) -> None:
        mods = red_flags.detect_modifiers("怀孕20周，做过肾移植，正在化疗")
        assert "pregnancy" in mods and "transplant" in mods and "immunosuppressed" in mods


# ---------------------------------------------------------------- 2. 数据契约
class TestContracts:
    def test_worst_urgency(self) -> None:
        assert worst_urgency("E4", "E1", "EU") == "E1"

    def test_clamp_never_downgrades(self) -> None:
        plan = ActionablePlan(case_id="c1", urgency="E4", care_mode="in_person")
        plan = clamp_urgency(plan, "E0")
        assert plan.urgency == "E0"
        assert plan.urgency_assessment == "E4"

    def test_eu_fallback_plan_is_valid(self) -> None:
        assert validate_plan(make_eu_plan("c1", "信息不足")) == []

    def test_self_care_requires_observation(self) -> None:
        plan = ActionablePlan(case_id="c1", urgency="E4", care_mode="self_care")
        assert any("home_observation" in v for v in validate_plan(plan))

    def test_feasible_requires_checks(self) -> None:
        plan = ActionablePlan(case_id="c1", state="FEASIBLE")
        assert any("核验" in v for v in validate_plan(plan))

    def test_unverified_facility_rejected(self) -> None:
        plan = ActionablePlan(case_id="c1", destination={"facility_id": "f1", "verified": False})
        assert any("facility_id" in v for v in validate_plan(plan))

    def test_missing_data_cannot_be_low_risk(self) -> None:
        plan = ActionablePlan(case_id="c1", urgency="E4", missing_critical_data=["年龄"])
        assert any("关键缺失信息" in v for v in validate_plan(plan))

    def test_e0_requires_immediate_action(self) -> None:
        plan = ActionablePlan(case_id="c1", urgency="E0", care_mode="in_person")
        violations = validate_plan(plan)
        assert any("E0/E1" in v for v in violations)

    def test_triage_output_tolerates_nulls(self) -> None:
        """LLM 把字符串/列表字段返回 null 时不能整次分诊失败（转成空值）。"""
        from app.wecom.fd.contracts import TriageOutput

        t = TriageOutput.model_validate({"facility_level": None, "questions": None,
                                         "possible_conditions": None, "self_care": None,
                                         "urgency": None, "collected_facts": None})
        assert t.facility_level == "" and t.questions == [] and t.possible_conditions == []
        assert t.self_care.eligible is False and t.collected_facts == {}


# ---------------------------------------------------------------- 3. 渲染与一致性
class TestRender:
    def test_emergency_card_gives_action_and_no_internal_code(self) -> None:
        res = _screen("胸痛，出冷汗")
        plan = fd_engine.emergency_plan("c1", res)
        view = fd_render.render(plan, emergency_number="120")
        text = view.text()
        assert "120" in text and "立即" in text
        assert "E0" not in text and "JSON" not in text
        assert fd_render.check_patient_text(view, plan) == []

    def test_emergency_card_cannot_say_wait(self) -> None:
        res = _screen("胸痛，出冷汗")
        plan = fd_engine.emergency_plan("c1", res)
        view = fd_render.render(plan, emergency_number="120")
        view.headline = "先在家观察，明天再去医院"
        assert fd_render.check_patient_text(view, plan)

    def test_self_care_card_has_review_note(self) -> None:
        pw = pathways.find_pathway("嗓子疼，流鼻涕，低烧", release_stage="DEMO")
        assert pw is not None
        obs = obs_mod.build_observation_plan("c1", 1, pw, task_id="t1")
        plan = ActionablePlan(
            case_id="c1", urgency="E4", care_mode="self_care", home_observation=obs,
            followup={"required": True, "task_id": "t1", "due_at": obs.next_review_at,
                      "owner_queue_id": "q", "assignee_id": None, "unavailable_reason": None},
            primary_action="先在家观察", time={"display_window": "居家观察"},
            condition_summary="两天来咽痛、流涕、低热，没有危险表现",
            possible_conditions=[{"name": "普通上呼吸道感染（较可能）", "likelihood": "较可能"}],
            risks_to_exclude=["呼吸困难/低氧", "持续高热不退"],
            rationale="目前没有危险信号，属于可居家观察的轻症。",
            facility_level="居家观察",
            confirm_method="如超过观察期未改善，线下就诊明确病因",
            doctor_review_note="本结论已提交平台医生审核；如有调整会在本群同步。",
        )
        assert validate_plan(plan) == []
        view = fd_render.render(plan, emergency_number="120")
        assert "本群" in (view.observation_review_note or "")
        assert fd_render.check_patient_text(view, plan) == []
        # 任务清单：观察动作 + 主动回访，标题短句，不再是模型长段落
        assert view.headline == "可以先居家观察，按下面的清单执行。"
        assert view.tasks and all(t.kind == "observe" for t in view.tasks)
        assert any("回访" in t.title for t in view.tasks)

    def test_headline_short_and_tasks_structured(self) -> None:
        """标题只留「时限 + 具体机构」；预约/准备/检查拆成带状态的任务清单。"""
        long_action = ("请今天尽快到线下门诊就诊（消化内科、全科或肠道门诊），评估脱水、电解质和感染情况；"
                       "继续少量多次口服补液盐或淡盐水，清淡饮食，不要自行吃止泻药。"
                       "若出现血便/黑便、剧烈腹痛、晕厥、尿少，立即拨打120或前往急诊。")
        plan = ActionablePlan(
            case_id="c1", urgency="E2", care_mode="in_person", primary_action=long_action,
            time={"display_window": "今天内"}, facility_level="二级医院",
            destination={"care_setting": "in_person", "department": "消化内科"},
            preparation=["带上身份证、医保卡"], tests_needed=["血常规", "电解质"],
            condition_summary="腹泻两天，能正常喝水",
            possible_conditions=[{"name": "急性胃肠炎（较可能）", "likelihood": "较可能"}],
            risks_to_exclude=["脱水"], rationale="需要线下评估脱水与感染情况。",
            confirm_method="医生结合检查判断", doctor_review_note="本结论已由平台医生审核。",
        )
        view = fd_render.render(plan, emergency_number="120")
        assert view.headline == "今天内到示例市第三医院·消化内科就诊。", "标题要落到具体机构名"
        assert long_action not in view.headline, "模型长段落不得进入首屏标题"
        assert view.care_options is not None and view.care_options.options[0].name == "示例市第三医院"
        kinds = [t.kind for t in view.tasks]
        assert kinds[0] == "book" and view.tasks[0].status == "todo"
        assert "示例市第三医院" in view.tasks[0].detail, "预约任务要给出具体机构的入口"
        book = view.tasks[0]
        assert book.when == "今天内" and book.where == "示例市第三医院·消化内科"
        assert book.cost == "挂号费约 ¥30"
        assert "prepare" in kinds and "check" in kinds
        assert any("血常规" in t.detail for t in view.tasks if t.kind == "check")
        assert fd_render.check_patient_text(view, plan) == []

    def test_every_task_has_four_elements(self) -> None:
        """任务四要素：任务（title）+ 时间（when）+ 地点（where）+ 花费（cost）。"""
        res = _screen("胸痛，出冷汗")
        e2 = ActionablePlan(
            case_id="c1", urgency="E2", care_mode="in_person",
            time={"display_window": "今天内"}, facility_level="二级医院",
            destination={"care_setting": "in_person", "department": "呼吸内科"},
            tests_needed=["血常规", "C反应蛋白（CRP）"], preparation=["身份证、医保卡"],
            condition_summary="发热咳嗽3天", possible_conditions=[{"name": "肺炎（需要优先排除）"}],
            risks_to_exclude=["肺炎"], rationale="需要面诊。", confirm_method="医生结合检查判断",
            doctor_review_note="已审核",
        )
        plans = [
            e2,
            fd_engine.emergency_plan("c1", res),
            ActionablePlan(
                case_id="c1", urgency="E4", care_mode="self_care",
                facility_level="居家观察",
                home_observation=obs_mod.build_observation_plan(
                    "c1", 1, pathways.find_pathway("嗓子疼，流鼻涕，低烧", release_stage="DEMO"), task_id="t1"),
                time={"display_window": "居家观察"}, primary_action="先在家观察",
                condition_summary="咽痛流涕低热", possible_conditions=[{"name": "普通上呼吸道感染（较可能）"}],
                risks_to_exclude=["持续高热不退"], rationale="低风险轻症",
                confirm_method="超过观察期未改善线下就诊", doctor_review_note="已审核",
            ),
        ]
        for plan in plans:
            view = fd_render.render(plan, emergency_number="120", profile="住址：示例市朝阳区")
            assert view.tasks, f"{plan.care_mode} 应有任务"
            for t in view.tasks:
                assert t.title, "缺少任务"
                assert t.when, f"任务缺少时间：{t.title}"
                assert t.where, f"任务缺少地点：{t.title}"
                assert t.cost, f"任务缺少花费：{t.title}"

    def test_clinical_findings_are_structured(self) -> None:
        """疑似诊断（含可能性/依据）、重点排除、建议检查、如何确诊都要在患者视图里说清楚。"""
        plan = ActionablePlan(
            case_id="c1", urgency="E2", care_mode="in_person",
            time={"display_window": "今天内"}, facility_level="二级医院",
            destination={"care_setting": "in_person", "department": "呼吸内科"},
            tests_needed=["血常规", "C反应蛋白（CRP）", "胸部X线或CT（按医生判断）", "流感抗原"],
            condition_summary="发热咳嗽3天",
            possible_conditions=[
                {"name": "细菌性肺炎（需要优先排除）", "likelihood": "", "evidence": "发热3天伴黄痰"},
                {"name": "急性支气管炎", "likelihood": "较可能", "evidence": ""},
            ],
            risks_to_exclude=["肺炎", "低氧血症"], rationale="需要检查排除肺炎。",
            confirm_method="医生结合影像与化验判断", doctor_review_note="本结论已由平台医生审核。",
        )
        view = fd_render.render(plan, emergency_number="120")
        first = view.conditions[0]
        assert first.name == "细菌性肺炎", "名称里自带的括号可能性要拆到 likelihood"
        assert first.likelihood == "需要优先排除" and first.evidence == "发热3天伴黄痰"
        assert view.conditions[1].name == "急性支气管炎" and view.conditions[1].likelihood == "较可能"
        check = next(t for t in view.tasks if t.kind == "check")
        assert "流感抗原" in check.detail, "检查项要按优先级列全，不能截断"
        assert "按医生判断" not in check.detail
        assert view.confirm_method == "医生结合影像与化验判断"
        assert fd_render.check_patient_text(view, plan) == []

    def test_confirmed_booking_task_marks_confirmed(self) -> None:
        plan = ActionablePlan(
            case_id="c1", urgency="E2", care_mode="in_person",
            time={"display_window": "今天内"}, facility_level="二级医院",
            destination={"care_setting": "in_person", "department": "消化内科"},
            booking={"state": "CONFIRMED", "request_id": "r", "receipt_id": "rcpt",
                     "appointment_at": "2026-10-14T09:30:00"},
            condition_summary="腹泻两天", possible_conditions=[{"name": "急性胃肠炎（较可能）"}],
            risks_to_exclude=["脱水"], rationale="需要线下评估。", confirm_method="医生结合检查判断",
            doctor_review_note="本结论已由平台医生审核。",
        )
        view = fd_render.render(plan, emergency_number="120")
        book = next(t for t in view.tasks if t.kind == "book")
        assert book.status == "confirmed" and "预约已确认" in book.title
        assert fd_render.check_patient_text(view, plan) == []

    def test_booking_not_confirmed_cannot_be_claimed(self) -> None:
        plan = ActionablePlan(case_id="c1", urgency="E2", care_mode="in_person",
                              booking={"state": "NOT_REQUESTED", "request_id": None,
                                       "receipt_id": None, "appointment_at": None})
        view = fd_render.render(plan, emergency_number="120")
        view.headline = "已经帮你预约成功，明天去就行"
        assert any("预约成功" in v for v in fd_render.check_patient_text(view, plan))


# ---------------------------------------------------------------- 4. 居家观察
class TestObservation:
    def _obs(self, pathway_id: str = "PATHWAY-DEMO-URI-MILD") -> HomeObservationPlan:
        pw = pathways.pathway_by_id(pathway_id, release_stage="DEMO")
        assert pw is not None
        return obs_mod.build_observation_plan("c1", 1, pw, task_id="t1")

    def test_time_windows_are_ordered(self) -> None:
        obs = self._obs()
        assert obs.next_review_at <= obs.response_due_at <= obs.reassessment_due_at <= obs.maximum_end_at

    def test_continue_does_not_reset_max_deadline(self) -> None:
        obs = self._obs()
        before = obs.maximum_end_at
        obs = obs_mod.apply_review(obs, decision="CONTINUE",
                                   next_review_at=datetime.now() + timedelta(hours=4))
        assert obs.state == ObservationState.ACTIVE.value
        assert obs.maximum_end_at == before
        assert len(obs.reviews) == 1

    def test_extend_requires_clinician_receipt(self) -> None:
        obs = self._obs()
        with pytest.raises(ValueError):
            obs_mod.extend_observation(obs, new_end_at=datetime.now() + timedelta(days=7), approval_receipt_id="",
                                       reason="再观察")
        obs2 = obs_mod.extend_observation(
            obs, new_end_at=datetime.fromisoformat(obs.maximum_end_at) + timedelta(hours=12),
            approval_receipt_id="doctor-receipt-1", reason="医师评估后延长")
        assert len(obs2.extensions) == 1
        assert validate_plan(ActionablePlan(case_id="c1", urgency="E4", care_mode="self_care",
                                            home_observation=obs2,
                                            followup={"required": True, "task_id": "t1",
                                                      "due_at": obs2.next_review_at,
                                                      "owner_queue_id": "q", "assignee_id": None,
                                                      "unavailable_reason": None},
                                            time={"display_window": "居家观察"},
                                            condition_summary="轻症上呼吸道感染",
                                            possible_conditions=[{"name": "普通上呼吸道感染", "likelihood": "较可能"}],
                                            risks_to_exclude=["呼吸困难"],
                                            rationale="无危险信号，可继续观察",
                                            facility_level="居家观察",
                                            confirm_method="如未改善，线下就诊明确",
                                            doctor_review_note="已提交医生审核")) == []

    def test_overdue_is_not_improved(self) -> None:
        obs = obs_mod.mark_contact_overdue(self._obs())
        assert obs.state == ObservationState.CONTACT_OVERDUE.value
        assert not obs.reviews

    def test_expired_detection(self) -> None:
        obs = self._obs()
        assert not obs_mod.is_expired(obs)
        assert obs_mod.is_expired(obs, now=datetime.fromisoformat(obs.maximum_end_at) + timedelta(minutes=1))


# ---------------------------------------------------------------- 5. 引擎
def _stub_store(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    async def fake_save(group_id: str, plan: ActionablePlan, *, idem_key: str | None = None) -> dict[str, Any]:
        captured["plan"] = plan
        plan.plan_version = 1
        return {"plan_version": 1}

    async def fake_create(group_id: str, obs: HomeObservationPlan, *,
                          idem_key: str | None = None) -> dict[str, Any]:
        captured["obs"] = obs
        return {"observation_id": obs.observation_id, "plan": obs.model_dump(mode="json")}

    async def fake_update(observation_id: str, obs: HomeObservationPlan, **kw: Any) -> dict[str, Any]:
        captured["updated_obs"] = obs
        return {"observation_id": observation_id}

    monkeypatch.setattr(fd_store, "save_plan", fake_save)
    monkeypatch.setattr(fd_store, "create_observation", fake_create)
    monkeypatch.setattr(fd_store, "update_observation", fake_update)


_TRIAGE_SELF_CARE: dict[str, Any] = {
    "status": "concluded",
    "questions": [],
    "collected_facts": {"起病时间": "两天前"},
    "condition_summary": "两天来咽痛、流涕、低热 37.8℃，没有呼吸困难等危险表现",
    "possible_conditions": [{"name": "普通上呼吸道感染（较可能）", "likelihood": "较可能",
                              "evidence": "咽痛、流涕、低热"},
                            {"name": "流感或其他病毒感染（需要观察）", "likelihood": "需观察", "evidence": "低热"}],
    "risks_to_exclude": ["呼吸困难/低氧", "持续高热不退"],
    "rationale": "目前没有危险信号，属于可居家观察的轻症，按计划观察即可。",
    "facility_level": "居家观察",
    "tests_needed": ["体温监测"],
    "confirm_method": "如超过观察期未改善，线下就诊明确病因",
    "primary_problem": "上呼吸道感染（轻症）",
    "urgency": "E4",
    "care_mode": "self_care",
    "confidence_status": "qualified",
    "required_capabilities": [],
    "care_setting": "self_care",
    "department": None,
    "time_window": "居家观察，症状加重随时就医",
    "deadline_hours": None,
    "action_now": "先在家观察，多喝水休息",
    "actions": ["多喝温水、注意休息"],
    "escalation_triggers": [{"change": "呼吸困难或高热不退", "action": "立即拨打120或前往急诊", "urgency": "E0"}],
    "missing_critical_data": [],
    "safety_flags": [],
    "preparation": [],
    "self_care": {"eligible": True, "pathway_id": None, "reason": "轻症、无危险信号"},
    "explanation": "多为病毒感染，先观察并对症处理",
    "question": "",
    "red_flags": [],
}

_TRIAGE_MISSING_DATA: dict[str, Any] = {
    **_TRIAGE_SELF_CARE, "missing_critical_data": ["年龄和基础疾病"], "self_care": {"eligible": False},
    "care_mode": "in_person", "care_setting": "in_person", "urgency": "E4", "confidence_status": "uncertain",
}


class TestEngine:
    def test_emergency_skips_llm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}
        _stub_store(monkeypatch, captured)
        calls = {"n": 0}

        async def fake_call_json(*a: Any, **kw: Any) -> tuple[dict[str, Any], Any]:
            calls["n"] += 1
            return {}, None

        monkeypatch.setattr("app.wecom.llm.call_json", fake_call_json)
        out = asyncio.run(fd_engine.assess(group_id="g1", profile="", user_text="胸痛，出冷汗",
                                           context="", idem_key="run1"))
        assert out.emergency and out.plan.urgency == "E0"
        assert calls["n"] == 0, "已知危险信号不得等待 LLM"
        assert "120" in out.text

    def test_self_care_creates_observation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}
        _stub_store(monkeypatch, captured)

        async def fake_call_json(*a: Any, **kw: Any) -> tuple[dict[str, Any], Any]:
            return _TRIAGE_SELF_CARE, None

        monkeypatch.setattr("app.wecom.llm.call_json", fake_call_json)
        out = asyncio.run(fd_engine.assess(group_id="g1", profile="", user_text="嗓子疼，流鼻涕，低烧",
                                           context="", idem_key="run2", consultation={"rounds": 2}))
        assert out.kind == "plan"
        assert out.plan.care_mode == "self_care"
        assert out.plan.home_observation is not None
        assert out.plan.followup["required"] is True and out.plan.followup["task_id"]
        assert out.plan.home_observation.observation_id == out.plan.followup["task_id"]
        assert validate_plan(out.plan) == []
        assert "本群" in out.text
        assert "病情了解" in out.text and "为什么" in out.text
        assert out.plan.doctor_review_note and "审核" in out.plan.doctor_review_note

    def test_missing_data_becomes_eu(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}
        _stub_store(monkeypatch, captured)

        async def fake_call_json(*a: Any, **kw: Any) -> tuple[dict[str, Any], Any]:
            return _TRIAGE_MISSING_DATA, None

        monkeypatch.setattr("app.wecom.llm.call_json", fake_call_json)
        out = asyncio.run(fd_engine.assess(group_id="g1", profile="", user_text="感觉不太舒服",
                                           context="", idem_key="run3", consultation={"rounds": 2}))
        assert out.plan.urgency == "EU"
        assert out.plan.human_review_required

    def test_llm_failure_falls_back_to_eu(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}
        _stub_store(monkeypatch, captured)

        async def boom(*a: Any, **kw: Any) -> tuple[dict[str, Any], Any]:
            raise RuntimeError("模型不可用")

        monkeypatch.setattr("app.wecom.llm.call_json", boom)
        out = asyncio.run(fd_engine.assess(group_id="g1", profile="", user_text="有点头晕",
                                           context="", idem_key="run4"))
        assert out.plan.urgency == "EU"
        assert "居家" not in out.text or "不能" in out.text

    def test_self_care_rejected_without_pathway(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """没有匹配的已批准路径时不得居家观察（不能凭模型自证）。"""
        captured: dict[str, Any] = {}
        _stub_store(monkeypatch, captured)
        raw = {**_TRIAGE_SELF_CARE, "primary_problem": "全身不适，原因待查",
               "condition_summary": "全身不适，原因待查", "collected_facts": {}}

        async def fake_call_json(*a: Any, **kw: Any) -> tuple[dict[str, Any], Any]:
            return raw, None

        monkeypatch.setattr("app.wecom.llm.call_json", fake_call_json)
        out = asyncio.run(fd_engine.assess(group_id="g1", profile="", user_text="全身有点不得劲",
                                           context="", idem_key="run5", consultation={"rounds": 2}))
        assert out.plan.care_mode != "self_care"
        assert out.plan.urgency in ("E3", "EU") and out.plan.home_observation is None

    def test_question_turn_asks_before_concluding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """信息不足：只追问，不给结论、不建观察任务。"""
        captured: dict[str, Any] = {}
        _stub_store(monkeypatch, captured)
        raw = {"status": "need_more_info", "questions": ["疼痛是从什么时候开始的？", "有没有出冷汗、气短？"],
               "collected_facts": {"症状": "上腹痛"}, "condition_summary": "目前了解到上腹不适",
               "urgency": "EU", "care_mode": "initial_navigation"}

        async def fake_call_json(*a: Any, **kw: Any) -> tuple[dict[str, Any], Any]:
            return raw, None

        monkeypatch.setattr("app.wecom.llm.call_json", fake_call_json)
        out = asyncio.run(fd_engine.assess(group_id="g1", profile="", user_text="肚子有点不舒服",
                                           context="", idem_key="run7", consultation={}))
        assert out.kind == "question" and out.plan is None
        assert len(out.questions) == 2 and out.consultation["rounds"] == 1
        assert out.consultation["status"] == "in_progress"
        assert "检查" in out.text

    def test_min_rounds_prevents_early_conclusion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """普通病例：即使模型想直接下结论，前 min_rounds 轮也先追问。"""
        captured: dict[str, Any] = {}
        _stub_store(monkeypatch, captured)

        async def fake_call_json(*a: Any, **kw: Any) -> tuple[dict[str, Any], Any]:
            return _TRIAGE_SELF_CARE, None

        monkeypatch.setattr("app.wecom.llm.call_json", fake_call_json)
        out = asyncio.run(fd_engine.assess(group_id="g1", profile="", user_text="嗓子疼，流鼻涕，低烧",
                                           context="", idem_key="run8", consultation={}))
        assert out.kind == "question" and out.plan is None
        assert out.consultation["rounds"] == 1

    def test_max_rounds_forces_conservative_conclusion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """达到最大轮次后不再无限追问，按现有信息给保守结论。"""
        captured: dict[str, Any] = {}
        _stub_store(monkeypatch, captured)
        raw = {"status": "need_more_info", "questions": ["还有哪里不舒服？"], "urgency": "EU",
               "care_mode": "in_person", "care_setting": "in_person",
               "condition_summary": "症状资料仍不完整", "rationale": "按现有信息保守处理",
               "possible_conditions": [{"name": "需线下评估", "likelihood": "待明确"}],
               "risks_to_exclude": ["不能排除的危险情况"], "facility_level": "二级医院",
               "missing_critical_data": ["起病时间"]}

        async def fake_call_json(*a: Any, **kw: Any) -> tuple[dict[str, Any], Any]:
            return raw, None

        monkeypatch.setattr("app.wecom.llm.call_json", fake_call_json)
        out = asyncio.run(fd_engine.assess(group_id="g1", profile="", user_text="还是不舒服",
                                           context="", idem_key="run9", consultation={"rounds": 4, "facts": {}}))
        assert out.kind == "plan" and out.plan is not None
        assert out.plan.urgency in ("EU", "E3", "E2")
        assert any("最大问诊轮次" in w for w in out.warnings)

    def test_stable_followup_not_downgraded_by_missing_labs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """已确诊稳定复诊不因缺少常规复查资料而降到 EU。"""
        captured: dict[str, Any] = {}
        _stub_store(monkeypatch, captured)
        raw = {**_TRIAGE_MISSING_DATA, "care_mode": "stable_followup", "care_setting": "online",
               "urgency": "E5", "missing_critical_data": ["最近一次血脂"], "confidence_status": "qualified"}

        async def fake_call_json(*a: Any, **kw: Any) -> tuple[dict[str, Any], Any]:
            return raw, None

        monkeypatch.setattr("app.wecom.llm.call_json", fake_call_json)
        out = asyncio.run(fd_engine.assess(group_id="g1", profile="", user_text="血压稳定，想问复诊时间",
                                           context="", idem_key="run11", consultation={"rounds": 2}))
        assert out.plan.care_mode == "stable_followup" and out.plan.urgency == "E5"

    def test_emergency_mode_normalized_when_urgency_conflicts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """EU + emergency 这种自我矛盾的组合要归一到线下评估。"""
        captured: dict[str, Any] = {}
        _stub_store(monkeypatch, captured)
        raw = {**_TRIAGE_SELF_CARE, "care_mode": "emergency", "care_setting": "emergency",
               "urgency": "EU", "confidence_status": "qualified", "missing_critical_data": []}

        async def fake_call_json(*a: Any, **kw: Any) -> tuple[dict[str, Any], Any]:
            return raw, None

        monkeypatch.setattr("app.wecom.llm.call_json", fake_call_json)
        out = asyncio.run(fd_engine.assess(group_id="g1", profile="", user_text="说不清楚哪里不舒服",
                                           context="", idem_key="run12", consultation={"rounds": 2}))
        assert out.plan.care_mode == "in_person" and out.plan.urgency == "EU"

    def test_in_person_facility_level_not_self_care(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """线下就医的机构级别不能写成“居家观察”（模型输出不一致时服务端纠正）。"""
        captured: dict[str, Any] = {}
        _stub_store(monkeypatch, captured)
        raw = {**_TRIAGE_SELF_CARE, "care_mode": "in_person", "care_setting": "in_person",
               "urgency": "E3", "facility_level": "居家观察", "missing_critical_data": []}

        async def fake_call_json(*a: Any, **kw: Any) -> tuple[dict[str, Any], Any]:
            return raw, None

        monkeypatch.setattr("app.wecom.llm.call_json", fake_call_json)
        out = asyncio.run(fd_engine.assess(group_id="g1", profile="", user_text="嗓子疼流鼻涕",
                                           context="", idem_key="run13", consultation={"rounds": 3}))
        assert out.plan.care_mode == "in_person"
        assert "居家" not in out.plan.facility_level

    def test_conclusion_submitted_for_doctor_review(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """第 3 条要求：结论由真人医生审核，不引导患者去找医生问诊。"""
        captured: dict[str, Any] = {}
        _stub_store(monkeypatch, captured)

        async def fake_call_json(*a: Any, **kw: Any) -> tuple[dict[str, Any], Any]:
            return _TRIAGE_SELF_CARE, None

        monkeypatch.setattr("app.wecom.llm.call_json", fake_call_json)
        out = asyncio.run(fd_engine.assess(group_id="g1", profile="", user_text="嗓子疼，流鼻涕，低烧",
                                           context="", idem_key="run10", consultation={"rounds": 3}))
        assert out.plan.doctor_review_note and "审核" in out.plan.doctor_review_note
        assert out.plan.handoff.get("state") == "REQUESTED"
        assert out.push_doctor_card is False
        assert "找医生" not in out.text and "线上问诊" not in out.text

    def test_observation_review_escalates_on_red_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}
        _stub_store(monkeypatch, captured)
        pw = pathways.pathway_by_id("PATHWAY-DEMO-URI-MILD", release_stage="DEMO")
        assert pw is not None
        obs = obs_mod.build_observation_plan("c1", 1, pw, task_id="t1")
        row = {"observation_id": obs.observation_id, "plan": obs.model_dump(mode="json"),
               "state": obs.state, "group_id": "g1", "case_id": "c1"}
        out = asyncio.run(fd_engine.review_observation(group_id="g1", obs_row=row,
                                                       user_text="现在喘不上气，嘴唇发紫",
                                                       context="", idem_key="run6"))
        assert out.emergency and out.plan.urgency == "E0"
        assert out.review and out.review["decision"] == "ESCALATE"
        assert captured["updated_obs"].state == ObservationState.ESCALATED.value


# ---------------------------------------------------------------- 6. 群内发布：计划卡承载完整规划
class TestFdRespond:
    """启用结构化计划卡时，群里只发一句引导，完整规划由卡片承载；未启用时发完整纯文本。"""

    @staticmethod
    def _plan_view() -> tuple[ActionablePlan, dict[str, Any]]:
        plan = ActionablePlan(
            case_id="c1", urgency="E4", care_mode="in_person",
            primary_action="今天内到呼吸内科面诊", time={"display_window": "今天内"},
            condition_summary="发热咳嗽3天，没有呼吸困难",
            possible_conditions=[{"name": "急性支气管炎（较可能）", "likelihood": "较可能"}],
            risks_to_exclude=["肺炎"], rationale="需要面诊听诊并化验。",
            facility_level="二级医院", confirm_method="医生结合检查判断",
            doctor_review_note="本结论已由平台医生审核。",
        )
        view = fd_render.render(plan, emergency_number="120")
        return plan, {"kind": "plan", "plan": plan.model_dump(mode="json"),
                      "view": view.model_dump(mode="json"), "notify_manager": False}

    def _run(self, monkeypatch: pytest.MonkeyPatch, *, card: bool) -> list[dict[str, Any]]:
        from app.core.config import get_settings
        from app.wecom import graph as wecom_graph

        _, fd = self._plan_view()
        sent: list[dict[str, Any]] = []

        async def fake_emit(state: dict[str, Any], **kw: Any) -> dict[str, Any]:
            sent.append(kw)
            return {"id": len(sent)}

        async def fake_load(state: dict[str, Any]) -> tuple[Any, Any]:
            g = type("G", (), {"case_manager_id": None, "case_manager_name": None})()
            m = type("M", (), {"id": 1, "sender_name": "王叔叔"})()
            return g, m

        async def fake_batch(state: dict[str, Any]) -> list[Any]:
            return [type("B", (), {"sender_id": "u1"})()]

        settings = get_settings().model_copy(update={"wecom_fd_plan_card": card})
        monkeypatch.setattr(wecom_graph, "get_settings", lambda: settings)
        monkeypatch.setattr(wecom_graph, "emit", fake_emit)
        monkeypatch.setattr(wecom_graph, "_load", fake_load)
        monkeypatch.setattr(wecom_graph, "_batch", fake_batch)
        monkeypatch.setattr(wecom_graph, "_commit", lambda cfg: None)

        state = {"group_id": "g1", "message_id": 1, "message_ids": [1], "run_id": "run-x", "fd": fd}
        out = asyncio.run(wecom_graph.node_fd_respond(state, {}))
        assert out["reply"] == sent[0]["content"]  # 运行记录与群内文本一致
        return sent

    def test_card_enabled_sends_lead_then_card(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent = self._run(monkeypatch, card=True)
        assert [x["kind"] for x in sent] == ["text", "plan_card"]
        assert sent[0]["content"] == "我把您的情况整理成了下面的就医规划，请按卡片执行；症状有变化随时在群里说。"
        assert "病情了解" not in sent[0]["content"], "卡片承载完整规划，文本不应再发一遍长文"
        card_extra = sent[1]["extra"]
        assert card_extra["urgency"] == "E4" and card_extra["view"]["headline"]

    def test_card_disabled_sends_full_plain_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent = self._run(monkeypatch, card=False)
        assert [x["kind"] for x in sent] == ["text"]
        text = sent[0]["content"]
        assert "病情了解" in text and "为什么这样安排" in text, "纯文本通道必须保留完整规划"


# ---------------------------------------------------------------- 7. 风险信号 / 推荐机构与费用


class TestRiskSignalsAndCareNetwork:
    def _plan_e2(self) -> ActionablePlan:
        return ActionablePlan(
            case_id="c1", urgency="E2", care_mode="in_person",
            time={"display_window": "今天内"}, facility_level="二级医院",
            destination={"care_setting": "in_person", "department": "呼吸内科"},
            tests_needed=["血常规", "C反应蛋白（CRP）", "胸部X线或CT", "流感抗原"],
            condition_summary="发热咳嗽3天", possible_conditions=[{"name": "肺炎（需要优先排除）"}],
            risks_to_exclude=["肺炎", "低氧血症"], rationale="需要线下面诊。",
            confirm_method="医生结合影像与化验判断", doctor_review_note="本结论已由平台医生审核。",
            escalation_triggers=[{"change": "出现呼吸困难、口唇发紫", "action": "立即拨打120或前往急诊"}],
        )

    def test_risk_signals_mapping_and_fallback(self) -> None:
        from app.wecom.fd import risk_signals as rs

        assert rs.signals_for("肺炎"), "肺炎应有对应的危险信号"
        assert rs.signals_for("未知风险") == []
        plan = self._plan_e2()
        risks = fd_render._build_risk_signals(plan)
        assert [r.name for r in risks] == ["肺炎", "低氧血症"], "风险按输入顺序、有名称去重"
        assert risks[0].signals, "每条风险都要有危险信号"
        assert risks[0].action and risks[1].action

    def test_safety_groups_merge_dedup_and_order(self) -> None:
        """升级指征 + 风险信号合并去重，按紧急程度排序分组（首屏安全提醒）。"""
        plan = self._plan_e2()
        plan.escalation_triggers = [
            {"change": "出现呼吸困难、口唇发紫", "action": "立即拨打120或前往急诊"},
            {"change": "发热持续不退、精神变差", "action": "今天内到急诊评估"},
        ]
        plan.risks_to_exclude = ["肺炎"]
        view = fd_render.render(plan, emergency_number="120")
        levels = [g.level for g in view.safety_groups]
        assert levels and levels[0] == "emergency", "紧急信号必须排在最前"
        assert "today" in levels
        signals = [s for g in view.safety_groups for s in g.signals]
        normalized = [fd_render._norm_signal(s) for s in signals]
        assert len(normalized) == len(set(normalized)), "跨分组不得出现重复信号"
        assert sum("呼吸困难、口唇发紫" == s for s in signals) == 1
        assert any(g.level == "today" and "发热持续不退、精神变差" in g.signals for g in view.safety_groups)

    def test_risk_signals_dedup(self) -> None:
        plan = self._plan_e2()
        plan.risks_to_exclude = ["肺炎", "肺炎 ", "低氧血症", "低氧血症"]
        risks = fd_render._build_risk_signals(plan)
        assert [r.name for r in risks] == ["肺炎", "低氧血症"]
        for r in risks:
            assert len(r.signals) == len(set(r.signals)), "同一风险的信号不能重复"

    def test_render_attaches_risks_and_care_options(self) -> None:
        plan = self._plan_e2()
        view = fd_render.render(plan, emergency_number="120", profile="患者：王某；住址：示例市朝阳区")
        assert [r.name for r in view.risk_signals] == ["肺炎", "低氧血症"]
        assert view.care_options is not None
        rec = view.care_options.options[0]
        assert rec.recommended and rec.district == "朝阳区" and rec.level_rank == 2
        assert rec.total == 215 and rec.reimbursed == 69 and rec.out_of_pocket == 146
        assert rec.covers_needs is True, "推荐机构要明确具备本次所需能力"
        assert view.care_options.saving == 129
        assert view.care_options.options[1].level_rank == 3
        assert "呼吸内科" in view.care_options.needs and "血常规" in view.care_options.needs
        assert view.care_options.plan_note and "不会改变" in view.care_options.plan_note

    def test_care_options_skipped_for_emergency_and_self_care(self) -> None:
        e0 = ActionablePlan(case_id="c1", urgency="E0", care_mode="emergency",
                            destination={"care_setting": "emergency", "department": "急诊科"})
        assert fd_render.render(e0, emergency_number="120", profile="住址：示例市朝阳区").care_options is None
        self_care = ActionablePlan(case_id="c1", urgency="E4", care_mode="self_care",
                                   destination={"care_setting": "self_care"})
        assert fd_render.render(self_care, emergency_number="120", profile="住址：示例市朝阳区").care_options is None

    def test_region_detection_and_network_basics(self) -> None:
        from app.wecom.fd import care_network

        assert care_network.detect_district("住址：示例市海淀区 中关村") == "海淀区"
        assert care_network.detect_district("没有填写地址") == "朝阳区"
        assert care_network.facility_count() >= 4
        plan = self._plan_e2()
        plan.destination.department = "呼吸内科"
        haidian = care_network.recommend(plan, "住址：示例市海淀区")
        assert haidian is not None and haidian.options[0].district == "海淀区"

    def test_old_view_payload_still_renders(self) -> None:
        """旧消息的 view（无 tasks/conditions/risk_signals/care_options）仍能反序列化并通过门控。"""
        old = {
            "case_id": "c1", "plan_version": 1, "rendered_at": "2026-10-01T00:00:00",
            "headline": "按下面时限就医。", "steps": ["二级医院·消化内科"], "alerts": ["出现呕血请立即急诊"],
            "possible_conditions": ["胆管炎（需要优先排除）"], "risks_to_exclude": ["胆管炎"],
            "condition_summary": "上腹痛", "rationale": "需要面诊", "confirm_method": "医生判断",
            "doctor_review_note": "已审核", "arrangement_note": "当前尚未预约",
        }
        view = PatientPlanView.model_validate(old)
        assert view.tasks == [] and view.conditions == [] and view.risk_signals == []
        assert view.care_options is None
        assert "病情了解" in view.text()


# ---------------------------------------------------------------- 8. 问诊进行中不能漏回
class TestNeedReplyDuringConsultation:
    """多轮问诊进行中：患者的短回答不能被“不需要回复”规则跳过，否则问诊会被卡死。"""

    @staticmethod
    def _msg(role: str, content: str) -> Any:
        return type("M", (), {"id": 1, "sender_role": role, "sender_id": "u1", "content": content,
                              "mentions": [], "attachments": []})()

    def test_answer_during_consultation_always_needs_reply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.wecom import graph as wecom_graph
        from app.wecom.fd import store as fd_store

        msg = self._msg("patient", "平时身体挺好，没有基础病")

        async def fake_load(state: dict[str, Any]) -> tuple[Any, Any]:
            return type("G", (), {"group_id": "g1", "profile": ""})(), msg

        async def fake_batch(state: dict[str, Any]) -> list[Any]:
            return [msg]

        async def no_observation(gid: str) -> None:
            return None

        async def in_progress_case(gid: str) -> dict[str, Any]:
            return {"consultation": {"status": "in_progress", "rounds": 1}}

        async def fail_call_json(*a: Any, **kw: Any) -> Any:
            raise AssertionError("问诊进行中不应再做 LLM“是否需要回复”判断")

        monkeypatch.setattr(wecom_graph, "_load", fake_load)
        monkeypatch.setattr(wecom_graph, "_batch", fake_batch)
        monkeypatch.setattr(wecom_graph, "call_json", fail_call_json)
        monkeypatch.setattr(fd_store, "active_observation", no_observation)
        monkeypatch.setattr(fd_store, "get_case", in_progress_case)

        out = asyncio.run(wecom_graph.node_need_reply(
            {"group_id": "g1", "message_id": 1, "message_ids": [1]}, {}))
        assert out["need_reply"] is True
        assert "问诊" in out["need_reply_reason"]
