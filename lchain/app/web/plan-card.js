/* 就医规划卡（plan_card）渲染器：wecom.html、plan-card.html、plan-preview.html 共用。
 *
 * 用法：planCardHTML(extra, fallbackContent) -> HTML 字符串
 *
 * 信息分层（患者视角）：
 *   ① 首屏：一句建议 + 关键事实 → 接下来要做（可勾选）→ 安全提醒（合并去重、按紧急程度分组）
 *   ② 次屏：在哪看更划算（推荐机构 vs 升级对照，数字一眼可读，详情可展开）
 *   ③ 折叠：病情与诊断依据（疑似诊断/重点排除/如何确诊/为什么）
 * 旧消息（无 tasks/safety_groups/conditions/care_options）回退到编号步骤 + 标签式风险 + 备注。
 */
(function () {
  const RENDER_VERSION = "2026.10-v2";   // 渲染器版本：卡片根节点会带 data-pc-version，便于排查缓存
  const TONE = { E0: "critical", E1: "critical", E2: "urgent", E3: "urgent", E4: "routine", E5: "routine", EU: "unknown" };
  const LABEL = { E0: "立即急救", E1: "尽快急诊", E2: "当日就医", E3: "限时面诊", E4: "计划门诊", E5: "稳定复诊", EU: "暂不能分级" };
  const TASK_STATUS = { todo: "待办", confirmed: "已确认", active: "进行中" };
  const SAFETY_LABEL = { emergency: "立即拨打 120 / 急诊", today: "今天内就医", soon: "尽快就医" };

  function isSelfCare(extra, v) {
    return extra.care_mode === "self_care" || /居家观察/.test(String(v.urgency_word || ""));
  }

  function toneOf(extra, v) {
    if (isSelfCare(extra, v)) return "safe";
    if (TONE[extra.urgency]) return TONE[extra.urgency];
    const word = String(v.urgency_word || "");
    if (/立即|尽快|急诊/.test(word)) return "critical";
    if (/今天|当日|时限/.test(word)) return "urgent";
    return "routine";
  }

  function labelOf(extra, v) {
    if (isSelfCare(extra, v)) return "居家观察";
    return LABEL[extra.urgency] || v.urgency_word || "就医建议";
  }

  function clean(items) {
    return (items || []).map((x) => String(x == null ? "" : x).trim()).filter(Boolean);
  }

  function esc_(s) { return esc(s); }

  const money = (x) => "¥" + Math.round(Number(x) || 0);

  /* ---------------- 任务勾选状态（本机记忆，刷新/重进不丢） ---------------- */
  function taskKey(v) { return "pc:tasks:" + String(v.case_id || "") + ":" + String(v.plan_version || 0); }

  function readDone(key) {
    try { return new Set(JSON.parse(localStorage.getItem(key) || "[]").map(String)); } catch (e) { return new Set(); }
  }

  function writeDone(key, set) {
    try { localStorage.setItem(key, JSON.stringify([...set])); } catch (e) { /* 隐私模式忽略 */ }
  }

  /* ---------------- 组件 ---------------- */
  function factChips(v, care, tasks) {
    const rec = care && ((care.options || []).find((o) => o.recommended) || (care.options || [])[0]);
    const facts = [];
    if (rec) {
      if (rec.level) facts.push({ text: `${rec.level}医院`, cls: "" });
      if (rec.travel_minutes) {
        const same = String(care.region || "").includes(String(rec.district || ""));
        facts.push({ text: `${same ? "同区" : "跨区"}约 ${rec.travel_minutes} 分钟`, cls: "" });
      }
      if (rec.insurance) facts.push({ text: rec.insurance, cls: "" });
    }
    const book = (tasks || []).find((t) => t.kind === "book");
    if (book) facts.push({ text: book.status === "confirmed" ? "预约已确认" : "尚未预约", cls: book.status === "confirmed" ? "ok" : "warn" });
    return facts.length ? `<div class="pc-facts">${facts.map((f) => `<span class="pc-fact ${f.cls}">${esc_(f.text)}</span>`).join("")}</div>` : "";
  }

  /* 任务四要素：任务（title）+ 时间/地点/花费 */
  function taskFacts(t) {
    const facts = [];
    if (t.when) facts.push({ k: "时间", v: t.when });
    if (t.where) facts.push({ k: "地点", v: t.where });
    if (t.cost) facts.push({ k: "花费", v: t.cost });
    if (!facts.length) return "";
    return `<div class="pc-task-facts">${facts.map((f) => `<span><i>${f.k}</i>${esc_(f.v)}</span>`).join("")}</div>`;
  }

  function taskRow(t, i, doneSet) {
    const status = String(t.status || "");
    const done = doneSet.has(String(i));
    const cls = done ? "done" : status === "confirmed" ? "confirmed" : status === "todo" ? "todo" : status === "active" ? "active" : "";
    const tag = done ? "已完成" : (TASK_STATUS[status] || "");
    const copyable = t.kind === "book" && t.detail;
    return `<li class="pc-task ${cls}" data-task-index="${i}" data-status="${esc_(status)}">
      <button class="pc-check" type="button" data-task="${i}" aria-pressed="${done}" aria-label="标记完成">${icon("check", { size: 12 })}</button>
      <div class="pc-task-body">
        <div class="pc-task-head"><span class="pc-task-title">${esc_(t.title)}</span>${tag ? `<span class="pc-task-tag" data-tag="${esc_(tag)}">${esc_(tag)}</span>` : ""}</div>
        ${taskFacts(t)}
        ${t.detail ? `<div class="pc-task-detail">${esc_(t.detail)}</div>` : ""}
        ${copyable ? `<button class="pc-copy" type="button" data-copy="${esc_(t.detail)}">${icon("copy", { size: 12 })}复制预约入口</button>` : ""}
      </div>
    </li>`;
  }

  function tasksHTML(tasks, doneSet) {
    if (!tasks.length) return "";
    const todo = tasks.filter((t, i) => t.status === "todo" && !doneSet.has(String(i))).length;
    return `<section class="pc-sec" data-sec="tasks">
      <div class="pc-sec-head"><span class="pc-sec-title">${icon("listChecks", { size: 14 })}接下来要做</span>${todo ? `<span class="pc-count" data-count="${todo}">${todo} 项待办</span>` : ""}</div>
      <ul class="pc-tasks">${tasks.map((t, i) => taskRow(t, i, doneSet)).join("")}</ul>
    </section>`;
  }

  function safetyHTML(v) {
    const groups = (v.safety_groups || []).filter((g) => g && (g.signals || []).length);
    if (groups.length) {
      return `<section class="pc-sec" data-sec="safety">
        <div class="pc-sec-head"><span class="pc-sec-title">${icon("alert", { size: 14 })}安全提醒</span><span class="pc-sec-sub">出现任一情况，别等</span></div>
        <div class="pc-safety">
          ${groups.map((g) => `<div class="pc-safety-group" data-level="${esc_(g.level || "soon")}">
            <div class="pc-safety-action">${esc_(g.action || SAFETY_LABEL[g.level] || "出现以下情况请尽快就医")}</div>
            <ul class="pc-safety-signals">${g.signals.map((s) => `<li>${esc_(s)}</li>`).join("")}</ul>
          </div>`).join("")}
        </div>
      </section>`;
    }
    const alerts = clean(v.alerts);
    if (!alerts.length) return "";
    return `<section class="pc-sec" data-sec="safety">
      <div class="pc-sec-head"><span class="pc-sec-title">${icon("alert", { size: 14 })}安全提醒</span></div>
      <div class="pc-alert" role="alert"><ul>${alerts.map((a) => `<li>${esc_(a)}</li>`).join("")}</ul></div>
    </section>`;
  }

  function careOptionDetails(o) {
    return `<div class="pc-care-option${o.recommended ? " rec" : ""}">
      <div class="pc-care-head">
        ${o.recommended ? `<span class="pc-care-badge">推荐</span>` : `<span class="pc-care-badge alt">对照</span>`}
        <span class="pc-care-name">${esc_(o.name)}</span>
      </div>
      <div class="pc-care-meta">${[o.level, o.department, o.district ? `位于${o.district}` : "",
        o.travel_minutes ? `距您约 ${o.travel_minutes} 分钟` : "", o.insurance].filter(Boolean).map((x) => `<span>${esc_(String(x))}</span>`).join("")}</div>
      <div class="pc-care-cost">
        <span>估算总费用<b>${money(o.total)}</b></span>
        <span>医保报销<b>${money(o.reimbursed)}</b></span>
        <span>个人支付<b>${money(o.out_of_pocket)}</b></span>
      </div>
      ${o.entry_method ? `<div class="pc-care-entry">${icon("external", { size: 12 })}${esc_(o.entry_method)}</div>` : ""}
      ${o.reason ? `<div class="pc-care-reason">${esc_(o.reason)}</div>` : ""}
    </div>`;
  }

  function travelText(o, care) {
    if (!o.travel_minutes) return "—";
    const same = String(care.region || "").includes(String(o.district || ""));
    return `${same ? "同区" : "跨区"}约 ${o.travel_minutes} 分钟`;
  }

  function compareCol(o, care, isRec, alt) {
    const rows = [
      { k: "医学能力", v: o.covers_needs || isRec ? "本次所需全部具备" : "具备所需能力", good: true },
      { k: "检查方案", v: alt ? `与本方案一致` : "按本方案执行", good: true },
      { k: "路程时间", v: travelText(o, care) },
      { k: "个人自付", v: money(o.out_of_pocket), money: true },
    ];
    return `<div class="pc-compare-col ${isRec ? "rec" : "alt"}">
      <div class="pc-compare-head">
        <span class="pc-care-badge${isRec ? "" : " alt"}">${isRec ? "推荐" : "升级对照"}</span>
        <b class="pc-care-name">${esc_(o.name)}</b>
        ${o.department ? `<span class="pc-care-tag">${esc_(o.department)}</span>` : ""}
      </div>
      <ul class="pc-compare-rows">
        ${rows.map((r) => `<li><span class="k">${r.k}</span><span class="v${r.good ? " ok" : ""}${r.money ? " money" : ""}">${r.good ? "✓ " : ""}${esc_(r.v)}</span></li>`).join("")}
      </ul>
    </div>`;
  }

  function careHTML(v) {
    const care = v.care_options || null;
    if (!care || !(care.options || []).length) return "";
    const rec = care.options.find((o) => o.recommended) || care.options[0];
    const alt = care.options.find((o) => o !== rec);
    const maxOop = Math.max(rec.out_of_pocket || 0, alt ? alt.out_of_pocket || 0 : 0, 1);
    const pct = (x) => Math.max(6, Math.round(((x || 0) / maxOop) * 100));
    const conclusion = alt && care.saving
      ? `${esc_(rec.level)}医院具备本次所需的全部能力，检查与处理方案与${esc_(alt.level)}一致；个人自付比升级到${esc_(alt.level)}<b>少 ${money(care.saving)}</b>`
      : `${esc_(rec.level)}医院具备本次所需的全部能力`;
    return `<section class="pc-sec" data-sec="care" data-care="1">
      <div class="pc-sec-head"><span class="pc-sec-title">${icon("target", { size: 14 })}在哪看更划算</span></div>
      <div class="pc-care">
        <div class="pc-care-conclusion">${icon("checkCircle", { size: 14 })}本次安排：${conclusion}</div>
        ${(care.needs || []).length ? `<div class="pc-care-needs">本次需要：${care.needs.map((n) => `<span>${esc_(n)}</span>`).join("")}</div>` : ""}
        ${compareCol(rec, care, true, alt)}
        ${alt ? `<div class="pc-compare-vs">${icon("arrowRight", { size: 12 })}升级对照</div>${compareCol(alt, care, false, rec)}` : ""}
        ${alt ? `<div class="pc-bars">
          <div class="pc-bar-row"><span>推荐自付</span><div class="pc-bar"><i class="rec" style="width:${pct(rec.out_of_pocket)}%"></i></div><b>${money(rec.out_of_pocket)}</b></div>
          <div class="pc-bar-row"><span>升级自付</span><div class="pc-bar"><i class="alt" style="width:${pct(alt.out_of_pocket)}%"></i></div><b>${money(alt.out_of_pocket)}</b></div>
          ${care.saving ? `<div class="pc-bar-tip">${icon("info", { size: 12 })}升级到${esc_(alt.level)}医院，个人多花 ${money(care.saving)}（检查与处理方案不变）</div>` : ""}
        </div>` : ""}
        <details class="pc-fold pc-fold-sub">
          <summary>${icon("fileText", { size: 13 })}费用与机构详情${icon("chevronDown", { size: 13, cls: "chev" })}</summary>
          <div class="pc-fold-body">
            ${care.options.map(careOptionDetails).join("")}
            ${care.plan_note ? `<div class="pc-care-saving">${icon("info", { size: 13 })}${esc_(care.plan_note)}</div>` : ""}
            ${care.disclaimer ? `<div class="pc-care-note">${esc_(care.disclaimer)}</div>` : ""}
          </div>
        </details>
      </div>
    </section>`;
  }

  function clinicalFold(v) {
    const conditions = (v.conditions || []).filter((c) => c && String(c.name || "").trim());
    const conditionChips = clean(v.possible_conditions);
    const riskSignals = (v.risk_signals || []).filter((r) => r && String(r.name || "").trim());
    const confirmText = String(v.confirm_method || "").trim();
    const rationale = String(v.rationale || "").trim();
    const summary = String(v.condition_summary || "").trim();
    if (!conditions.length && !conditionChips.length && !riskSignals.length && !confirmText && !rationale && !summary) return "";

    const conditionsHTML = conditions.length
      ? `<ul class="pc-diagnoses">${conditions.map((c) => {
          const lk = String(c.likelihood || "");
          const cls = /排除/.test(lk) ? "exclude" : /较可能|可能/.test(lk) ? "likely" : "unclear";
          return `<li class="pc-diagnosis"><div class="pc-diagnosis-head"><span class="pc-diagnosis-name">${esc_(c.name)}</span>`
            + (lk ? `<span class="pc-likelihood ${cls}">${esc_(lk)}</span>` : "") + `</div>`
            + (c.evidence ? `<div class="pc-diagnosis-evidence">依据：${esc_(c.evidence)}</div>` : "") + `</li>`;
        }).join("")}</ul>`
      : (conditionChips.length ? `<div class="pc-chips">${conditionChips.map((c) => `<span class="pc-chip${/排除/.test(c) ? " risk" : ""}">${esc_(c)}</span>`).join("")}</div>` : "");

    const risksHTML = riskSignals.length
      ? `<ul class="pc-risks">${riskSignals.map((r) => {
          const signals = clean(Array.isArray(r.signals) ? r.signals : []);
          return `<li class="pc-risk"><div class="pc-risk-name">${esc_(r.name)}</div>`
            + (signals.length ? `<ul class="pc-risk-signals">${signals.map((s) => `<li>${esc_(s)}</li>`).join("")}</ul>` : "")
            + (r.action ? `<div class="pc-risk-action">${icon("alert", { size: 12 })}${esc_(r.action)}</div>` : "") + `</li>`;
        }).join("")}</ul>`
      : (clean(v.risks_to_exclude).length
        ? `<div class="pc-chips">${clean(v.risks_to_exclude).map((r) => `<span class="pc-chip risk">${esc_(r)}</span>`).join("")}</div>` : "");

    const signalCount = riskSignals.reduce((n, r) => n + (Array.isArray(r.signals) ? r.signals.length : 0), 0);
    const hint = [conditions.length || conditionChips.length ? `疑似诊断 ${conditions.length || conditionChips.length}` : "",
      signalCount ? `危险信号 ${signalCount}` : "", confirmText ? "如何确诊" : ""].filter(Boolean).join(" · ");

    return `<details class="pc-fold" data-fold="clinical">
      <summary>${icon("stethoscope", { size: 14 })}病情与诊断依据${hint ? `<span class="pc-fold-hint">${esc_(hint)}</span>` : ""}${icon("chevronDown", { size: 14, cls: "chev" })}</summary>
      <div class="pc-fold-body">
        ${summary ? `<div class="pc-field"><div class="pc-field-label">病情了解</div><p>${esc_(summary)}</p></div>` : ""}
        ${conditionsHTML ? `<div class="pc-field"><div class="pc-field-label">疑似诊断</div>${conditionsHTML}</div>` : ""}
        ${risksHTML ? `<div class="pc-field"><div class="pc-field-label">重点排除与危险信号</div>${risksHTML}</div>` : ""}
        ${confirmText ? `<div class="pc-field"><div class="pc-field-label">如何确诊</div><p>${esc_(confirmText)}</p></div>` : ""}
        ${rationale ? `<div class="pc-field"><div class="pc-field-label">为什么这样安排</div><p>${esc_(rationale)}</p></div>` : ""}
      </div>
    </details>`;
  }

  /* 旧消息回退：编号步骤 + 标签式风险 + 安排备注 + 单项折叠 */
  function legacyBody(v, tone) {
    const steps = clean(v.steps);
    const alerts = clean(v.alerts);
    const conditions = clean(v.possible_conditions);
    const risks = clean(v.risks_to_exclude);
    const notes = [];
    if (v.observation_review_note) notes.push({ icon: "clock", cls: "", text: v.observation_review_note });
    if (v.arrangement_note) {
      const text = String(v.arrangement_note);
      const cls = /尚未|未预约|未落实|没有预约/.test(text) ? "warn"
        : /已确认|已预约|已安排|已挂/.test(text) ? "ok" : "";
      notes.push({ icon: "calendar", cls, text });
    }
    const stepsHTML = steps.length ? `<section class="pc-sec">
      <div class="pc-sec-head"><span class="pc-sec-title">${icon("listChecks", { size: 14 })}现在要做</span></div>
      <ol class="pc-steps">${steps.map((s, i) => `<li class="pc-step"><span class="pc-num">${i + 1}</span><span class="pc-step-txt">${esc_(s)}</span></li>`).join("")}</ol>
    </section>` : "";
    const alertsHTML = alerts.length ? `<div class="pc-alert" role="alert">
      <div class="pc-alert-title">${icon("alert", { size: 14 })}重要安全提醒</div>
      <ul>${alerts.map((a) => `<li>${esc_(a)}</li>`).join("")}</ul>
    </div>` : "";
    const confirmText = String(v.confirm_method || "").trim();
    const rationale = String(v.rationale || "").trim();
    const more = (conditions.length || risks.length || confirmText || rationale) ? `<details class="pc-more">
      <summary>${icon("fileText", { size: 14 })}病情判断依据${icon("chevronDown", { size: 14, cls: "chev" })}</summary>
      <div class="pc-more-body">
        ${conditions.length ? `<div class="pc-field"><div class="pc-field-label">可能的方向</div><div class="pc-chips">${conditions.map((c) => `<span class="pc-chip${/排除/.test(c) ? " risk" : ""}">${esc_(c)}</span>`).join("")}</div></div>` : ""}
        ${risks.length ? `<div class="pc-field"><div class="pc-field-label">需要优先排除</div><div class="pc-chips">${risks.map((r) => `<span class="pc-chip risk">${esc_(r)}</span>`).join("")}</div></div>` : ""}
        ${confirmText ? `<div class="pc-field"><div class="pc-field-label">如何确诊</div><p>${esc_(confirmText)}</p></div>` : ""}
        ${rationale ? `<div class="pc-field"><div class="pc-field-label">为什么这样安排</div><p>${esc_(rationale)}</p></div>` : ""}
      </div>
    </details>` : "";
    const body = tone === "critical" ? alertsHTML + stepsHTML : stepsHTML + alertsHTML;
    return { body, notes, more };
  }

  /* ---------------- 主渲染 ---------------- */
  function planCardHTML(extra, fallbackContent) {
    extra = extra || {};
    const v = extra.view || {};
    const tone = toneOf(extra, v);
    const headline = String(v.headline || fallbackContent || "").trim();
    const version = v.plan_version ? ` · v${esc_(String(v.plan_version))}` : "";
    const tasks = (v.tasks || []).filter((t) => t && (String(t.title || "").trim() || String(t.detail || "").trim()));
    const isModern = tasks.length || (v.safety_groups || []).length || v.care_options
      || (v.conditions || []).length || (v.risk_signals || []).length;

    let main = "";
    let fold = "";
    let notesHTML = "";
    const doneKey = taskKey(v);
    const doneSet = readDone(doneKey);

    if (isModern) {
      const tasksBlock = tasksHTML(tasks, doneSet);
      const safetyBlock = safetyHTML(v);
      main = tone === "critical" ? safetyBlock + tasksBlock : tasksBlock + safetyBlock;
      main += careHTML(v);
      fold = clinicalFold(v);
    } else {
      const legacy = legacyBody(v, tone);
      main = legacy.body;
      fold = legacy.more;
      notesHTML = legacy.notes.map((n) => `<div class="pc-note${n.cls ? " " + n.cls : ""}">${icon(n.icon, { size: 14 })}<span>${esc_(n.text)}</span></div>`).join("");
    }

    return `<div class="pc" data-pc-version="${RENDER_VERSION}" data-tone="${tone}" data-task-key="${esc_(doneKey)}" data-case="${esc_(String(v.case_id || ""))}">
  ${v.revision_notice ? `<div class="pc-revision">${icon("refresh", { size: 14 })}<span>${esc_(v.revision_notice)}</span></div>` : ""}
  <div class="pc-head">
    <span class="pc-pill"><span class="dot"></span>${esc_(labelOf(extra, v))}</span>
    <span class="pc-ver">家庭医生计划${version}</span>
  </div>
  <section class="pc-hero">
    ${headline ? `<h3 class="pc-headline">${esc_(headline)}</h3>` : ""}
    ${isModern ? factChips(v, v.care_options, tasks) : ""}
  </section>
  ${main}
  ${notesHTML}
  ${!isModern && v.condition_summary ? `<div class="pc-summary"><div class="pc-summary-label">${icon("info", { size: 14 })}病情了解</div><p>${esc_(v.condition_summary)}</p></div>` : ""}
  ${fold}
  ${v.doctor_review_note ? `<div class="pc-review">${icon("shield", { size: 14 })}<span>${esc_(v.doctor_review_note)}</span></div>` : ""}
  <div class="pc-foot">
    <span>家庭医生服务${version}</span>
    ${extra.release_stage ? `<span class="stage">${esc_(extra.release_stage)}</span>` : ""}
  </div>
</div>`;
  }

  /* ---------------- 交互：勾选任务 / 复制预约入口 ---------------- */
  function initInteractions() {
    if (typeof document === "undefined") return;   // node 测试环境
    if (window.__pcInteractions) return;
    window.__pcInteractions = true;
    document.addEventListener("click", (e) => {
      const check = e.target.closest ? e.target.closest(".pc-check") : null;
      if (check) { toggleTask(check); return; }
      const copy = e.target.closest ? e.target.closest("[data-copy]") : null;
      if (copy) { copyText(copy.dataset.copy || "", copy); }
    });
  }

  function toggleTask(btn) {
    const card = btn.closest(".pc");
    const li = btn.closest(".pc-task");
    if (!card || !li || !li.dataset.taskIndex) return;
    const idx = String(li.dataset.taskIndex);
    const done = readDone(card.dataset.taskKey || "");
    if (done.has(idx)) done.delete(idx); else done.add(idx);
    writeDone(card.dataset.taskKey || "", done);
    li.classList.toggle("done", done.has(idx));
    btn.setAttribute("aria-pressed", done.has(idx) ? "true" : "false");
    const tag = li.querySelector(".pc-task-tag");
    if (tag) tag.textContent = done.has(idx) ? "已完成" : (tag.dataset.tag || "");
    const count = card.querySelector("[data-count]");
    if (count) {
      const todo = [...card.querySelectorAll(".pc-task[data-status='todo']")].filter((el) => !el.classList.contains("done")).length;
      count.textContent = todo + " 项待办";
      count.style.display = todo ? "" : "none";
    }
  }

  function copyText(text, btn) {
    const done = () => {
      const old = btn.innerHTML;
      btn.classList.add("ok");
      btn.textContent = "已复制";
      setTimeout(() => { btn.innerHTML = old; btn.classList.remove("ok"); }, 1600);
    };
    const fallback = () => {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); done(); } catch (e) { /* 忽略 */ }
      ta.remove();
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(fallback);
    } else { fallback(); }
  }

  initInteractions();
  window.planCardHTML = planCardHTML;
  window.PLAN_CARD_VERSION = RENDER_VERSION;
})();
