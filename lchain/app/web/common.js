// 通用工具
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const md = (s) => (window.marked ? marked.parse(s || "") : `<p>${esc(s).replace(/\n/g, "<br>")}</p>`);

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

// 解析 POST 返回的 SSE 流
async function streamSSE(path, body, onEvent) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const raw = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let event = "message", data = "";
      for (const line of raw.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trimStart();
      }
      if (data) { try { onEvent(event, JSON.parse(data)); } catch { onEvent(event, data); } }
    }
  }
}

// 滑块已填充部分的比例（CSS 用 --fill 画进度）
function paintRange(el) {
  if (!el) return;
  const min = Number(el.min || 0), max = Number(el.max || 100);
  const pct = max > min ? ((Number(el.value) - min) / (max - min)) * 100 : 0;
  el.style.setProperty("--fill", pct + "%");
}

async function loadPromptSelectors(nameSel, versionSel) {
  const { items } = await api("/api/prompts");
  nameSel.innerHTML = items.map((p) => `<option value="${esc(p.name)}">${esc(p.name)}</option>`).join("");
  const fill = () => {
    const p = items.find((i) => i.name === nameSel.value) || { versions: [], labels: [] };
    const labels = [...new Set(["production", "latest", ...p.labels.filter((l) => l !== "local")])];
    versionSel.innerHTML =
      labels.map((l) => `<option value="label:${esc(l)}">label: ${esc(l)}</option>`).join("") +
      [...p.versions].sort((a, b) => b - a).map((v) => `<option value="version:${v}">version: v${v}</option>`).join("");
  };
  nameSel.onchange = () => { fill(); nameSel.dispatchEvent(new Event("prompt-changed")); };
  fill();
  return items;
}

function parseVersionSel(v) {
  if (!v) return {};
  const [k, val] = v.split(":");
  return k === "version" ? { prompt_version: Number(val) } : { prompt_label: val };
}
