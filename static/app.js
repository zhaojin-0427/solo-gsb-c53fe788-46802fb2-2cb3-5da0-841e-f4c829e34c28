"use strict";

/* ---------------- 全局状态 ---------------- */
const state = {
  netlist: null,
  params: null,
  result: null,
  limits: null,
  samples: [],
  wave: { pxPerT: 28, showFire: true },
  netView: { scale: 1, ox: 40, oy: 30 },
};

/* 层次验证结果中节点 → 可读路径（实例路径 + 端口映射） */
function hNodeName(node) {
  const hh = state.result && state.result.result && state.result.result.hierarchy;
  if (hh && hh.node_catalog && hh.node_catalog[node]) {
    const inf = hh.node_catalog[node];
    const cr = (inf.crossings || []).map(c =>
      `${c.instance_path || c.instance_id}.${c.port}`);
    return cr.length ? `${node}  [${cr.join(" ← ")}]` : node;
  }
  return node;
}
/* 波形行短标签：实例路径优先（最后两段），避免整行过长 */
function hWaveLabel(wv) {
  if (wv.route && wv.route.length > 1) {
    return (wv.kind === "input" ? "▸ " : "■ ") +
      wv.node.split("/").slice(-2).join("/");
  }
  return (wv.kind === "input" ? "▸ " : "■ ") + wv.node;
}

const $ = (id) => document.getElementById(id);

function toast(msg, ms = 2600) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), ms);
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (e) { /* 非 JSON */ }
  if (!r.ok) {
    const msg = (data && data.error) ? data.error : `HTTP ${r.status}`;
    throw new Error(msg);
  }
  return data;
}

/* ---------------- Tab 切换 ---------------- */
document.querySelectorAll(".tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach(s => s.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "netlist") drawNetlist();
    if (btn.dataset.tab === "wave") drawWave();
    if (btn.dataset.tab === "records") loadRecords();
  });
});

/* ---------------- JSON 编辑 ---------------- */
function readJson(ta) {
  const raw = ta.value.trim();
  if (!raw) return null;
  return JSON.parse(raw);
}

$("file-input").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const rd = new FileReader();
  rd.onload = () => {
    try {
      const obj = JSON.parse(rd.result);
      if (obj.netlist) { // 示例文件：{name, netlist, params}
        $("netlist-json").value = JSON.stringify(obj.netlist, null, 2);
        if (obj.params) $("params-json").value = JSON.stringify(obj.params, null, 2);
      } else {
        $("netlist-json").value = JSON.stringify(obj, null, 2);
      }
      buildForm();
      toast("已导入 " + f.name);
    } catch (err) {
      toast("JSON 解析失败：" + err.message);
    }
  };
  rd.readAsText(f);
});

async function loadSamples() {
  const d = await api("/api/samples");
  state.samples = d.samples;
  if (state.samples[0]) useSample(0);
}

function useSample(i) {
  const s = state.samples[i];
  $("netlist-json").value = JSON.stringify(s.netlist, null, 2);
  $("params-json").value = JSON.stringify(s.params, null, 2);
  buildForm();
}

$("btn-sample1").addEventListener("click", () => useSample(0));
$("btn-sample2").addEventListener("click", () => useSample(1));
$("btn-sample3").addEventListener("click", () => useSample(2));

/* ---------------- 参数表单 ---------------- */
function buildForm() {
  let nl;
  try { nl = readJson($("netlist-json")); } catch (e) { return; }
  if (!nl || !Array.isArray(nl.inputs)) return;
  let p = {};
  try { p = readJson($("params-json")) || {}; } catch (e) { /* 忽略坏 JSON */ }

  const states = $("form-states");
  const wins = $("form-windows");
  states.innerHTML = "";
  wins.innerHTML = "";
  nl.inputs.forEach((n) => {
    const i0 = (p.initial && p.initial[n]) ?? 0;
    const i1 = (p.final && p.final[n]) ?? i0;
    states.insertAdjacentHTML("beforeend",
      `<label>${n} 初
        <select data-k="i" data-n="${n}"><option value="0" ${i0===0?"selected":""}>0</option><option value="1" ${i0===1?"selected":""}>1</option></select></label>
       <label>${n} 终
        <select data-k="f" data-n="${n}"><option value="0" ${i1===0?"selected":""}>0</option><option value="1" ${i1===1?"selected":""}>1</option></select></label>`);
    const w = (p.windows && p.windows[n]) || ["", ""];
    wins.insertAdjacentHTML("beforeend",
      `<label class="span2">${n} 窗口
        <input type="number" min="0" data-k="w0" data-n="${n}" value="${w[0] ?? ""}" placeholder="tmin">
        <input type="number" min="0" data-k="w1" data-n="${n}" value="${w[1] ?? ""}" placeholder="tmax">
       </label>`);
  });

  const outs = $("form-outputs");
  outs.innerHTML = "";
  const outNodes = (p.outputs && p.outputs.length)
    ? p.outputs.map(o => typeof o === "string" ? { name: o } : o)
    : (nl.outputs || []).map(n => ({ name: n }));
  const all = [...nl.inputs, ...(nl.gates || []).map(g => g.id)];
  outNodes.forEach((o, idx) => {
    const opts = all.map(n => `<option ${n === o.name ? "selected" : ""}>${n}</option>`).join("");
    outs.insertAdjacentHTML("beforeend",
      `<div class="out-row">
         <select data-k="on" data-i="${idx}">${opts}</select>
         最小脉宽 <input type="number" min="1" data-k="op" data-i="${idx}" value="${o.min_pulse_width ?? ""}" placeholder="—">
         最多跃迁 <input type="number" min="0" data-k="ok" data-i="${idx}" value="${o.max_transitions ?? ""}" placeholder="—">
       </div>`);
  });
  $("form-horizon").value = p.horizon ?? "";
}

$("btn-sync-form").addEventListener("click", buildForm);

$("btn-form-json").addEventListener("click", () => {
  const initial = {}, final = {}, windows = {};
  document.querySelectorAll('#form-states select').forEach(s => {
    const n = s.dataset.n;
    if (s.dataset.k === "i") initial[n] = +s.value; else final[n] = +s.value;
  });
  document.querySelectorAll('#form-windows input').forEach(inp => {
    const n = inp.dataset.n;
    windows[n] = windows[n] || [];
    windows[n][inp.dataset.k === "w0" ? 0 : 1] =
      inp.value === "" ? null : +inp.value;
  });
  Object.keys(windows).forEach(n => {
    const w = windows[n];
    if (w[0] === null || w[1] === null) delete windows[n];
  });
  const outputs = [];
  const rows = $("form-outputs").querySelectorAll(".out-row");
  rows.forEach(r => {
    const name = r.querySelector('[data-k="on"]').value;
    const pwin = r.querySelector('[data-k="op"]').value;
    const pk = r.querySelector('[data-k="ok"]').value;
    const o = { name };
    if (pwin !== "") o.min_pulse_width = +pwin;
    if (pk !== "") o.max_transitions = +pk;
    outputs.push(o);
  });
  const p = { initial, final, windows, outputs };
  const h = $("form-horizon").value;
  if (h !== "") p.horizon = +h;
  $("params-json").value = JSON.stringify(p, null, 2);
  toast("已生成参数 JSON");
});

["netlist-json"].forEach(id => $(id).addEventListener("input", () => {
  clearTimeout(window._bt);
  window._bt = setTimeout(buildForm, 400);
}));

/* ---------------- 校验 / 分析 ---------------- */
async function collectPayload() {
  let nl, p;
  try { nl = readJson($("netlist-json")); }
  catch (e) { throw new Error("网表 JSON 解析失败：" + e.message); }
  if (!nl) throw new Error("请填写网表 JSON");
  try { p = readJson($("params-json")); }
  catch (e) { throw new Error("参数 JSON 解析失败：" + e.message); }
  if (!p) throw new Error("请填写分析参数 JSON");
  return { netlist: nl, params: p };
}

$("btn-validate").addEventListener("click", async () => {
  const box = $("validate-info");
  try {
    const pay = await collectPayload();
    const d = await api("/api/validate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(pay),
    });
    state.netlist = pay.netlist; state.params = pay.params;
    const s = d.summary;
    box.className = "infobox ok";
    box.textContent =
      `✓ 网表合法：${s.n_inputs} 输入 / ${s.n_gates} 门，无环路。\n` +
      `拓扑序: ${s.order.join(" → ")}\n` +
      `自动时间上界 H = ${s.horizon}（最长静态路径 ${s.longest_static_path}）\n` +
      `初态门值: ${JSON.stringify(s.initial_values)}`;
    drawNetlist();
  } catch (e) {
    box.className = "infobox err";
    box.textContent = "✗ " + e.message;
  }
});

$("btn-analyze").addEventListener("click", async () => {
  const btn = $("btn-analyze");
  btn.disabled = true; btn.textContent = "z3 求解中…";
  try {
    const pay = await collectPayload();
    const t0 = performance.now();
    const d = await api("/api/analyze", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(pay),
    });
    d.elapsed_ms = Math.round(performance.now() - t0);
    state.netlist = pay.netlist;
    state.params = pay.params;
    state.result = d;
    renderResult(d);
    toast(`验证完成（${d.elapsed_ms} ms），记录 #${d.record.id}` +
          (d.record.reused ? "（已有记录已更新）" : ""));
    document.querySelector('.tabs button[data-tab="wave"]').click();
  } catch (e) {
    toast("分析失败：" + e.message, 4500);
  } finally {
    btn.disabled = false; btn.textContent = "运行验证";
  }
});

/* ---------------- 结果横幅与反例 ---------------- */
function renderResult(d) {
  const r = d.result;
  const ban = $("result-banner");
  const cex = $("counterexample");
  ban.className = "banner " + r.status;
  if (r.status === "unsafe") {
    ban.innerHTML = `⚠ 存在风险：输出 <b>${r.bad_output_port ? r.bad_output_port + "（" + r.bad_output + "）" : r.bad_output}</b> 在 <b>t=${r.bad_time}</b> ` +
      `违反 <b>${r.violation === "min_pulse_width" ? "最小脉冲宽度" : "最大跃迁次数"}</b> 约束。` +
      `<span class="detail">${r.message}　（求解耗时 ${d.elapsed_ms} ms，记录 #${d.record.id}，H=${r.horizon}）</span>`;
    renderCex(r, cex);
    cex.classList.add("show");
    renderWavePaths(r, $("wave-paths"));
  } else if (r.status === "safe") {
    ban.innerHTML = `✓ 安全：在 t=0..${r.horizon} 内，所有门延迟在区间内的任意取值都不能违反约束。` +
      `<span class="detail">${safeDetail(r)}（求解耗时 ${d.elapsed_ms} ms，记录 #${d.record.id}）</span>`;
    cex.classList.remove("show");
    $("wave-paths").classList.remove("show");
  } else {
    ban.innerHTML = `? ${r.message || "无法判定"}` +
      `<span class="detail">状态：unknown（${r.reason || "—"}）</span>`;
    cex.classList.remove("show");
    $("wave-paths").classList.remove("show");
  }
  drawWave();
}

function safeDetail(r) {
  const c = r.coverage || {};
  let s = `已覆盖时间 t=0..${c.covered_time ?? r.horizon}；` +
    `跃迁窗口最晚结束 ${c.window_end_max ?? "?"}，最长静态路径 ${c.longest_static_path ?? "?"}，` +
    `完全稳定界 ${c.settling_bound ?? "?"}。`;
  s += c.transient_fully_covered
    ? "瞬态已被完整覆盖（H ≥ 稳定界），结论可作为该时间范围内的安全性保证。"
    : (c.pending_note || "注意：H 小于完全稳定界，结论仅覆盖给定时间范围。");
  return s;
}

function renderCex(r, box) {
  const ce = r.counterexample;
  let html = `<h3>可复现的最早反例（bad_time=${ce.bad_time}）</h3>`;

  html += `<table><tr><th>输入</th><th>跃迁时刻 τ</th><th>实例路径/端口映射</th></tr>`;
  Object.entries(ce.input_transition_times).forEach(([n, t]) => {
    const np = ce.node_paths && ce.node_paths[n];
    html += `<tr><td>${n}</td><td>${t === null ? "（不跃迁）" : t}</td>` +
      `<td class="route-chain">${np ? (np.route || []).join(" ← ") : "—"}</td></tr>`;
  });
  html += `</table>`;

  const delays = ce.chosen_delays || {};
  const gd = Object.keys(delays);
  if (gd.length) {
    html += `<h3>各门实际选用的点火延迟（按事件时刻）</h3><table>
      <tr><th>门（实例路径 / 模块版本）</th><th>事件 (点火时刻 t, 延迟 d)</th></tr>`;
    gd.forEach(g => {
      const evs = delays[g].map(e => `t=${e.t}, d=${e.delay}`).join("；");
      const meta = ce.chosen_delays_paths && ce.chosen_delays_paths[g];
      const o = meta && meta.origin;
      const src = o && (o.instance_id || o.kind === "wire_buf")
        ? (o.instance_id
            ? `${o.instance_path ? o.instance_path + "/" : ""}${o.instance_id}` +
              (o.module ? ` (${o.module}@${o.version})` : "")
            : `内部导线 ${o.local || g}`)
        : "";
      html += `<tr><td><b>${g}</b>${src ? `<br><span class="route-chain">${src}</span>` : ""}</td>` +
        `<td>${evs || "—"}</td></tr>`;
    });
    html += `</table>`;
  }

  const paths = ce.race_paths || [];
  if (paths.length) {
    const groups = new Map();
    paths.forEach(p => {
      if (!groups.has(p.edge_t)) groups.set(p.edge_t, []);
      groups.get(p.edge_t).push(p);
    });
    const total = ce.race_path_count ?? paths.length;
    html += `<h3>竞争路径（敏化因果链回溯到主输入；共 ${total} 条` +
      (ce.race_paths_truncated ? `，仅展示前 ${paths.length} 条` : "") +
      `）</h3><ol class="paths">`;
    [...groups.entries()].sort((a, b) => a[0] - b[0]).forEach(([et, ps]) => {
      ps.forEach(p => {
        const chain = p.hops.map(h => hopHtml(h)).join(" ← ");
        html += `<li>输出沿 t=${et}：${chain}</li>`;
      });
    });
    html += `</ol>`;
  } else if (r.status === "unsafe") {
    html += `<h3 class="err-note">未能生成竞争路径（不应发生，请检查求解器输出）</h3>`;
  }
  box.innerHTML = html;
}

/* 竞争链上一个 hop 的 HTML：同时显示实例路径、模块版本与端口映射路由 */
function hopHtml(h) {
  if (!h.gate) {
    const route = h.route && h.route.length > 1
      ? `<span class="route-chain">[${h.route.join(" ← ")}]</span>` : "";
    return `<b>${h.node}</b>@t${h.t} ${route}`;
  }
  const where = h.instance_id
    ? `<span class="origin-pill">${h.instance_path ? h.instance_path + "/" : ""}${h.instance_id}` +
      (h.module ? ` · ${h.module}@${h.module_version}` : "") + `</span>` : "";
  const route = h.route && h.route.length > 1
    ? `<br><span class="route-chain">端口映射: ${h.route.join(" ← ")}</span>` : "";
  const joint = h.joint && h.joint.length
    ? ` <span class="joint">共同触发{${h.joint.map(j => {
        const jp = h.joint_paths && h.joint_paths[j];
        return jp ? `${j}≡${jp.path}` : j;
      }).join(",")}}</span>` : "";
  return `<span class="tag">${h.node}</span>[${h.gate}]@t${h.t}` +
    (h.delay !== null && h.delay !== undefined ? ` d=${h.delay}` : "") +
    where + joint + route;
}

/* 波形下方：同一份 counterexample.race_paths 的分组详情（与 API/存档数据同源） */
function renderWavePaths(r, box) {
  const paths = (r.counterexample && r.counterexample.race_paths) || [];
  if (!paths.length) { box.classList.remove("show"); box.innerHTML = ""; return; }
  const groups = new Map();
  paths.forEach(p => {
    if (!groups.has(p.edge_t)) groups.set(p.edge_t, []);
    groups.get(p.edge_t).push(p);
  });
  const ce = r.counterexample;
  let html = `<h3>波形因果链详情（输出沿 → 主输入，共 ${ce.race_path_count ?? paths.length} 条）</h3>`;
  [...groups.entries()].sort((a, b) => a[0] - b[0]).forEach(([et, ps]) => {
    const isBad = et === r.bad_time;
    html += `<div class="path-group ${isBad ? "is-bad" : "is-edge"}">` +
      `<div class="path-edge">输出 <b>${r.bad_output_port ? r.bad_output_port + "（" + r.bad_output + "）" : r.bad_output}</b> 的${isBad ? "违规沿" : "另一条脉冲沿"} t=${et}` +
      `（${ps.length} 条链）</div><ol class="paths">`;
    ps.forEach(p => {
      const chain = p.hops.map(h => hopHtml(h)).join(" ← ");
      html += `<li>${chain}</li>`;
    });
    html += `</ol></div>`;
  });
  if (ce.race_paths_truncated) {
    html += `<div class="hint">链路过多，仅展示前 ${paths.length} 条（共 ${ce.race_path_count} 条）。</div>`;
  }
  box.innerHTML = html;
  box.classList.add("show");
}

$("wave-export").addEventListener("click", () => {
  if (!state.result) { toast("尚无分析结果"); return; }
  const blob = new Blob([JSON.stringify(state.result, null, 2)],
    { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "hazard-counterexample.json";
  a.click();
});

/* ---------------- 网表 Canvas（层级布局） ---------------- */
function netlistLayout(nl) {
  // 层级 = 到主输入的最长路径（按门数），Kahn 拓扑序计算
  const level = {};
  nl.inputs.forEach(n => level[n] = 0);
  const gmap = Object.fromEntries(nl.gates.map(g => [g.id, g]));
  const indeg = {}, faninOf = {};
  nl.gates.forEach(g => {
    faninOf[g.id] = g.inputs.filter(s => gmap[s]);
    indeg[g.id] = faninOf[g.id].length;
  });
  const queue = nl.gates.filter(g => indeg[g.id] === 0).map(g => g.id);
  const order = [];
  while (queue.length) {
    const n = queue.shift(); order.push(n);
    nl.gates.forEach(g => {
      if (g.inputs.includes(n)) {
        indeg[g.id]--;
        if (indeg[g.id] === 0) queue.push(g.id);
      }
    });
  }
  order.forEach(id => {
    const g = gmap[id];
    level[id] = 1 + Math.max(...g.inputs.map(s => level[s] ?? 0));
  });
  // 每列放置
  const cols = {};
  Object.entries(level).forEach(([n, l]) => {
    (cols[l] = cols[l] || []).push(n);
  });
  const pos = {};
  Object.entries(cols).forEach(([l, nodes]) => {
    nodes.forEach((n, i) => {
      pos[n] = { x: (+l) * 170, y: (i - (nodes.length - 1) / 2) * 70 };
    });
  });
  return { pos, gmap };
}

function topoOrder(nl) {
  // 服务端已提供 order（校验后），本地兜底用网表顺序（假定无环按序）
  return nl.gates.map(g => g.id);
}

function drawNetlist() {
  const nl = state.netlist;
  const cv = $("netlist-canvas");
  const ctx = cv.getContext("2d");
  fitCanvas(cv);
  ctx.clearRect(0, 0, cv.width, cv.height);
  if (!nl || !nl.gates) {
    drawEmpty(ctx, cv, "请先导入并校验网表");
    return;
  }
  const { pos, gmap } = netlistLayout(nl);
  const v = state.netView;
  ctx.save();
  ctx.translate(v.ox, cv.height / 2 + v.oy);
  ctx.scale(v.scale, v.scale);

  const outSet = new Set(nl.outputs);
  // 连线
  ctx.strokeStyle = "#46547a"; ctx.lineWidth = 1.5;
  nl.gates.forEach(g => {
    g.inputs.forEach(s => {
      const a = pos[s], b = pos[g.id];
      if (!a || !b) return;
      ctx.beginPath();
      ctx.moveTo(a.x + 26, a.y);
      ctx.bezierCurveTo(a.x + 70, a.y, b.x - 70, b.y, b.x - 30, b.y);
      ctx.stroke();
    });
  });
  // 主输入（圆）
  nl.inputs.forEach(n => {
    const p = pos[n];
    ctx.fillStyle = "#243353"; ctx.strokeStyle = "#5b74ad";
    roundNode(ctx, p.x, p.y, 22, true);
    ctx.fillStyle = "#dbe5fb"; ctx.font = "12px monospace";
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(n, p.x, p.y);
  });
  // 门（矩形）
  nl.gates.forEach(g => {
    const p = pos[g.id];
    const isOut = outSet.has(g.id);
    ctx.fillStyle = isOut ? "#3a3320" : "#1d2842";
    ctx.strokeStyle = isOut ? "#f0c04f" : "#4f9dff";
    roundNode(ctx, p.x, p.y, 30, false);
    ctx.fillStyle = isOut ? "#ffd874" : "#9cc4ff";
    ctx.font = "bold 11px monospace";
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(g.type.toUpperCase(), p.x, p.y - 7);
    ctx.fillStyle = "#e6ebf5"; ctx.font = "11px monospace";
    ctx.fillText(g.id, p.x, p.y + 8);
    const d = g.delay || [1, 3];
    ctx.fillStyle = "#7f8db0"; ctx.font = "9.5px monospace";
    ctx.fillText(`[${d[0]},${d[1]}]`, p.x, p.y + 20);
  });
  ctx.restore();
}

function roundNode(ctx, x, y, r, circle) {
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  if (circle) ctx.arc(x, y, r, 0, Math.PI * 2);
  else ctx.roundRect(x - r, y - 18, r * 2, 36, 7);
  ctx.fill(); ctx.stroke();
}

/* 网表画布交互：滚轮缩放 + 拖拽平移 */
(function netViewInteract() {
  const cv = $("netlist-canvas");
  cv.addEventListener("wheel", (e) => {
    e.preventDefault();
    const v = state.netView;
    v.scale *= e.deltaY < 0 ? 1.12 : 0.89;
    v.scale = Math.min(4, Math.max(0.3, v.scale));
    drawNetlist();
  }, { passive: false });
  let drag = null;
  cv.addEventListener("mousedown", e => drag = { x: e.clientX, y: e.clientY,
    ox: state.netView.ox, oy: state.netView.oy });
  window.addEventListener("mousemove", e => {
    if (!drag) return;
    state.netView.ox = drag.ox + (e.clientX - drag.x);
    state.netView.oy = drag.oy + (e.clientY - drag.y);
    drawNetlist();
  });
  window.addEventListener("mouseup", () => drag = null);
})();

/* ---------------- 波形 Canvas（可缩放） ---------------- */
$("wave-zoom").addEventListener("input", (e) => {
  state.wave.pxPerT = +e.target.value;
  drawWave();
});
$("wave-fire").addEventListener("change", (e) => {
  state.wave.showFire = e.target.checked;
  drawWave();
});

function drawWave() {
  const cv = $("wave-canvas");
  const ctx = cv.getContext("2d");
  const d = state.result;
  fitCanvas(cv);
  ctx.clearRect(0, 0, cv.width, cv.height);
  if (!d || !d.result.waves) {
    drawEmpty(ctx, cv, "运行验证后在此查看输入与各门输出波形");
    return;
  }
  const waves = d.result.waves;
  const H = d.result.horizon;
  const T = H + 1;
  const px = state.wave.pxPerT;
  const hier = !!(d.result.hierarchy);
  const labelW = hier ? 190 : 110, top = 42, rowH = 46;
  const cssW = cv.getBoundingClientRect().width || 600;
  const needW = Math.max(cssW, labelW + 20 + T * px + 20);
  const needH = waves.length * rowH + 62;
  cv.width = needW;
  cv.height = needH;
  cv.style.height = needH + "px";
  const x0 = labelW + 10;

  ctx.font = "11px monospace";
  // 时间刻度
  ctx.strokeStyle = "#222c45"; ctx.fillStyle = "#7886a8";
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  for (let t = 0; t <= T; t++) {
    const x = x0 + t * px;
    ctx.beginPath(); ctx.moveTo(x, top - 8); ctx.lineTo(x, cv.height - 6); ctx.stroke();
    if (t % Math.ceil(40 / px) === 0 || px >= 24) ctx.fillText(t, x, top - 24);
  }

  waves.forEach((wv, i) => {
    const yMid = top + i * rowH + rowH / 2;
    const lo = yMid + 13, hi = yMid - 13;
    // 标签（层次结果显示实例路径短名，完整端口映射链见下方节点目录）
    ctx.fillStyle = wv.kind === "input" ? "#9cc4ff" : "#ffd874";
    ctx.textAlign = "left"; ctx.textBaseline = "middle";
    const label = hier ? hWaveLabel(wv)
      : ((wv.kind === "input" ? "▸ " : "■ ") + wv.node);
    ctx.save();
    ctx.font = hier ? "10px monospace" : "11px monospace";
    ctx.fillText(label, 8, yMid - (hier ? 6 : 0));
    if (hier && wv.origin && wv.origin.module) {
      ctx.fillStyle = "#6f84b8";
      ctx.fillText(`${wv.origin.module}@${wv.origin.version}`, 8, yMid + 8);
    }
    ctx.restore();

    // 波形
    let cur = wv.initial;
    let px1 = x0;
    const yOf = val => val ? hi : lo;
    ctx.strokeStyle = wv.kind === "input" ? "#5aa9ff" : "#e6ebf5";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(px1, yOf(cur));
    const edgeAt = {};
    wv.edges.forEach(e => edgeAt[e.t] = e.v);
    for (let t = 0; t <= T; t++) {
      const x = x0 + t * px;
      if (edgeAt[t] !== undefined) {
        ctx.lineTo(x, yOf(cur));
        cur = edgeAt[t];
        ctx.lineTo(x, yOf(cur));
      } else {
        ctx.lineTo(x, yOf(cur));
      }
    }
    ctx.stroke();

    // 点火点
    if (state.wave.showFire && wv.fires) {
      ctx.fillStyle = "#ef5b6b";
      wv.fires.forEach(f => {
        const x = x0 + f.t * px;
        ctx.beginPath(); ctx.arc(x, yOf(f.v), 3.2, 0, Math.PI * 2); ctx.fill();
      });
    }
    // 行分隔
    ctx.strokeStyle = "#1a2338";
    ctx.beginPath(); ctx.moveTo(0, yMid + rowH / 2);
    ctx.lineTo(cv.width, yMid + rowH / 2); ctx.stroke();
  });

  // 高亮违规时刻，并用另一种颜色标出窄脉冲的另一条因果沿（起止两条沿）
  const r0 = d.result;
  const pathEdges = new Set(
    ((r0.counterexample && r0.counterexample.race_paths) || [])
      .map(p => p.edge_t));
  pathEdges.forEach(t => {
    if (t === r0.bad_time) return;
    const x = x0 + t * px;
    ctx.strokeStyle = "rgba(240,167,62,.6)"; ctx.lineWidth = 1.2;
    ctx.setLineDash([3, 4]);
    ctx.beginPath(); ctx.moveTo(x, top - 8); ctx.lineTo(x, cv.height - 6); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#f0a73e"; ctx.textAlign = "center";
    ctx.fillText("脉冲沿 t=" + t, x, 2);
  });
  if (r0.bad_time !== undefined && r0.bad_time !== null) {
    const x = x0 + r0.bad_time * px;
    ctx.strokeStyle = "rgba(239,91,107,.65)"; ctx.lineWidth = 1.5;
    ctx.setLineDash([5, 4]);
    ctx.beginPath(); ctx.moveTo(x, top - 8); ctx.lineTo(x, cv.height - 6); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#ef5b6b"; ctx.textAlign = "center";
    ctx.fillText("bad t=" + r0.bad_time, x, top - 38);
  }
}

/* ---------------- 记录 ---------------- */
async function loadRecords() {
  try {
    const d = await api("/api/records");
    const tb = $("records-table").querySelector("tbody");
    tb.innerHTML = "";
    if (!d.records.length) {
      tb.innerHTML = `<tr><td colspan="7" class="hint">暂无记录</td></tr>`;
      return;
    }
    d.records.forEach(r => {
      const dt = new Date(r.created_at * 1000).toLocaleString();
      tb.insertAdjacentHTML("beforeend",
        `<tr>
          <td>${r.id}</td>
          <td>${r.netlist_name || "—"}</td>
          <td class="mono">${r.netlist_hash}</td>
          <td class="mono">${r.params_hash}</td>
          <td><span class="pill ${r.status}">${r.status}</span></td>
          <td class="mono">${dt}</td>
          <td><button class="mini" data-id="${r.id}">载入</button></td>
        </tr>`);
    });
    tb.querySelectorAll("button[data-id]").forEach(b =>
      b.addEventListener("click", () => openRecord(+b.dataset.id)));
  } catch (e) {
    toast("加载记录失败：" + e.message);
  }
}

async function openRecord(id) {
  try {
    const d = await api("/api/records/" + id);
    const rec = d.record;
    $("netlist-json").value = JSON.stringify(rec.netlist_json, null, 2);
    $("params-json").value = JSON.stringify(rec.params_json, null, 2);
    state.netlist = rec.netlist_json;
    state.params = rec.params_json;
    state.result = { result: rec.result_json, record: { id: rec.id },
                     elapsed_ms: 0 };
    buildForm();
    renderResult(state.result);
    document.querySelector('.tabs button[data-tab="wave"]').click();
    toast("已载入记录 #" + id);
  } catch (e) {
    toast("载入失败：" + e.message);
  }
}

$("btn-refresh-records").addEventListener("click", loadRecords);

/* ---------------- 工具 ---------------- */
function fitCanvas(cv) {
  const r = cv.getBoundingClientRect();
  const w = Math.max(300, r.width);
  const h = Math.max(160, r.height || cv.clientHeight || 300);
  if (cv.width !== w) cv.width = w;
  if (cv.height !== h) cv.height = h;
  cv.style.height = h + "px";
}

function drawEmpty(ctx, cv, msg) {
  ctx.fillStyle = "#7886a8"; ctx.font = "13px sans-serif";
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillText(msg, cv.width / 2, cv.height / 2);
}

window.addEventListener("resize", () => { drawNetlist(); drawWave(); });

/* ---------------- 启动 ---------------- */
(async function init() {
  try {
    state.limits = await api("/api/limits");
    $("scale-info").textContent =
      `规模上限：${state.limits.max_gates} 门 / H≤${state.limits.max_horizon} / ` +
      `${state.limits.max_cells} 单元；求解超时 ${state.limits.solver_timeout_ms}ms`;
  } catch (e) { /* 忽略 */ }
  await loadSamples();
  buildForm();
})();
