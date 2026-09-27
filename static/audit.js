"use strict";

/* ⑥ 组合修订行为等价审计页面 */

const auditState = {
  compositions: [],
  revisions: [],
  result: null,
  record: null,
};

const audit$ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

function auditStatusClass(status) {
  if (status === "equivalent") return "safe";
  if (status === "inequivalent") return "unsafe";
  return "unknown";
}

async function loadAuditCompositions() {
  try {
    const d = await api("/api/hierarchy/compositions");
    auditState.compositions = d.compositions || [];
    const sel = audit$("audit-comp-select");
    const old = sel.value;
    sel.innerHTML = auditState.compositions.map(c =>
      `<option value="${esc(c.name)}">${esc(c.name)}（当前修订 ${c.version}）</option>`).join("");
    if (old && auditState.compositions.some(c => c.name === old)) sel.value = old;
    if (sel.value) await loadAuditRevisions(sel.value);
  } catch (e) {
    toast("加载组合列表失败：" + e.message);
  }
}

async function loadAuditRevisions(name) {
  const info = audit$("audit-rev-info");
  try {
    const d = await api(`/api/hierarchy/compositions/${encodeURIComponent(name)}/revisions`);
    auditState.revisions = d.revisions || [];
    const opts = auditState.revisions.map(r =>
      `<option value="${r.revision}">修订 ${r.revision} · 展开 ${r.flat_hash || "—"}</option>`).join("");
    audit$("audit-left-rev").innerHTML = opts;
    audit$("audit-right-rev").innerHTML = opts;
    if (auditState.revisions.length >= 2) {
      audit$("audit-left-rev").value = auditState.revisions[1].revision;
      audit$("audit-right-rev").value = auditState.revisions[0].revision;
    }
    info.className = "infobox";
    info.textContent = auditState.revisions.length
      ? `已保存 ${auditState.revisions.length} 个修订快照；请选择两个不同修订。`
      : "该组合还没有修订快照（请先保存组合）。";
  } catch (e) {
    info.className = "infobox err";
    info.textContent = "加载修订失败：" + e.message;
  }
}

function readAuditParams() {
  const raw = audit$("audit-params-json").value.trim();
  if (!raw) return {};
  return JSON.parse(raw);
}

async function runAudit() {
  const comp = audit$("audit-comp-select").value;
  const left = +audit$("audit-left-rev").value;
  const right = +audit$("audit-right-rev").value;
  if (!comp || !left || !right) { toast("请选择组合与两个修订"); return; }
  if (left === right) { toast("请选择两个不同修订"); return; }
  let params;
  try { params = readAuditParams(); }
  catch (e) { toast("审计参数 JSON 解析失败：" + e.message); return; }
  const btn = audit$("btn-audit-run");
  btn.disabled = true; btn.textContent = "枚举审计中…";
  try {
    const d = await api(`/api/hierarchy/compositions/${encodeURIComponent(comp)}/audits`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ left_revision: left, right_revision: right, params }),
    });
    auditState.result = d.result;
    auditState.record = d.record;
    renderAuditResult(d.result, d.record);
    loadAuditRecords();
  } catch (e) {
    toast("审计失败：" + e.message, 5000);
  } finally {
    btn.disabled = false; btn.textContent = "同步枚举并审计";
  }
}

function renderAuditResult(r, record) {
  const banner = audit$("audit-banner");
  const box = audit$("audit-result");
  banner.className = "banner " + auditStatusClass(r.status);
  const recText = record ? `（记录 #${record.id}${record.reused ? "，已去重更新" : ""}）` : "";
  if (r.status === "equivalent") {
    banner.innerHTML = `✓ 行为等价：两修订所有可行轨迹的审计输出波形相同${recText}` +
      `<span class="detail">${esc(r.message || "")}</span>`;
  } else if (r.status === "inequivalent") {
    const dv = r.earliest_divergence || {};
    banner.innerHTML = `⚠ 行为不等价：最早分歧 t=${dv.time}，输出 ${esc(dv.left_output || "")}${recText}` +
      `<span class="detail">${esc(r.message || "")}</span>`;
  } else {
    banner.innerHTML = `? 无法判定（unknown）${recText}` +
      `<span class="detail">${esc(r.message || "")}</span>`;
  }

  let html = "";
  const st = r.statistics || {};
  html += `<h3>枚举统计</h3><table class="audit-table"><tbody>` +
    `<tr><th>输入跃迁组合</th><td>${st.input_assignments ?? "—"}</td></tr>` +
    `<tr><th>修订 A 轨迹 / 输出波形</th><td>${st.left_trajectories ?? "—"} / ${st.left_distinct_output_waveforms ?? "—"}</td></tr>` +
    `<tr><th>修订 B 轨迹 / 输出波形</th><td>${st.right_trajectories ?? "—"} / ${st.right_distinct_output_waveforms ?? "—"}</td></tr>` +
    `<tr><th>审计时间界</th><td>t=0..${r.horizon ?? "—"}</td></tr>` +
    `</tbody></table>`;

  if (r.status === "equivalent" && r.coverage) {
    const c = r.coverage;
    html += `<h3>覆盖边界</h3><table class="audit-table"><tbody>` +
      `<tr><th>覆盖时间</th><td>t=0..${c.covered_time}</td></tr>` +
      `<tr><th>跃迁窗口最晚结束</th><td>${c.window_end_max}</td></tr>` +
      `<tr><th>最长静态路径</th><td>${c.longest_static_path}</td></tr>` +
      `<tr><th>完全稳定界</th><td>${c.settling_bound}</td></tr>` +
      `<tr><th>瞬态完整覆盖</th><td>${c.transient_fully_covered ? "是" : "否"}</td></tr>` +
      (c.pending_note ? `<tr><th>提示</th><td>${esc(c.pending_note)}</td></tr>` : "") +
      `</tbody></table>`;
  }

  if (r.status === "inequivalent" && r.earliest_divergence) {
    const dv = r.earliest_divergence;
    html += `<h3>触发输入跃迁</h3><table class="audit-table"><thead><tr><th>输入</th><th>跃迁时刻</th></tr></thead><tbody>`;
    Object.entries(dv.trigger.input_transition_times || {}).forEach(([n, t]) => {
      html += `<tr><td>${esc(n)}</td><td>${t === null ? "（不跃迁）" : t}</td></tr>`;
    });
    html += `</tbody></table>`;
    html += `<h3>门延迟选择</h3>`;
    Object.entries(dv.delay_choices || {}).forEach(([rev, delays]) => {
      html += `<h4>${esc(rev)}</h4><table class="audit-table"><thead><tr><th>门</th><th>事件 (t, 延迟)</th></tr></thead><tbody>`;
      Object.entries(delays || {}).forEach(([g, evs]) => {
        html += `<tr><td>${esc(g)}</td><td>${(evs || []).map(e => `t=${e.t}, d=${e.delay}`).join("；") || "—"}</td></tr>`;
      });
      html += `</tbody></table>`;
    });
    html += `<h3>两版输出波形</h3>${auditWaveSvg(dv, r.horizon)}`;
  }
  box.innerHTML = html;
}

function auditWaveSvg(dv, H) {
  const revs = Object.keys(dv.waveforms || {});
  const rows = [];
  revs.forEach(rev => (dv.waveforms[rev] || []).forEach(w => rows.push({ rev, ...w })));
  const px = 34, labelW = 130, rowH = 44, top = 28;
  const width = Math.max(520, labelW + (H + 1) * px + 30);
  const height = top + rows.length * rowH + 28;
  let s = `<svg class="audit-wave" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img">`;
  for (let t = 0; t <= H + 1; t++) {
    const x = labelW + t * px;
    s += `<line x1="${x}" y1="${top - 8}" x2="${x}" y2="${height - 8}" class="grid"/>`;
    if (t <= H) s += `<text x="${x}" y="12" class="tick">${t}</text>`;
  }
  rows.forEach((w, i) => {
    const y = top + i * rowH + rowH / 2;
    const hi = y - 12, lo = y + 12;
    s += `<text x="8" y="${y}" class="label">${esc(w.rev)} · ${esc(w.output)}</text>`;
    let cur = w.initial, x0 = labelW;
    const edgeMap = new Map((w.edges || []).map(e => [e.t, e.v]));
    let d = `M ${x0} ${cur ? hi : lo}`;
    for (let t = 0; t <= H; t++) {
      const x = labelW + t * px;
      if (edgeMap.has(t)) {
        d += ` L ${x} ${cur ? hi : lo}`;
        cur = edgeMap.get(t);
        d += ` L ${x} ${cur ? hi : lo}`;
      } else {
        d += ` L ${x} ${cur ? hi : lo}`;
      }
    }
    d += ` L ${labelW + (H + 1) * px} ${cur ? hi : lo}`;
    s += `<path d="${d}" class="wave ${w.rev.includes(String(dv.trigger.left_revision)) ? "left" : "right"}"/>`;
  });
  if (dv.time !== undefined && dv.time !== null) {
    const x = labelW + dv.time * px;
    s += `<line x1="${x}" y1="4" x2="${x}" y2="${height - 4}" class="divergence"/>` +
      `<text x="${x}" y="${height - 8}" class="divergence-label">分歧 t=${dv.time}</text>`;
  }
  return s + `</svg>`;
}

async function loadAuditRecords() {
  try {
    const d = await api("/api/hierarchy/audits");
    const tb = audit$("audit-records-table").querySelector("tbody");
    tb.innerHTML = "";
    if (!d.records.length) {
      tb.innerHTML = `<tr><td colspan="8" class="hint">暂无审计记录</td></tr>`;
      return;
    }
    d.records.forEach(r => {
      const dt = new Date(r.created_at * 1000).toLocaleString();
      tb.insertAdjacentHTML("beforeend", `<tr>
        <td>${r.id}</td><td>${esc(r.comp_name)}</td>
        <td>${r.left_revision} ↔ ${r.right_revision}</td>
        <td class="mono">${esc(r.params_hash)}</td>
        <td><span class="pill ${auditStatusClass(r.status)}">${r.status}</span></td>
        <td class="mono">${r.earliest_divergence ?? (r.reason || "—")}</td><td class="mono">${dt}</td>
        <td>
          <button class="mini" data-audit-view="${r.id}">查看</button>
          <button class="mini" data-audit-replay="${r.id}">重放</button>
          <button class="mini" data-audit-export="${r.id}">导出</button>
        </td></tr>`);
    });
    tb.querySelectorAll("[data-audit-view]").forEach(b =>
      b.addEventListener("click", () => viewAuditRecord(+b.dataset.auditView)));
    tb.querySelectorAll("[data-audit-replay]").forEach(b =>
      b.addEventListener("click", () => replayAuditRecord(+b.dataset.auditReplay)));
    tb.querySelectorAll("[data-audit-export]").forEach(b =>
      b.addEventListener("click", () => exportAuditRecord(+b.dataset.auditExport)));
  } catch (e) {
    toast("加载审计记录失败：" + e.message);
  }
}

async function viewAuditRecord(id) {
  try {
    const d = await api(`/api/hierarchy/audits/${id}`);
    const rec = d.record;
    auditState.result = rec.result_json;
    auditState.record = { id: rec.id, reused: true };
    audit$("audit-params-json").value = JSON.stringify(rec.params_json, null, 2);
    renderAuditResult(rec.result_json, { id: rec.id, reused: true });
    toast(`已载入审计记录 #${id}`);
  } catch (e) { toast("载入审计记录失败：" + e.message); }
}

async function replayAuditRecord(id) {
  try {
    const d = await api(`/api/hierarchy/audits/${id}/replay`, { method: "POST" });
    auditState.result = d.result;
    renderAuditResult(d.result, { id, reused: true });
    toast(`审计记录 #${id} 重放完成：${d.result.status}`);
  } catch (e) { toast("重放失败：" + e.message); }
}

async function exportAuditRecord(id) {
  try {
    const d = await api(`/api/hierarchy/audits/${id}`);
    downloadJson(d.record, `behavior-audit-${id}.json`);
  } catch (e) { toast("导出失败：" + e.message); }
}

async function exportAllAuditRecords() {
  try {
    const d = await api("/api/hierarchy/audits/export");
    downloadJson(d, "behavior-audit-records.json");
  } catch (e) { toast("导出失败：" + e.message); }
}

function downloadJson(obj, filename) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

audit$("btn-audit-refresh").addEventListener("click", loadAuditCompositions);
audit$("btn-audit-comp-load").addEventListener("click", () =>
  loadAuditRevisions(audit$("audit-comp-select").value));
audit$("audit-comp-select").addEventListener("change", e =>
  loadAuditRevisions(e.target.value));
audit$("btn-audit-run").addEventListener("click", runAudit);
audit$("btn-audit-records-refresh").addEventListener("click", loadAuditRecords);
audit$("btn-audit-export-all").addEventListener("click", exportAllAuditRecords);
audit$("btn-audit-sample").addEventListener("click", () => {
  audit$("audit-params-json").value = JSON.stringify({
    initial: { a: 1 }, final: { a: 0 }, windows: { a: [2, 2] },
    outputs: ["z"], horizon: 12,
  }, null, 2);
});

(async function initAuditTab() {
  await loadAuditCompositions();
  await loadAuditRecords();
})();
