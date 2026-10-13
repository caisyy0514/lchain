"""家庭医生 Agent（VBC 分级诊疗 / 就医路径导航）。

模块划分：
- contracts：数据契约与服务端不变量（E0–E5/EU、计划/观察状态机）
- red_flags：确定性红旗规则注册表（安全硬门控）
- pathways：已批准低风险居家观察路径（演示版，生产放行前需医学组审核）
- prompts：Langfuse 提示词（fd-system / fd-triage / fd-followup）
- engine：分诊流水线与服务端门控
- observation：观察准入、时限、复评与退出
- render：患者计划卡与一致性门控
- store：病例/计划/观察任务持久化
"""
