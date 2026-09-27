"use strict";

/* ⑤ 层次化模块库与组合验证 */
const H = {
  modules: [],
  comps: [],
  records: [],
  samples: [],
  flat: null,
  compName: null,
};

const h$ = (id) => document.getElementById(id);

/* ---------------- 模块库 ---------------- */
async function hLoadModules() {
  const d = await api("/api/hierarchy/modules");
  H.modules = d.modules;
  const tb = h$("hier-mod-table").querySelector("tbody");
  tb.innerHTML = "";
  if (!H.modules.length) {
    tb.innerHTML = `<tr><td colspan="6" class="hint">暂无模块，请在下方登记或导入示例。</td></tr>`;
    return;
  }
  H.modules.forEach((m) => {
    tb.insertAdjacentHTML("beforeend",
      `<tr>
        <td>${m.name}</td><td class="mono">${m.version}</td>
        <td class="mono">${m.n_inputs}/${m.n_outputs}</td>
        <td>${m.n_gates}</td><td>${m.n_instances}</td>
        <td><button class="mini" data-m="${m.name}" data-v="${m.version}">查看</button></td>
      </tr>`);
  });
  tb.querySelectorAll("button[data-m]").forEach((b) =>
    b.addEventListener("click", async () => {
      const d = await api(`/api/hierarchy/modules/${encodeURIComponent(b.dataset.m)}/${encodeURIComponent(b.dataset.v)}`);
      h$("hier-module-json").value = JSON.stringify(d.module, null, 2);
      toast(`已载入模块 ${b.dataset.m}@${b.dataset.v}（只读）`);
    }));
}

h$("btn-hier-refresh").addEventListener("click", async () => {
  await Promise.all([hLoadModules(), hLoadComps(true), hLoadRecords()]);
});

h$("btn-hier-module-validate").addEventListener("click", async () => {
  await hPostJson(h$("hier-module-json"), "/api/hierarchy/modules/validate",
    "hier-module-info", (d) => `✓ 模块合法：${d.name}@${d.version}\n` +
      `输入 ${d.summary.inputs.join(",") || "—"} → 输出 ${d.summary.outputs.join(",")}\n` +
      `门 ${d.summary.n_gates} / 实例 ${d.summary.n_instances}；引用 ${d.summary.references.join(", ") || "无"}\n` +
      `稳定序: ${d.summary.order.join(" → ")}`);
});

h$("btn-hier-module-save").addEventListener("click", async () => {
  let doc;
  try { doc = JSON.parse(h$("hier-module-json").value); }
  catch (e) { return hInfo("hier-module-info", "err", "JSON 解析失败：" + e.message); }
  try {
    const d = await api("/api/hierarchy/modules", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(doc),
    });
    hInfo("hier-module-info", "ok",
      `✓ 已登记 ${d.module.name}@${d.module.version}（id=${d.module.id}，版本不可修改）`);
    await hLoadModules();
  } catch (e) { hInfo("hier-module-info", "err", "✗ " + e.message); }
});

/* ---------------- 整包导入/导出 ---------------- */
h$("btn-hier-bundle-import").addEventListener("click", async () => {
  let doc;
  try { doc = JSON.parse(h$("hier-bundle-json").value); }
  catch (e) { return hInfo("hier-module-info", "err", "JSON 解析失败：" + e.message); }
  try {
    const d = await api("/api/hierarchy/import", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(doc),
    });
    hInfo("hier-module-info", "ok",
      `✓ 已登记模块：${d.created_modules.join(", ") || "无"}\n` +
      `跳过已存在相同版本：${d.skipped_existing.join(", ") || "无"}\n` +
      (d.compositions.length ? `组合：${d.compositions.map(c => c.composition.name + "#rev" + c.composition.version).join(", ")}` : ""));
    await Promise.all([hLoadModules(), hLoadComps(true)]);
    if (d.compositions.length) {
      H.compName = d.compositions[0].composition.name;
      h$("hier-comp-select").value = H.compName;
    }
  } catch (e) { hInfo("hier-module-info", "err", "✗ " + e.message); }
});

h$("btn-hier-export").addEventListener("click", async () => {
  const d = await api("/api/hierarchy/export");
  h$("hier-bundle-json").value = JSON.stringify(d, null, 2);
  downloadJson(d, "hierarchy-library.json");
  toast("已导出整库（modules + compositions）");
});

h$("hier-file").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const rd = new FileReader();
  rd.onload = () => {
    try {
      const obj = JSON.parse(rd.result);
      if (obj.modules || obj.compositions) {
        h$("hier-bundle-json").value = JSON.stringify(obj, null, 2);
        toast("已载入导入包，点“导入整包”执行");
      } else if (obj.instances) {
        h$("hier-comp-json").value = JSON.stringify(obj, null, 2);
      } else {
        h$("hier-module-json").value = JSON.stringify(obj, null, 2);
      }
    } catch (err) { toast("JSON 解析失败：" + err.message); }
  };
  rd.readAsText(f);
});

/* ---------------- 组合 ---------------- */
async function hLoadComps(reloadSelect) {
  const d = await api("/api/hierarchy/compositions");
  H.comps = d.compositions;
  const tbSel = h$("hier-comp-select");
  if (reloadSelect || !tbSel.options.length) {
    tbSel.innerHTML = H.comps.length
      ? H.comps.map((c) => `<option value="${c.name}">${c.name} (rev${c.version})</option>`).join("")
      : `<option value="">（无组合）</option>`;
  }
  await hLoadRecords();
}

h$("btn-hier-comp-refresh").addEventListener("click", () => hLoadComps(true));

h$("btn-hier-comp-new").addEventListener("click", () => {
  h$("hier-comp-json").value = JSON.stringify({
    name: "new_top", inputs: ["a"], outputs: ["z"],
    instances: [{ id: "i0", module: "inv", version: "1",
                 connections: { a: "a", y: "z" } }],
  }, null, 2);
  H.compName = null;
});

h$("btn-hier-comp-load").addEventListener("click", async () => {
  const name = h$("hier-comp-select").value;
  if (!name) return;
  const d = await api(`/api/hierarchy/compositions/${encodeURIComponent(name)}`);
  h$("hier-comp-json").value = JSON.stringify(d.composition, null, 2);
  H.compName = name;
  toast(`已载入组合 ${name}（rev${d.meta.version}）`);
});

h$("btn-hier-comp-delete").addEventListener("click", async () => {
  const name = h$("hier-comp-select").value;
  if (!name) return;
  if (!confirm(`确认删除组合 ${name}？分析记录仍保留（可重放）。`)) return;
  await api(`/api/hierarchy/compositions/${encodeURIComponent(name)}`, { method: "DELETE" });
  toast("已删除组合 " + name);
  hLoadComps(true);
});

h$("btn-hier-comp-validate").addEventListener("click", async () => {
  let doc;
  try { doc = JSON.parse(h$("hier-comp-json").value); }
  catch (e) { return hInfo("hier-comp-info", "err", "JSON 解析失败：" + e.message); }
  try {
    const d = await api("/api/hierarchy/compositions/validate-flatten", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(doc),
    });
    H.flat = d.flatten;
    const f = d.flatten;
    hInfo("hier-comp-info", "ok",
      `✓ 合法并可展开：${f.netlist.inputs.length} 输入 / ${f.netlist.gates.length} 展开门（含纯导线 buf）` +
      ` / ${f.instances.length} 实例，最大深度 ${f.statistics.max_depth}。`);
    hRenderFlat(f);
  } catch (e) { hInfo("hier-comp-info", "err", "✗ " + e.message); }
});

h$("btn-hier-comp-save").addEventListener("click", async () => {
  let doc;
  try { doc = JSON.parse(h$("hier-comp-json").value); }
  catch (e) { return hInfo("hier-comp-info", "err", "JSON 解析失败：" + e.message); }
  const name = doc.name;
  if (!name) return hInfo("hier-comp-info", "err", "组合需要 name");
  try {
    const d = await api(`/api/hierarchy/compositions/${encodeURIComponent(name)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(doc),
    });
    H.compName = name;
    const c = d.composition;
    hInfo("hier-comp-info", "ok",
      c.reused
        ? (c.unchanged
            ? `✓ 组合 ${c.name} 内容未变化，保留 rev${c.version}`
            : `✓ 已更新组合 ${c.name} 到 rev${c.version}`)
        : `✓ 已创建组合 ${c.name}（rev1）`);
    await hLoadComps(true);
    const f = (await api(`/api/hierarchy/compositions/${encodeURIComponent(name)}/flatten`));
    H.flat = f.flatten || f;
    hRenderFlat(H.flat);
  } catch (e) { hInfo("hier-comp-info", "err", "✗ " + e.message); }
});

h$("btn-hier-flatten").addEventListener("click", async () => {
  const name = H.compName || h$("hier-comp-select").value;
  if (!name) return toast("请先保存组合或在下拉中选择");
  const d = await api(`/api/hierarchy/compositions/${encodeURIComponent(name)}/flatten`);
  H.flat = d.flatten || d;
  hRenderFlat(H.flat);
  toast("已展开 " + name);
});

/* ---------------- 层次验证 ---------------- */
h$("btn-hier-analyze").addEventListener("click", async () => {
  let name = H.compName;
  if (!name) name = h$("hier-comp-select").value;
  if (!name) return toast("请先在下拉中选择或“保存组合”（分析按已保存组合执行）");
  H.compName = name;
  let params;
  try { params = JSON.parse(h$("hier-params-json").value); }
  catch (e) { return hInfo("hier-comp-info", "err", "参数 JSON 解析失败：" + e.message); }
  const btn = h$("btn-hier-analyze");
  btn.disabled = true; btn.textContent = "z3 求解中…";
  try {
    const t0 = performance.now();
    const d = await api(`/api/hierarchy/compositions/${encodeURIComponent(name)}/analyze`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ params }),
    });
    d.elapsed_ms = Math.round(performance.now() - t0);
    state.result = d;
    renderResult(d);
    hRenderHierarchy(d.result);
    toast(`层次验证完成（${d.elapsed_ms} ms），记录 #${d.record.id}` +
          (d.record.reused ? "（复用去重记录）" : ""));
    document.querySelector('.tabs button[data-tab="wave"]').click();
    await hLoadRecords();
  } catch (e) { toast("层次验证失败：" + e.message, 4500); }
  finally { btn.disabled = false; btn.textContent = "运行层次验证"; }
});

/* ---------------- 展开结果渲染 ---------------- */
function hRenderFlat(f) {
  const box = h$("hier-flat-detail");
  const net = f.netlist;
  let html = `<div class="hint">展开网表名 <b>${net.name}</b>：${net.inputs.length} 输入、` +
    `${net.gates.length} 门、${net.outputs.length} 输出；实例 ${f.statistics.n_instances} 个，` +
    `最大层次深度 ${f.statistics.max_depth}。固定模块版本：` +
    Object.entries(f.module_versions).map(([k, v]) => `${k}@${v}`).join(", ") + `</div>`;

  html += `<h3>实例树（路径：模块@版本 + 端口映射）</h3>`;
  f.instances.forEach((it) => {
    const depth = it.path.split("/").length - 1;
    const conns = Object.entries(it.connections)
      .map(([p, w]) => `${p}→${w}`).join(", ");
    html += `<div class="flat-inst" style="padding-left:${depth * 18}px">` +
      `${"└ ".repeat(depth ? 1 : 0)}${it.path} : ${it.module}@${it.version} ` +
      `<span class="route-chain">[${conns}]</span></div>`;
  });

  html += `<h3>展平后的门级网表（喂给既有 z3 流水线）</h3><pre>${
    JSON.stringify(net, null, 1).replace(/[&<>]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c]))
  }</pre>`;

  html += `<h3>节点来源（实例路径 / 端口映射 crossings）</h3><table>` +
    `<tr><th>扁平节点</th><th>类型</th><th>来源</th><th>穿越的实例边界端口</th></tr>`;
  Object.entries(f.nodes).forEach(([node, inf]) => {
    const src = inf.kind === "gate"
      ? `${inf.instance_path ? inf.instance_path + "/" : ""}${inf.instance_id || ""} 的局部门 ${inf.local}` +
        (inf.module ? `（${inf.module}@${inf.version}）` : "")
      : inf.kind === "input" ? "顶层主输入"
      : inf.kind === "wire_buf" ? `内部导线 ${inf.local} 的零延迟恒等门`
      : `内部 wire ${inf.local}`;
    const cr = (inf.crossings || []).map((c) =>
      `${c.instance_path || c.instance_id}.${c.port}(${c.direction})`).join(" ← ");
    html += `<tr><td class="mono">${node}</td><td>${inf.kind}</td>` +
      `<td>${src}</td><td class="route-chain">${cr || "—"}</td></tr>`;
  });
  html += `</table>`;
  box.innerHTML = html;
  box.classList.add("show");
}

/* 波形 Tab 下方：层次版本/实例/端口映射总览 */
function hRenderHierarchy(result) {
  const box = h$("wave-hierarchy");
  const hh = result.hierarchy;
  if (!hh) { box.classList.remove("show"); box.innerHTML = ""; return; }
  let html = `<h3>层次信息（模块版本固定）</h3><div class="hint">` +
    Object.entries(hh.module_versions).map(([k, v]) => `${k}@${v}`).join(", ") +
    `；实例 ${hh.instances.length} 个，展开门 ${hh.statistics.n_gates}。</div>`;
  html += `<h3>实例路径与端口映射</h3>`;
  hh.instances.forEach((it) => {
    const conns = Object.entries(it.connections).map(([p, w]) => `${p}→${w}`).join(", ");
    html += `<div class="flat-inst">${it.path} : ${it.module}@${it.version} <span class="route-chain">[${conns}]</span></div>`;
  });
  html += `<h3>节点目录（race_paths / 波形 / 记录节点同时显示实例路径与端口映射）</h3>` +
    `<table><tr><th>扁平节点</th><th>可读路径</th><th>来源</th></tr>`;
  Object.entries(hh.node_catalog).forEach(([node, inf]) => {
    if (inf.alias_of) return;
    const crossings = (inf.crossings || []).map((c) =>
      `${c.instance_path || c.instance_id}.${c.port}`).join(" ← ");
    const origin = inf.kind === "gate"
      ? `实例 ${inf.instance_path == null ? "(顶层)" : inf.instance_path} 局部门 ${inf.local}` +
        (inf.module ? ` (${inf.module}@${inf.version})` : "") +
        (inf.delay ? ` d=[${inf.delay[0]},${inf.delay[1]}]` : "")
      : inf.kind === "input" ? "顶层主输入"
      : inf.kind === "wire_buf" ? `导线 ${inf.local}` : `内部 wire ${inf.local}`;
    html += `<tr><td class="mono">${node}</td><td class="route-chain">${node}${crossings ? " ← " + crossings : ""}</td>` +
      `<td>${origin}</td></tr>`;
  });
  html += `</table>`;
  box.innerHTML = html;
  box.classList.add("show");
}

/* ---------------- 层次分析记录 ---------------- */
async function hLoadRecords() {
  const d = await api("/api/hierarchy/analyses");
  H.records = d.records;
  const tb = h$("hier-records-table").querySelector("tbody");
  tb.innerHTML = "";
  if (!H.records.length) {
    tb.innerHTML = `<tr><td colspan="7" class="hint">暂无层次分析记录</td></tr>`;
    return;
  }
  H.records.forEach((r) => {
    const dt = new Date(r.created_at * 1000).toLocaleString();
    tb.insertAdjacentHTML("beforeend",
      `<tr>
        <td>${r.id}</td><td>${r.comp_name}</td><td>rev${r.comp_version}</td>
        <td class="mono">${r.flat_hash}</td>
        <td><span class="pill ${r.status}">${r.status}</span></td>
        <td class="mono">${dt}</td>
        <td>
          <button class="mini" data-act="view" data-id="${r.id}">查看</button>
          <button class="mini" data-act="replay" data-id="${r.id}">重放</button>
        </td>
      </tr>`);
  });
  tb.querySelectorAll("button[data-id]").forEach((b) =>
    b.addEventListener("click", () =>
      b.dataset.act === "replay" ? hReplay(+b.dataset.id) : hOpenRecord(+b.dataset.id)));
}

async function hOpenRecord(id) {
  const d = await api(`/api/hierarchy/analyses/${id}`);
  const rec = d.record;
  state.result = { result: rec.result_json, record: { id: rec.id }, elapsed_ms: 0 };
  state.netlist = rec.flat_json;
  renderResult(state.result);
  hRenderHierarchy(rec.result_json);
  document.querySelector('.tabs button[data-tab="wave"]').click();
  toast(`已载入层次记录 #${id}（${rec.comp_name} rev${rec.comp_version}）`);
}

async function hReplay(id) {
  const d = await api(`/api/hierarchy/analyses/${id}/replay`, { method: "POST" });
  state.result = { result: d.result, record: { id }, elapsed_ms: 0 };
  renderResult(state.result);
  const full = await api(`/api/hierarchy/analyses/${id}`);
  state.netlist = full.record.flat_json;
  hRenderHierarchy(d.result);
  document.querySelector('.tabs button[data-tab="wave"]').click();
  toast(`已用固定版本重放记录 #${id}：${d.result.status}`);
}

/* ---------------- 示例 ---------------- */
async function hLoadSamples() {
  try { H.samples = (await api("/api/hierarchy/samples")).samples; }
  catch (e) { H.samples = []; }
}
h$("btn-hier-sample1").addEventListener("click", () => hUseSample(0));
h$("btn-hier-sample2").addEventListener("click", () => hUseSample(1));

function hUseSample(i) {
  const s = H.samples[i];
  if (!s) return toast("示例未找到");
  h$("hier-bundle-json").value = JSON.stringify(
    { modules: s.modules, compositions: s.compositions }, null, 2);
  const cname = s.analyze?.composition || Object.keys(s.compositions || {})[0];
  H.compName = cname;
  h$("hier-comp-json").value = JSON.stringify(
    (s.compositions || {})[cname] || {}, null, 2);
  h$("hier-params-json").value = JSON.stringify(
    (s.analyze && s.analyze.params) || {}, null, 2);
  toast(`示例「${s.name}」：先点“导入整包”，即可直接“运行层次验证”`);
}

/* ---------------- 工具 ---------------- */
function hInfo(id, cls, msg) {
  const box = h$(id);
  box.className = "infobox " + cls;
  box.textContent = msg;
}

async function hPostJson(ta, url, infoId, okMsg) {
  let doc;
  try { doc = JSON.parse(ta.value); }
  catch (e) { return hInfo(infoId, "err", "JSON 解析失败：" + e.message); }
  try {
    const d = await api(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(doc),
    });
    hInfo(infoId, "ok", typeof okMsg === "function" ? okMsg(d) : "✓ 合法");
  } catch (e) { hInfo(infoId, "err", "✗ " + e.message); }
}

function downloadJson(obj, filename) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
}

/* 切到层次页时刷新数据 */
document.addEventListener("click", (e) => {
  const btn = e.target.closest?.('.tabs button[data-tab="hier"]');
  if (btn) { hLoadModules(); hLoadComps(false); hLoadRecords(); hLoadSamples(); }
});

hLoadSamples();
