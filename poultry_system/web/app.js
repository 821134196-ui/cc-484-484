/* 禽肉检疫票与分割批次系统 —— 单页前端（原生 JS，无依赖） */
"use strict";
const $ = (s, el=document) => el.querySelector(s);
const $$ = (s, el=document) => [...el.querySelectorAll(s)];
const state = { actor: "admin", meta: {users:[],stalls:[],suppliers:[],locations:[]}, timer:null };

const kg = g => g==null ? "—" : (g/1000).toFixed(3).replace(/0+$/,"").replace(/\.$/,"") + " kg";
const esc = s => String(s??"").replace(/[&<>"']/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

async function api(path, opts={}){
  const o = Object.assign({headers:{"Content-Type":"application/json","X-Actor":state.actor}}, opts);
  const res = await fetch(path, o);
  const data = await res.json().catch(()=>({}));
  if(!res.ok) throw new Error(data.error || ("HTTP "+res.status));
  return data;
}
const post = (path, body) => api(path,{method:"POST",body:JSON.stringify(body||{})});
const qs = o => Object.entries(o).filter(([,v])=>v!=null&&v!=="")
  .map(([k,v])=>`${k}=${encodeURIComponent(v)}`).join("&");

function toast(msg, kind="ok"){
  const box=$("#toast"); const t=document.createElement("div");
  t.className="t "+(kind==="err"?"err":kind==="info"?"":"ok");
  t.textContent=msg; box.appendChild(t);
  setTimeout(()=>{t.style.opacity="0";t.style.transition="opacity .4s";setTimeout(()=>t.remove(),400)},3800);
}
const errToast = e => toast(e.message||String(e),"err");

const STALL_NAME = c => (state.meta.stalls.find(s=>s.code===c)||{}).name || c;

/* --------------------------------------------------------------- 状态徽标 */
const BATCH_STATUS = {pending:["待放行","amber"],released:["已放行","green"],rejected:["拒收","red"]};
const TRAY_STATUS  = {on_sale:["在售","green"],sold:["售罄","gray"],remainder:["已转余料","amber"],waste:["已废弃","gray"],frozen:["冻结","red"]};
const TICKET_STATUS= {active:["有效","green"],revoked:["已撤销","red"],expired:["已过期","red"],consumed:["已完结","gray"]};
function chip(text, color){return `<span class="chip ${color}">${esc(text)}</span>`;}
function freezeReason(r){ return r ? `<div class="frozen-box">${esc(r)}</div>` : ""; }

/* --------------------------------------------------------------- 票据缩略条 */
function ticketTicketIcon(){
  return `<svg viewBox="0 0 24 24" fill="none" stroke="#2e7d32" stroke-width="2">
    <path d="M4 7h16v3a2 2 0 000 4v3H4v-3a2 2 0 000-4V7z"/><path d="M12 7v10" stroke-dasharray="2 2"/></svg>`;
}
function ticketStrip({ticket_no, ticket_status, supplier_code, category, valid_until, ticket_image, weight_g}){
  const ts = TICKET_STATUS[ticket_status]||["—","gray"];
  return `<div class="ticket-strip">
    <div class="stub"><i>检疫合格</i></div>
    <div class="body">
      <div class="ttop">
        <span class="tno mono">🏷 ${esc(ticket_no)}</span>
        ${chip(ts[0],ts[1])}
        ${ticket_image?`<a href="/data/${esc(ticket_image)}" target="_blank" class="tag">票据原件(SVG)</a>`:""}
      </div>
      <div class="tgrow">
        <span>供应商代号 <b class="mono">${esc(supplier_code)}</b></span>
        <span>品类 <b>${esc(category)}</b></span>
        <span>有效期至 <b>${esc((valid_until||"").replace("T"," "))}</b></span>
      </div>
    </div>
    <div class="weight"><div class="w">${kg(weight_g)}</div><div class="muted" style="font-size:11px">票面重量</div></div>
    <div class="thumb">${ticketTicketIcon()}</div>
  </div>`;
}

/* --------------------------------------------------------------- 布局 */
const PAGES = [
  ["arrival","到货验票"],["batches","批次库存"],["cabinets","冷藏格位"],
  ["cut","分割称重"],["labels","托盘标签"],["inspection","抽查冻结"],
  ["recalls","召回清单"],["waste","废弃物台账"]
];
function renderNav(){
  $("#nav").innerHTML = PAGES.map(([k,n],i)=>
    `<button data-p="${k}" class="${location.hash==="#"+k?"active":""}">${n}</button>`).join("");
  $$("#nav button").forEach(b=>b.onclick=()=>{location.hash=b.dataset.p;});
}

/* --------------------------------------------------------------- 首页/概览（并入到货验票顶部） */
async function overviewCards(stall){
  const d = await api("/api/overview?"+qs({actor:state.actor,stall}));
  return `<div class="cards">
    <div class="stat blue"><div class="n">${d.n_batches}</div><div class="l">到货批次</div></div>
    <div class="stat green"><div class="n">${d.n_trays_on_sale}</div><div class="l">在售托盘</div></div>
    <div class="stat red"><div class="n">${d.n_frozen}</div><div class="l">冻结托盘</div></div>
    <div class="stat amber"><div class="n">${d.open_incidents}</div><div class="l">未处理异常</div></div>
    <div class="stat red"><div class="n">${d.active_recalls}</div><div class="l">生效召回</div></div>
    <div class="stat amber"><div class="n">${d.n_abnormal_waste}</div><div class="l">废弃异常单</div></div>
  </div>`;
}

/* =============================================================== 到货验票 */
async function viewArrival(){
  const isAdmin = actorRole()==="market_admin";
  const opts = state.meta.stalls.map(s=>`<option value="${esc(s.code)}">${esc(s.code)} ${esc(s.name)}</option>`).join("");
  const sup = state.meta.suppliers.map(s=>`<option value="${esc(s.code)}">${esc(s.code)} ${esc(s.name)}</option>`).join("");
  const now = new Date(); const iso=d=>d.toISOString().slice(0,16);
  const from = iso(new Date(now.getTime()-3600e3));
  const until = iso(new Date(now.getTime()+2*86400e3));
  return `
  ${await overviewCards(isAdmin?"":myStall())}
  <div class="grid2">
    <div class="panel">
      <h2><span class="bar"></span>到货登记 · 检疫票核验</h2>
      <p class="section-note">登记即过磅（模拟秤回执），系统核验票号唯一、有效期、票货品类/重量相符与称重差异；放行后才能入柜。</p>
      <form id="fArrive">
        <div class="row"><div class="field"><label>摊位</label><select name="stall" ${isAdmin?"":"disabled"}>${opts}</select>
          <input type="hidden" name="stall_real" value="${isAdmin?"":myStall()}"></div>
          <div class="field"><label>供应商代号</label><select name="supplier">${sup}</select></div></div>
        <div class="field"><label>检疫票编号</label><input name="ticket_no" placeholder="Q-2026-0001（虚构）" required></div>
        <div class="row"><div class="field"><label>到货品类</label><input name="category" value="白条鸡" required></div>
          <div class="field"><label>票面品类</label><input name="ticket_category" value="白条鸡" required></div></div>
        <div class="row"><div class="field"><label>票面重量 (g)</label><input name="ticket_weight_g" type="number" value="10000" required></div>
          <div class="field"><label>报称重量 (g)</label><input name="declared_weight_g" type="number" value="10000" required></div></div>
        <div class="row"><div class="field"><label>检疫生效</label><input name="valid_from" type="datetime-local" value="${from}"></div>
          <div class="field"><label>检疫有效期至</label><input name="valid_until" type="datetime-local" value="${until}"></div></div>
        <div class="field"><label>备注</label><input name="note" placeholder="可空"></div>
        <button class="btn green">核验并登记到货</button>
      </form>
    </div>
    <div class="panel">
      <h2><span class="bar"></span>异常处置（${isAdmin?"市场管理员":"仅管理员可处置"}）</h2>
      <div id="incidentList" class="muted">加载中…</div>
    </div>
  </div>`;
}
function bindArrival(){
  const f=$("#fArrive"); if(!f) return;
  f.onsubmit=async e=>{
    e.preventDefault(); const fd=new FormData(f);
    const isAdmin=actorRole()==="market_admin";
    const body={
      stall: isAdmin?fd.get("stall"):myStall(),
      supplier:fd.get("supplier"),ticket_no:fd.get("ticket_no"),
      category:fd.get("category"),ticket_category:fd.get("ticket_category"),
      ticket_weight_g:Number(fd.get("ticket_weight_g")),
      declared_weight_g:Number(fd.get("declared_weight_g")),
      valid_from:fd.get("valid_from")+":00",valid_until:fd.get("valid_until")+":00",
      note:fd.get("note")};
    try{
      const r=await post("/api/arrive",body);
      if(r.status==="rejected") toast("到货拒收："+(r.incidents.join("，")||"核验未过"),"err");
      else if(r.incidents.length) toast("已登记，待处理："+r.incidents.join("，"),"info");
      else toast(`到货 ${r.batch_no} 验票通过，待放行入柜`);
      route();
    }catch(ex){errToast(ex);}
  };
  loadIncidents();
}
async function loadIncidents(){
  const box=$("#incidentList"); if(!box) return;
  const list=await api("/api/incidents");
  const KIND={duplicate_ticket:"票号重复",ticket_mismatch:"票货不符",weight_discrepancy:"称重差异",
    mixed_batch:"跨批次混放",overtemp:"冷藏超温",label_lost:"标签脱落",abnormal_waste:"废弃重量异常",
    inspection_fail:"抽查不合格",ticket_revoked:"检疫票撤销"};
  if(!list.length){box.innerHTML=`<span class="muted">暂无异常</span>`;return;}
  box.innerHTML=list.slice(0,30).map(i=>`
    <div style="border:1px solid var(--line);border-radius:9px;padding:9px 11px;margin-bottom:8px;${i.handled?"opacity:.55":""}">
      <div style="display:flex;gap:8px;align-items:center">
        ${chip(KIND[i.kind]||i.kind, i.handled?"gray":"red")}
        <span class="tag">${esc(i.stall_code||"")}</span>
        ${i.handled?chip("已处理","green"):chip("待处理","amber")}
      </div>
      <div style="font-size:12.5px;margin-top:5px">${esc(i.detail)}</div>
      ${i.resolution?`<div class="muted" style="font-size:12px">处理：${esc(i.resolution)}</div>`:""}
      ${(!i.handled && actorRole()==="market_admin")?`
        <div class="oprow">
          ${i.kind==="weight_discrepancy"?`
            <button class="btn sm green" data-acc="1" data-id="${i.id}">确认过磅放行</button>
            <button class="btn sm red" data-acc="0" data-id="${i.id}">拒收</button>`:`
            <button class="btn sm" data-resolve="${i.id}">标记已处置</button>`}
        </div>`:""}
    </div>`).join("");
  $$("#incidentList [data-acc]").forEach(b=>b.onclick=async()=>{
    const inc=list.find(x=>x.id==b.dataset.id);
    const m=(b.dataset.acc==="1")?
      (inc.detail.match(/批次\s*(\S+)/)||[])[1]:null;
    if(!m){toast("未定位批次","err");return;}
    try{ await post("/api/discrepancy/resolve",{batch_no:m,accept:b.dataset.acc==="1",note:"管理员核验"});
      toast("已处理");route();}catch(e){errToast(e);}
  });
  $$("#incidentList [data-resolve]").forEach(b=>b.onclick=async()=>{
    try{await post("/api/incident/resolve",{id:Number(b.dataset.resolve),resolution:"管理员处置完成"});
      toast("已处置");route();}catch(e){errToast(e);}
  });
}

/* =============================================================== 批次库存 */
async function viewBatches(){
  const rows=await api("/api/batches"+(actorRole()==="market_admin"?"":"?stall="+myStall()));
  return `
  <h1 class="page-title">批次库存</h1><p class="page-sub">到货批次以检疫票缩略条配重量显示；重量链路可逐批核算守恒。</p>
  <div id="batchList">${rows.length?rows.map(batchCard).join(""):`<div class="panel muted">暂无批次</div>`}</div>`;
}
function batchCard(b){
  const bs=BATCH_STATUS[b.inspect_status]||["","gray"];
  const frozen=b.frozen?chip("已冻结","red"):"";
  return `<div class="batch-card ${b.inspect_status} ${b.frozen?"frozen":""}">
    ${ticketStrip({ticket_no:b.ticket_no,ticket_status:b.ticket_status,supplier_code:b.supplier_code,
      category:b.category,valid_until:b.valid_until,ticket_image:b.ticket_image,weight_g:b.ticket_weight_g})}
    <div class="batch-meta">
      <span>批次号 <b class="mono">${esc(b.batch_no)}</b></span>
      <span>摊位 <b>${esc(b.stall_code)} ${esc(b.stall_name)}</b></span>
      <span>到货 <b>${kg(b.declared_weight_g)}</b></span>
      <span>批次余量 <b style="color:var(--blue)">${kg(b.remaining_g)}</b></span>
      <span>柜位 <b>${b.cabinet_code?esc(b.cabinet_code+"-"+b.shelf+b.slot_code):"—"}</b></span>
      <span>${chip(bs[0],bs[1])} ${frozen}</span>
    </div>
    ${b.inspect_note?`<div class="muted" style="font-size:12.5px;margin-top:6px">${esc(b.inspect_note)}</div>`:""}
    ${freezeReason(b.freeze_reason)}
    <details class="ops"><summary>批次操作 · 守恒核算</summary><div class="oprow" data-batch="${esc(b.batch_no)}">
      <button class="btn sm" data-act="conserve">重量守恒核算</button>
      ${b.inspect_status==="pending"&&b.ticket_status==="active"?`<button class="btn sm green" data-act="release">放行入柜</button>`:""}
      ${b.location_id?`<button class="btn sm ghost" data-act="vacate">腾出柜位</button>`:""}
      <span class="conserve-out"></span>
    </div></details>
  </div>`;
}
function bindBatches(){
  $$("#batchList [data-act]").forEach(btn=>btn.onclick=async()=>{
    const row=btn.closest(".oprow"); const bno=row.dataset.batch;
    if(btn.dataset.act==="conserve"){
      try{const r=await api("/api/conservation?batch="+bno);
        const bk=r.buckets;
        row.querySelector(".conserve-out").innerHTML=
          chip(r.conserved?"守恒 ✓":"不守恒 ✗",r.conserved?"green":"red")+
          `<span class="tag">销售 ${kg(bk.sold_g)}</span>`+
          `<span class="tag">批废 ${kg(bk.waste_batch_g)}</span>`+
          `<span class="tag">分割损耗 ${kg(bk.waste_cut_g)}</span>`+
          `<span class="tag">托废 ${kg(bk.waste_tray_g)}</span>`+
          `<span class="tag">余废 ${kg(bk.waste_remainder_g)}</span>`+
          `<span class="tag">托存 ${kg(bk.held_on_trays_g)}</span>`+
          `<span class="tag">余料存 ${kg(bk.held_remainders_g)}</span>`+
          `<span class="tag">未分割 ${kg(bk.uncut_on_batch_g)}</span>`+
          `<span class="tag">合计 ${kg(r.chain_total_g)}/${kg(r.declared_g)}</span>`;
      }catch(e){errToast(e);}
      return;
    }
    if(btn.dataset.act==="release"){
      const out=row.querySelector(".conserve-out");
      if(out.dataset.on)return; out.dataset.on="1";
      try{
        const cabs=await api("/api/cabinets");
        const free=[];
        cabs.forEach(c=>c.locations.forEach(l=>{ if(!l.occupied) free.push({cabinet:c.code,shelf:l.shelf,slot:l.slot}); }));
        if(!free.length){out.textContent="无空格位";return;}
        out.innerHTML=`<select class="locsel" style="width:auto;padding:4px 8px">${
          free.map(f=>`<option value="${f.cabinet}|${f.shelf}|${f.slot}">${f.cabinet}-${f.shelf}${f.slot}</option>`).join("")
        }</select> <button class="btn sm green confirm">确认入柜</button>`;
        out.querySelector(".confirm").onclick=async()=>{
          const [cabinet,shelf,slot]=out.querySelector(".locsel").value.split("|");
          try{await post("/api/release",{batch_no:bno,cabinet,shelf,slot});toast("已放行入柜");route();}
          catch(e){errToast(e);out.dataset.on="";}
        };
      }catch(e){errToast(e);out.dataset.on="";}
      return;
    }
    if(btn.dataset.act==="vacate"){
      try{await post("/api/vacate",{batch_no:bno});toast("已腾出柜位");route();}catch(e){errToast(e);}
    }
  });
}

/* =============================================================== 冷藏格位 */
async function viewCabinets(){
  const cabs=await api("/api/cabinets");
  return `<h1 class="page-title">冷藏格位</h1>
  <p class="page-sub">一个格位同时只放一个未结批次，跨批次混放入柜即拒绝并记异常；最新读数超上限触发整链冻结。</p>
  <div class="grid2">
    ${cabs.map(c=>{
      const over=c.locations.some(l=>l.latest_temp && l.latest_temp.temp_c>c.max_temp_c);
      return `<div class="cab">
        <div class="cab-head">
          <div><b>${esc(c.code)}</b> ${esc(c.name)}</div>
          <div class="temp" style="color:${over?"var(--red)":"var(--green)"}">
            ${c.locations[0]?.latest_temp?c.locations[0].latest_temp.temp_c.toFixed(1)+"℃":"—"}
            <span class="muted" style="font-weight:400">/ 上限 ${c.max_temp_c}℃ ${over?"⚠超温":""}</span>
          </div>
        </div>
        <div class="cab-body">
          ${c.locations.map(l=>`
            <div class="slot ${l.occupied?"busy":"empty"}">
              <div class="sl">${esc(l.shelf)}${esc(l.slot)}</div>
              ${l.occupied?`<div class="mono" style="margin-top:3px">${esc(l.occupied.batch_no)}</div>
                <div class="muted">${esc(l.occupied.stall_code)}</div>`:`<div class="muted">空格位</div>`}
            </div>`).join("")}
        </div>
        <div class="cab-head" style="background:#fff;border-top:1px solid var(--line)">
          <form class="oprow" data-cab="${esc(c.code)}" style="margin:0">
            <input type="number" step="0.1" placeholder="温度℃" name="t" style="width:90px" required>
            <button class="btn sm">上报温度</button>
            <span class="tag">管理员可制造超温回执</span>
          </form>
        </div>
      </div>`;
    }).join("")}
  </div>
  <div class="legend"><span style="color:var(--blue)">蓝=占用</span><span style="color:var(--muted)">虚线=空位</span><span style="color:var(--red)">红=超温</span></div>`;
}
function bindCabinets(){
  $$("#view form[data-cab]").forEach(f=>f.onsubmit=async e=>{
    e.preventDefault();
    try{await post("/api/temp",{cabinet:f.dataset.cab,temp_c:Number(f.t.value)});
      toast("温度已上报（可点右上角“立即扫描”触发超温冻结）","info");f.reset();}
    catch(ex){errToast(ex);}
  });
}

/* =============================================================== 分割称重 */
async function viewCut(){
  const stall=actorRole()==="market_admin"?"":myStall();
  const batches=await api("/api/batches"+(stall?"?"+qs({stall}):""));
  const rel=batches.filter(b=>b.inspect_status==="released"&&!b.frozen);
  const opt=rel.map(b=>`<option value="${esc(b.batch_no)}">${esc(b.batch_no)} · ${esc(b.category)} · 余量 ${kg(b.remaining_g)}</option>`).join("");
  return `<h1 class="page-title">分割称重</h1>
  <p class="page-sub">从原批次扣减重量（模拟秤回执），同重量不可重复分割；并发提交以批次余量为准。投入 = 托盘合计 + 分割损耗。</p>
  <div class="grid2">
    <div class="panel">
      <h2><span class="bar"></span>登记分割</h2>
      <form id="fCut">
        <div class="field"><label>来源批次（已放行）</label><select name="batch_no">${opt||'<option value="">无可分割批次</option>'}</select></div>
        <div class="row">
          <div class="field"><label>分割投入 (g)</label><input name="weight_g" type="number" value="3000"></div>
          <div class="field"><label>分割损耗 (g，入废弃)</label><input name="loss_g" type="number" value="0"></div>
        </div>
        <div id="trayRows"></div>
        <div class="oprow"><button type="button" class="btn sm ghost" id="addTray">+ 托盘</button>
          <span class="tag">托盘合计须 = 投入 − 损耗</span></div>
        <div style="margin-top:12px"><button class="btn green">提交分割（取批次写锁）</button></div>
      </form>
    </div>
    <div class="panel">
      <h2><span class="bar"></span>来源批次 → 托盘 流向图</h2>
      <div class="field"><label>选择批次查看流向</label>
        <select id="flowBatch">${batches.map(b=>`<option value="${esc(b.batch_no)}">${esc(b.batch_no)}</option>`).join("")}</select>
      </div>
      <div id="flowBox" class="muted">选择批次后生成</div>
    </div>
  </div>`;
}
function trayRow(no, product="", weight=""){
  return `<div class="row tray-line" style="margin-bottom:8px">
    <input placeholder="品名，如 鸡腿分装" value="${esc(product)}" class="tp" style="flex:2">
    <input placeholder="重量g" type="number" value="${weight}" class="tw">
    <button type="button" class="btn sm red" style="flex:0 0 auto">删</button></div>`;
}
function bindCut(){
  const rows=$("#trayRows");
  function add(p,w){const d=document.createElement("div");d.innerHTML=trayRow(0,p,w);rows.appendChild(d.firstElementChild);
    rows.lastElementChild.querySelector("button").onclick=()=>rows.lastElementChild.remove();}
  add("鸡腿分装",1500);add("鸡胸分装",1500);
  $("#addTray").onclick=()=>add();
  $$("#trayRows .tray-line button").forEach(b=>b.onclick=()=>b.closest(".tray-line").remove());
  $("#fCut").onsubmit=async e=>{
    e.preventDefault();const fd=new FormData($("#fCut"));
    const trays=$$("#trayRows .tray-line").map(l=>({product:$(".tp",l).value,weight_g:Number($(".tw",l).value)}));
    try{
      const r=await post("/api/cut",{batch_no:fd.get("batch_no"),weight_g:Number(fd.get("weight_g")),
        loss_g:Number(fd.get("loss_g")),trays,operator:state.actor});
      toast(`分割 ${r.cut_no} 生成 ${r.trays.length} 个托盘，批次余量 ${kg(r.batch_remaining_g)}`);
      route();
    }catch(ex){errToast(ex);}
  };
  $("#flowBatch").onchange=drawFlow; drawFlow();
}
async function drawFlow(){
  const bno=$("#flowBatch").value; const box=$("#flowBox"); if(!bno)return;
  const [trays,rems] = await Promise.all([
    api("/api/trays?"+qs({batch:bno})), api("/api/remainders?"+qs({batch:bno}))]);
  // 列：批次 | 分割/托盘 | 销售/余料 | 再托
  const W=900,H=Math.max(260, 90+trays.length*56+rems.length*30);
  let svg=`<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">
    <defs><marker id="arrow" markerWidth="9" markerHeight="9" refX="8" refY="4" orient="auto">
      <path d="M0,0 L9,4 L0,8 z" fill="#1565c0"/></marker></defs>`;
  const edge=(x1,y1,x2,y2,rem)=>`<path class="flow-edge ${rem?"rem":""}" d="M${x1},${y1} C${(x1+x2)/2},${y1} ${(x1+x2)/2},${y2} ${x2-8},${y2}"/>`;
  // batch node
  const by=H/2;
  svg+=`<rect class="node-rect" x="20" y="${by-34}" width="150" height="68" fill="#e7f4e8" stroke="#2e7d32"/>
    <text x="95" y="${by-10}" text-anchor="middle" font-size="12" font-weight="700" fill="#2e7d32">到货批次</text>
    <text x="95" y="${by+8}" text-anchor="middle" font-size="11" fill="#1f2d3d">${esc(bno)}</text>
    <text x="95" y="${by+24}" text-anchor="middle" font-size="10" fill="#6b7a8d">检疫票链源头</text>`;
  trays.forEach((t,i)=>{
    const y=46+i*56;
    const frozen=t.frozen, col=frozen?"#fdecea":"#e6f0fb", stk=frozen?"#c62828":"#1565c0";
    svg+=edge(170,by,300,y+18);
    svg+=`<rect class="node-rect" x="300" y="${y}" width="180" height="40" fill="${col}" stroke="${stk}"/>
      <text x="312" y="${y+16}" font-size="11" font-weight="700" fill="${stk}">${esc(t.tray_no)}</text>
      <text x="312" y="${y+31}" font-size="10" fill="#6b7a8d">${esc(t.product_name)} · ${kg(t.weight_g)}${frozen?" · ❄冻结":""}</text>`;
    const sold=t.weight_g-t.remaining_g;
    if(sold>0){
      svg+=edge(480,y+20,600,y+8);
      svg+=`<rect class="node-rect" x="600" y="${y-4}" width="120" height="26" fill="#e7f4e8" stroke="#2e7d32"/>
        <text x="660" y="${y+13}" text-anchor="middle" font-size="10.5" fill="#2e7d32">销售 ${kg(sold)}</text>`;
    }
  });
  rems.forEach((r,i)=>{
    const y=46+i*42+trays.length*56+10;
    svg+=edge(170,by,600,y+14,true);
    svg+=`<rect class="node-rect" x="600" y="${y}" width="130" height="30" fill="#fdf2dd" stroke="#b26a00"/>
      <text x="665" y="${y+19}" text-anchor="middle" font-size="10.5" fill="#b26a00">余料 ${esc(r.remainder_no)} ${kg(r.weight_g)}</text>`;
    (r.child_tray_nos||[]).forEach((cn,j)=>{
      svg+=edge(730,y+15,772,y+40+j*22,true);
      svg+=`<text x="776" y="${y+44+j*22}" font-size="10" fill="#b26a00">↳ 再托 ${esc(cn)}</text>`;
    });
  });
  svg+=`</svg>`;
  box.innerHTML=`<div class="flow">${svg}</div>
    <div class="legend"><span style="color:var(--green)">绿=批次/销售</span><span style="color:var(--blue)">蓝=分割托盘</span><span style="color:var(--amber)">黄虚线=余料/再托</span><span style="color:var(--red)">红=冻结</span></div>`;
}

/* =============================================================== 托盘标签 */
async function viewLabels(){
  const stall=actorRole()==="market_admin"?"":myStall();
  const trays=await api("/api/trays"+(stall?"?"+qs({stall}):""));
  return `<h1 class="page-title">托盘标签</h1>
  <p class="page-sub">标签沿用批次链（批次号/检疫票）；冻结托盘红色标注并始终附文字原因；标签脱落暂停销售待管理员重贴。</p>
  <div class="label-grid">${trays.map(trayLabel).join("")||'<div class="panel muted">暂无托盘</div>'}</div>`;
}
function trayLabel(t){
  const st=TRAY_STATUS[t.status]||["","gray"];
  return `<div class="tray-label ${t.frozen?"frozen":""}">
    ${!t.label_intact?'<div class="label-lost">标签脱落</div>':""}
    <div class="head"><span class="no">${esc(t.tray_no)}</span>
      <span style="font-size:11px">${esc(t.stall_code)}</span></div>
    <div class="bd">
      <div class="prod">${esc(t.product_name)}</div>
      <div class="wt">${kg(t.weight_g)}</div>
      <div style="margin-top:4px">
        ${t.frozen?chip("冻结中","red"):chip(st[0],st[1])}
        ${t.recalled?chip("召回","red"):""}
        ${!t.label_intact?chip("标签脱落","amber"):""}
      </div>
      <div class="meta">
        来源批次 <span class="mono">${esc(t.batch_no)}</span><br>
        检疫票 <span class="mono">${esc(t.ticket_no)}</span><br>
        可售结存 ${kg(t.remaining_g)} · 产出 ${t.produced_at.replace("T"," ")}
      </div>
      <div class="barcode"></div>
      ${freezeReason(t.freeze_reason)}
      <div class="oprow">
        ${t.frozen?"":`<button class="btn sm" data-op="sell" data-no="${esc(t.tray_no)}">销售</button>`}
        ${t.frozen||t.remaining_g<=0?"":`<button class="btn sm ghost" data-op="rec" data-no="${esc(t.tray_no)}">余料回收</button>`}
        ${t.frozen||t.status==="waste"?"":`<button class="btn sm ghost" data-op="waste" data-no="${esc(t.tray_no)}">废弃</button>`}
        ${t.label_intact&&!t.frozen?`<button class="btn sm ghost" data-op="lost" data-no="${esc(t.tray_no)}">报标签脱落</button>`
          :(!t.label_intact&&actorRole()==="market_admin"?`<button class="btn sm green" data-op="relabel" data-no="${esc(t.tray_no)}">核验重贴</button>`:"")}
      </div>
    </div>
  </div>`;
}
function bindLabels(){
  $$("#view [data-op]").forEach(b=>b.onclick=async()=>{
    const no=b.dataset.no, op=b.dataset.op;
    try{
      if(op==="sell"){const w=Number(prompt("销售重量(g)，不得超过可售结存：","1000"));if(!w)return;
        await post("/api/sell",{tray_no:no,weight_g:w});toast("已销售");}
      else if(op==="rec"){await post("/api/remainder/recover",{tray_no:no});toast("已回收为余料，批次链不变");}
      else if(op==="waste"){const w=Number(prompt("废弃重量(g)：","100"));const reason=prompt("废弃原因：","变质");if(!w||!reason)return;
        await post("/api/waste",{source_type:"tray",ref_no:no,weight_g:w,reason});toast("已记废弃台账（异常比例会标记）");}
      else if(op==="lost"){await post("/api/label/lost",{tray_no:no,note:"摊主现场报告"});toast("已报标签脱落，暂停销售");}
      else if(op==="relabel"){await post("/api/label/relabel",{tray_no:no,note:"现场核验批次链一致"});toast("已重贴标签");}
      route();
    }catch(e){errToast(e);}
  });
}

/* =============================================================== 抽查冻结 */
async function viewInspection(){
  const admin=actorRole()==="market_admin";
  const [batches,trays,events]=await Promise.all([
    api("/api/batches"+(admin?"":"?stall="+myStall())),
    api("/api/trays"+(admin?"":"?stall="+myStall())),
    api("/api/freeze-events")]);
  const bopt=batches.map(b=>`<option>${esc(b.batch_no)}</option>`).join("");
  const topt=trays.map(t=>`<option>${esc(t.tray_no)}</option>`).join("");
  const ticketOpt=[...new Set(batches.map(b=>b.ticket_no))].map(x=>`<option>${esc(x)}</option>`).join("");
  return `<h1 class="page-title">抽查 · 冻结 · 解封</h1>
  <p class="page-sub">市场管理员执行抽查、检疫票撤销、召回与解封；摊主只有查看权。抽查不合格/撤销即时冻结相关未售托盘，解封需根因消除。</p>
  ${admin?`<div class="grid2">
    <div class="panel"><h2><span class="bar"></span>市场抽查</h2>
      <form id="fInsp">
        <div class="row"><div class="field"><label>对象类型</label><select name="target_type"><option value="batch">整批</option><option value="tray">单托盘</option></select></div>
        <div class="field"><label>编号</label><input name="ref_no" list="dlBatch" value=""></div></div>
        <datalist id="dlBatch">${bopt}${topt}</datalist>
        <div class="row"><div class="field"><label>结果</label><select name="result"><option value="pass">合格</option><option value="fail">不合格→冻结</option></select></div></div>
        <div class="field"><label>说明</label><input name="note" placeholder="如：抽检药残超标 / 感官正常"></div>
        <button class="btn green">提交抽查</button>
      </form>
      <h2 style="margin-top:18px"><span class="bar"></span>检疫票撤销</h2>
      <form id="fRevoke">
        <div class="field"><label>检疫票编号</label><input name="ticket_no" list="dlTicket"></div>
        <datalist id="dlTicket">${ticketOpt}</datalist>
        <div class="field"><label>撤销原因</label><input name="reason" placeholder="如：产地通报复检阳性（虚构）"></div>
        <button class="btn red">撤销并冻结整票链</button>
      </form>
    </div>
    <div class="panel"><h2><span class="bar"></span>解冻（需根因消除）</h2>
      <form id="fUnfreeze">
        <div class="row"><div class="field"><label>类型</label><select name="target_type"><option value="batch">批次(级联)</option><option value="tray">托盘</option><option value="remainder">余料</option></select></div>
        <div class="field"><label>编号</label><input name="ref_no"></div></div>
        <div class="field"><label>解封说明</label><input name="note" placeholder="复检合格 / 召回撤回 / 温度恢复"></div>
        <button class="btn">执行解封</button>
      </form>
      <h2 style="margin-top:18px"><span class="bar"></span>部分召回</h2>
      <form id="fPartial">
        <div class="field"><label>托盘号（逗号分隔，仅未售托盘）</label><input name="tray_nos" placeholder="T-A101-0002,T-A101-0003"></div>
        <div class="field"><label>召回原因</label><input name="reason" placeholder="如：客户投诉局部异味（虚构）"></div>
        <button class="btn red">发起部分召回并冻结</button>
      </form>
    </div>
  </div>`:`<div class="panel muted">摊主视图：以下为本摊冻结/解封留痕，冻结与解封由市场管理员执行。</div>`}
  <div class="panel"><h2><span class="bar"></span>冻结 / 解封留痕（冻结状态均附文字原因）</h2>
    <table><thead><tr><th>时间</th><th>对象</th><th>动作</th><th>操作人</th><th>原因 / 说明</th></tr></thead><tbody>
    ${events.map(e=>`<tr><td class="mono">${esc(e.acted_at.replace("T"," "))}</td>
      <td>${chip(e.target_type,"blue")} #${e.target_id}</td>
      <td>${e.action==="freeze"?chip("冻结","red"):chip("解封","green")}</td>
      <td>${esc(e.acted_by)}</td><td>${esc(e.reason)}</td></tr>`).join("")}
    </tbody></table></div>`;
}
function bindInspection(){
  const admin=actorRole()==="market_admin";
  if(!admin)return;
  $("#fInsp").onsubmit=async e=>{e.preventDefault();const fd=new FormData(e.target);
    try{await post("/api/inspect",Object.fromEntries(fd));toast("抽查已提交");route();}catch(x){errToast(x);}};
  $("#fRevoke").onsubmit=async e=>{e.preventDefault();const fd=new FormData(e.target);
    try{const r=await post("/api/ticket/revoke",{ticket_no:fd.get("ticket_no"),reason:fd.get("reason")});
      toast(`已撤销，冻结 ${r.frozen_targets} 个链上目标`);route();}catch(x){errToast(x);}};
  $("#fUnfreeze").onsubmit=async e=>{e.preventDefault();const fd=new FormData(e.target);
    try{const r=await post("/api/unfreeze",{target_type:fd.get("target_type"),ref_no:fd.get("ref_no"),
      note:fd.get("note"),cascade:true});toast("已解封："+r.unfrozen.join(", "));route();}catch(x){errToast(x);}};
  $("#fPartial").onsubmit=async e=>{e.preventDefault();const fd=new FormData(e.target);
    const tray_nos=String(fd.get("tray_nos")).split(",").map(s=>s.trim()).filter(Boolean);
    try{const r=await post("/api/recall",{scope:"partial",tray_nos,reason:fd.get("reason")});
      toast(`部分召回 ${r.recall_no}：${r.items} 项，冻结 ${r.frozen}`);route();}catch(x){errToast(x);}};
}

/* =============================================================== 召回清单 */
async function viewRecalls(){
  const rows=await api("/api/recalls");
  return `<h1 class="page-title">召回清单</h1>
  <p class="page-sub">按摊位、托盘号排序；标注在售/冻结/已售/已废与冻结文字原因。仅市场管理员可撤回召回。</p>
  <div class="panel" style="overflow-x:auto"><table>
    <thead><tr><th>摊位</th><th>托盘号</th><th>产品/重量</th><th>来源批次</th><th>召回单</th><th>范围</th><th>状态</th><th>原因 / 冻结说明</th><th></th></tr></thead>
    <tbody>${rows.map(r=>`<tr>
      <td>${esc(r.stall_code)}<br><span class="muted">${esc(r.stall_name)}</span></td>
      <td class="mono"><b>${esc(r.tray_no)}</b></td>
      <td>${kg(r.weight_g)}<br><span class="muted">已售 ${kg(r.sold_g)}</span></td>
      <td class="mono">${esc(r.batch_no)}</td>
      <td class="mono">${esc(r.recall_no)}</td>
      <td>${chip({batch:"整批",partial:"部分",ticket:"整票"}[r.scope],"blue")}</td>
      <td>${r.active?chip("生效中","red"):chip("已撤回","gray")}
          ${r.frozen?chip("冻结","red"):chip("未冻结","green")}
          ${chip({on_sale:"在售",sold:"售罄",remainder:"余料",waste:"已废"}[r.tray_status]||r.tray_status,"gray")}</td>
      <td style="max-width:300px">${esc(r.reason)}${freezeReason(r.freeze_reason)}</td>
      <td>${r.active&&actorRole()==="market_admin"?`<button class="btn sm ghost" data-lift="${esc(r.recall_no)}">撤回召回</button>`:""}</td>
    </tr>`).join("")||'<tr><td colspan="9" class="muted">暂无召回</td></tr>'}</tbody></table></div>`;
}
function bindRecalls(){
  $$("#view [data-lift]").forEach(b=>b.onclick=async()=>{
    try{await post("/api/recall/lift",{recall_no:b.dataset.lift,note:"管理员撤回，之后按根因逐目标解封"});
      toast("召回已撤回（仍需逐目标解封）");route();}catch(e){errToast(e);}});
}

/* =============================================================== 废弃物台账 */
async function viewWaste(){
  const stall=actorRole()==="market_admin"?"":myStall();
  const rows=await api("/api/waste"+(stall?"?"+qs({stall}):""));
  const total=rows.reduce((a,r)=>a+r.weight_g,0);
  const abn=rows.filter(r=>r.abnormal).length;
  return `<h1 class="page-title">废弃物台账</h1>
  <p class="page-sub">废弃重量按批次/托盘/余料各自余量校验，不可超量；单次/累计超阈值自动标记“废弃重量异常”。</p>
  <div class="cards">
    <div class="stat amber"><div class="n">${rows.length}</div><div class="l">废弃记录</div></div>
    <div class="stat blue"><div class="n">${kg(total)}</div><div class="l">废弃合计</div></div>
    <div class="stat red"><div class="n">${abn}</div><div class="l">重量异常待核查</div></div>
  </div>
  <div class="panel" style="overflow-x:auto"><table>
    <thead><tr><th>单号</th><th>时间</th><th>摊位</th><th>来源</th><th>重量</th><th>原因</th><th>登记人</th><th>异常</th></tr></thead>
    <tbody>${rows.map(w=>`<tr>
      <td class="mono">${esc(w.waste_no)}</td>
      <td class="mono">${esc(w.logged_at.replace("T"," "))}</td>
      <td>${esc(w.stall_code)}</td>
      <td class="mono">${w.batch_no?esc(w.batch_no):w.tray_no?esc(w.tray_no):esc(w.remainder_no)}
        <span class="tag">${ {batch:"批次",tray:"托盘",remainder:"余料",cut:"分割损耗"}[w.source_type]}</span></td>
      <td><b>${kg(w.weight_g)}</b></td>
      <td>${esc(w.reason)}</td>
      <td>${esc(w.logged_by)}</td>
      <td>${w.abnormal?chip("重量异常","red"):chip("正常","green")}
        ${w.abnormal_note?`<div class="muted" style="font-size:11.5px;max-width:280px">${esc(w.abnormal_note)}</div>`:""}</td>
    </tr>`).join("")||'<tr><td colspan="8" class="muted">暂无废弃记录</td></tr>'}</tbody></table></div>`;
}

/* --------------------------------------------------------------- 角色辅助 */
function actorRole(){return (state.meta.users.find(u=>u.username===state.actor)||{}).role||"stall_owner";}
function myStall(){const u=state.meta.users.find(x=>x.username===state.actor);
  const s=state.meta.stalls.find(x=>x.owner_user_id===u?.id);return s?s.code:"";}

/* --------------------------------------------------------------- 路由 */
const RENDER={arrival:[viewArrival,bindArrival],batches:[viewBatches,bindBatches],
  cabinets:[viewCabinets,bindCabinets],cut:[viewCut,bindCut],labels:[viewLabels,bindLabels],
  inspection:[viewInspection,bindInspection],recalls:[viewRecalls,bindRecalls],
  waste:[viewWaste,()=>{}]};
async function route(){
  const key=location.hash.replace("#","")||"arrival";
  renderNav();
  const [view,bind]=RENDENT(key);
  $("#view").innerHTML=`<div class="muted">加载中…</div>`;
  try{$("#view").innerHTML=await view();bind();}
  catch(e){$("#view").innerHTML=`<div class="panel">加载失败：${esc(e.message)}</div>`;}
}
function RENDENT(k){return RENDER[k]||RENDER.arrival;}

async function init(){
  state.meta=await api("/api/meta");
  const sel=$("#actorSel");
  sel.innerHTML=state.meta.users.map(u=>`<option value="${esc(u.username)}">${esc(u.display_name)}（${u.role==="market_admin"?"管理员":"摊主"}）</option>`).join("");
  sel.value=state.actor;
  sel.onchange=()=>{state.actor=sel.value;$("#roleChip").textContent=actorRole()==="market_admin"?"市场管理员":"摊主";route();};
  $("#roleChip").textContent=actorRole()==="market_admin"?"市场管理员":"摊主";
  $("#scanBtn").onclick=async()=>{try{const r=await post("/api/scan",{});
    const n=(r.ticket_expiry?.length||0)+(r.overtemp?.length||0);
    toast(n?`扫描完成：到期 ${r.ticket_expiry?.length||0}，超温 ${r.overtemp?.length||0}`:"扫描完成：无新冻结","info");
    route();}catch(e){errToast(e);}};
  window.addEventListener("hashchange",route);
  if(!location.hash)location.hash="arrival"; else route();
  // 轻量轮询（进程内任务也在后台周期扫描）
  state.timer=setInterval(()=>{const k=location.hash.replace("#","");if(["batches","cabinets","inspection","recalls","labels"].includes(k))route();},15000);
}
init();
