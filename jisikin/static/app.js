"use strict";

const $ = (sel) => document.querySelector(sel);
const STORE_KEY = "jisikin.filters";

const state = {
  view: "feed", // feed: 새 질문 / exposure: 상위노출 글 (둘 다 지식iN) / youtube · threads: 유튜브·쓰레드
  kinView: "feed", // 지식iN 탭에서 마지막으로 본 화면
  product: "", category: "", status: "todo", sort: "priority",
  unanswered: false, low: false, q: "", expUnanswered: false, expMode: "keyword",
  sStatus: "todo", sSort: "priority", ytSort: "views", ytKind: "", sLow: false, sQ: "",
  rPlatform: "", rState: "", rBy: "",
};
let resultItems = null;
const PLATFORM_NAMES = { youtube: "유튜브", threads: "쓰레드", cafe: "네이버 카페" };
// 유튜브는 조회수 많은 순(= 사람들이 많이 보는 영상)이 기본
const SORT_OPTIONS = {
  youtube: [["views", "조회수 많은 순"], ["latest", "최신순"]],
  threads: [["priority", "추천순"], ["latest", "최신순"]],
  cafe: [["latest", "최신순"], ["priority", "추천순"]],
};
const isSocial = () => state.view === "youtube" || state.view === "threads" || state.view === "cafe";
const groupOf = (view) => (view === "feed" || view === "exposure" ? "kin" : view);
const sortKey = () => (state.view === "youtube" ? "ytSort" : "sSort");

// 카드 종류별 API 주소 (지식iN 질문 / 유튜브·쓰레드 글)
function itemPath(card, suffix) {
  const base = card.dataset.kind === "social" ? "/api/social/" : "/api/questions/";
  return base + encodeURIComponent(card.dataset.id) + suffix;
}
let meta = null;
let exposureGroups = null;
let wasRunning = false;
let listTimer = null;

// ---------------------------------------------------------------- 유틸
function loadFilters() {
  try { Object.assign(state, JSON.parse(localStorage.getItem(STORE_KEY) || "{}")); } catch (e) { /* 무시 */ }
}
function saveFilters() {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch (e) { /* 무시 */ }
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Jisikin": "1" },
    body: JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  if (res.status === 401) {
    location.href = "/login?next=" + encodeURIComponent(location.pathname);
    throw new Error("로그인이 필요합니다");
  }
  let data = {};
  try { data = await res.json(); } catch (e) { /* 무시 */ }
  if (!res.ok) throw new Error(data.error || `요청 실패 (${res.status})`);
  return data;
}

function personName(username) {
  if (!username) return "";
  return meta?.people?.[username] || username;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function highlight(text, terms) {
  let html = esc(text);
  const list = [...new Set((terms || []).filter((t) => t && t.length >= 1))].sort((a, b) => b.length - a.length);
  if (!list.length) return html;
  const pattern = list.map((t) => esc(t).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
  return html.replace(new RegExp(pattern, "g"), (m) => `<mark>${m}</mark>`);
}

function relTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const sec = (Date.now() - d.getTime()) / 1000;
  if (sec < 60) return "방금";
  if (sec < 3600) return `${Math.floor(sec / 60)}분 전`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}시간 전`;
  if (sec < 86400 * 7) return `${Math.floor(sec / 86400)}일 전`;
  return `${d.getFullYear()}.${d.getMonth() + 1}.${d.getDate()}.`;
}

function untilTime(iso) {
  if (!iso) return "";
  const sec = (new Date(iso).getTime() - Date.now()) / 1000;
  if (sec <= 60) return "곧";
  if (sec >= 5400) return `${Math.round(sec / 3600)}시간 후`;
  return `${Math.round(sec / 60)}분 후`;
}

function num(n) {
  return Number(n).toLocaleString("ko-KR");
}

let toastTimer = null;
let toastUndo = null;
// undo 를 주면 [되돌리기] 버튼이 붙고 조금 더 오래 보인다 (실수로 누른 완료·건너뛰기를 바로 되돌릴 수 있게)
function toast(msg, undo) {
  const el = $("#toast");
  el.querySelector(".msg").textContent = msg;
  const btn = el.querySelector(".undo");
  toastUndo = undo || null;
  btn.hidden = !undo;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.classList.remove("show"); toastUndo = null; }, undo ? 6000 : 2500);
}
$("#toast .undo").addEventListener("click", async () => {
  const fn = toastUndo;
  toastUndo = null;
  $("#toast").classList.remove("show");
  if (fn) {
    try { await fn(); } catch (e) { toast(e.message); }
  }
});

// 안내 상자 닫기 (브라우저에 기억)
const DISMISS_KEY = "jisikin.dismissed";
function dismissed(id) {
  try { return (JSON.parse(localStorage.getItem(DISMISS_KEY) || "[]")).includes(id); } catch (e) { return false; }
}
function dismiss(id) {
  try {
    const list = JSON.parse(localStorage.getItem(DISMISS_KEY) || "[]");
    if (!list.includes(id)) list.push(id);
    localStorage.setItem(DISMISS_KEY, JSON.stringify(list));
  } catch (e) { /* 무시 */ }
}

function productById(id) {
  return (meta?.products || []).find((p) => p.id === id);
}

// ---------------------------------------------------------------- 상단 상태/탭
async function loadMeta() {
  meta = await api("/api/meta");
  renderViews();
  renderStatus();
  renderTabs();
  renderChips();
  renderNotice();
  renderExposureBar();
  renderSocialBar();
  renderResultsBar();
  if (wasRunning && !meta.running) loadList();
  wasRunning = meta.running;
  const newTotal = Object.values(meta.counts.products).reduce((a, p) => a + p.new, 0);
  document.title = (newTotal ? `(${newTotal}) ` : "") + "답변·댓글 센터";
}

function renderViews() {
  const group = groupOf(state.view);
  document.querySelectorAll(".view").forEach((b) => b.classList.toggle("active", b.dataset.group === group));
  document.querySelectorAll(".subview").forEach((b) => b.classList.toggle("active", b.dataset.view === state.view));
  $("#subviews").hidden = group !== "kin";
  $("#feed-toolbar").hidden = state.view !== "feed";
  $("#exp-toolbar").hidden = state.view !== "exposure";
  $("#social-toolbar").hidden = !isSocial();
  $("#results-toolbar").hidden = state.view !== "results";
  $("#s-kind").hidden = state.view !== "youtube";
  if (isSocial()) {
    const sel = $("#s-sort");
    const opts = SORT_OPTIONS[state.view];
    if (!opts.some(([v]) => v === state[sortKey()])) state[sortKey()] = opts[0][0];
    sel.innerHTML = opts.map(([v, label]) => `<option value="${v}">${label}</option>`).join("");
    sel.value = state[sortKey()];
  }
  if (meta) renderGroupCounts();
}

// 상단 탭 옆 숫자: 처리할 글 수
function renderGroupCounts() {
  const sum = (counts) => Object.values(counts || {}).reduce((a, p) => a + p.total, 0);
  const n = {
    kin: sum(meta.counts.products), youtube: sum(meta.social.counts.youtube),
    threads: sum(meta.social.counts.threads), cafe: sum(meta.social.counts.cafe),
  };
  for (const [k, v] of Object.entries(n)) {
    const el = $(`#cnt-${k}`);
    el.textContent = v ? num(v) : "";
    el.title = v ? `처리할 글 ${num(v)}개` : "";
  }
  const bad = (resultItems || []).filter((i) => resultBucket(i) === "problem").length;
  $("#cnt-results").textContent = bad ? `⚠️ ${bad}` : "";
  $("#cnt-results").title = bad ? `안 보이는 답변·댓글 ${bad}개` : "";
}

function myToday() {
  const me = meta.auth ? meta.user.username : "";
  return (meta.answer_stats.find((s) => s.username === me) || {}).today || 0;
}

function renderStatus() {
  // 상단: 오늘 완료한 답변·댓글 (내가 얼마나 했는지 한눈에)
  const today = meta.answer_stats.reduce((a, s) => a + s.today, 0);
  const people = meta.auth ? meta.answer_stats.filter((s) => s.today) : [];
  const who = people.map((s) => `${esc(s.name)} <b>${s.today}</b>`).join(" · ");
  const week = meta.answer_stats.reduce((a, s) => a + (s.week || 0), 0);
  $("#run-status").innerHTML =
    `<span class="today" title="지식iN 답변 + 유튜브·쓰레드 댓글 · 이번 주 ${week}건">✓ 오늘 완료 <b>${today}</b>건</span>`
    + (who ? `<span class="who">${who}</span>` : "");

  // 새 질문 도구줄: 지식iN 수집 상태
  const r = meta.last_run;
  const parts = [];
  if (meta.running && meta.running_kind === "collect") parts.push("<b>새 질문 찾는 중…</b>");
  else if (r) parts.push(`${relTime(r.finished_at || r.started_at)} 새로 찾음 · 새 질문 ${r.new_relevant}건`);
  else parts.push("아직 찾아본 적이 없어요");
  if (!meta.interval) parts.push("자동으로 찾기 꺼짐");
  else if (!meta.running && meta.next_run_at) parts.push(`다음 ${untilTime(meta.next_run_at)}`);
  if (meta.running && meta.running_kind !== "collect") {
    const label = { exposure: "상위노출 확인 중", social: "유튜브·쓰레드 찾는 중", keywords: "검색어 만드는 중", track: "작업 결과 확인 중" }[meta.running_kind];
    if (label) parts.push(`<span class="muted">(${label}…)</span>`);
  }
  $("#feed-status").innerHTML = parts.join(" · ");
  const btn = $("#collect-btn");
  const collecting = meta.running && meta.running_kind === "collect";
  btn.disabled = collecting;
  btn.textContent = collecting ? "찾는 중…" : "지금 찾기";
}

function renderExposureBar() {
  const x = meta.exposure;
  const parts = [];
  if (meta.running && meta.running_kind === "exposure") parts.push("<b>확인 중…</b> (검색어가 많으면 몇 분 걸립니다)");
  else if (x.last_run) parts.push(`마지막 확인 ${relTime(x.last_run.finished_at || x.last_run.started_at)}`);
  else parts.push("아직 확인해 본 적이 없어요");
  if (x.interval_hours) parts.push(`${x.interval_hours}시간마다 자동 확인${x.next_at && !meta.running ? ` (다음 ${untilTime(x.next_at)})` : ""}`);
  parts.push(`검색어 ${x.keywords}개`);
  $("#exp-status").innerHTML = parts.join(" · ");
  const btn = $("#exp-check-btn");
  const checking = meta.running && meta.running_kind === "exposure";
  btn.disabled = checking || !x.keywords;
  btn.textContent = checking ? "확인 중…" : "지금 확인";
}

function renderCafeBar() {
  const x = meta.cafe;
  const parts = [];
  const busy = meta.running && meta.running_kind === "cafe";
  if (busy) parts.push("<b>찾는 중…</b>");
  else if (x.last_run) parts.push(`${relTime(x.last_run.finished_at || x.last_run.started_at)} 새로 찾음`);
  else parts.push("아직 찾아본 적이 없어요");
  if (x.interval_minutes) parts.push(`${x.interval_minutes}분마다 자동${x.next_at && !meta.running ? ` (다음 ${untilTime(x.next_at)})` : ""}`);
  parts.push(`검색어 ${x.queries}개`);
  $("#social-status").innerHTML = parts.join(" · ");
  const btn = $("#social-collect-btn");
  btn.disabled = busy || !x.enabled || !x.queries;
  btn.textContent = busy ? "찾는 중…" : "지금 찾기";
  btn.removeAttribute("title");
}

function renderSocialBar() {
  if (state.view === "cafe") return renderCafeBar();
  const x = meta.social;
  const parts = [];
  const on = x.platforms[state.view];
  if (meta.running && meta.running_kind === "social") parts.push("<b>찾는 중…</b>");
  else if (x.last_run) parts.push(`${relTime(x.last_run.finished_at || x.last_run.started_at)} 새로 찾음`);
  else parts.push("아직 찾아본 적이 없어요");
  if (x.interval_hours) parts.push(`${x.interval_hours}시간마다 자동${x.next_at && !meta.running ? ` (다음 ${untilTime(x.next_at)})` : ""}`);
  parts.push(`검색어 ${x.queries}개`);
  $("#social-status").innerHTML = parts.join(" · ");
  const btn = $("#social-collect-btn");
  const busy = meta.running && meta.running_kind === "social";
  btn.disabled = busy || !(x.platforms.youtube || x.platforms.threads);
  btn.textContent = busy ? "찾는 중…" : "지금 찾기";
  if (!on && isSocial()) btn.title = `${PLATFORM_NAMES[state.view]} 키가 없어 이 탭은 수집하지 않습니다`;
  else btn.removeAttribute("title");
}

function renderResultsBar() {
  const x = meta.track;
  const parts = [];
  const busy = meta.running && meta.running_kind === "track";
  if (busy) parts.push("<b>확인 중…</b> (글이 많으면 몇 분 걸립니다)");
  if (x.hour < 0) parts.push("자동 확인 꺼짐");
  else parts.push(`매일 오전 ${x.hour}시 자동 확인`);
  if (!busy && x.last_run) parts.push(`마지막 확인 ${relTime(x.last_run.finished_at || x.last_run.started_at)}`);
  if (!busy && x.next_at) parts.push(`다음 ${untilTime(x.next_at)}`);
  if (resultItems) {
    const n = { visible: 0, problem: 0, unknown: 0 };
    resultItems.forEach((i) => n[resultBucket(i)]++);
    parts.push(`<b class="ok-txt">✅ ${n.visible}</b> · <b class="bad-txt">⚠️ ${n.problem}</b> · 확인 전 ${n.unknown}`);
  }
  $("#results-status").innerHTML = parts.join(" · ");
  const btn = $("#results-check-btn");
  btn.disabled = busy || x.hour < 0;
  btn.textContent = busy ? "확인 중…" : "지금 확인";
  // 직원별 보기 (로그인 사용 시)
  const sel = $("#r-by");
  sel.hidden = !meta.auth;
  if (meta.auth && !sel.options.length) {
    sel.innerHTML = `<option value="">모든 직원</option>` + Object.entries(meta.people || {}).map(([u, n]) => `<option value="${esc(u)}">${esc(n)}</option>`).join("");
    sel.value = state.rBy;
  }
}

function exposureCounts() {
  // 제품별: 상위노출 글 중 아직 처리 안 한 글 수
  const counts = {};
  for (const g of exposureGroups || []) {
    const c = counts[g.product] || (counts[g.product] = { total: 0, new: 0, seen: new Set() });
    for (const p of g.posts) {
      if (c.seen.has(p.doc_id) || !["new", "opened"].includes(p.status)) continue;
      c.seen.add(p.doc_id);
      c.total += 1;
    }
  }
  return counts;
}

function resultCounts() {
  const counts = {};
  for (const i of filteredResults(false)) {
    const c = counts[i.product] || (counts[i.product] = { total: 0, new: 0 });
    c.total += 1;
    if (resultBucket(i) === "problem") c.new += 1; // 빨간 숫자 = 안 보이는 글
  }
  return counts;
}

// [할 일 N] 숫자: 지금 고른 제품 기준
function renderSegCounts() {
  const pick = (counts) => {
    if (!counts) return 0;
    if (state.product) return counts[state.product]?.total || 0;
    return Object.values(counts).reduce((a, p) => a + p.total, 0);
  };
  const f = pick(meta.counts.products);
  $("#f-status [data-count]").textContent = f ? num(f) : "";
  const sc = isSocial() ? pick(meta.social.counts[state.view]) : 0;
  $("#s-status [data-count]").textContent = sc ? num(sc) : "";
}

function renderTabs() {
  if (meta) renderSegCounts();
  const counts = state.view === "results" ? resultCounts() : state.view === "exposure" ? exposureCounts() : isSocial() ? (meta.social.counts[state.view] || {}) : meta.counts.products;
  const all = Object.values(counts).reduce((a, p) => ({ total: a.total + p.total, new: a.new + p.new }), { total: 0, new: 0 });
  const tabs = [{ id: "", name: "전체", color: "", c: all }]
    .concat(meta.products.map((p) => ({ ...p, c: counts[p.id] || { total: 0, new: 0 } })));
  $("#tabs").innerHTML = tabs.map((t) => `
    <button class="tab ${state.product === t.id ? "active" : ""}" data-product="${esc(t.id)}" style="${t.color ? `--c:${esc(t.color)}` : ""}">
      ${t.color ? '<span class="dot"></span>' : ""}${esc(t.name)}
      <span class="n">${t.c.total}</span>${t.c.new ? `<span class="new ${state.view === "results" ? "warn" : ""}">${t.c.new}</span>` : ""}
    </button>`).join("");
}

function renderChips() {
  const p = productById(state.product);
  if (!p || state.view === "exposure" || state.view === "results") { $("#chips").innerHTML = ""; return; }
  const counts = isSocial() ? (meta.social.counts[state.view] || {}) : meta.counts.products;
  const cc = counts[p.id]?.categories || {};
  const chips = [{ name: "", label: "모든 카테고리" }].concat(p.categories.map((c) => ({ name: c, label: c })));
  $("#chips").innerHTML = chips.map((c) => `
    <button class="chip ${state.category === c.name ? "active" : ""}" data-category="${esc(c.name)}">
      ${esc(c.label)}${c.name ? `<span class="n">${cc[c.name] || 0}</span>` : ""}
    </button>`).join("");
}

function renderNotice() {
  if (isSocial()) return renderSocialNotice();
  if (state.view === "results") return renderResultsNotice();
  const r = state.view === "exposure" ? meta.exposure.last_run : meta.last_run;
  const what = state.view === "exposure" ? "상위노출 확인" : "수집";
  const notes = [];
  if (r && r.errors && r.errors.length) {
    notes.push(`<b>마지막 ${what}에서 오류 ${r.errors.length}건</b><ul>${r.errors.slice(0, 5).map((e) => `<li>${esc(e)}</li>`).join("")}</ul>`);
  }
  if (meta.mode === "web" && state.view === "feed" && meta.user.role === "admin" && !dismissed("webmode")) {
    notes.push(`<button class="close" data-dismiss="webmode" title="닫기" aria-label="닫기">×</button>`
      + "네이버 검색 API 키가 없어 <b>웹 검색 모드</b>로 찾고 있어요. 키를 넣으면 더 빠르고 안정적입니다 → <a href='/settings'>설정</a>");
  }
  $("#notice").innerHTML = notes.map((n) => `<div class="notice">${n}</div>`).join("");
}

function renderSocialNotice() {
  const name = PLATFORM_NAMES[state.view];
  const notes = [];
  if (state.view === "cafe") {
    const errs = meta.cafe.last_run?.errors || [];
    if (errs.length) notes.push(`<b>마지막으로 찾을 때 오류 ${errs.length}건</b><ul>${errs.slice(0, 5).map((e) => `<li>${esc(e)}</li>`).join("")}</ul>`);
    if (!meta.cafe.enabled) notes.push("네이버 API 키가 없어 카페 글을 찾지 않습니다. 지식iN 과 같은 키를 씁니다.");
    else if (!meta.cafe.queries && meta.user.role === "admin") notes.push("카페 검색어가 없습니다. <a href='/settings'>설정</a>에서 제품에 <b>cafe: keywords</b> 를 넣어 주세요.");
    notes.push(`<button class="close" data-dismiss="cafe-help" title="닫기" aria-label="닫기">×</button>카페 글에 댓글을 달려면 그 카페에 <b>가입</b>해야 하는 경우가 많아요. 제목을 누르면 카페 글이 새 창으로 열립니다.`);
    $("#notice").innerHTML = notes.filter((n) => !(n.includes("cafe-help") && dismissed("cafe-help"))).map((n) => `<div class="notice">${n}</div>`).join("");
    return;
  }
  const errors = (meta.social.last_run?.errors || []).filter((e) => e.startsWith(name) || !/^(유튜브|쓰레드) /.test(e));
  if (errors.length) {
    notes.push(`<b>마지막으로 찾을 때 오류 ${errors.length}건</b><ul>${errors.slice(0, 5).map((e) => `<li>${esc(e)}</li>`).join("")}</ul>`);
  }
  if (!meta.social.platforms[state.view]) {
    const key = state.view === "youtube" ? "유튜브 Data API 키" : "쓰레드 액세스 토큰";
    const [missing, obj] = state.view === "youtube" ? ["유튜브 API 키가", "를"] : ["쓰레드 토큰이", "을"];
    const how = meta.user.role === "admin" ? `<a href="/settings">설정</a> → API 키 → 유튜브 · 쓰레드에 <b>${key}</b>${obj} 넣으면 자동으로 찾아옵니다. (발급 방법도 거기 있어요)` : `관리자에게 ${key} 등록을 요청하세요.`;
    let links = "";
    if (state.view === "threads") {
      const qs = [...new Set(meta.products.flatMap((p) => p.social_queries || []))];
      links = `<div class="links">토큰 없이 직접 보기: ${qs.map((q) => `<a href="https://www.threads.net/search?q=${encodeURIComponent(q)}&serp_type=recent" target="_blank" rel="noopener">${esc(q)} ↗</a>`).join(" ")}</div>`;
    }
    notes.push(`${missing} 없습니다. ${how}${links}`);
  }
  $("#notice").innerHTML = notes.map((n) => `<div class="notice">${n}</div>`).join("");
}

function renderResultsNotice() {
  const notes = [];
  const r = meta.track.last_run;
  if (r && r.errors && r.errors.length) {
    notes.push(`<b>마지막 확인에서 오류 ${r.errors.length}건</b> (다음 확인 때 다시 시도합니다)<ul>${r.errors.slice(0, 5).map((e) => `<li>${esc(e)}</li>`).join("")}</ul>`);
  }
  if (!meta.track.youtube) notes.push("유튜브 API 키가 없어 유튜브 댓글은 확인하지 않습니다.");
  $("#notice").innerHTML = notes.map((n) => `<div class="notice">${n}</div>`).join("");
}

// ---------------------------------------------------------------- 작업 결과
function resultBucket(i) {
  const st = i.check?.state;
  if (st === "visible") return "visible";
  if (st === "missing" || st === "gone" || st === "comments_off") return "problem";
  return "unknown";
}

function filteredResults(byProduct = true) {
  return (resultItems || []).filter((i) =>
    (!byProduct || !state.product || i.product === state.product)
    && (!state.rPlatform || i.platform === state.rPlatform)
    && (!state.rState || resultBucket(i) === state.rState)
    && (!state.rBy || i.status_by === state.rBy));
}

async function loadResults() {
  const data = await api("/api/results");
  if (state.view !== "results") return;
  resultItems = data.items;
  renderTabs();
  renderGroupCounts();
  renderResultsBar();
  const items = filteredResults();
  const list = $("#list");
  if (!items.length) {
    list.innerHTML = resultItems.length
      ? `<div class="empty">조건에 맞는 글이 없습니다.</div>`
      : `<div class="empty">아직 완료한 답변·댓글이 없습니다.<br><span class="muted">[✓ 답변완료] / [✓ 댓글완료]를 누른 글이 여기에 모이고, 매일 아침 아직 보이는지 확인합니다.</span></div>`;
    return;
  }
  list.innerHTML = items.map(resultCardHtml).join("");
}

function resultStateHtml(i) {
  const c = i.check;
  const out = [];
  if (!c) {
    if (i.platform === "cafe") out.push(`<span class="rs-badge none" title="카페 댓글은 회원만 볼 수 있는 경우가 많아 자동으로 확인하지 않습니다">카페는 자동 확인 안 함</span>`);
    else if (i.platform === "threads") out.push(`<span class="rs-badge none" title="Meta 앱 검수 전에는 남의 글에 단 답글을 확인할 수 없습니다">쓰레드는 자동 확인 안 함</span>`);
    else out.push(`<span class="rs-badge none">확인 전</span><span>다음 확인 ${untilTime(meta.track.next_at) || "-"}</span>`);
    return out.join("");
  }
  const labels = {
    visible: ["ok", "✅ 노출 중"], missing: ["bad", "⚠️ 안 보임"], gone: ["bad", "🗑 원글 삭제됨"],
    comments_off: ["bad", "🚫 댓글 막힘"], no_text: ["none", "✏️ 확인 불가"],
  };
  const [cls, label] = labels[c.state] || ["none", c.state];
  const hints = {
    missing: i.platform === "kin" ? "질문 페이지에 내 답변이 없습니다 (삭제·신고로 숨김 의심)" : "공개 댓글에 없습니다 (삭제·스팸 필터로 숨김 의심)",
    no_text: "올린 답변·댓글 내용이 저장되지 않아 비교할 수 없습니다. 아래에 실제로 올린 내용을 붙여넣어 주세요.",
  };
  out.push(`<span class="rs-badge ${cls}" title="${esc(hints[c.state] || "")}">${label}</span>`);
  if (c.state === "visible") {
    if (c.adopted) out.push(`<span class="badge hot">채택됨</span>`);
    if (c.rank) out.push(`<span class="badge ${c.rank <= 3 ? "hot" : ""}" title="인기 댓글순 순위">인기 댓글 ${c.rank}위</span>`);
    if (c.likes != null) out.push(`<span title="내 댓글 좋아요">♥ ${compactNum(c.likes)}</span>`);
    if (c.replies) out.push(`<span title="내 댓글에 달린 답글">답글 ${c.replies}</span>`);
    if (c.note) out.push(`<span>${esc(c.note)}</span>`);
    if (c.visible_days > 1) out.push(`<span title="보이는 걸 확인한 날 수">${c.visible_days}일째 노출</span>`);
  }
  if (c.state !== "visible" && c.prev_state === "visible") out.push(`<b class="bad-txt">지난 확인까지는 보였음</b>`);
  out.push(`<span>확인 ${relTime(c.checked_at)}</span>`);
  return out.join("");
}

function resultCardHtml(i) {
  const p = productById(i.product);
  const yt = i.platform === "youtube";
  const pf = { kin: ["kin", "N"], youtube: ["yt", "▶"], threads: ["th", "@"], cafe: ["cf", "C"] }[i.platform];
  const by = meta.auth && i.status_by ? ` · ${esc(personName(i.status_by))}` : "";
  const exp = i.exposure ? `<span class="badge hot" title="[상위노출 글] 검색어 중 가장 높은 순위">검색 '${esc(i.exposure.keyword)}' ${i.exposure.rank}위</span>` : "";
  const noText = i.check?.state === "no_text" || (!i.check && !(i.draft || "").trim() && i.platform !== "threads");
  return `
  <article class="card result ${resultBucket(i)}" data-kind="${i.platform === "kin" ? "question" : "social"}" data-id="${esc(i.item_id)}" style="${p ? `--c:${esc(p.color)}` : ""}">
    ${yt && i.thumbnail ? `<a class="thumb" href="${esc(i.url)}" target="_blank" rel="noopener"><img src="${esc(i.thumbnail)}" alt="" loading="lazy"></a>` : ""}
    <div class="card-main">
      <a class="title" href="${esc(i.url)}" target="_blank" rel="noopener"><span class="pf ${pf[0]} mini">${pf[1]}</span>${i.is_short ? '<span class="shorts-badge">숏츠</span>' : ""}${esc(i.title || "(제목 없음)")}</a>
      <div class="meta result-state">${resultStateHtml(i)}${exp}</div>
      <div class="meta">
        ${productBadge(p, null)}
        <span>완료 ${relTime(i.status_changed_at)}${by}</span>
        ${yt && i.views != null ? `<span>조회 ${compactNum(i.views)}</span>` : ""}
        ${(i.draft || "").trim() ? `<button class="linkish" data-act="toggle-text">올린 내용 보기</button>` : ""}
      </div>
      <div class="draft" ${noText ? "" : "hidden"}>
        <textarea spellcheck="false" placeholder="실제로 올린 답변·댓글 내용을 붙여넣으세요">${esc(i.draft || "")}</textarea>
        <div class="row">
          <button class="btn small primary" data-act="save-text">저장</button>
          <span class="hint">이 내용으로 내 답변·댓글을 찾습니다. 올린 뒤 고쳤다면 고친 내용으로 바꿔 주세요.</span>
        </div>
      </div>
    </div>
  </article>`;
}

// ---------------------------------------------------------------- 질문 목록
async function loadList() {
  if (state.view === "results") return loadResults();
  if (state.view === "exposure") return loadExposure();
  if (isSocial()) return loadSocial();
  const params = new URLSearchParams({
    product: state.product, category: state.category, status: state.status, sort: state.sort,
    unanswered: state.unanswered ? "1" : "", include_low: state.low ? "1" : "", q: state.q,
  });
  const data = await api("/api/questions?" + params);
  renderList(data.items);
}

function renderList(items) {
  const list = $("#list");
  if (!items.length) {
    list.innerHTML = state.status === "todo" && !state.q
      ? `<div class="empty"><div class="big">🎉 할 일을 다 끝냈어요</div><span class="muted">${meta.interval ? `새 질문은 ${meta.interval}분마다 자동으로 찾아와요.` : "[지금 찾기]를 누르면 새 질문을 찾아옵니다."}</span></div>`
      : `<div class="empty">해당하는 질문이 없어요.<br><span class="muted">위의 조건을 바꿔 보세요.</span></div>`;
    return;
  }
  list.innerHTML = items.map(cardHtml).join("");
}

function cardHtml(q) {
  const p = productById(q.product);
  const bestMatch = (q.matches || []).find((m) => m.product_id === q.product) || (q.matches || [])[0] || {};
  const terms = (q.matches || []).flatMap((m) => m.terms || []);
  const low = !q.product;
  const lowProduct = low ? productById(bestMatch.product_id) : null;
  const color = (p || lowProduct)?.color || "";
  const recent = q.first_seen && (Date.now() - new Date(q.first_seen).getTime()) < 3 * 3600 * 1000;
  const isNew = q.status === "new" && recent;

  const ansBadge = answerBadge(q);
  const time = q.asked_at ? `작성 ${relTime(q.asked_at)}` : `수집 ${relTime(q.first_seen)}`;
  const snippet = q.body || q.snippet || "";
  const cats = (q.categories || []).length ? q.categories : (bestMatch.categories || []);
  const others = (q.matches || []).filter((m) => m.relevant && m.product_id !== q.product)
    .map((m) => productById(m.product_id)?.name).filter(Boolean);

  return `
  <article class="card ${low ? "low" : ""} ${q.status === "opened" ? "opened" : ""}" data-id="${esc(q.doc_id)}" ${cardData(q)} style="${color ? `--c:${esc(color)}` : ""}">
    <div class="card-main">
      <a class="title" href="${esc(q.url)}" target="_blank" rel="noopener" data-open title="관련도 점수 ${Math.round(bestMatch.score || q.score || 0)} · 누르면 질문이 새 창으로 열려요">
        ${isNew ? '<span class="new-badge">NEW</span>' : ""}${highlight(q.title, terms)}
      </a>
      ${snippet ? `<p class="snippet">${highlight(snippet, terms)}</p>` : ""}
      <div class="meta">
        ${ansBadge}
        ${q.reward ? `<span class="badge">내공 ${q.reward}</span>` : ""}
        <span>${time}</span>
        ${productBadge(p, lowProduct)}
        ${cats.map((c) => `<span class="cat">#${esc(c)}</span>`).join("")}
        ${others.length ? `<span>· ${esc(others.join(", "))}에도 해당</span>` : ""}
        ${statusLabel(q)}
      </div>
      ${draftBoxHtml(q)}
    </div>
    <div class="actions">${actionsHtml(q)}</div>
  </article>`;
}

// 제품 탭을 골랐으면 카드마다 제품명을 또 보여주지 않음 (카드 왼쪽 색 띠로 구분)
function productBadge(p, lowProduct) {
  if (!p) return `<span class="badge">관련도 낮음${lowProduct && !state.product ? " · " + esc(lowProduct.name) : ""}</span>`;
  return state.product ? "" : `<span class="badge product">${esc(p.name)}</span>`;
}

function statusLabel(q, social) {
  const by = meta.auth && q.status_by ? ` · ${esc(personName(q.status_by))}` : "";
  if (q.status === "answered") return `<b>${social ? "댓글완료" : "답변완료"}${by}</b>`;
  if (q.status === "skipped") return `<b>건너뜀${by}</b>`;
  // 다른 사람이 이미 열어본 글: 같은 질문에 두 명이 답하지 않도록 표시
  if (q.status === "opened" && meta.auth && q.status_by && q.status_by !== meta.user.username) {
    return `<span class="badge busy" title="${esc(personName(q.status_by))} 님이 이 글을 열어봤습니다">${esc(personName(q.status_by))} 확인 중</span>`;
  }
  return "";
}

function answerBadge(q) {
  const ans = q.answer_count;
  if (ans === 0) return `<span class="badge zero" title="아직 답변이 없어요 — 첫 답변이 가장 잘 보입니다">답변 0</span>`;
  if (ans != null) return `<span class="badge ${ans >= 5 ? "many" : ""}">답변 ${ans}</span>`;
  return `<span class="badge" title="상세 정보를 아직 못 가져왔습니다">답변 ?</span>`;
}

function cardData(q) {
  return `data-status="${esc(q.status)}" data-has-draft="${(q.draft || "").trim() ? 1 : 0}"`;
}

// 카드 버튼: 지금 할 단계 하나만 크게 (① AI 초안 → ② 복사하고 열기 → ③ 달았어요)
function actionsHtml(q, social) {
  const noun = social ? "댓글" : "답변";
  if (q.status !== "new" && q.status !== "opened") {
    return `<button class="btn" data-act="opened">↩ 할 일로 되돌리기</button>`;
  }
  const hasDraft = !!(q.draft || "").trim();
  // 초안 칸이 열려 있으면 그 안의 [복사하고 열기]가 다음 단계 → 여기서는 크게 강조하지 않음
  if (q.draftOpen) {
    return `<button class="btn" data-act="answered">✓ ${noun} 달았어요</button>`
      + `<button class="btn quiet" data-act="draft">초안 접기</button>`
      + `<button class="linkbtn skip" data-act="skipped">건너뛰기</button>`;
  }
  const done = (cls) => `<button class="btn ${cls}" data-act="answered" title="${noun}을 올렸으면 눌러주세요. [작업 결과]에서 노출 여부를 매일 확인합니다">✓ ${noun} 달았어요</button>`;
  const draftBtn = (cls) => `<button class="btn ${cls}" data-act="draft">${hasDraft ? "초안 보기" : `✨ AI ${noun} 초안`}</button>`;
  const out = [];
  if (meta.ai.enabled && q.status !== "opened") out.push(draftBtn("primary"), done(""));
  else out.push(done("primary"), meta.ai.enabled ? draftBtn("") : "");
  out.push(`<button class="linkbtn skip" data-act="skipped" title="${noun}을 달지 않을 글 — 목록에서 빠집니다 (되돌리기 가능)">건너뛰기</button>`);
  return out.join("");
}

function refreshActions(card) {
  const box = card.querySelector(".actions");
  if (!box) return;
  const q = {
    status: card.dataset.status, draft: card.dataset.hasDraft === "1" ? "y" : "",
    draftOpen: card.querySelector(".draft") && !card.querySelector(".draft").hidden,
  };
  box.innerHTML = actionsHtml(q, card.dataset.kind === "social");
}

function markOpened(card) {
  if (card.dataset.status !== "new") return;
  card.dataset.status = "opened";
  card.classList.add("opened");
  refreshActions(card);
}

// 지식iN 글자 수 세는 방식: 한글 등 2byte, 영문·숫자·공백 1byte, 줄바꿈 2byte
function answerBytes(text) {
  let n = 0;
  for (const c of (text || "").trim()) n += (c.charCodeAt(0) > 127 || c === "\n") ? 2 : 1;
  return n;
}

function bytesHtml(text, limit) {
  if (!limit) return "";
  const n = answerBytes(text);
  return `<span class="bytes ${n > limit ? "over" : ""}" title="한글 1자 2byte, 영문·숫자·공백 1byte">${num(n)} / ${num(limit)} byte${n > limit ? " · 너무 길어요" : ""}</span>`;
}

// 명연당 계산 결과 (AI 초안이 쓴 사주·이름 계산) — 직원이 만세력·한자 사전과 바로 대조
function calcBoxHtml(calc) {
  if (!calc || (!calc.warning && !(calc.rows || []).length)) return "";
  const rows = (calc.rows || []).map((r) => `
      <div class="calc-row ${r.kind === "error" ? "err" : ""}"><b>${esc(r.title)}</b>${r.lines.map((l) => `<div>${esc(l)}</div>`).join("")}</div>`).join("");
  return `
      <details class="calc-box" open>
        <summary>${calc.warning ? "⚠ " : "🧮 "}명연당 계산 결과 ${rows ? `<span class="muted">(초안의 사주·한자 값은 이 결과로 썼어요)</span>` : ""}</summary>
        ${calc.warning ? `<p class="calc-warn">${esc(calc.warning)}</p>` : ""}
        ${rows || `<p class="muted">계산한 것이 없어요 (생년월일·한자가 없는 질문)</p>`}
      </details>`;
}

function draftBoxHtml(q, social) {
  const what = social ? (q.platform === "youtube" ? "영상" : "글") : "질문";
  const limit = social ? 0 : (productById(q.product)?.max_bytes || 0);
  return `
      <div class="draft" hidden>
        <textarea spellcheck="false">${esc(q.draft || "")}</textarea>
        <div class="calc-slot">${social ? "" : calcBoxHtml(q.draft_calc)}</div>
        <div class="row">
          <button class="btn primary" data-act="copy-open">📋 복사하고 ${what} 열기</button>
          <button class="btn" data-act="copy">복사만</button>
          <button class="btn quiet" data-act="regen">다시 쓰기</button>
          ${limit ? `<span class="bytes-box" data-limit="${limit}">${bytesHtml(q.draft, limit)}</span>` : ""}
        </div>
        <p class="steps"><span>① 내용 확인·수정</span><span>② 복사하고 ${what} 열어 붙여넣고 등록</span><span>③ 돌아와서 <b>✓ ${social ? "댓글" : "답변"} 달았어요</b></span></p>
      </div>`;
}

// ---------------------------------------------------------------- 유튜브 · 쓰레드
async function loadSocial() {
  const view = state.view;
  const params = new URLSearchParams({
    platform: view, product: state.product, category: state.category, status: state.sStatus,
    sort: state[sortKey()],
    shorts: view === "youtube" ? state.ytKind : "",
    include_low: state.sLow ? "1" : "", q: state.sQ,
  });
  const data = await api("/api/social?" + params);
  if (state.view !== view) return; // 그사이 다른 탭으로 옮김
  const list = $("#list");
  if (!data.items.length) {
    const name = view === "youtube" ? "영상" : "글";
    if (!meta.social.platforms[view] && view !== "cafe") list.innerHTML = `<div class="empty">아직 찾아온 ${name}이 없어요.<br><span class="muted">키를 넣으면 여기에 쌓입니다.</span></div>`;
    else if (state.sStatus === "todo" && !state.sQ) list.innerHTML = `<div class="empty"><div class="big">🎉 할 일을 다 끝냈어요</div><span class="muted">${view === "cafe" ? (meta.cafe.interval_minutes ? `새 글은 ${meta.cafe.interval_minutes}분마다 자동으로 찾아와요.` : "[지금 찾기]를 누르면 새로 찾아옵니다.") : meta.social.interval_hours ? `새 ${name}은 ${meta.social.interval_hours}시간마다 자동으로 찾아와요.` : "[지금 찾기]를 누르면 새로 찾아옵니다."}</span></div>`;
    else list.innerHTML = `<div class="empty">해당하는 ${name}이 없어요.<br><span class="muted">위의 조건을 바꿔 보세요.</span></div>`;
    return;
  }
  list.innerHTML = data.items.map(socialCardHtml).join("");
}

function compactNum(n) {
  if (n == null) return "?";
  if (n >= 100000000) return `${(n / 100000000).toFixed(1).replace(/\.0$/, "")}억`;
  if (n >= 10000) return `${(n / 10000).toFixed(n >= 100000 ? 0 : 1).replace(/\.0$/, "")}만`;
  return num(n);
}

function durationText(sec) {
  if (sec == null) return "";
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mm = h ? String(m).padStart(2, "0") : m;
  return `${h ? h + ":" : ""}${mm}:${String(s).padStart(2, "0")}`;
}

function socialCardHtml(q) {
  const p = productById(q.product);
  const bestMatch = (q.matches || []).find((m) => m.product_id === q.product) || (q.matches || [])[0] || {};
  const terms = (q.matches || []).flatMap((m) => m.terms || []);
  const low = !q.product;
  const lowProduct = low ? productById(bestMatch.product_id) : null;
  const color = (p || lowProduct)?.color || "";
  const yt = q.platform === "youtube";
  const recent = q.first_seen && (Date.now() - new Date(q.first_seen).getTime()) < 6 * 3600 * 1000;
  const isNew = q.status === "new" && recent;
  let title = q.title, body = q.body || "";
  if (!yt) { // 쓰레드는 제목이 없으니 첫 줄을 제목처럼
    const i = body.indexOf("\n");
    title = i >= 0 ? body.slice(0, i) : body;
    body = i >= 0 ? body.slice(i + 1) : "";
    if (title.length > 90) { body = title.slice(90) + (body ? "\n" + body : ""); title = title.slice(0, 90) + "…"; }
  }
  const cats = (q.categories || []).length ? q.categories : (bestMatch.categories || []);
  const stats = yt ? [
    `<span class="badge views" title="조회수 ${q.views == null ? "?" : num(q.views)}회">조회 ${compactNum(q.views)}</span>`,
    q.comments != null ? `<span class="badge ${q.comments < 5 ? "zero" : ""}" title="댓글 수">댓글 ${compactNum(q.comments)}</span>` : "",
    q.likes != null ? `<span title="좋아요">♥ ${compactNum(q.likes)}</span>` : "",
  ].join("") : "";
  const author = q.author ? (q.author_url ? `<a href="${esc(q.author_url)}" target="_blank" rel="noopener">${q.platform === "threads" ? "@" : q.platform === "cafe" ? "☕ " : ""}${esc(q.author)}</a>` : esc(q.author)) : "";
  return `
  <article class="card social ${yt ? "yt" : "th"} ${low ? "low" : ""} ${q.status === "opened" ? "opened" : ""}" data-kind="social" data-id="${esc(q.post_id)}" ${cardData(q)} style="${color ? `--c:${esc(color)}` : ""}">
    ${yt && q.thumbnail ? `<a class="thumb" href="${esc(q.url)}" target="_blank" rel="noopener" data-open><img src="${esc(q.thumbnail)}" alt="" loading="lazy">${q.duration ? `<span class="dur">${durationText(q.duration)}</span>` : ""}</a>` : ""}
    <div class="card-main">
      <a class="title" href="${esc(q.url)}" target="_blank" rel="noopener" data-open title="찾은 검색어: ${esc((q.queries || []).join(", "))} · 누르면 새 창으로 열려요">
        ${isNew ? '<span class="new-badge">NEW</span>' : ""}${q.is_short ? '<span class="shorts-badge">숏츠</span>' : ""}${highlight(title || "(내용 없음)", terms)}
      </a>
      ${body.trim() ? `<p class="snippet">${highlight(body.trim(), terms)}</p>` : ""}
      <div class="meta">
        ${stats}
        ${author ? `<span>${author}</span>` : ""}
        <span>${q.published_at ? relTime(q.published_at) : `수집 ${relTime(q.first_seen)}`}</span>
        ${productBadge(p, lowProduct)}
        ${cats.map((c) => `<span class="cat">#${esc(c)}</span>`).join("")}
        ${statusLabel(q, true)}
      </div>
      ${draftBoxHtml(q, true)}
    </div>
    <div class="actions">${actionsHtml(q, true)}</div>
  </article>`;
}

// ---------------------------------------------------------------- 상위노출 글
async function loadExposure() {
  const data = await api("/api/exposure");
  exposureGroups = data.groups; // 탭 개수는 전체 기준
  renderTabs();
  let groups = data.groups.filter((g) => !state.product || g.product === state.product);
  if (state.expUnanswered) {
    groups = groups.map((g) => ({ ...g, posts: g.posts.filter((p) => ["new", "opened"].includes(p.status)) }));
  }
  renderExposure(groups);
}

function rankBadges(q) {
  return meta.exposure.sources.map((s) => {
    const rank = q.ranks?.[s.id];
    if (rank == null) return "";
    let change = "";
    if (q.prev_ranks && s.id in q.prev_ranks) {
      const prev = q.prev_ranks[s.id];
      if (prev == null) change = '<em class="rk-new">NEW</em>';
      else if (prev > rank) change = `<em class="rk-up">▲${prev - rank}</em>`;
      else if (prev < rank) change = `<em class="rk-down">▼${rank - prev}</em>`;
    }
    return `<span class="rank ${rank === 1 ? "top" : ""}">${esc(s.name)} <b>${rank}위</b>${change}</span>`;
  }).join("");
}

function viewsBadges(q) {
  const out = [];
  if (q.views != null) out.push(`<span class="badge">조회 ${num(q.views)}</span>`);
  if (q.views_per_day != null) {
    const label = q.views_per_day_kind === "recent" ? `최근 하루 +${num(Math.round(q.views_per_day))}` : `하루 평균 ${num(Math.round(q.views_per_day))}`;
    const hot = q.views_per_day >= 30;
    out.push(`<span class="badge ${hot ? "hot" : ""}" title="${q.views_per_day_kind === "recent" ? "최근 확인 사이 조회수 증가량" : "작성일 이후 평균 조회수"}">${label}</span>`);
  }
  return out.join("");
}

// 조회수 높은 순: 여러 검색어에서 찾은 같은 글은 하나로 합치고, 조회수가 많은 글부터
const FLAT_LIMIT = 300;
function flatExposure(groups) {
  const byId = new Map();
  for (const g of groups) {
    for (const q of g.posts) {
      const cur = byId.get(q.doc_id);
      if (cur) cur.keywords.push(g.keyword);
      else byId.set(q.doc_id, { ...q, keywords: [g.keyword] });
    }
  }
  return [...byId.values()].sort((a, b) => (b.views ?? -1) - (a.views ?? -1)).slice(0, FLAT_LIMIT);
}

function exposureCardHtml(q) {
  const p = productById(q.product);
  const terms = (q.matches || []).flatMap((m) => m.terms || []);
  const snippet = q.body || q.snippet || "";
  const time = q.asked_at ? `작성 ${relTime(q.asked_at)}` : "";
  const done = q.status === "answered" || q.status === "skipped";
  return `
  <article class="card exp ${done ? "done" : ""} ${q.status === "opened" ? "opened" : ""}" data-id="${esc(q.doc_id)}" ${cardData(q)} style="${p ? `--c:${esc(p.color)}` : ""}">
    <div class="card-main">
      <div class="ranks">${rankBadges(q)}</div>
      <a class="title" href="${esc(q.url)}" target="_blank" rel="noopener" data-open>${highlight(q.title, terms)}</a>
      ${snippet ? `<p class="snippet">${highlight(snippet, terms)}</p>` : ""}
      <div class="meta">
        ${viewsBadges(q)}
        ${answerBadge(q)}
        ${time ? `<span>${time}</span>` : ""}
        ${statusLabel(q)}
      </div>
      ${draftBoxHtml(q)}
    </div>
    <div class="actions">${actionsHtml(q)}</div>
  </article>`;
}

function renderExposure(groups) {
  const list = $("#list");
  if (!meta.exposure.keywords) {
    const how = meta.user.role === "admin"
      ? '<a href="/keywords">검색어 관리</a>에서 메인 키워드(예: 건선)를 넣어주세요.'
      : "관리자에게 메인 키워드 등록을 요청하세요.";
    list.innerHTML = `<div class="empty">상위노출을 확인할 검색어가 없습니다.<br><span class="muted">${how}</span></div>`;
    return;
  }
  if (!groups.length) {
    list.innerHTML = `<div class="empty">아직 확인 기록이 없습니다.<br><span class="muted">[지금 확인]을 누르면 검색어별로 지금 상위에 노출된 지식iN 글을 찾아옵니다.</span></div>`;
    return;
  }
  if (state.expMode === "views") {
    const posts = flatExposure(groups);
    list.innerHTML = posts.length
      ? `<div class="flat-note muted">검색어 ${groups.length}개에서 찾은 글 ${posts.length}개 · 조회수 높은 순${posts.length >= FLAT_LIMIT ? ` (상위 ${FLAT_LIMIT}개)` : ""}</div>`
        + posts.map((q) => exposureCardHtml(q).replace('<div class="ranks">',
          `<div class="kw-chips">${q.keywords.slice(0, 4).map((k) => `<span class="kw-chip">🔎 ${esc(k)}</span>`).join("")}${q.keywords.length > 4 ? `<span class="muted">외 ${q.keywords.length - 4}개</span>` : ""}</div><div class="ranks">`)).join("")
      : `<div class="empty">${state.expUnanswered ? "남은 글이 없습니다 (모두 처리함)" : "아직 찾은 글이 없습니다"}</div>`;
    return;
  }
  list.innerHTML = groups.map((g) => {
    const p = productById(g.product);
    const srcs = meta.exposure.sources.map((s) => {
      const info = g.sources[s.id];
      if (!info) return "";
      if (info.error) return `<span class="src err" title="${esc(info.error)}">${esc(s.name)} 오류</span>`;
      return `<span class="src">${esc(s.name)} ${info.count}개</span>`;
    }).join("");
    const body = g.posts.length
      ? g.posts.map(exposureCardHtml).join("")
      : `<div class="empty small">${state.expUnanswered ? "남은 글이 없습니다 (모두 처리함)" : "지금 이 검색어로 노출되는 지식iN 글이 없습니다"}</div>`;
    return `
    <section class="kw-group" style="${p ? `--c:${esc(p.color)}` : ""}">
      <header class="kw-head">
        <h3><a href="https://search.naver.com/search.naver?query=${encodeURIComponent(g.keyword)}" target="_blank" rel="noopener" title="네이버에서 직접 검색해 보기">${esc(g.keyword)}</a></h3>
        ${p && !state.product ? `<span class="badge product">${esc(p.name)}</span>` : ""}
        <span class="muted">확인 ${relTime(g.checked_at)}</span>
        <span class="srcs">${srcs}</span>
      </header>
      ${body}
    </section>`;
  }).join("");
}

// ---------------------------------------------------------------- 동작
async function setStatus(card, status) {
  const id = card.dataset.id;
  const body = { status };
  const ta = card.querySelector(".draft textarea");
  if (ta && ta.value.trim()) {
    // 초안 칸에서 고친 최종본을 함께 보냄 → 관리자 [답변 예시]의 후보로 쌓임
    clearTimeout(draftSaveTimers[id]);
    body.draft = ta.value;
  }
  const prev = card.dataset.status === "opened" ? "opened" : "new";
  const nth = myToday() + 1;
  await api(itemPath(card, "/status"), body);
  const noun = card.dataset.kind === "social" ? "댓글" : "답변";
  // 완료·건너뛰기는 [되돌리기]로 바로 취소할 수 있게
  const undo = status === "answered" || status === "skipped"
    ? async () => {
      await api(itemPath(card, "/status"), { status: prev });
      toast("되돌렸어요. 다시 할 일에 있습니다");
      await loadMeta();
      await loadList();
    }
    : null;
  const msg = status === "answered" ? `✓ ${noun} 완료 — 오늘 ${nth}건째예요. 내일 아침 노출 여부를 확인할게요`
    : status === "skipped" ? "건너뛰었어요" : "할 일로 되돌렸어요";
  toast(msg, undo);
  if (state.view === "exposure") {
    // 같은 글이 여러 검색어에 걸쳐 있을 수 있으니 목록을 다시 그림
    loadMeta();
    return loadExposure();
  }
  const listStatus = isSocial() ? state.sStatus : state.status;
  if (status === "opened" && listStatus === "todo") {
    markOpened(card);
  } else if (listStatus !== "all") {
    card.classList.add("leaving");
    setTimeout(() => card.remove(), 180);
  } else {
    card.dataset.status = status;
    refreshActions(card);
  }
  loadMeta();
}

async function draft(card, btn, regen) {
  const box = card.querySelector(".draft");
  const ta = box.querySelector("textarea");
  if (!regen && ta.value.trim()) {
    box.hidden = !box.hidden;
    refreshActions(card);
    return;
  }
  const label = btn.textContent;
  let ok = false;
  btn.disabled = true;
  btn.textContent = "작성 중…";
  try {
    const data = await api(itemPath(card, "/draft"), {});
    ta.value = data.draft;
    const slot = box.querySelector(".calc-slot");
    if (slot) slot.innerHTML = calcBoxHtml(data.calc);
    box.hidden = false;
    refreshBytes(card);
    ok = true;
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
    if (ok) {
      card.dataset.hasDraft = "1";
      refreshActions(card);
      ta.focus();
    }
  }
}

// 초안 칸에서 고친 내용은 잠시 뒤 서버에 저장 (새로고침해도 남고, 답변 예시의 최종본이 됨)
const draftSaveTimers = {};
function saveDraftSoon(card, delay = 1500) {
  const id = card.dataset.id;
  const ta = card.querySelector(".draft textarea");
  clearTimeout(draftSaveTimers[id]);
  draftSaveTimers[id] = setTimeout(() => {
    delete draftSaveTimers[id];
    if (ta.value.trim()) api(itemPath(card, "/draft/save"), { draft: ta.value }).catch(() => {});
  }, delay);
}

function refreshBytes(card) {
  const box = card.querySelector(".bytes-box");
  if (box) box.innerHTML = bytesHtml(card.querySelector(".draft textarea").value, Number(box.dataset.limit));
}

document.addEventListener("input", (ev) => {
  const ta = ev.target.closest(".draft textarea");
  if (ta) {
    saveDraftSoon(ta.closest(".card"));
    refreshBytes(ta.closest(".card"));
  }
});

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
}

document.addEventListener("click", async (ev) => {
  const close = ev.target.closest("[data-dismiss]");
  if (close) {
    dismiss(close.dataset.dismiss);
    close.closest(".notice")?.remove();
    return;
  }
  // 관리 메뉴는 바깥을 누르면 닫힘
  const menu = $("#menu");
  if (menu && menu.open && !ev.target.closest("#menu")) menu.open = false;
  const view = ev.target.closest(".view, .subview");
  if (view) {
    const next = view.dataset.view || (view.dataset.group === "kin" ? state.kinView : view.dataset.group);
    if (next === state.view) return;
    state.view = next;
    if (groupOf(next) === "kin") state.kinView = next;
    saveFilters(); renderViews(); renderTabs(); renderChips(); renderNotice(); renderSocialBar();
    $("#list").innerHTML = "";
    loadList();
    return;
  }
  const tab = ev.target.closest(".tab");
  if (tab) {
    state.product = tab.dataset.product;
    state.category = "";
    saveFilters(); renderTabs(); renderChips(); loadList();
    return;
  }
  const chip = ev.target.closest(".chip");
  if (chip) {
    state.category = chip.dataset.category;
    saveFilters(); renderChips(); loadList();
    return;
  }
  const card = ev.target.closest(".card");
  if (!card) return;
  if (card.classList.contains("result")) {
    const act = ev.target.closest("[data-act]")?.dataset.act;
    if (act === "toggle-text") card.querySelector(".draft").hidden = !card.querySelector(".draft").hidden;
    if (act === "save-text") {
      const text = card.querySelector(".draft textarea").value;
      if (!text.trim()) return toast("내용을 붙여넣어 주세요");
      try {
        await api(itemPath(card, "/draft/save"), { draft: text });
        toast("저장했습니다. 다음 확인 때 이 내용으로 찾습니다");
      } catch (e) { toast(e.message); }
    }
    return;
  }
  if (ev.target.closest("[data-open]")) {
    const todo = state.view === "exposure" ? !card.classList.contains("done") : (isSocial() ? state.sStatus : state.status) === "todo";
    if (!card.classList.contains("opened") && todo) {
      api(itemPath(card, "/status"), { status: "opened" }).then(() => markOpened(card)).catch(() => {});
    }
    return; // 링크는 새 탭으로 열림
  }
  const btn = ev.target.closest("[data-act]");
  if (!btn) return;
  const act = btn.dataset.act;
  try {
    if (act === "answered" || act === "skipped" || act === "opened") await setStatus(card, act);
    else if (act === "draft") await draft(card, btn, false);
    else if (act === "regen") await draft(card, btn, true);
    else if (act === "copy" || act === "copy-open") {
      saveDraftSoon(card, 0);
      await copyText(card.querySelector(".draft textarea").value);
      const noun = card.dataset.kind === "social" ? "댓글" : "답변";
      if (act === "copy-open") {
        window.open(card.querySelector(".title").href, "_blank", "noopener");
        toast(`복사했어요. 새 창에 붙여넣고 등록한 뒤, 여기서 [✓ ${noun} 달았어요]를 눌러주세요`);
        if (state.view === "exposure" || (isSocial() ? state.sStatus : state.status) === "todo") {
          api(itemPath(card, "/status"), { status: "opened" }).then(() => markOpened(card)).catch(() => {});
        }
      } else {
        toast("복사했어요");
      }
    }
  } catch (e) {
    toast(e.message);
  }
});

$("#collect-btn").addEventListener("click", async () => {
  try {
    const r = await api("/api/collect", {});
    toast(r.started ? (meta.running ? "지금 작업이 끝나면 이어서 수집합니다" : "수집을 시작했습니다") : "이미 수집 중입니다");
    wasRunning = true;
    setTimeout(loadMeta, 800);
  } catch (e) {
    toast(e.message);
  }
});

$("#social-collect-btn").addEventListener("click", async () => {
  try {
    if (state.view === "cafe") {
      const r = await api("/api/cafe/collect", {});
      toast(r.started ? (meta.running ? "지금 작업이 끝나면 이어서 찾습니다" : "네이버 카페에서 찾기 시작했습니다") : "이미 찾는 중입니다");
      wasRunning = true;
      setTimeout(loadMeta, 800);
      return;
    }
    const r = await api("/api/social/collect", {});
    toast(r.started ? (meta.running ? "지금 작업이 끝나면 이어서 찾습니다" : "유튜브·쓰레드에서 찾기 시작했습니다") : "이미 찾는 중입니다");
    wasRunning = true;
    setTimeout(loadMeta, 800);
  } catch (e) {
    toast(e.message);
  }
});

$("#results-check-btn").addEventListener("click", async () => {
  try {
    const r = await api("/api/results/check", {});
    toast(r.started ? (meta.running ? "지금 작업이 끝나면 이어서 확인합니다" : "확인을 시작했습니다") : "이미 확인 중입니다");
    wasRunning = true;
    setTimeout(loadMeta, 800);
  } catch (e) {
    toast(e.message);
  }
});

$("#exp-check-btn").addEventListener("click", async () => {
  try {
    const r = await api("/api/exposure/check", {});
    toast(r.started ? (meta.running ? "지금 작업이 끝나면 이어서 확인합니다" : "상위노출 확인을 시작했습니다") : "이미 확인 중입니다");
    wasRunning = true;
    setTimeout(loadMeta, 800);
  } catch (e) {
    toast(e.message);
  }
});

// [할 일 | 완료 | 건너뜀 | 전체] 같은 버튼 묶음
function bindSeg(sel) {
  const el = $(sel);
  const key = el.dataset.key;
  const paint = () => el.querySelectorAll("button").forEach((b) => {
    const on = b.dataset.v === (state[key] || "");
    b.classList.toggle("on", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
  paint();
  el.addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    state[key] = b.dataset.v;
    paint();
    saveFilters();
    loadList();
  });
}

function bindFilter(sel, key, isCheck) {
  const el = $(sel);
  if (isCheck) el.checked = !!state[key]; else el.value = state[key];
  el.addEventListener(isCheck ? "change" : "input", () => {
    state[key] = isCheck ? el.checked : el.value;
    saveFilters();
    clearTimeout(listTimer);
    listTimer = setTimeout(loadList, key === "q" || key === "sQ" ? 250 : 0);
  });
}

// ---------------------------------------------------------------- 시작
(async function init() {
  loadFilters();
  bindSeg("#f-status");
  bindFilter("#f-sort", "sort");
  bindFilter("#f-unanswered", "unanswered", true);
  bindFilter("#f-low", "low", true);
  bindFilter("#f-q", "q");
  bindFilter("#x-unanswered", "expUnanswered", true);
  bindFilter("#x-mode", "expMode");
  bindSeg("#s-status");
  $("#s-sort").addEventListener("input", (ev) => {
    state[sortKey()] = ev.target.value;
    saveFilters();
    loadList();
  });
  bindFilter("#s-kind", "ytKind");
  bindFilter("#r-platform", "rPlatform");
  bindSeg("#r-state");
  bindFilter("#r-by", "rBy");
  bindFilter("#s-low", "sLow", true);
  bindFilter("#s-q", "sQ");
  if (!["feed", "exposure", "youtube", "threads", "cafe", "results"].includes(state.view)) state.view = "feed";
  if (!["feed", "exposure"].includes(state.kinView)) state.kinView = "feed";
  renderViews();
  try {
    await loadMeta();
    if (state.product && !productById(state.product)) { state.product = ""; state.category = ""; renderTabs(); renderChips(); }
    await loadList();
  } catch (e) {
    $("#list").innerHTML = `<div class="empty">서버에 연결할 수 없습니다: ${esc(e.message)}</div>`;
  }
  // 수집 중이면 3초마다, 아니면 1분마다 새로고침
  (function tick() {
    const delay = meta && meta.running ? 3000 : 60000;
    setTimeout(async () => {
      try {
        const running = meta && meta.running;
        await loadMeta();
        if (!running && document.visibilityState === "visible" && !document.querySelector(".draft:not([hidden])")) await loadList();
      } catch (e) { /* 서버가 잠시 꺼져 있어도 계속 시도 */ }
      tick();
    }, delay);
  })();
})();
