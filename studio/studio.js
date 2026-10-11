"use strict";
const $ = id => document.getElementById(id);
const labels = {unreviewed:"未审", pass:"通过", manual_review:"待复核", quarantine:"垃圾篓"};
const state = {
  view:"review", results:[], visible:[], selectedId:"", filter:"all", series:"", search:"",
  settings:{}, characters:[], jobs:[], drafts:{}, presets:{}, mode:"redo", compare:"inspiration",
  inspire:[], inspireTotal:0, picked:null, submitting:false, reviewing:false, preparing:false, initialized:false,
};
const storageKey = "fanart.creator-drafts.v2";
const saveTimers = new Map();
const saveChains = new Map();
let toastTimer, resultsSignature = "", prepareSequence = 0, inspireSequence = 0;
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fileUrl = (path, thumb = false) => (thumb ? "/api/thumb?path=" : "/api/file?path=") + encodeURIComponent(path);
const current = () => state.results.find(it => it.result_id === state.selectedId);
const draftKey = () => state.selectedId + "::" + state.mode;
function toast(text) { $("toast").textContent = text; $("toast").hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $("toast").hidden = true, 4500); }
function error(text) { $("appError").textContent = text; $("appError").hidden = !text; }
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok || !data.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}
function backupDrafts() {
  try { localStorage.setItem(storageKey, JSON.stringify(state.drafts)); }
  catch { error("浏览器草稿空间不足；请保持页面打开，工作台仍会尝试保存到本机。"); }
}
function saveDraft(key, draft, statusId = "draftStatus") {
  state.drafts[key] = {...draft, updated_at:new Date().toISOString()};
  backupDrafts();
  $(statusId).textContent = "正在保存…";
  clearTimeout(saveTimers.get(key));
  saveTimers.set(key, setTimeout(() => {
    const value = structuredClone(state.drafts[key]);
    if (!value) return;
    const chain = (saveChains.get(key) || Promise.resolve()).catch(() => {}).then(() => api("/api/draft/save", {key, draft:value}));
    saveChains.set(key, chain);
    chain.then(() => { if (key === "__create__" || key === draftKey()) $(statusId).textContent = "草稿已保存"; })
      .catch(() => { $(statusId).textContent = "本机保存失败，浏览器已保留"; });
  }, 500));
}
async function removeDraft(key) {
  clearTimeout(saveTimers.get(key));
  delete state.drafts[key]; backupDrafts();
  const chain = (saveChains.get(key) || Promise.resolve()).catch(() => {}).then(() => api("/api/draft/save", {key, draft:null}));
  saveChains.set(key, chain);
  await chain;
}
function options(select, rows, value) {
  select.replaceChildren(...rows.map(([v, text]) => new Option(text, v)));
  if (value && [...select.options].some(o => o.value === value)) select.value = value;
}
function parameterOptions(prefix, values) {
  const models = [...new Set([...(state.settings.models || []), values.model].filter(Boolean))];
  options($(prefix + "Model"), models.map(m => [m,m]), values.model || state.settings.model);
  const sizes = Object.entries(state.settings.size_presets || {auto:"auto", "2:3":"1024x1536"});
  const rows = sizes.map(([ratio, size]) => [size, ratio === "auto" ? "auto · 自动比例" : `${ratio} · ${size}`]);
  if (values.size && !rows.some(([v]) => v === values.size)) rows.push([values.size, values.size]);
  options($(prefix + "Size"), rows, values.size || state.settings.size || "auto");
  $(prefix + "Quality").value = values.quality || "high";
}
function setView(view) {
  state.view = view;
  for (const key of ["review", "create", "jobs"]) $(key + "View").hidden = key !== view;
  document.querySelectorAll("[data-view]").forEach(el => {
    el.classList.toggle("active", el.dataset.view === view);
    el.setAttribute("aria-pressed", String(el.dataset.view === view));
  });
  if (view === "create" && !state.inspire.length) loadInspire().catch(e => error(e.message));
}
function updateFiltered(keepSelection = true) {
  const query = state.search.toLowerCase().trim();
  state.visible = state.results.filter(it => (!state.series || it.series === state.series) &&
    (state.filter === "all" || it.review.status === state.filter) &&
    (!query || [it.id, it.character, it.series, it.review.note].join(" ").toLowerCase().includes(query)));
  if (!keepSelection || !state.visible.some(it => it.result_id === state.selectedId)) {
    state.selectedId = (state.visible.find(it => it.inspire) || state.visible[0])?.result_id || "";
    state.mode = "redo";
  }
  renderFilters(); renderFilm();
  $("reviewWorkspace").hidden = !state.visible.length;
  $("reviewEmpty").hidden = !!state.visible.length;
  if (state.visible.length) showCurrent();
}
function renderFilters() {
  const candidates = state.results.filter(it => !state.series || it.series === state.series);
  const counts = Object.fromEntries(Object.keys(labels).map(k => [k,candidates.filter(it => it.review.status === k).length]));
  counts.all = candidates.length;
  $("reviewFilters").innerHTML = [["all","全部"],...Object.entries(labels)].map(([key,label]) => `<button type="button" data-filter="${key}" class="${state.filter === key ? "active" : ""}" aria-pressed="${state.filter === key}">${label} <span>${counts[key]}</span></button>`).join("");
  $("reviewFilters").querySelectorAll("button").forEach(btn => btn.onclick = () => {
    state.filter = btn.dataset.filter; updateFiltered(false);
  });
}
function renderFilm() {
  $("resultTotal").textContent = `· ${state.visible.length} 张`;
  $("resultFilm").innerHTML = state.visible.map(it => `<button type="button" class="result-thumb ${it.result_id === state.selectedId ? "active" : ""}" data-rid="${esc(it.result_id)}" aria-label="查看 ${esc(it.id)}" aria-pressed="${it.result_id === state.selectedId}"><img loading="lazy" src="${fileUrl(it.path,true)}" alt="${esc(it.id)} 预览"><span class="dot ${esc(it.review.status)}"></span><span>${esc(it.id)}</span></button>`).join("");
  $("resultFilm").querySelectorAll("button").forEach(btn => btn.onclick = () => selectResult(btn.dataset.rid));
}
function selectResult(id) {
  if (id === state.selectedId) return;
  state.selectedId = id; state.mode = "redo";
  state.compare = "inspiration";
  renderFilm(); showCurrent();
  try { localStorage.setItem("fanart.selected-result", id); } catch {}
}
function move(delta) {
  const index = state.visible.findIndex(it => it.result_id === state.selectedId);
  const target = state.visible[index + delta];
  if (target) selectResult(target.result_id);
}
function badge() {
  const it = current(); if (!it) return;
  $("reviewBadge").textContent = labels[it.review.status] || "未审";
  $("reviewBadge").className = "badge " + it.review.status;
  $("undoReview").disabled = !it.review.history?.length;
  for (const [id, status] of [["markPass","pass"],["markReview","manual_review"],["markTrash","quarantine"]]) {
    $(id).setAttribute("aria-pressed", String(it.review.status === status));
    $(id).classList.toggle("active", it.review.status === status);
  }
}
function updateImages() {
  const it = current(); if (!it) return;
  const parentMode = state.compare === "parent" && it.parent_path;
  const left = parentMode ? it.parent_path : it.inspire;
  $("leftCaption").textContent = parentMode ? "上一版本 · " + it.parent_id : "灵感原图";
  $("rightCaption").textContent = "成图 · " + it.character;
  $("compareParent").disabled = !it.parent_path;
  for (const [id, on] of [["compareParent",!!parentMode],["compareInspire",!parentMode]]) {
    $(id).classList.toggle("active",on); $(id).setAttribute("aria-pressed",String(on));
  }
  $("leftMissing").hidden = !!left; $("leftImage").hidden = !left; $("leftOriginal").hidden = !left;
  if (left) { $("leftImage").src = fileUrl(left); $("leftOriginal").href = fileUrl(left); }
  else $("leftImage").removeAttribute("src");
  $("rightImage").src = fileUrl(it.path);
  $("downloadResult").href = fileUrl(it.path) + "&download=1";
  $("downloadResult").download = it.id;
  $("leftViewport").scrollTo(0,0); $("rightViewport").scrollTo(0,0);
  applyZoom();
}
function showCurrent() {
  const it = current(); if (!it) return;
  const index = state.visible.findIndex(x => x.result_id === it.result_id);
  $("workTitle").textContent = it.id;
  $("workSeries").textContent = [it.series,it.character,it.folder].join(" / ");
  $("pagePosition").textContent = `${index+1} / ${state.visible.length}`;
  $("previous").disabled = index <= 0; $("next").disabled = index >= state.visible.length-1;
  badge(); updateImages();
  const a = it.actual || {};
  const facts = [
    ["实际输出", a.width && a.height ? `${a.width} × ${a.height}` : "未知"],
    ["请求尺寸", it.size || "未记录"], ["历史质量", it.quality || "未记录"],
    ["模型", it.model || "未记录"], ["文件", (it.bytes/1048576).toFixed(2)+" MB"],
  ];
  $("resultFacts").innerHTML = facts.map(([k,v]) => `<span>${esc(k)} <strong>${esc(v)}</strong></span>`).join("");
  $("reviewNote").value = state.drafts[it.result_id + "::note"]?.note ?? it.review.note ?? "";
  renderVersions(it); loadEditor(it);
}
function renderVersions(it) {
  const ids = [it.parent_result_id,...(it.children || [])].filter(Boolean);
  $("versionLinks").innerHTML = ids.length ? '<span>关联版本</span>' + ids.map(id => {
    const other = state.results.find(x => x.result_id === id);
    return other ? `<button type="button" data-id="${esc(id)}">${id === it.parent_result_id ? "来自" : "后续"} ${esc(other.id)}</button>` : "";
  }).join("") : '<span>此版本暂无已记录的父子关系</span>';
  $("versionLinks").querySelectorAll("button").forEach(btn => btn.onclick = () => {
    state.filter="all"; state.series=""; state.search=""; $("resultSeries").value=""; $("resultSearch").value="";
    state.selectedId=btn.dataset.id; state.mode="redo"; updateFiltered();
  });
}
function baseline(it) {
  const prompt = it.mode === "refine" ? (it.prompt_preview || "") + (it.negative_preview ? "\nNegative: " + it.negative_preview : "") : it.suggested_prompt;
  return {prompt:prompt || "", model:it.model || state.settings.model,
    size:it.size || state.settings.size, quality:"high", send_inspire:!!it.inspire && it.send_inspire !== false, mode:"redo"};
}
function loadEditor(it) {
  $("editMode").value = state.mode;
  $("historyNotice").className = "notice" + (it.prompt_source === "recorded" ? "" : " unknown");
  $("historyNotice").textContent = it.prompt_source === "recorded"
    ? "这张保留了实际发送的完整提示词。下方修改只影响下一版。"
    : "这张没有完整历史提示词。下方是重建的重做草稿，不代表当时的请求。";
  if (it.task_status === "interrupted") $("historyNotice").textContent += " 任务曾中断，但发现了输出文件；请核对成图和账单。";
  if (it.mode === "refine" && state.mode === "redo") $("historyNotice").textContent += " 当前是精修产物，重新创作使用身份与灵感模板。";
  const draft = state.drafts[draftKey()] || baseline(it);
  if (!state.drafts[draftKey()]) state.drafts[draftKey()] = draft;
  $("editPrompt").value = draft.prompt || "";
  $("editSend").checked = !!draft.send_inspire; $("editSend").disabled = !it.inspire;
  parameterOptions("edit", draft);
  $("draftStatus").textContent = draft.updated_at ? "草稿已保存" : "编辑即自动保存";
  $("submitStatus").textContent = "";
  updateModeHelp(); renderReferences();
  $("regenerate").disabled = !it.char_id || state.submitting || !state.settings.has_key;
}
function readEditor() {
  return {prompt:$("editPrompt").value, model:$("editModel").value, size:$("editSize").value,
    quality:$("editQuality").value, send_inspire:$("editSend").checked, mode:state.mode};
}
function editChanged() { if (current()) saveDraft(draftKey(), readEditor()); }
function updateModeHelp() {
  $("modeHelp").textContent = state.mode === "refine"
    ? "当前成图会作为第一张底图。适合保留构图、表情，只修改指定细节。请在下方写清要改与不改的内容。"
    : "重新依据角色身份与灵感绘制。构图和细节可能变化，原版本会保留。";
}
function renderReferences() {
  const it = current(); if (!it) return;
  const character = state.characters.find(c => c.id === it.char_id);
  const refs = [];
  if (state.mode === "refine") refs.push([it.path,"当前成图 · 精修底图"]);
  if (character?.ref_path) refs.push([character.ref_path,"角色身份"]);
  if ($("editSend").checked && it.inspire) refs.push([it.inspire,"灵感 · 构图与画风"]);
  $("referenceList").innerHTML = refs.map(([path,role],i) => `<div class="reference-item"><img src="${fileUrl(path,true)}" alt="${esc(role)}参考"><span>第 ${i+1} 张 · ${esc(role)}</span></div>`).join("");
}
async function changeMode() {
  const requested = $("editMode").value;
  const previous = state.mode;
  const previousDraft = readEditor();
  state.mode = requested;
  const it = current(); if (!it) return;
  const key = draftKey(), sequence = ++prepareSequence;
  if (state.drafts[key]) { loadEditor(it); return; }
  state.preparing = true;
  $("regenerate").disabled = true; $("editPrompt").disabled = true;
  try {
    const result = await api("/api/prepare", {character:it.char_id, parent_result_id:it.result_id,
      inspire:it.inspire, send_inspire:previousDraft.send_inspire, mode:requested});
    const draft = {...previousDraft, mode:requested, prompt:result.final_prompt};
    saveDraft(key, draft);
    if (sequence === prepareSequence && key === draftKey()) loadEditor(it);
  } catch(e) {
    if (key === draftKey()) { state.mode = previous; loadEditor(it); }
    error(e.message);
  } finally {
    state.preparing = false; $("editPrompt").disabled = false;
    $("regenerate").disabled = state.submitting || !current()?.char_id || !state.settings.has_key;
  }
}
async function submitVersion() {
  const it = current(); if (!it || state.submitting || state.preparing) return;
  const draft = readEditor();
  if (!draft.prompt.trim()) return toast("先填写完整提示词");
  state.submitting = true; $("regenerate").disabled = true;
  $("submitStatus").textContent = "正在提交一张新版本…";
  const requestId = crypto.randomUUID();
  try {
    const data = await api("/api/generate", {character:it.char_id, final_prompt:draft.prompt,
      model:draft.model, size:draft.size, quality:draft.quality, mode:draft.mode,
      inspire:it.inspire, send_inspire:draft.send_inspire, parent_result_id:it.result_id,
      client_request_id:requestId});
    $("submitStatus").textContent = `已排队 ${data.job.id}。完成后会提示，不会覆盖当前作品。`;
    toast("新版本已排队，可继续审核其他作品");
    await pollState();
  } catch(e) { $("submitStatus").textContent = e.message + "；未自动重试。若连接中断，先到任务页核对。"; }
  finally { state.submitting = false; $("regenerate").disabled = !state.settings.has_key; }
}
async function review(status, undo=false, noteOnly=false) {
  const it = current(); if (!it || state.reviewing) return;
  state.reviewing = true;
  const nextId = state.visible[state.visible.findIndex(x => x.result_id === it.result_id)+1]?.result_id;
  try {
    const response = await api("/api/review", {result_id:it.result_id, status, note:$("reviewNote").value, undo});
    it.review=response.review;
    await removeDraft(it.result_id + "::note");
    resultsSignature="";
    if (current()?.result_id === it.result_id) {
      if ($("autoNext").checked && !undo && !noteOnly && nextId) { state.selectedId=nextId; state.mode="redo"; }
      updateFiltered();
    }
    toast(undo ? "已撤销上一次审核操作" : (status === "quarantine" ? "已标记垃圾篓，原文件保留" : "审核已保存"));
  } catch(e) { error(e.message); }
  finally { state.reviewing = false; }
}
function applyZoom() {
  const scale = $("zoom").value;
  for (const side of ["left", "right"]) {
    const image=$(side+"Image"), viewport=$(side+"Viewport");
    viewport.classList.toggle("zoomed",scale !== "fit");
    image.style.width=scale === "fit" ? "100%" : image.naturalWidth * Number(scale) + "px";
    image.style.height=scale === "fit" ? "100%" : image.naturalHeight * Number(scale) + "px";
  }
}
let syncing=false;
for (const [side,other] of [["left","right"],["right","left"]]) {
  const viewport=$(side+"Viewport"), target=$(other+"Viewport");
  $(side+"Image").onload=applyZoom;
  viewport.addEventListener("scroll", () => {
    if (!$("syncPan").checked || syncing || $("zoom").value === "fit") return;
    syncing=true;
    target.scrollLeft=viewport.scrollLeft/Math.max(1,viewport.scrollWidth-viewport.clientWidth)*(target.scrollWidth-target.clientWidth);
    target.scrollTop=viewport.scrollTop/Math.max(1,viewport.scrollHeight-viewport.clientHeight)*(target.scrollHeight-target.clientHeight);
    requestAnimationFrame(() => syncing=false);
  });
  let drag=null;
  viewport.addEventListener("pointerdown",e => {if ($("zoom").value !== "fit") {drag={x:e.clientX,y:e.clientY,left:viewport.scrollLeft,top:viewport.scrollTop}; viewport.setPointerCapture(e.pointerId);}});
  viewport.addEventListener("pointermove",e => {if(drag) viewport.scrollTo(drag.left+drag.x-e.clientX,drag.top+drag.y-e.clientY);});
  viewport.addEventListener("pointerup",()=>drag=null); viewport.addEventListener("pointercancel",()=>drag=null);
}
function renderPresets() {
  const currentValue=$("presetSelect").value;
  options($("presetSelect"), [["","选择模板"],...Object.keys(state.presets).map(n=>[n,n])],currentValue);
}
async function savePreset() {
  const name=$("presetName").value.trim(); if(!name) return toast("先给模板起个名字");
  if(state.presets[name] && !confirm("已有同名模板，覆盖它吗？")) return;
  try {const response=await api("/api/preset",{name,preset:readEditor()}); state.presets=response.presets; renderPresets(); toast("模板已保存到本机");}
  catch(e){error(e.message);}
}
function applyPreset() {
  const draft=state.presets[$("presetSelect").value]; if(!draft || !current()) return;
  if(!confirm("用模板替换当前提示词和参数？")) return;
  const next={...draft,mode:state.mode,send_inspire:!!current().inspire && draft.send_inspire};
  saveDraft(draftKey(),next); loadEditor(current()); toast("模板已应用，请检查参考图职责");
}
async function loadResults(initial=false) {
  const data=await api("/api/results");
  const signature=JSON.stringify(data.items);
  if(!initial && signature===resultsSignature) return;
  resultsSignature=signature; state.results=data.items;
  options($("resultSeries"), [["","全部作品"],...(data.series||[]).map(s=>[s,s])],state.series);
  if(initial){try {state.selectedId=localStorage.getItem("fanart.selected-result")||"";}catch{}}
  const same=state.selectedId && state.results.some(x=>x.result_id===state.selectedId);
  if(!initial && same){
    const oldId=state.selectedId;
    const query=state.search.toLowerCase();
    state.visible=state.results.filter(it=>(!state.series||it.series===state.series)&&(state.filter==="all"||it.review.status===state.filter)&&(!query||[it.id,it.character,it.series,it.review.note].join(" ").toLowerCase().includes(query)));
    if(state.visible.some(x=>x.result_id===oldId)){renderFilters();renderFilm();badge();renderVersions(current()); $("pagePosition").textContent=`${state.visible.findIndex(x=>x.result_id===oldId)+1} / ${state.visible.length}`; $("previous").disabled=state.visible[0]?.result_id===oldId; $("next").disabled=state.visible.at(-1)?.result_id===oldId; return;}
  }
  updateFiltered();
}
const jobLabels={queued:"排队中",running:"生成中",ok:"已完成",fail:"失败",interrupted:"已中断",cancelled:"已取消"};
function renderJobs() {
  const jobs=[...state.jobs].reverse();
  $("jobCount").textContent=String(jobs.filter(j=>["queued","running"].includes(j.status)).length);
  $("jobsList").innerHTML=jobs.length ? jobs.map(j=>`<article class="job-card"><div class="job-top"><strong>${esc(j.id)}</strong><span class="badge">${esc(jobLabels[j.status]||j.status)}</span></div><p>${esc([j.model,j.size,j.quality,j.sec?j.sec+" 秒":""].filter(Boolean).join(" · "))}</p>${j.error?`<p>${esc(j.error)}</p>`:""}${j.status==="ok"?`<button type="button" data-result-path="${esc(j.right)}">查看成图</button>`:""}${j.status==="queued"?`<button type="button" data-cancel="${esc(j.id)}">取消排队</button>`:""}</article>`).join(""):'<div class="empty-panel"><h2>还没有工作台任务</h2><p>历史成图已在作品审阅中显示。这里记录之后每次提交。</p></div>';
  $("jobsList").querySelectorAll("[data-result-path]").forEach(b=>b.onclick=()=>{
    const it=state.results.find(x=>x.path===b.dataset.resultPath); if(!it)return toast("结果仍在载入，请点刷新");
    state.filter="all";state.search="";state.series="";$("resultSearch").value="";$("resultSeries").value="";state.selectedId=it.result_id;state.mode="redo";updateFiltered();setView("review");
  });
  $("jobsList").querySelectorAll("[data-cancel]").forEach(b=>b.onclick=async()=>{try{await api("/api/cancel",{id:b.dataset.cancel});await pollState();toast("已取消排队");}catch(e){error(e.message);}});
}
async function pollState(initial=false) {
  const data=await api("/api/state");
  const previousJobs=state.jobs;
  state.settings=data.settings;state.characters=data.characters;state.jobs=data.jobs;
  $("connection").textContent=data.settings.has_key ? `${data.settings.profile} · 默认 ${data.settings.quality}` : "未配置生图接口";
  if(JSON.stringify(previousJobs)!==JSON.stringify(state.jobs) || initial) renderJobs();
  if(!initial){
    const completed=state.jobs.filter(j=>j.status==="ok" && previousJobs.some(p=>p.id===j.id && p.status!=="ok"));
    if(completed.length) toast(`${completed.length} 张新图已完成，已加入作品库`);
    await loadResults();
  }
}
function createDraft() {
  return {character:$("createCharacter").value,inspire:state.picked?.path||"",intent:$("createIntent").value,
    prompt:$("createPrompt").value,model:$("createModel").value,size:$("createSize").value,
    quality:$("createQuality").value,send_inspire:$("createSend").checked};
}
function saveCreate(){saveDraft("__create__",createDraft(),"createSaved");}
async function loadInspire(append=false) {
  const character=state.characters.find(c=>c.id===$("createCharacter").value);
  const sequence=++inspireSequence;
  const qs=new URLSearchParams({exclude:character?.series||"",series:$("inspireSeries").value,vision:$("inspireVision").value,limit:"60",offset:String(append?state.inspire.length:0)});
  const data=await api("/api/inspire?"+qs);
  if(sequence!==inspireSequence)return;
  state.inspire=append?[...state.inspire,...data.items]:data.items;state.inspireTotal=data.total;
  options($("inspireSeries"),[["","全部其他作品"],...(data.series||[]).map(s=>[s,s])],$("inspireSeries").value);
  $("inspireTotal").textContent=`已显示 ${state.inspire.length} / ${data.total}`;
  $("moreInspire").hidden=state.inspire.length>=data.total;
  renderInspire();
}
function renderInspire(){
  $("inspireGrid").innerHTML=state.inspire.map((it,i)=>`<button type="button" class="inspire-card ${it.path===state.picked?.path?"active":""}" data-i="${i}" aria-label="选择灵感 ${esc(it.file)}"><img loading="lazy" src="${fileUrl(it.path,true)}" alt="${esc(it.title)}"><span>${esc(it.title)}</span><small>${it.vision.done?"已筛选":"未筛选"} · ${esc(it.series)}</small></button>`).join("") || '<p class="muted">没有匹配的素材，请更换作品或筛选条件。</p>';
  $("inspireGrid").querySelectorAll("button").forEach(b=>b.onclick=()=>{state.picked=state.inspire[Number(b.dataset.i)];renderInspire();renderPicked();saveCreate();});
}
function renderPicked(){
  const it=state.picked;
  $("createPreview").hidden=!it;
  if(it) $("createPreview").src=fileUrl(it.path,true);
  $("createSelected").textContent=it ? (it.title||it.file||"已选参考图") : "从左侧选择一张灵感图";
  const v=it?.vision||{};
  $("createDescription").textContent=[v.shot,v.action,v.expression,v.scene,v.lighting_and_palette].filter(Boolean).join("\n") || "这张暂无视觉描述，可直接填写自己的创作要求。";
  $("createGo").disabled=!!v.split_wide?.is_split_wide;
}
async function prepareCreate(){
  if(!state.picked)return toast("先选择灵感图");
  if(state.picked.vision?.split_wide?.is_split_wide)return toast("这张属于宽图组，不能只提交半张");
  if($("createPrompt").value.trim() && !confirm("重新整理会替换当前新建提示词，继续吗？"))return;
  const d=createDraft(),v=state.picked.vision||{};
  try{const data=await api("/api/prepare",{character:d.character,inspire:d.inspire,send_inspire:d.send_inspire,mode:"redo",shot:v.shot||"",pose:d.intent||[v.action,v.expression].filter(Boolean).join("，"),style:v.lighting_and_palette||""});$("createPrompt").value=data.final_prompt;saveCreate();toast("已整理提示词，可直接修改后生成");}catch(e){error(e.message);}
}
async function submitCreate(){
  if(state.submitting)return;
  const d=createDraft();if(!d.prompt.trim())return toast("先整理或填写完整提示词");
  if(state.picked?.vision?.split_wide?.is_split_wide)return toast("请完整选取宽图组，本次不会提交半张");
  state.submitting=true;$("createGo").disabled=true;
  try{const response=await api("/api/generate",{character:d.character,inspire:d.inspire,send_inspire:d.send_inspire,mode:"redo",final_prompt:d.prompt,model:d.model,size:d.size,quality:d.quality,client_request_id:crypto.randomUUID()});$("createSubmitStatus").textContent="已排队 "+response.job.id;toast("已提交一张新创作");await pollState();}catch(e){$("createSubmitStatus").textContent=e.message+"；未自动重试，请先核对任务页。";}finally{state.submitting=false;$("createGo").disabled=false;}
}

// Polling refreshes records only; editor nodes stay mounted while a person types.
document.querySelectorAll("[data-view]").forEach(b=>b.onclick=()=>setView(b.dataset.view));
$("refresh").onclick=()=>pollState().then(()=>toast("作品与任务已刷新")).catch(e=>error(e.message));
$("resultSeries").onchange=()=>{state.series=$("resultSeries").value;updateFiltered(false);};
$("resultSearch").oninput=()=>{state.search=$("resultSearch").value;updateFiltered();};
$("clearFilters").onclick=()=>{state.filter="all";state.search="";state.series="";$("resultSearch").value="";$("resultSeries").value="";updateFiltered(false);};
$("previous").onclick=()=>move(-1);$("next").onclick=()=>move(1);
$("compareInspire").onclick=()=>{state.compare="inspiration";updateImages();};$("compareParent").onclick=()=>{state.compare="parent";updateImages();};
$("zoom").onchange=applyZoom;
$("toggleEditor").onclick=()=>{const hidden=$("reviewWorkspace").classList.toggle("editor-hidden");$("toggleEditor").textContent=hidden?"展开编辑区":"收起编辑区";$("toggleEditor").setAttribute("aria-expanded",String(!hidden));};
$("editPrompt").oninput=editChanged;
for(const id of ["editModel","editSize","editQuality"])$(id).onchange=editChanged;
$("editSend").onchange=()=>{editChanged();renderReferences();toast("参考图清单已更新，请同步检查提示词中的图片编号");};
$("editMode").onchange=changeMode;
$("restorePrompt").onclick=async()=>{if(!current()||!confirm("恢复本模式的初始提示词？当前文字会被替换。"))return;try{const it=current();const response=await api("/api/prepare",{character:it.char_id,parent_result_id:it.result_id,inspire:it.inspire,send_inspire:$("editSend").checked,mode:state.mode});$("editPrompt").value=response.final_prompt;editChanged();}catch(e){error(e.message);}};
$("copyPrompt").onclick=()=>navigator.clipboard.writeText($("editPrompt").value).then(()=>toast("提示词已复制")).catch(()=>toast("请选中文字后复制"));
$("regenerate").onclick=submitVersion;
$("markPass").onclick=()=>review("pass");$("markReview").onclick=()=>review("manual_review");$("markTrash").onclick=()=>review("quarantine");$("restoreReview").onclick=()=>review("unreviewed");$("undoReview").onclick=()=>review("",true);
$("reviewNote").oninput=()=>{if(current())saveDraft(current().result_id+"::note",{note:$("reviewNote").value});};
$("saveNote").onclick=()=>review(current()?.review.status||"unreviewed",false,true);
$("savePreset").onclick=savePreset;$("applyPreset").onclick=applyPreset;
$("createCharacter").onchange=()=>{saveCreate();loadInspire().catch(e=>error(e.message));};
for(const id of ["inspireSeries","inspireVision"])$(id).onchange=()=>loadInspire().catch(e=>error(e.message));
$("moreInspire").onclick=()=>loadInspire(true).catch(e=>error(e.message));
for(const id of ["createIntent","createPrompt"])$(id).oninput=saveCreate;
for(const id of ["createModel","createSize","createQuality","createSend"])$(id).onchange=saveCreate;
$("prepareCreate").onclick=prepareCreate;$("createGo").onclick=submitCreate;
document.addEventListener("keydown",e=>{
  if(e.defaultPrevented||e.ctrlKey||e.metaKey||e.altKey||e.repeat||state.view!=="review")return;
  if(e.target.closest("input,textarea,select,[contenteditable=true]"))return;
  if(e.key==="ArrowLeft"){e.preventDefault();move(-1);}else if(e.key==="ArrowRight"){e.preventDefault();move(1);}else if(["1","2","3"].includes(e.key)){e.preventDefault();review({1:"pass",2:"manual_review",3:"quarantine"}[e.key]);}
});
async function init(){
  await pollState(true);
  const editor=await api("/api/editor");state.drafts=editor.drafts;state.presets=editor.presets;
  try{const backup=JSON.parse(localStorage.getItem(storageKey)||"{}");for(const [key,value] of Object.entries(backup)){if(value && (!state.drafts[key] || (value.updated_at||"")>(state.drafts[key].updated_at||"")))state.drafts[key]=value;}}catch{}
  renderPresets();
  const draft=state.drafts.__create__||{};
  options($("createCharacter"),state.characters.map(c=>[c.id,c.name+(c.has_ref?"":"（缺身份图）")]),draft.character||state.characters.find(c=>c.has_ref)?.id);
  parameterOptions("create",draft);$("createIntent").value=draft.intent||"";$("createPrompt").value=draft.prompt||"";$("createSend").checked=draft.send_inspire!==false;
  if(draft.inspire)state.picked={path:draft.inspire,title:draft.inspire.split(/[\\/]/).at(-1)};
  renderPicked();await loadResults(true);state.initialized=true;
  setInterval(()=>{if(!document.hidden)pollState().catch(e=>error("本机服务连接失败："+e.message));},4500);
}
init().catch(e=>error("载入失败："+e.message));
