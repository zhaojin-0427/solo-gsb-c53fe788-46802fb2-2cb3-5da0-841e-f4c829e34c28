"use strict";

/* ⑦ 反例驱动修复方案搜索页面 */

const repairState = {
  sourceKind: "flat",
  sources: { flat: [], hier: [] },
  source: null,      // GET /records 或 hier analyses 的完整记录
  tunables: { gates: [], windows: [], outputs: [] },
  result: null,
};

const r$ = (id) => document.getElementById(id);

async function loadRepairSources() {
  try {
    const d = await api("/api/repairs/sources");
    repairState.sources = { flat: d.flat || [], hier: d.hier || [] };
    fillRepairSourceSelect();
  } catch (e) {
    toast("加载修复源列表失败：" + e.message);
  }
}

function fillRepairSourceSelect() {
  const kind = r$("repair-source-kind").value;
  repairState.sourceKind = kind;
  const sel = r$("repair-source-id");
  const list = repairState.sources[kind] || [];
  sel.innerHTML = list.map((x) => {
    if (kind === "flat") {
      return `<option value="${x.id}">#${x.id} ${esc(x.name || "未命名")} （${esc(x.netlist_hash || "")}）</option>`;
    }
    return `<option value="${x.id}">#${x.id} ${esc(x.comp_name)} 修订${x.comp_version}</option>`;
  }).join("");
  if (!list.length) {
    sel.innerHTML = `<option value="">（暂无 unsafe 记录，请先在①运行验证或在⑤做层次验证）</option>`;
  }
}

r$("repair-source-kind").addEventListener("change", fillRepairSourceSelect);
r$("btn-repair-refresh-sources").addEventListener("click", loadRepairSources);

async function loadRepairSource() {
  const kind = repairState.sourceKind;
  const id = +r$("repair-source-id").value;
  if (!id) { toast("请选择一条违规记录"); return; }
  try {
    const path = kind === "flat"
      ? `/api/records/${id}`
      : `/api/hierarchy/analyses/${id}`;
    const d = await api(path);
    repairState.source = d.record;
    buildRepairTunables();
    renderRepairSourceInfo();
  } catch (e) {
    toast("载入源记录失败：" + e.message, 4000);
  }
}
r$("btn-repair-load").addEventListener("click", loadRepairSource);

function renderRepairSourceInfo() {
  const r0 = repairState.source.result_json;
  const ce = r0.counterexample || {};
  r$("repair-source-info").className = "infobox err";
  r$("repair-source-info").textContent =
    `源 #${repairState.source.id}：输出 ${r0.bad_output} 在 t=${r0.bad_time} ` +
    `违反 ${r0.violation}；竞争路径 ${ce.race_path_count ?? 0} 条；` +
    `H=${r0.horizon}。修复后可在右侧查看 Pareto 方案并“预览为新参数版本”。`;
}

/* 依据源网表/参数生成可调表（默认全部不勾选；预填合理范围） */
function buildRepairTunables() {
  const rec = repairState.source;
  const nl = rec.netlist_json || rec.flat_json;
  const p = rec.params_json;
  repairState.tunables = { gates: [], windows: [], outputs: [] };

  const gb = r$("repair-gates-table").querySelector("tbody");
  gb.innerHTML = "";
  (nl.gates || []).forEach((g) => {
    const [lo, hi] = Array.isArray(g.delay) ? g.delay : [g.delay, g.delay];
    gb.insertAdjacentHTML("beforeend",
      `<tr data-gate="${esc(g.id)}">
        <td>${esc(g.id)} <span class="hint">${esc(g.type)}</span></td>
        <td class="mono">[${lo},${hi}]</td>
        <td><input type="number" data-lo min="0" value="${lo}" disabled></td>
        <td><input type="number" data-hi min="0" value="${hi}" disabled></td>
        <td><label class="hint"><input type="checkbox" data-en> 可调</label></td>
      </tr>`);
  });
  gb.querySelectorAll("tr").forEach((tr) => {
    const ck = tr.querySelector("[data-en]");
    ck.addEventListener("change", () => {
      tr.querySelectorAll("input[type=number]").forEach(i => {
        i.disabled = !ck.checked;
        i.classList.toggle("off", !ck.checked);
      });
    });
  });

  const wb = r$("repair-windows-table").querySelector("tbody");
  wb.innerHTML = "";
  const wins = p.windows || {};
  Object.entries(wins).forEach(([n, w]) => {
    wb.insertAdjacentHTML("beforeend",
      `<tr data-win="${esc(n)}">
        <td>${esc(n)}</td><td class="mono">[${w[0]},${w[1]}]</td>
        <td><input type="number" data-lo min="0" value="${w[0]}" disabled></td>
        <td><input type="number" data-hi min="0" value="${w[1]}" disabled></td>
        <td><label class="hint"><input type="checkbox" data-en> 可调</label></td>
      </tr>`);
  });
  wb.querySelectorAll("tr").forEach((tr) => {
    const ck = tr.querySelector("[data-en]");
    ck.addEventListener("change", () => {
      tr.querySelectorAll("input[type=number]").forEach(i => {
        i.disabled = !ck.checked;
        i.classList.toggle("off", !ck.checked);
      });
    });
  });

  const ob = r$("repair-outputs-table").querySelector("tbody");
  ob.innerHTML = "";
  (p.outputs || []).forEach((o) => {
    const o2 = typeof o === "string" ? { name: o } : o;
    if (o2.min_pulse_width != null) {
      ob.insertAdjacentHTML("beforeend",
        `<tr data-out="${esc(o2.name)}" data-field="pw">
          <td>${esc(o2.name)}</td><td>最小脉宽</td>
          <td class="mono">${o2.min_pulse_width}</td>
          <td><input type="number" data-lo min="1" value="${o2.min_pulse_width}" disabled></td>
          <td><input type="number" data-hi min="1" value="${Math.max(o2.min_pulse_width + 2, 6)}" disabled></td>
          <td><label class="hint"><input type="checkbox" data-en> 可调</label></td></tr>`);
    }
    if (o2.max_transitions != null) {
      ob.insertAdjacentHTML("beforeend",
        `<tr data-out="${esc(o2.name)}" data-field="k">
          <td>${esc(o2.name)}</td><td>最多跃迁</td>
          <td class="mono">${o2.max_transitions}</td>
          <td><input type="number" data-lo min="0" value="${o2.max_transitions}" disabled></td>
          <td><input type="number" data-hi min="0" value="${o2.max_transitions + 4}" disabled></td>
          <td><label class="hint"><input type="checkbox" data-en> 可调</label></td></tr>`);
    }
  });
  ob.querySelectorAll("tr").forEach((tr) => {
    const ck = tr.querySelector("[data-en]");
    ck.addEventListener("change", () => {
      tr.querySelectorAll("input[type=number]").forEach(i => {
        i.disabled = !ck.checked;
        i.classList.toggle("off", !ck.checked);
      });
    });
  });
}

function collectRepairRequest() {
  const gates = [], windows = [], outputsMap = {};
  r$("repair-gates-table").querySelectorAll("tr[data-gate]").forEach((tr) => {
    if (!tr.querySelector("[data-en]").checked) return;
    gates.push({ id: tr.dataset.gate,
      range: [+tr.querySelector("[data-lo]").value, +tr.querySelector("[data-hi]").value] });
  });
  r$("repair-windows-table").querySelectorAll("tr[data-win]").forEach((tr) => {
    if (!tr.querySelector("[data-en]").checked) return;
    windows.push({ name: tr.dataset.win,
      range: [+tr.querySelector("[data-lo]").value, +tr.querySelector("[data-hi]").value] });
  });
  r$("repair-outputs-table").querySelectorAll("tr[data-out]").forEach((tr) => {
    if (!tr.querySelector("[data-en]").checked) return;
    const item = outputsMap[tr.dataset.out] || { name: tr.dataset.out };
    const lo = +tr.querySelector("[data-lo]").value;
    const hi = +tr.querySelector("[data-hi]").value;
    if (tr.dataset.field === "pw") item.min_pulse_width_range = [lo, hi];
    if (tr.dataset.field === "k") item.max_transitions_range = [lo, hi];
    outputsMap[tr.dataset.out] = item;
  });
  return {
    gates, windows, outputs: Object.values(outputsMap),
    budget: {
      max_changes: +r$("repair-max-changes").value,
      max_total_deviation: +r$("repair-max-deviation").value,
    },
  };
}

r$("btn-repair-json").addEventListener("click", () => {
  const ta = r$("repair-request-json");
  if (ta.style.display === "none") {
    ta.style.display = "block";
    ta.value = JSON.stringify(collectRepairRequest(), null, 2);
  } else {
    try {
      const q = JSON.parse(ta.value);
      applyRepairRequest(q);
      ta.style.display = "none";
    } catch (e) { toast("请求 JSON 解析失败：" + e.message); }
  }
});

function applyRepairRequest(q) {
  // 勾选并填表（用于从 JSON 恢复表单）
  const setRow = (tbody, key, id, range) => {
    const tr = [...tbody.querySelectorAll("tr")].find(t => t.dataset[key] === id);
    if (!tr) return;
    tr.querySelector("[data-en]").checked = true;
    const lo = tr.querySelector("[data-lo]"), hi = tr.querySelector("[data-hi]");
    lo.disabled = hi.disabled = false; lo.value = range[0]; hi.value = range[1];
  };
  (q.gates || []).forEach(g =>
    setRow(r$("repair-gates-table").querySelector("tbody"), "gate", g.id, g.range));
  (q.windows || []).forEach(w =>
    setRow(r$("repair-windows-table").querySelector("tbody"), "win", w.name, w.range));
  (q.outputs || []).forEach((o) => {
    if (o.min_pulse_width_range)
      setOutRow(o.name, "pw", o.min_pulse_width_range);
    if (o.max_transitions_range)
      setOutRow(o.name, "k", o.max_transitions_range);
  });
  function setOutRow(name, field, range) {
    const tr = [...r$("repair-outputs-table").querySelectorAll("tr")]
      .find(t => t.dataset.out === name && t.dataset.field === field);
    if (!tr) return;
    tr.querySelector("[data-en]").checked = true;
    const lo = tr.querySelector("[data-lo]"), hi = tr.querySelector("[data-hi]");
    lo.disabled = hi.disabled = false; lo.value = range[0]; hi.value = range[1];
  }
  if (q.budget) {
    r$("repair-max-changes").value = q.budget.max_changes ?? 3;
    r$("repair-max-deviation").value = q.budget.max_total_deviation ?? 20;
  }
}

async function runRepairSearch() {
  if (!repairState.source) { toast("请先载入一条违规记录"); return; }
  const ta = r$("repair-request-json");
  let body;
  if (ta.style.display !== "none" && ta.value.trim()) {
    try { body = JSON.parse(ta.value); }
    catch (e) { toast("请求 JSON 解析失败：" + e.message); return; }
  } else {
    body = collectRepairRequest();
  }
  if (!body.gates.length && !body.windows.length && !body.outputs.length) {
    toast("请至少勾选一个可调范围（门/窗口/输出约束）"); return;
  }
  const kind = repairState.sourceKind;
  const id = repairState.source.id;
  const btn = r$("btn-repair-run");
  btn.disabled = true; btn.textContent = "z3 约束优化搜索中…";
  try {
    const t0 = performance.now();
    const d = await api(`/api/repairs/search/${kind}/${id}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    d.elapsed_ms = Math.round(performance.now() - t0);
    repairState.result = d;
    renderRepairResult(d);
    loadRepairRecords();
  } catch (e) {
    toast("修复搜索失败：" + e.message, 6000);
  } finally {
    btn.disabled = false; btn.textContent = "z3 约束优化搜索";
  }
}
r$("btn-repair-run").addEventListener("click", runRepairSearch);

function repairStatusClass(s) {
  if (s === "safe_plans") return "safe";
  if (s === "no_solution") return "unknown";
  return "unknown";
}

function renderRepairResult(d) {
  const r = d.result;
  const ban = r$("repair-banner");
  ban.className = "banner " + repairStatusClass(r.status);
  const stats = r.statistics || {};
  if (r.status === "safe_plans") {
    ban.innerHTML = `✓ 找到 ${r.plans.length} 条 Pareto 修复方案（z3 证明安全并经既有验证器复核）` +
      `<span class="detail">搜索耗时 ${d.elapsed_ms} ms；z3 安全候选 ${stats.z3_safe_candidates ?? "?"}，` +
      `验证器复核 ${stats.verifier_calls ?? "?"} 次，记录 #${d.record.id}${d.record.reused ? "（已去重更新）" : ""}</span>`;
  } else if (r.status === "no_solution") {
    ban.innerHTML = `✗ 预算内无修复方案：所有候选仍不能消除违规（${esc(r.reason || "")}）` +
      `<span class="detail">最小改动证据见下方；搜索耗时 ${d.elapsed_ms} ms</span>`;
  } else {
    ban.innerHTML = `? 无法证明安全（unknown，${esc(r.reason || "—")}）` +
      `<span class="detail">${esc(unknownText(r.reason))}　搜索耗时 ${d.elapsed_ms} ms</span>`;
  }

  // 最小改动证据
  const ev = r.min_change_evidence;
  const evBox = r$("repair-evidence");
  if (ev) {
    evBox.className = "infobox ok";
    evBox.textContent =
      `预算内最小改动证据：至少需要 ${ev.min_changes} 处改动；` +
      `更小改动层 [${(ev.lower_change_levels_unsat || []).join(",")}] 已被 z3 证明 unsat（不可行）。` +
      `最小改动层最小总偏差 = ${ev.min_total_deviation_at_min_changes}` +
      `（偏差预算 ${ev.budget_max_total_deviation}，改动预算 ${ev.budget_max_changes}）。证据：${ev.proof === "complete" ? "完整" : "部分"}。`;
  } else {
    evBox.className = "infobox";
    evBox.textContent = r.status === "no_solution"
      ? "无可信最小改动证据：零改动（基线）即违规，且预算内未找到任何安全候选。"
      : "未生成最小改动证据（搜索可能在低层就超时/截断）。";
  }

  renderRepairPlans(r);
  const ub = r$("repair-unknown");
  const reasons = r.unknown_reasons || {};
  const reasonKeys = Object.keys(reasons);
  ub.className = "infobox " + (reasonKeys.length ? "err" : "");
  ub.textContent = reasonKeys.length
    ? "无法证明安全的候选原因统计：" +
      reasonKeys.map(k => `${k}×${reasons[k]}`).join("，")
    : "所有候选均由验证器给出明确 safe/unsafe 结论，无 unknown 候选。";

  const unc = r.uncovered || { items: [] };
  const uc = r$("repair-uncovered");
  uc.className = "infobox " + (unc.items.length ? "err" : "ok");
  uc.textContent = unc.items.length
    ? "未覆盖边界（H 之外）：" + unc.items
        .map(i => `方案 ${i.changes}改 稳定界 ${i.settling_bound} > H ${i.horizon}`).join("；")
    : "返回方案的瞬态均在时间上界内完整覆盖。";
}

function unknownText(reason) {
  return {
    solver_timeout: "z3 求解超时，部分候选未判定",
    verifier_unknown: "既有验证器对部分候选返回 unknown（超时/规模）",
    search_budget: "搜索枚举/复核预算用尽，未穷尽全部候选",
    recheck_mismatch: "z3 判安全但验证器仍判违规（结论不一致）",
    scale_limit: "超过修复搜索规模上限",
  }[reason] || "详见统计与审计日志";
}

function renderRepairPlans(r) {
  const box = r$("repair-plans");
  if (!r.plans || !r.plans.length) { box.innerHTML = ""; return; }
  let html = "";
  r.plans.forEach((p, idx) => {
    const d = p.change_detail || { gates: [], windows: [], outputs: [] };
    const rows = [];
    d.gates.forEach(g => rows.push(
      `<tr><td>门 ${esc(g.id)}</td><td class="mono">[${g.from}] → [${g.to}]</td></tr>`));
    d.windows.forEach(w => rows.push(
      `<tr><td>窗口 ${esc(w.name)}</td><td class="mono">[${w.from}] → [${w.to}]</td></tr>`));
    d.outputs.forEach(o => rows.push(
      `<tr><td>${esc(o.name)} ${o.field === "pw" ? "最小脉宽" : "最多跃迁"}</td>` +
      `<td class="mono">${o.from} → ${o.to}</td></tr>`));
    const paths = p.eliminated_race_paths || [];
    const byCause = {};
    paths.forEach(x => { byCause[x.cause.kind] = (byCause[x.cause.kind] || 0) + 1; });
    const causeText = Object.entries(byCause)
      .map(([k, n]) => ({ gate_delay: "门延迟", input_window: "输入窗口",
        output_constraint: "输出约束", global: "全局重收敛" }[k] || k) + `×${n}`)
      .join("，");
    const cov = p.coverage || {};
    html += `<div class="plan-card">
      <div class="plan-head">
        <b>方案 ${idx + 1}</b>
        <span class="metric">改动 <b>${p.changes}</b> · 总偏差 <b>${p.total_deviation}</b>
          · 风险消除于 t≤<b>${p.risk_eliminated_at}</b>
          · 复核 <b>${p.recheck_ms}ms</b></span>
        <button class="mini" data-mat="${idx}">预览为新参数版本</button>
      </div>
      <table class="repair-table"><tbody>${rows.join("")}</tbody></table>
      <div class="paths">已消除反例路径 ${paths.length} 条（${esc(causeText || "—")}）；
        覆盖 t=0..${cov.covered_time ?? "?"}，完全稳定界 ${cov.settling_bound ?? "?"}
        ${cov.transient_fully_covered ? "（瞬态完整覆盖）" : "（H 之外未覆盖）"}</div>
    </div>`;
  });
  box.innerHTML = html;
  box.querySelectorAll("[data-mat]").forEach(b => b.addEventListener("click", () =>
    materializePlan(r.plans[+b.dataset.mat])));
}

async function materializePlan(plan) {
  if (!repairState.result) return;
  const sid = repairState.result.record.id;
  try {
    const d = await api(`/api/repairs/records/${sid}/materialize`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_key: plan.key }),
    });
    toast(`已另存为新分析记录 #${d.analysis_record.id}（${d.result.status}），原记录未改动`, 4200);
  } catch (e) { toast("预览/另存失败：" + e.message, 4500); }
}

/* ---------------- 搜索记录 ---------------- */

async function loadRepairRecords() {
  try {
    const d = await api("/api/repairs/records");
    const tb = r$("repair-records-table").querySelector("tbody");
    tb.innerHTML = "";
    if (!d.records.length) {
      tb.innerHTML = `<tr><td colspan="6" class="hint">暂无修复搜索记录</td></tr>`;
      return;
    }
    d.records.forEach(x => {
      const dt = new Date(x.created_at * 1000).toLocaleString();
      const nPlans = x.plan_count ?? 0;
      tb.insertAdjacentHTML("beforeend", `<tr>
        <td>${x.id}</td>
        <td>${x.source_kind}#${x.source_id} ${esc((x.source_summary || {}).bad_output || "")}</td>
        <td><span class="pill ${repairStatusClass(x.status)}">${x.status}</span></td>
        <td>${nPlans}</td><td class="mono">${dt}</td>
        <td>
          <button class="mini" data-view="${x.id}">查看</button>
          <button class="mini" data-replay="${x.id}">重放</button>
          <button class="mini" data-export="${x.id}">导出</button>
        </td></tr>`);
    });
    tb.querySelectorAll("[data-view]").forEach(b =>
      b.addEventListener("click", () => viewRepairRecord(+b.dataset.view)));
    tb.querySelectorAll("[data-replay]").forEach(b =>
      b.addEventListener("click", () => replayRepairRecord(+b.dataset.replay)));
    tb.querySelectorAll("[data-export]").forEach(b =>
      b.addEventListener("click", () => exportRepairRecord(+b.dataset.export)));
  } catch (e) { toast("加载修复记录失败：" + e.message); }
}

async function viewRepairRecord(id) {
  try {
    const d = await api(`/api/repairs/records/${id}`);
    const rec = d.record;
    repairState.result = { record: { id: rec.id, reused: true },
      result: rec.result_json, elapsed_ms: 0 };
    // 同步源类型选择与表单调出
    r$("repair-source-kind").value = rec.source_kind;
    renderRepairResult(repairState.result);
    toast(`已载入修复搜索记录 #${id}`);
  } catch (e) { toast("载入修复记录失败：" + e.message); }
}

async function replayRepairRecord(id) {
  try {
    const d = await api(`/api/repairs/records/${id}/replay`, { method: "POST" });
    repairState.result = { record: { id, reused: true }, result: d.result, elapsed_ms: 0 };
    renderRepairResult(repairState.result);
    toast(`修复搜索 #${id} 重放完成：${d.result.status}（一致：${d.replay_consistent ? "是" : "否"}）`, 4200);
  } catch (e) { toast("重放失败：" + e.message); }
}

async function exportRepairRecord(id) {
  try {
    const d = await api(`/api/repairs/records/${id}`);
    downloadRepairJson(d.record, `repair-search-${id}.json`);
  } catch (e) { toast("导出失败：" + e.message); }
}

r$("btn-repair-records-refresh").addEventListener("click", loadRepairRecords);
r$("btn-repair-export-all").addEventListener("click", async () => {
  try {
    const d = await api("/api/repairs/export");
    downloadRepairJson(d, "repair-search-records.json");
  } catch (e) { toast("导出失败：" + e.message); }
});

function downloadRepairJson(obj, filename) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

// app.js 切换 tab 时联动
document.querySelector('.tabs button[data-tab="repair"]')
  .addEventListener("click", () => { loadRepairSources(); loadRepairRecords(); });

(async function initRepair() {
  await loadRepairSources();
  await loadRepairRecords();
})();
