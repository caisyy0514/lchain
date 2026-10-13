/* 前端计划卡渲染器（app/web/plan-card.js）的 node 测试驱动。
 *
 * 仓库没有 JS 构建/测试链，用 node 直接调用 planCardHTML 并把渲染结果的结构化断言
 * 以 JSON 输出，供 tests/agent/test_plan_card_render.py 断言。
 *
 * 用法: node plan_card_harness.js <plan-card.js 路径> '<cases JSON>'
 */
global.esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
global.icon = () => '<i class="ic"></i>';
global.window = {};
require(process.argv[2]);

const cases = JSON.parse(process.argv[3]);
const out = cases.map((c) => {
  const html = window.planCardHTML(c.extra, c.content || "");
  return {
    key: c.key,
    hasTasks: html.includes("接下来要做"),
    hasSteps: html.includes("现在要做"),
    taskCount: (html.match(/class="pc-task[ "]/g) || []).length,
    taskFactCount: (html.match(/class="pc-task-facts"/g) || []).length,
    compareCount: (html.match(/class="pc-compare-col[ "]/g) || []).length,
    barCount: (html.match(/class="pc-bar-row"/g) || []).length,
    hasConclusion: html.includes("pc-care-conclusion"),
    diagnosisCount: (html.match(/class="pc-diagnosis"/g) || []).length,
    riskCount: (html.match(/<li class="pc-risk"/g) || []).length,
    riskSignalCount: (html.match(/class="pc-risk-signals"/g) || []).length,
    safetyGroupCount: (html.match(/class="pc-safety-group[ "]/g) || []).length,
    riskChipCount: (html.match(/pc-chip risk/g) || []).length,
    hasCare: html.includes('data-care="1"'),
    careOptionCount: (html.match(/class="pc-care-option/g) || []).length,
    hasSaving: html.includes("多花") || html.includes("少 ¥"),
    hasNote: html.includes("pc-note"),
  };
});
console.log(JSON.stringify(out));
