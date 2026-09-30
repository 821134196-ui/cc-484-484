/* 禽肉检疫票与分割批次系统 —— 前端（原生 JS） */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const state = {
  role: "admin",
  meta: { stalls: [], suppliers: [], cells: [] },
};

const STATUS = {
  pending: "待验票", accepted: "已放行", rejected: "已拒收", empty: "已清空",
  in_stock: "在架", frozen: "冻结", sold: "已售", remainder: "转余料",
  waste: "已废弃", valid: "有效", revoked: "已撤销", expired: "已过期",
  pass: "合格", fail: "不合格", open: "进行中", closed: "已结案",
  destroyed: "已销毁", released: "已解除", reworked: "已返工",
};

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function st(v) {
  const cls = (v || "").replace(/[^a-z_]/g, "");
  return `<span class="st st-${cls}">${esc(STATUS[v] || v)}</span>`;
}
function reasonLine(r) {
  return r ? `<span class="freeze-reason">冻结原因：${esc(r)}</span>` : "";
}
function kg(v, d = 3) {
  return Number(v).toFixed(d);
}

async function api(method, path, body) {
  const h = { "Content-Type": "application/json" };
  if (state.role === "admin") h["X-Role"] = "admin";
  else { h["X-Role"] = "stall"; h["X-Stall"] = state.role; }
  const opt = { method, headers: h };
  if (body) opt.body = JSON.stringify(body);
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const e = new Error(data.message || (`请求失败 ${res.status}`));
    e.code = data.error; e.data = data;
    throw e;
  }
  return data;
}

function toast(msg, ok = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast " + (ok ? "ok" : "err");
  clearTimeout(toast._t);
  t._t = setTimeout(() => t.classList.add("hidden"), 3200);
}
function okToast(msg) { toast(msg, true); }

const isAdmin = () => state.role === "admin";
const stallParam = () => isAdmin() ? "" : `?stall=${state.role}`;

/* ---------------- 身份与选项卡 ---------------- */
function applyRole() {
  state.role = $("#roleSel").value;
  document.body.classList.toggle("is-admin", isAdmin());
  $("#roleHint").textContent = isAdmin()
    ? "可验票放行 / 抽查 / 冻结解封 / 召回"
    : `仅可操作本摊位 ${state.role}，跨摊访问被拒`;
  $$(".tab").forEach(b => b.classList.remove("active"));
  $$(".panel").forEach(p => p.classList.remove("active"));
  $('[data-tab="arrival"]').classList.add("active");
  $("#tab-arrival").classList.add("active");
  initForms();
  refreshAll();
}

function initTabs() {
  $("#tabs").addEventListener("click", e => {
    const b = e.target.closest(".tab");
    if (!b) return;
    $$(".tab").forEach(x => x.classList.remove("active"));
    $$(".panel").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    $(`#tab-${b.dataset.tab}`).classList.add("active");
    refreshTab(b.dataset.tab);
  });
}
function refreshTab(tab) {
  ({
    arrival: refreshArrival, batches: refreshBatches, cells: refreshCells,
    cuts: refreshCuts, trays: refreshTrays, freeze: refreshFreeze,
    recall: refreshRecall, waste: refreshWaste,
  })[tab]();
}
function refreshAll() {
  refreshArrival(); refreshBatches(); refreshCells(); refreshCuts();
  refreshTrays(); refreshFreeze(); refreshRecall(); refreshWaste();
}

/* ---------------- 1. 到货验票 ---------------- */
function initForms() {
  const sup = $("#arrivalSupplier");
  sup.innerHTML = state.meta.suppliers.map(s =>
    `<option value="${esc(s.code)}">${esc(s.code)} · ${esc(s.name)}</option>`).join("");
  const stall = $("#arrivalStall");
  stall.innerHTML = state.meta.stalls.map(s =>
    `<option value="${esc(s.code)}">${esc(s.code)} · ${esc(s.name)}</option>`).join("");
  stall.value = isAdmin() ? "A01" : state.role;
  stall.disabled = !isAdmin();
}

async function refreshArrival() {
  const [batches, events] = await Promise.all([
    api("GET", `/api/batches${stallParam()}`),
    api("GET", "/api/events?limit=60"),
  ]);
  // 待验票（仅管理员可见操作区）
  const pending = batches.filter(b => b.status === "pending");
  $("#pendingList").innerHTML = pending.length
    ? pending.map(b => {
        const opts = state.meta.cells.map(c =>
          `<option value="${esc(c.code)}">${esc(c.code)}（${esc(c.cabinet)} ${esc(c.slot)}）</option>`).join("");
        return `<div class="freeze-card" style="border-left-color:var(--blue)">
          <div class="stack-row">
            <strong class="mono">${esc(b.batch_no)}</strong>
            <span class="tag tag-blue">${esc(b.stall_code)}</span>
            <span>${esc(b.category)}</span>
            <span class="num">票面 ${kg(b.declared_weight)}kg</span>
            <span class="mono small">${esc(b.cert_no)}</span>
          </div>
          ${isAdmin() ? `<div class="stack-row" data-verify="${b.id}">
            <select data-cell>${opts}</select>
            <input type="number" step="0.01" placeholder="实测重量(可空)" data-w style="width:150px">
            <label class="small"><input type="checkbox" data-tol> 差异登记放行</label>
            <button class="btn btn-green btn-sm" data-do>验票入柜</button>
          </div>` : `<p class="small">等待市场管理员验票放行</p>`}
        </div>`;
      }).join("")
    : `<p class="small">暂无待验票批次</p>`;

  $$("[data-verify]").forEach(row => {
    $("[data-do]", row).onclick = async () => {
      const id = row.dataset.verify;
      const wv = $("[data-w]", row).value;
      try {
        const r = await api("POST", `/api/batches/${id}/verify`, {
          cell: $("[data-cell]", row).value,
          actual_weight: wv ? Number(wv) : null,
          tolerate: $("[data-tol]", row).checked,
        });
        if (r.status === "rejected") toast(`已拒收：${r.reason}`);
        else okToast(`${r.batch_no} 放行入柜 ${r.cell}，实测 ${kg(r.arrived_weight)}kg`);
        refreshAll();
      } catch (e) { toast(e.message); }
    };
  });

  // 到货批次表（票据缩略条 + 重量）
  $("#arrivalTable").innerHTML = `<table>
    <thead><tr><th>检疫票缩略条</th><th>批次</th><th>摊位</th><th>品类</th>
    <th class="num">票面kg</th><th class="num">实测kg</th><th>格位</th><th>状态</th><th>拒收/冻结原因</th></tr></thead>
    <tbody>${batches.map(b => `<tr>
      <td><div class="cert-strip">
        <img class="thumb" src="/docs/${esc(b.doc_file || "")}" alt="票据"
             onerror="this.style.visibility='hidden'">
        <div><div class="cno">${esc(b.cert_no)}</div>
        <div class="cmeta">${esc(b.supplier_code)} · 有效期 ${esc(b.valid_until)}</div></div>
      </div></td>
      <td class="mono clickable" data-batch="${b.id}">${esc(b.batch_no)}</td>
      <td>${esc(b.stall_code)}</td><td>${esc(b.category)}</td>
      <td class="num">${kg(b.declared_weight)}</td>
      <td class="num">${b.arrived_weight != null ? kg(b.arrived_weight) : "—"}</td>
      <td>${esc(b.cell_code || "—")}</td>
      <td>${st(b.status)} ${b.cert_status === "revoked" ? st("revoked") : ""}
          ${b.cert_status === "expired" ? st("expired") : ""}</td>
      <td class="small">${esc(b.reject_reason || "")}${reasonLine(b.freeze_reason)}</td>
    </tr>`).join("")}</tbody></table>`;
  $$("[data-batch]").forEach(el => el.onclick = () => openBatch(el.dataset.batch));

  $("#eventLog").innerHTML = events.map(e => `<div class="ev">
    <time>${esc(e.created_at)}</time>
    <span class="lvl lvl-${esc(e.level)}">${esc(e.kind)}</span>
    <span>${esc(e.message)}</span></div>`).join("");
}

$("#arrivalForm").onsubmit = async e => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await api("POST", "/api/arrivals", {
      cert_no: f.get("cert_no"), supplier: f.get("supplier"),
      category: f.get("category"), declared_weight: Number(f.get("declared_weight")),
      stall: e.target.elements.stall.value,
      valid_days: Number(f.get("valid_days") || 3),
    });
    okToast("到货已登记，等待验票");
    e.target.reset();
    initForms();
    refreshAll();
  } catch (err) {
    toast(err.code === "dup_cert" ? `票号重复被拒：${err.message}` : err.message);
  }
};

/* ---------------- 2. 批次库存 ---------------- */
async function refreshBatches() {
  const audit = await api("GET", "/api/audit");
  const am = Object.fromEntries(audit.map(a => [a.batch_id, a]));
  const batches = await api("GET", `/api/batches${stallParam()}`);
  $("#batchTable").innerHTML = `<table>
    <thead><tr><th>批次</th><th>票号/状态</th><th>摊位</th><th>品类</th><th>格位</th>
    <th class="num">到货</th><th class="num">余量</th><th class="num">在架/冻结</th>
    <th class="num">已售</th><th class="num">待处理余料</th><th class="num">废弃</th>
    <th>守恒</th><th>状态</th></tr></thead><tbody>
    ${batches.map(b => {
      const a = am[b.id];
      return `<tr>
      <td class="mono clickable" data-batch="${b.id}">${esc(b.batch_no)}</td>
      <td><span class="mono small">${esc(b.cert_no)}</span><br>${st(b.cert_status)}</td>
      <td>${esc(b.stall_code)}</td><td>${esc(b.category)}</td>
      <td>${esc(b.cell_code || "—")}</td>
      <td class="num">${a ? kg(a.arrived) : "—"}</td>
      <td class="num">${a ? kg(a.remaining) : "—"}</td>
      <td class="num">${a ? kg(a.trays_alive) : "—"}</td>
      <td class="num">${a ? kg(a.sold) : "—"}</td>
      <td class="num">${a ? kg(a.remainders_pending) : "—"}</td>
      <td class="num">${a ? kg(a.waste) : "—"}</td>
      <td>${a ? (a.conserved
          ? `<span class="balance-ok">守恒 ✓</span>`
          : `<span class="balance-bad">偏差 ${kg(a.gap)}</span>`) : "—"}</td>
      <td>${st(b.status)}${reasonLine(b.freeze_reason)}</td>
    </tr>`;
    }).join("")}</tbody></table>`;
  $$("#batchTable [data-batch]").forEach(el => el.onclick = () => openBatch(el.dataset.batch));
}

async function openBatch(id) {
  const d = await api("GET", `/api/batches/${id}`);
  $("#batchDetailCard").classList.remove("hidden");
  $("#batchDetailTitle").innerHTML =
    `批次 <span class="mono">${esc(d.batch.batch_no)}</span> ${st(d.batch.status)}
     <span class="tag tag-gray">${esc(d.batch.category)}</span>`;
  const rows = list => list.length
    ? `<table><thead><tr>${list === d.trays
        ? "<th>托盘</th><th>重量</th><th>状态</th><th>冻结原因</th>"
        : list === d.cuts
          ? "<th>分割单</th><th class='num'>毛重</th><th class='num'>托盘</th><th class='num'>余料</th><th class='num'>废弃</th>"
          : list === d.wastes
            ? "<th>单号</th><th>来源</th><th class='num'>重量</th><th>原因</th><th>异常</th>"
            : "<th>编号</th><th class='num'>重量</th><th>状态</th><th>冻结原因</th>"}</tr></thead>
       <tbody>${list.map(x => list === d.trays
        ? `<tr><td class="mono">${esc(x.tray_no)}</td><td class="num">${kg(x.weight)}</td>
           <td>${st(x.status)}${x.label_lost ? ' <span class="tag tag-amber">标签脱落</span>' : ""}</td>
           <td class="small">${esc(x.freeze_reason || "")}</td></tr>`
        : list === d.cuts
          ? `<tr><td class="mono">${esc(x.cut_no || "余料返工")}</td>
             <td class="num">${kg(x.gross_weight)}</td><td class="num">${kg(x.tray_weight)}</td>
             <td class="num">${kg(x.remainder_weight)}</td><td class="num">${kg(x.waste_weight)}</td></tr>`
          : list === d.wastes
            ? `<tr><td class="mono">${esc(x.code)}</td><td>${esc(x.source_type)}</td>
               <td class="num">${kg(x.weight)}</td><td class="small">${esc(x.reason)}</td>
               <td>${x.abnormal ? '<span class="tag tag-red">异常</span>' : ""}</td></tr>`
            : `<tr><td class="mono">${esc(x.code)}</td><td class="num">${kg(x.weight)}</td>
               <td>${st(x.status)}</td><td class="small">${esc(x.freeze_reason || "")}</td></tr>`)
        .join("")}</tbody></table>`
    : `<p class="small">无</p>`;
  const a = d.balance;
  $("#batchDetail").innerHTML = `
    <p>到货 ${kg(a.arrived)}kg = 余量 ${kg(a.remaining)} + 在架/冻结 ${kg(a.trays_alive)}
       + 已售 ${kg(a.sold)} + 待处理余料 ${kg(a.remainders_pending)} + 废弃 ${kg(a.waste)}
       → <span class="${a.conserved ? "balance-ok" : "balance-bad"}">
       ${a.conserved ? "守恒 ✓" : `偏差 ${kg(a.gap)}kg`}</span></p>
    <h3>分割记录（${d.cuts.length}）</h3>${rows(d.cuts)}
    <h3>托盘（${d.trays.length}）</h3>${rows(d.trays)}
    <h3>余料（${d.remainders.length}）</h3>${rows(d.remainders)}
    <h3>废弃物（${d.wastes.length}）</h3>${rows(d.wastes)}`;
  $("#batchDetailCard").scrollIntoView({ behavior: "smooth" });
}

/* ---------------- 3. 冷藏格位 ---------------- */
async function refreshCells() {
  const cells = await api("GET", "/api/cells");
  $("#cellGrid").innerHTML = cells.map(c => `
    <div class="cell ${c.temp_ok ? "" : "hot"}">
      <div class="cell-head">
        <div><div class="cell-code">${esc(c.code)}</div>
        <div class="small">${esc(c.cabinet)} · ${esc(c.slot)}</div></div>
        <div class="temp ${c.temp_ok ? "ok" : "bad"}">${Number(c.temp).toFixed(1)}℃
          <div class="small">${c.temp_ok ? "温度正常" : "超温冻结"}</div></div>
      </div>
      <div class="occ">${c.occupancy.length
        ? c.occupancy.map(o => `<div>
            <span class="mono">${esc(o.batch_no)} · ${esc(o.category)}</span>
            <span class="num">${kg(o.live_weight)}kg ${esc(o.stall_code)}</span></div>`).join("")
        : `<span class="small muted">空格位</span>`}</div>
    </div>`).join("");
}
$("#cellsScan")?.remove();
const scanBtn = document.createElement("button");
scanBtn.className = "btn btn-ghost btn-sm admin-only inline-admin";
scanBtn.textContent = "立即测温/到期扫描";
scanBtn.style.marginBottom = "10px";
scanBtn.onclick = async () => {
  try { const r = await api("POST", "/api/scan");
    okToast(`扫描完成：到期票 ${r.expiry?.expired?.length || 0}，超温格位 ${r.temp?.alerts?.length || 0}`);
    refreshAll();
  } catch (e) { toast(e.message); }
};
$(".hint", $("#tab-cells .card")).after(scanBtn);

/* ---------------- 4. 分割称重 + 流向图 ---------------- */
async function refreshCuts() {
  const batches = await api("GET", `/api/batches${stallParam()}`);
  const sel = $("#cutBatch");
  const cur = sel.value;
  const usable = batches.filter(b => b.status === "accepted");
  sel.innerHTML = usable.map(b =>
    `<option value="${b.id}">${esc(b.batch_no)} · ${esc(b.category)} · 余量 ${kg(b.remaining)}kg</option>`)
    .join("") || `<option value="">无可分割批次</option>`;
  if (cur && [...sel.options].some(o => o.value === cur)) sel.value = cur;

  // 流向图：批次 → 分割 → 托盘
  const [cuts, trays] = await Promise.all([
    api("GET", "/api/cuts"), api("GET", `/api/trays${stallParam()}`)]);
  const bm = Object.fromEntries(batches.map(b => [b.id, b]));
  const cutsByBatch = {};
  cuts.forEach(c => (cutsByBatch[c.batch_id] ||= []).push(c));
  const traysByCut = {};
  trays.forEach(t => (traysByCut[t.cut_id] ||= []).push(t));

  $("#cutFlow").innerHTML = usable.length ? usable.map(b => {
    const cs = cutsByBatch[b.id] || [];
    return `<div class="flow" style="margin-bottom:14px">
      <div class="flow-col">
        <div class="flow-node batch">
          <div class="n">${esc(b.batch_no)}</div>
          <div>${esc(b.category)} · ${esc(b.stall_code)}</div>
          <div class="w">到货 ${kg(b.arrived_weight)} · 余量 ${kg(b.remaining)}kg</div>
          ${reasonLine(b.freeze_reason)}
        </div>
      </div>
      <div class="flow-arrow">→</div>
      <div class="flow-col">${cs.length ? cs.map(c => `
        <div class="flow-node cut">
          <div class="n">${esc(c.cut_no || "余料返工")}</div>
          <div class="w">扣减 ${kg(c.gross_weight)}kg</div>
          <div class="small">托盘 ${kg(c.tray_weight)} · 余料 ${kg(c.remainder_weight)} · 废弃 ${kg(c.waste_weight)}</div>
        </div>`).join("") : '<span class="small muted">尚未分割</span>'}</div>
      <div class="flow-arrow">→</div>
      <div class="flow-col">${cs.length ? cs.map(c => {
        const ts = (traysByCut[c.id] || []);
        return `<div class="stack" style="gap:4px">${ts.length ? ts.map(t => `
          <div class="flow-node tray ${t.status === "frozen" ? "frozen" : ""}">
            <span class="n">${esc(t.tray_no)}</span>
            <span class="w">${kg(t.weight)}kg</span> ${st(t.status)}
            ${reasonLine(t.freeze_reason)}
          </div>`).join("") : '<span class="small muted">（余料/废弃）</span>'}</div>`;
      }).join("") : ""}</div>
    </div>`;
  }).join("") : `<p class="small">暂无可分割批次</p>`;
}
$("#cutBatch").onchange = async e => {
  const id = e.target.value;
  if (!id) return;
  const d = await api("GET", `/api/batches/${id}`);
  $("#cutBatchInfo").innerHTML = `<p class="hint">批次 <span class="mono">${esc(d.batch.batch_no)}</span>
    当前余量 <strong>${kg(d.balance.remaining)}kg</strong>
    （在架/冻结 ${kg(d.balance.trays_alive)}，已售 ${kg(d.balance.sold)}，
    待处理余料 ${kg(d.balance.remainders_pending)}，废弃 ${kg(d.balance.waste)}）
    ${d.batch.frozen ? `<span class="freeze-reason">${esc(d.batch.freeze_reason)}</span>` : ""}</p>`;
};
$("#cutForm").onsubmit = async e => {
  e.preventDefault();
  const id = $("#cutBatch").value;
  if (!id) return toast("请选择来源批次");
  const f = new FormData(e.target);
  const tw = String(f.get("tray_weights")).split(",").map(s => Number(s.trim())).filter(Boolean);
  try {
    const r = await api("POST", `/api/batches/${id}/cuts`, {
      tray_weights: tw,
      remainder_weight: Number(f.get("remainder_weight") || 0),
      waste_weight: Number(f.get("waste_weight") || 0),
      waste_reason: f.get("waste_reason"),
    });
    okToast(`分割 ${r.cut_no}：扣减 ${kg(r.gross_weight)}kg，生成 ${r.trays.length} 个托盘`
      + (r.waste_abnormal ? "（废弃占比异常已标记）" : ""));
    e.target.reset();
    refreshAll();
  } catch (err) {
    const map = { insufficient_weight: "分割被拒：超过批次余量（同一重量不可重复分割）",
      frozen: "分割被拒：批次处于冻结状态" };
    toast(map[err.code] || err.message);
  }
};

/* ---------------- 5. 托盘标签 / 余料 ---------------- */
async function refreshTrays() {
  const status = $("#trayStatusFilter").value;
  const q = new URLSearchParams();
  if (!isAdmin()) q.set("stall", state.role);
  if (status) q.set("status", status);
  const trays = await api("GET", `/api/trays?${q.toString()}`);
  $("#trayGrid").innerHTML = trays.length ? trays.map(t => `
    <div class="tray ${t.status === "frozen" ? "frozen" : ""}">
      <img class="tray-label" src="/docs/${esc(t.label_file || "")}"
           onerror="this.style.display='none'">
      <div class="tray-body">
        <div class="stack-row">
          <strong class="mono">${esc(t.tray_no)}</strong>${st(t.status)}
          <span class="tag tag-gray">${esc(t.stall_code)}</span>
        </div>
        <div class="small">来源批次 <span class="mono">${esc(t.batch_no)}</span> · ${kg(t.weight)}kg</div>
        ${t.label_lost ? '<span class="lost-flag">⚠ 标签脱落，补签前禁售</span>' : ""}
        ${reasonLine(t.freeze_reason)}
        ${trayActions(t)}
      </div>
    </div>`).join("") : `<p class="small">无托盘</p>`;

  $$("[data-tact]").forEach(btn => {
    btn.onclick = async () => {
      const id = btn.dataset.id, act = btn.dataset.tact;
      try {
        if (act === "sell") { await api("POST", `/api/trays/${id}/sell`); okToast("托盘已售出"); }
        if (act === "remainder") { await api("POST", `/api/trays/${id}/remainder`); okToast("已转余料回收"); }
        if (act === "label-lost") {
          await api("POST", `/api/trays/${id}/label-lost`, { note: "现场报告标签脱落" });
          okToast("已报告标签脱落，等待管理员补签");
        }
        if (act === "relabel") { await api("POST", `/api/trays/${id}/relabel`, {}); okToast("管理员已补签标签"); }
        if (act === "waste") {
          const reason = prompt("废弃原因", "托盘变质废弃");
          if (reason === null) return;
          await api("POST", `/api/trays/${id}/waste`, { reason });
          okToast("托盘已废弃并入台账");
        }
        if (act === "unfreeze") { await api("POST", "/api/freezes/release",
          { target_type: "tray", target_id: Number(id) }); okToast("托盘已解封"); }
        refreshAll();
      } catch (e) { toast(e.message); }
    };
  });
  await refreshRemainders();
}

function trayActions(t) {
  const own = isAdmin() || true; // 隔离由服务端强制
  const btns = [];
  if (t.status === "in_stock" && !t.label_lost)
    btns.push(["sell", "售出", "btn-green"]);
  if (t.status === "in_stock")
    btns.push(["remainder", "转余料", "btn-ghost"], ["waste", "废弃", "btn-ghost"],
              ["label-lost", "标签脱落", "btn-ghost"]);
  if (t.label_lost && isAdmin())
    btns.push(["relabel", "补签标签", "btn-blue"]);
  if (t.status === "frozen" && isAdmin())
    btns.push(["unfreeze", "管理员解封", "btn-red"]);
  return `<div class="tray-actions">${btns.map(([a, txt, c]) =>
    `<button class="btn ${c} btn-sm" data-tact="${a}" data-id="${t.id}">${txt}</button>`).join("")}</div>`;
}

$("#trayStatusFilter").onchange = refreshTrays;
$("#trayRefresh").onclick = refreshTrays;

async function refreshRemainders() {
  const rems = await api("GET", `/api/remainders${stallParam()}`);
  $("#remainderList").innerHTML = rems.length ? `<table><thead><tr>
    <th>余料号</th><th>来源批次</th><th>摊位</th><th class="num">重量</th><th>状态</th>
    <th>返工（新托盘kg，逗号分隔）</th><th>操作</th></tr></thead><tbody>
    ${rems.map(r => `<tr>
      <td class="mono">${esc(r.code)}</td><td class="mono">${esc(r.batch_no)}</td>
      <td>${esc(r.stall_code)}</td><td class="num">${kg(r.weight)}</td>
      <td>${st(r.status)}${reasonLine(r.freeze_reason)}</td>
      <td>${r.status === "pending" && !r.frozen
        ? `<input type="text" data-rw="${r.id}" placeholder="如 5.8" style="width:120px">
           <input type="number" step="0.1" data-rww="${r.id}" value="0" style="width:70px" title="返工废弃kg">`
        : "—"}</td>
      <td>${r.status === "pending" && !r.frozen
        ? `<button class="btn btn-green btn-sm" data-rework="${r.id}">返工分割</button>
           <button class="btn btn-ghost btn-sm" data-rwaste="${r.id}">废弃</button>`
        : ""}</td></tr>`).join("")}</tbody></table>` : `<p class="small">暂无余料</p>`;

  $$("[data-rework]").forEach(b => b.onclick = async () => {
    const id = b.dataset.rework;
    const tw = ($(`[data-rw="${id}"]`).value || "").split(",").map(Number).filter(Boolean);
    const ww = Number($(`[data-rww="${id}"]`).value || 0);
    try { await api("POST", `/api/remainders/${id}/rework`,
      { tray_weights: tw, waste_weight: ww });
      okToast("余料已返工为新托盘（批次链不变）"); refreshAll();
    } catch (e) { toast(e.message); }
  });
  $$("[data-rwaste]").forEach(b => b.onclick = async () => {
    const reason = prompt("余料废弃原因", "余料变质废弃");
    if (reason === null) return;
    try { await api("POST", `/api/remainders/${b.dataset.rwaste}/waste`, { reason });
      okToast("余料已废弃入台账"); refreshAll();
    } catch (e) { toast(e.message); }
  });
}

/* ---------------- 6. 抽查 / 冻结 ---------------- */
async function refreshFreeze() {
  const [insp, batches, trays] = await Promise.all([
    api("GET", "/api/inspections"),
    api("GET", `/api/batches${stallParam()}`),
    api("GET", `/api/trays${stallParam()}`),
  ]);
  let fz = { freezes: [] };
  if (isAdmin()) {
    try { fz = await api("GET", "/api/freezes"); } catch (e) { /* 管理员接口异常时降级 */ }
  }
  const scopeName = { cert: "检疫票", inspection: "市场抽查", recall: "召回", temp: "冷藏超温" };
  if (isAdmin()) {
  $("#freezeList").innerHTML = fz.freezes.length ? fz.freezes.map(f => `
    <div class="freeze-card">
      <div class="stack-row">
        <span class="tag tag-red">${esc(scopeName[f.scope_type] || f.scope_type)}</span>
        <span class="small mono">#${f.freeze_id}</span>
        <span class="small">签发：${esc(f.issued_by)}</span>
      </div>
      <div class="why">${esc(f.reason)}</div>
      <div class="freeze-meta">
        <span>批次 ${f.counts.batch || 0}</span><span>托盘 ${f.counts.tray || 0}</span>
        <span>余料 ${f.counts.remainder || 0}</span>
        <button class="btn btn-red btn-sm" data-release="${f.freeze_id}">解封该冻结单</button>
      </div>
    </div>`).join("") : `<p class="small">当前无冻结单</p>`;
  } else {
    const fb = batches.filter(b => b.frozen);
    const ft = trays.filter(t => t.status === "frozen");
    $("#freezeList").innerHTML = (fb.length + ft.length)
      ? [
          ...fb.map(b => `<div class="freeze-card"><div class="stack-row">
              <span class="tag tag-red">批次</span><span class="mono">${esc(b.batch_no)}</span></div>
              <div class="why">${esc(b.freeze_reason)}</div></div>`),
          ...ft.map(t => `<div class="freeze-card"><div class="stack-row">
              <span class="tag tag-red">托盘</span><span class="mono">${esc(t.tray_no)}</span>
              <span class="small">${esc(t.batch_no)}</span></div>
              <div class="why">${esc(t.freeze_reason)}</div></div>`),
        ].join("")
      : `<p class="small">本摊位当前无冻结对象</p>`;
  }
  if (isAdmin()) $$("[data-release]").forEach(b => b.onclick = async () => {
    try { await api("POST", "/api/freezes/release", { freeze_id: Number(b.dataset.release) });
      okToast("冻结单已解封，无其他冻结约束的目标已恢复"); refreshAll();
    } catch (e) { toast(e.message); }
  });

  $("#inspectionList").innerHTML = insp.length ? `<table><thead><tr>
    <th>抽查号</th><th>批次/托盘</th><th>摊位</th><th>结果</th><th>备注</th><th>检查人</th><th>时间</th>
    </tr></thead><tbody>${insp.map(i => `<tr>
    <td class="mono">${esc(i.insp_no)}</td>
    <td>${i.tray_no ? `托盘 ${esc(i.tray_no)}（${esc(i.batch_no || "")}）` : `批次 ${esc(i.batch_no || "")}`}</td>
    <td>${esc(i.stall_code)}</td><td>${st(i.result)}</td>
    <td class="small">${esc(i.note || "")}</td><td>${esc(i.inspector)}</td>
    <td class="small">${esc(i.created_at)}</td></tr>`).join("")}</tbody></table>`
    : `<p class="small">暂无抽查记录</p>`;

  // 抽查对象辅助选择（每次刷新更新选项，保留已选）
  {
    const form = $("#inspectionForm");
    let sel = $("#inspPick");
    if (!sel) {
      sel = document.createElement("select");
      sel.id = "inspPick";
      sel.onchange = () => {
        if (!sel.value) return;
        const [tg, id] = sel.value.split(":");
        form.elements.target.value = tg;
        form.elements.target_id.value = id;
      };
      form.prepend(sel);
    }
    sel.innerHTML = `<option value="">— 选择对象自动填充 —</option>
      <optgroup label="批次">${batches.map(b =>
        `<option value="batch:${b.id}">批次 ${b.batch_no} ${b.category}</option>`).join("")}</optgroup>
      <optgroup label="托盘">${trays.filter(t => t.status !== "sold").map(t =>
        `<option value="tray:${t.id}">${t.tray_no} (${t.batch_no})</option>`).join("")}</optgroup>`;
  }
}
$("#inspectionForm").onsubmit = async e => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    const r = await api("POST", "/api/inspections", {
      target: f.get("target"), target_id: Number(f.get("target_id")),
      result: f.get("result"), note: f.get("note"),
    });
    if (f.get("result") === "fail")
      okToast(`抽查不合格，已冻结 ${r.batches?.length || 0} 批次、${(r.trays || []).length} 个托盘`);
    else okToast("抽查合格已记录");
    refreshAll();
  } catch (err) { toast(err.message); }
};
$("#revokeForm").onsubmit = async e => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    const r = await api("POST", `/api/certs/${f.get("cert_no")}/revoke`, { reason: f.get("reason") });
    okToast(`票已撤销，冻结 ${r.trays.length} 个未售托盘`);
    refreshAll();
  } catch (err) { toast(err.message); }
};

/* ---------------- 7. 召回清单 ---------------- */
async function refreshRecall() {
  const [recalls, trays] = await Promise.all([
    api("GET", "/api/recalls"),
    api("GET", `/api/trays${stallParam()}`)]);
  // 托盘勾选清单
  const box = $("#recallTrayPick");
  if (box) box.innerHTML = trays.filter(t => t.status !== "waste" && t.status !== "remainder")
    .map(t => `<label class="small" style="display:inline-block;margin:2px 8px 2px 0">
      <input type="checkbox" value="${t.id}"> ${esc(t.tray_no)}
      <span class="muted">(${esc(t.stall_code)},${esc(t.batch_no)},${STATUS[t.status]})</span></label>`)
    .join("");

  $("#recallList").innerHTML = recalls.length ? recalls.map(r => `
    <div class="recall-card">
      <div class="recall-head">
        <div><strong class="mono">${esc(r.recall_no)}</strong>
          <span class="tag ${r.status === "open" ? "tag-red" : "tag-gray"}">${STATUS[r.status]}</span>
          ${r.cert_no ? `<span class="small mono">票 ${esc(r.cert_no)}</span>` : ""}
          ${r.batch_no ? `<span class="small mono">批 ${esc(r.batch_no)}</span>` : ""}</div>
        ${isAdmin() && r.status === "open"
          ? `<span><button class="btn btn-red btn-sm" data-destroy="${r.id}">销毁勾选托盘</button>
             <button class="btn btn-ghost btn-sm" data-close="${r.id}">召回结案/解除</button></span>` : ""}
      </div>
      <div class="recall-body">
        <p>${esc(r.reason)}</p>
        <table><thead><tr><th>摊位</th><th>托盘号</th><th class="num">重量</th><th>当前状态</th>
        <th>处置</th><th>冻结原因</th>${isAdmin() && r.status === "open" ? "<th>勾选</th>" : ""}
        </tr></thead><tbody>
        ${[...r.items].sort((a, b) =>
          (a.stall_code + a.tray_no).localeCompare(b.stall_code + b.tray_no)).map(i => `
          <tr><td>${esc(i.stall_code)}</td><td class="mono">${esc(i.tray_no)}</td>
          <td class="num">${kg(i.weight)}</td><td>${st(i.status)}</td>
          <td>${st(i.disposition)}</td><td class="small">${esc(i.freeze_reason || "")}</td>
          ${isAdmin() && r.status === "open"
            ? `<td><input type="checkbox" data-recall="${r.id}" data-tray="${i.tray_no}"
                 data-tid="${i.tray_id}" ${i.disposition === "pending" ? "" : "disabled"}></td>` : ""}
          </tr>`).join("")}
        </tbody></table>
      </div>
    </div>`).join("") : `<p class="small">暂无召回记录</p>`;

  $$("[data-close]").forEach(b => b.onclick = async () => {
    try { const r = await api("POST", `/api/recalls/${b.dataset.close}/close`, {});
      okToast(`召回已结案，解除 ${r.released_trays.length} 个托盘（其他冻结约束仍生效）`);
      refreshAll();
    } catch (e) { toast(e.message); }
  });
  $$("[data-destroy]").forEach(b => b.onclick = async () => {
    const rid = b.dataset.destroy;
    const ids = $$(`input[data-recall="${rid}"]:checked`).map(i => Number(i.dataset.tid));
    if (!ids.length) return toast("请先勾选待销毁托盘");
    try { const r = await api("POST", `/api/recalls/${rid}/destroy`, { tray_ids: ids });
      okToast(`已销毁 ${r.destroyed.length} 个托盘并入废弃物台账`); refreshAll();
    } catch (e) { toast(e.message); }
  });
}
$("#recallForm").onsubmit = async e => {
  e.preventDefault();
  const picked = $$("#recallTrayPick input:checked").map(i => Number(i.value));
  const f = new FormData(e.target);
  const typed = String(f.get("tray_ids")).split(",").map(s => Number(s.trim())).filter(Boolean);
  const ids = [...new Set([...picked, ...typed])];
  if (!ids.length) return toast("请勾选或填写召回托盘");
  try {
    const r = await api("POST", "/api/recalls", { reason: f.get("reason"), tray_ids: ids });
    okToast(`召回 ${r.recall_no}：冻结未售 ${r.frozen_trays.length} 个，`
      + `已售/不在架 ${r.sold_or_missing.length} 个转追溯`);
    refreshAll();
  } catch (err) { toast(err.message); }
};

/* ---------------- 8. 废弃物台账 ---------------- */
async function refreshWaste() {
  const rows = await api("GET", `/api/wastes${stallParam()}`);
  $("#wasteTable").innerHTML = rows.length ? `<table><thead><tr>
    <th>废弃单号</th><th>批次</th><th>摊位</th><th>来源</th><th class="num">重量kg</th>
    <th>原因</th><th>标记</th><th>时间</th></tr></thead><tbody>
    ${rows.map(w => `<tr style="${w.abnormal ? "background:var(--red-l)" : ""}">
      <td class="mono">${esc(w.code)}</td><td class="mono">${esc(w.batch_no)}</td>
      <td>${esc(w.stall_code)}</td><td>${esc(w.source_type)}</td>
      <td class="num"><strong>${kg(w.weight)}</strong></td>
      <td class="small">${esc(w.reason)}</td>
      <td>${w.abnormal ? '<span class="tag tag-red">重量异常</span>' : '<span class="tag tag-green">正常</span>'}</td>
      <td class="small">${esc(w.created_at)}</td></tr>`).join("")}</tbody></table>`
    : `<p class="small">暂无废弃记录</p>`;
}

/* ---------------- 启动 ---------------- */
(async function boot() {
  $("#roleSel").onchange = applyRole;
  initTabs();
  state.meta = await api("GET", "/api/meta");
  applyRole();
})();
