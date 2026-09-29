"use strict";

const $ = (sel) => document.querySelector(sel);
const STORE_KEY = "jisikin.filters";

const state = {
  view: "feed", // feed: 새 질문 / exposure: 상위노출 글 (둘 다 지식iN) / youtube · threads: 유튜브·쓰레드
  kinView: "feed", // 지식iN 탭에서 마지막으로 본 화면
  product: "", category: "", status: "todo", sort: "priority",
  unanswered: false, low: false, q: "", expUnanswered: false,
  sStatus: "todo", sSort: "priority", ytSort: "views", ytKind: "", sLow: false, sQ: "",
};
const PLATFORM_NAMES = { youtube: "유튜브", threads: "쓰레드" };
// 유튜브는 조회수 많은 순(= 사람들이 많이 보는 영상)이 기본
const SORT_OPTIONS = {
  youtube: [["views", "조회수 많은 순"], ["latest", "최신순"]],
  threads: [["priority", "추천순"], ["latest", "최신순"]],
};
const isSocial = () => state.view === "youtube" || state.view === "threads";
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
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2500);
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
  const n = { kin: sum(meta.counts.products), youtube: sum(meta.social.counts.youtube), threads: sum(meta.social.counts.threads) };
  for (const [k, v] of Object.entries(n)) {
    const el = $(`#cnt-${k}`);
    el.textContent = v ? num(v) : "";
    el.title = v ? `처리할 글 ${num(v)}개` : "";
  }
}

function renderStatus() {
  // 상단: 오늘 실적 (지식iN 답변 + 유튜브·쓰레드 댓글)
  const head = [];
  if (meta.running) {
    const label = { exposure: "상위노출 확인 중…", social: "유튜브·쓰레드 찾는 중…", keywords: "검색어 만드는 중…" }[meta.running_kind] || "지식iN 수집 중…";
    head.push(`<b>${label}</b>`);
  }
  const today = meta.answer_stats.reduce((a, s) => a + s.today, 0);
  const who = meta.auth ? meta.answer_stats.filter((s) => s.today).map((s) => `${esc(s.name)} ${s.today}`).join(", ") : "";
  head.push(`오늘 답변·댓글 <b>${today}</b>건${who ? ` (${who})` : ""}`);
  $("#run-status").innerHTML = head.join(" · ");

  // 새 질문 도구줄: 지식iN 수집 상태
  const r = meta.last_run;
  const parts = [];
  parts.push(meta.mode === "api" ? "네이버 검색 API" : "웹 검색 모드");
  if (meta.running && meta.running_kind === "collect") parts.push("<b>수집 중…</b>");
  else if (r) {
    parts.push(`마지막 수집 ${relTime(r.finished_at || r.started_at)} · 새 관련 질문 ${r.new_relevant}건`);
  } else parts.push("아직 수집 기록이 없습니다");
  if (!meta.running && meta.next_run_at) parts.push(`다음 수집 ${untilTime(meta.next_run_at)}`);
  if (!meta.interval) parts.push("자동 수집 꺼짐");
  $("#feed-status").innerHTML = parts.join(" · ");
  const btn = $("#collect-btn");
  const collecting = meta.running && meta.running_kind === "collect";
  btn.disabled = collecting;
  btn.textContent = collecting ? "수집 중…" : "지금 수집";
}

function renderExposureBar() {
  const x = meta.exposure;
  const parts = [];
  if (meta.running && meta.running_kind === "exposure") parts.push("<b>확인 중…</b> (검색어가 많으면 몇 분 걸립니다)");
  else if (x.last_run) parts.push(`마지막 확인 ${relTime(x.last_run.finished_at || x.last_run.started_at)}`);
  else parts.push("아직 확인 기록이 없습니다");
  if (x.interval_hours) parts.push(`${x.interval_hours}시간마다 자동 확인${x.next_at && !meta.running ? ` (다음 ${untilTime(x.next_at)})` : ""}`);
  parts.push(`검색어 ${x.keywords}개`);
  $("#exp-status").innerHTML = parts.join(" · ");
  const btn = $("#exp-check-btn");
  const checking = meta.running && meta.running_kind === "exposure";
  btn.disabled = checking || !x.keywords;
  btn.textContent = checking ? "확인 중…" : "지금 확인";
}

function renderSocialBar() {
  const x = meta.social;
  const parts = [];
  const on = x.platforms[state.view];
  if (meta.running && meta.running_kind === "social") parts.push("<b>찾는 중…</b>");
  else if (x.last_run) parts.push(`마지막 ${relTime(x.last_run.finished_at || x.last_run.started_at)}`);
  else parts.push("아직 찾은 기록이 없습니다");
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

function renderTabs() {
  const counts = state.view === "exposure" ? exposureCounts() : isSocial() ? (meta.social.counts[state.view] || {}) : meta.counts.products;
  const all = Object.values(counts).reduce((a, p) => ({ total: a.total + p.total, new: a.new + p.new }), { total: 0, new: 0 });
  const tabs = [{ id: "", name: "전체", color: "", c: all }]
    .concat(meta.products.map((p) => ({ ...p, c: counts[p.id] || { total: 0, new: 0 } })));
  $("#tabs").innerHTML = tabs.map((t) => `
    <button class="tab ${state.product === t.id ? "active" : ""}" data-product="${esc(t.id)}" style="${t.color ? `--c:${esc(t.color)}` : ""}">
      ${t.color ? '<span class="dot"></span>' : ""}${esc(t.name)}
      <span class="n">${t.c.total}</span>${t.c.new ? `<span class="new">${t.c.new}</span>` : ""}
    </button>`).join("");
}

function renderChips() {
  const p = productById(state.product);
  if (!p || state.view === "exposure") { $("#chips").innerHTML = ""; return; }
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
  const r = state.view === "exposure" ? meta.exposure.last_run : meta.last_run;
  const what = state.view === "exposure" ? "상위노출 확인" : "수집";
  const notes = [];
  if (r && r.errors && r.errors.length) {
    notes.push(`<b>마지막 ${what}에서 오류 ${r.errors.length}건</b><ul>${r.errors.slice(0, 5).map((e) => `<li>${esc(e)}</li>`).join("")}</ul>`);
  }
  if (meta.mode === "web" && state.view === "feed") {
    notes.push("네이버 검색 API 키가 없어 <b>웹 검색 모드</b>로 동작 중입니다. 더 빠르고 안정적으로 수집하려면 <a href='/settings'>설정</a>에서 네이버 API 키를 붙여넣으세요. (발급 방법도 거기 있어요)");
  }
  $("#notice").innerHTML = notes.map((n) => `<div class="notice">${n}</div>`).join("");
}

function renderSocialNotice() {
  const name = PLATFORM_NAMES[state.view];
  const notes = [];
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

// ---------------------------------------------------------------- 질문 목록
async function loadList() {
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
    list.innerHTML = `<div class="empty">${state.status === "todo" ? "처리할 질문이 없습니다." : "질문이 없습니다."}<br><span class="muted">조건을 바꾸거나 [지금 수집]을 눌러보세요.</span></div>`;
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
  <article class="card ${low ? "low" : ""} ${q.status === "opened" ? "opened" : ""}" data-id="${esc(q.doc_id)}" style="${color ? `--c:${esc(color)}` : ""}">
    <div class="card-main">
      <a class="title" href="${esc(q.url)}" target="_blank" rel="noopener" data-open>
        ${isNew ? '<span class="new-badge">NEW</span>' : ""}${highlight(q.title, terms)}
      </a>
      ${snippet ? `<p class="snippet">${highlight(snippet, terms)}</p>` : ""}
      <div class="meta">
        ${productBadge(p, lowProduct)}
        ${cats.map((c) => `<span class="badge">${esc(c)}</span>`).join("")}
        ${ansBadge}
        ${q.reward ? `<span class="badge">내공 ${q.reward}</span>` : ""}
        <span>${time}</span>
        <span title="관련도 점수">점수 ${Math.round(bestMatch.score || q.score || 0)}</span>
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
  if (q.status === "skipped") return `<b>제외함${by}</b>`;
  // 다른 사람이 이미 열어본 글: 같은 질문에 두 명이 답하지 않도록 표시
  if (q.status === "opened" && meta.auth && q.status_by && q.status_by !== meta.user.username) {
    return `<span class="badge busy" title="${esc(personName(q.status_by))} 님이 이 글을 열어봤습니다">${esc(personName(q.status_by))} 확인 중</span>`;
  }
  return "";
}

function answerBadge(q) {
  const ans = q.answer_count;
  if (ans === 0) return `<span class="badge zero">답변 0</span>`;
  if (ans != null) return `<span class="badge ${ans >= 5 ? "many" : ""}">답변 ${ans}</span>`;
  return `<span class="badge" title="상세 정보를 아직 못 가져왔습니다">답변 ?</span>`;
}

function actionsHtml(q, social) {
  const actions = [];
  const done = social ? "댓글완료" : "답변완료";
  if (q.status === "new" || q.status === "opened") {
    actions.push(`<button class="btn small" data-act="answered" title="${social ? "댓글을" : "답변을"} 달았으면 눌러주세요">✓ ${done}</button>`);
    actions.push(`<button class="btn small" data-act="skipped" title="${social ? "댓글을 달지 않을" : "답변하지 않을"} 글">제외</button>`);
  } else {
    actions.push(`<button class="btn small" data-act="opened">할 일로</button>`);
  }
  if (meta.ai.enabled) {
    actions.push(`<button class="btn small" data-act="draft">${q.draft ? "초안 보기" : social ? "AI 댓글" : "AI 초안"}</button>`);
  }
  return actions.join("");
}

function draftBoxHtml(q, social) {
  const what = social ? (q.platform === "youtube" ? "영상" : "글") : "질문";
  return `
      <div class="draft" hidden>
        <textarea spellcheck="false">${esc(q.draft || "")}</textarea>
        <div class="row">
          <button class="btn small primary" data-act="copy-open">복사하고 ${what} 열기</button>
          <button class="btn small" data-act="copy">복사</button>
          <button class="btn small" data-act="regen">다시 작성</button>
          <span class="hint">AI 초안입니다. 내용을 확인·수정한 뒤 직접 등록하세요.</span>
        </div>
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
    const empty = state.sStatus === "todo" ? "처리할 글이 없습니다." : "글이 없습니다.";
    list.innerHTML = `<div class="empty">${empty}<br><span class="muted">${meta.social.platforms[view] ? "[지금 찾기]를 누르거나 조건을 바꿔보세요." : "키를 넣으면 여기에 쌓입니다."}</span></div>`;
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
  const author = q.author ? (q.author_url ? `<a href="${esc(q.author_url)}" target="_blank" rel="noopener">${yt ? "" : "@"}${esc(q.author)}</a>` : esc(q.author)) : "";
  return `
  <article class="card social ${yt ? "yt" : "th"} ${low ? "low" : ""} ${q.status === "opened" ? "opened" : ""}" data-kind="social" data-id="${esc(q.post_id)}" style="${color ? `--c:${esc(color)}` : ""}">
    ${yt && q.thumbnail ? `<a class="thumb" href="${esc(q.url)}" target="_blank" rel="noopener" data-open><img src="${esc(q.thumbnail)}" alt="" loading="lazy">${q.duration ? `<span class="dur">${durationText(q.duration)}</span>` : ""}</a>` : ""}
    <div class="card-main">
      <a class="title" href="${esc(q.url)}" target="_blank" rel="noopener" data-open>
        ${isNew ? '<span class="new-badge">NEW</span>' : ""}${q.is_short ? '<span class="shorts-badge">숏츠</span>' : ""}${highlight(title || "(내용 없음)", terms)}
      </a>
      ${body.trim() ? `<p class="snippet">${highlight(body.trim(), terms)}</p>` : ""}
      <div class="meta">
        ${productBadge(p, lowProduct)}
        ${cats.map((c) => `<span class="badge">${esc(c)}</span>`).join("")}
        ${stats}
        ${author ? `<span>${author}</span>` : ""}
        <span>${q.published_at ? relTime(q.published_at) : `수집 ${relTime(q.first_seen)}`}</span>
        <span title="이 글을 찾은 검색어">🔎 ${esc((q.queries || []).join(", "))}</span>
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

function exposureCardHtml(q) {
  const p = productById(q.product);
  const terms = (q.matches || []).flatMap((m) => m.terms || []);
  const snippet = q.body || q.snippet || "";
  const time = q.asked_at ? `작성 ${relTime(q.asked_at)}` : "";
  const done = q.status === "answered" || q.status === "skipped";
  return `
  <article class="card exp ${done ? "done" : ""} ${q.status === "opened" ? "opened" : ""}" data-id="${esc(q.doc_id)}" style="${p ? `--c:${esc(p.color)}` : ""}">
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
  await api(itemPath(card, "/status"), body);
  if (state.view === "exposure") {
    // 같은 글이 여러 검색어에 걸쳐 있을 수 있으니 목록을 다시 그림
    toast(status === "answered" ? "답변완료로 표시했습니다" : status === "skipped" ? "제외했습니다" : "할 일로 되돌렸습니다");
    loadMeta();
    return loadExposure();
  }
  const listStatus = isSocial() ? state.sStatus : state.status;
  if (status === "opened" && listStatus === "todo") {
    card.classList.add("opened");
  } else if (listStatus !== "all") {
    card.remove();
  }
  if (status === "answered") toast(isSocial() ? "댓글완료로 표시했습니다" : "답변완료로 표시했습니다");
  if (status === "skipped") toast("제외했습니다");
  if (status === "opened" && listStatus !== "todo") toast("할 일로 되돌렸습니다");
  loadMeta();
}

async function draft(card, btn, regen) {
  const box = card.querySelector(".draft");
  const ta = box.querySelector("textarea");
  if (!regen && ta.value.trim()) {
    box.hidden = !box.hidden;
    return;
  }
  const label = btn.textContent;
  let ok = false;
  btn.disabled = true;
  btn.textContent = "작성 중…";
  try {
    const data = await api(itemPath(card, "/draft"), {});
    ta.value = data.draft;
    box.hidden = false;
    ok = true;
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
    const main = card.querySelector('[data-act="draft"]');
    if (ok && main) main.textContent = "초안 보기";
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

document.addEventListener("input", (ev) => {
  const ta = ev.target.closest(".draft textarea");
  if (ta) saveDraftSoon(ta.closest(".card"));
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
  const view = ev.target.closest(".view, .subview");
  if (view) {
    const next = view.dataset.view || (view.dataset.group === "kin" ? state.kinView : view.dataset.group);
    if (next === state.view) return;
    state.view = next;
    if (groupOf(next) === "kin") state.kinView = next;
    saveFilters(); renderViews(); renderTabs(); renderChips(); renderNotice();
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
  if (ev.target.closest("[data-open]")) {
    const todo = state.view === "exposure" ? !card.classList.contains("done") : (isSocial() ? state.sStatus : state.status) === "todo";
    if (!card.classList.contains("opened") && todo) {
      api(itemPath(card, "/status"), { status: "opened" }).then(() => card.classList.add("opened")).catch(() => {});
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
      toast("초안을 복사했습니다");
      if (act === "copy-open") {
        window.open(card.querySelector(".title").href, "_blank", "noopener");
        if (state.view === "exposure" || (isSocial() ? state.sStatus : state.status) === "todo") {
          api(itemPath(card, "/status"), { status: "opened" }).catch(() => {});
        }
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
    const r = await api("/api/social/collect", {});
    toast(r.started ? (meta.running ? "지금 작업이 끝나면 이어서 찾습니다" : "유튜브·쓰레드에서 찾기 시작했습니다") : "이미 찾는 중입니다");
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
  bindFilter("#f-status", "status");
  bindFilter("#f-sort", "sort");
  bindFilter("#f-unanswered", "unanswered", true);
  bindFilter("#f-low", "low", true);
  bindFilter("#f-q", "q");
  bindFilter("#x-unanswered", "expUnanswered", true);
  bindFilter("#s-status", "sStatus");
  $("#s-sort").addEventListener("input", (ev) => {
    state[sortKey()] = ev.target.value;
    saveFilters();
    loadList();
  });
  bindFilter("#s-kind", "ytKind");
  bindFilter("#s-low", "sLow", true);
  bindFilter("#s-q", "sQ");
  if (!["feed", "exposure", "youtube", "threads"].includes(state.view)) state.view = "feed";
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
