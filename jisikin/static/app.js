"use strict";

const $ = (sel) => document.querySelector(sel);
const STORE_KEY = "jisikin.filters";

const state = {
  product: "", category: "", status: "todo", sort: "priority",
  unanswered: false, low: false, q: "",
};
let meta = null;
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
  let data = {};
  try { data = await res.json(); } catch (e) { /* 무시 */ }
  if (!res.ok) throw new Error(data.error || `요청 실패 (${res.status})`);
  return data;
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
  return `${Math.round(sec / 60)}분 후`;
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
  renderStatus();
  renderTabs();
  renderChips();
  renderNotice();
  if (wasRunning && !meta.running) loadList();
  wasRunning = meta.running;
  const newTotal = Object.values(meta.counts.products).reduce((a, p) => a + p.new, 0);
  document.title = (newTotal ? `(${newTotal}) ` : "") + "지식iN 질문 수집기";
}

function renderStatus() {
  const r = meta.last_run;
  const parts = [];
  parts.push(meta.mode === "api" ? "네이버 검색 API" : "웹 검색 모드");
  if (meta.running) parts.push("<b>수집 중…</b>");
  else if (r) {
    parts.push(`마지막 수집 ${relTime(r.finished_at || r.started_at)} · 새 관련 질문 ${r.new_relevant}건`);
  } else parts.push("아직 수집 기록이 없습니다");
  if (!meta.running && meta.next_run_at) parts.push(`다음 수집 ${untilTime(meta.next_run_at)}`);
  if (!meta.interval) parts.push("자동 수집 꺼짐");
  parts.push(`오늘 답변 ${meta.counts.answered_today}건`);
  $("#run-status").innerHTML = parts.join(" · ");
  const btn = $("#collect-btn");
  btn.disabled = meta.running;
  btn.textContent = meta.running ? "수집 중…" : "지금 수집";
}

function renderTabs() {
  const counts = meta.counts.products;
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
  if (!p) { $("#chips").innerHTML = ""; return; }
  const cc = meta.counts.products[p.id]?.categories || {};
  const chips = [{ name: "", label: "모든 카테고리" }].concat(p.categories.map((c) => ({ name: c, label: c })));
  $("#chips").innerHTML = chips.map((c) => `
    <button class="chip ${state.category === c.name ? "active" : ""}" data-category="${esc(c.name)}">
      ${esc(c.label)}${c.name ? `<span class="n">${cc[c.name] || 0}</span>` : ""}
    </button>`).join("");
}

function renderNotice() {
  const r = meta.last_run;
  const notes = [];
  if (r && r.errors && r.errors.length) {
    notes.push(`<b>마지막 수집에서 오류 ${r.errors.length}건</b><ul>${r.errors.slice(0, 5).map((e) => `<li>${esc(e)}</li>`).join("")}</ul>`);
  }
  if (meta.mode === "web") {
    notes.push("네이버 검색 API 키가 없어 <b>웹 검색 모드</b>로 동작 중입니다. 더 빠르고 안정적으로 수집하려면 <a href='/settings'>설정</a>에서 네이버 API 키를 붙여넣으세요. (발급 방법도 거기 있어요)");
  }
  $("#notice").innerHTML = notes.map((n) => `<div class="notice">${n}</div>`).join("");
}

// ---------------------------------------------------------------- 질문 목록
async function loadList() {
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

  const ans = q.answer_count;
  let ansBadge = `<span class="badge" title="상세 정보를 아직 못 가져왔습니다">답변 ?</span>`;
  if (ans === 0) ansBadge = `<span class="badge zero">답변 0</span>`;
  else if (ans != null) ansBadge = `<span class="badge ${ans >= 5 ? "many" : ""}">답변 ${ans}</span>`;

  const time = q.asked_at ? `작성 ${relTime(q.asked_at)}` : `수집 ${relTime(q.first_seen)}`;
  const snippet = q.body || q.snippet || "";
  const cats = (q.categories || []).length ? q.categories : (bestMatch.categories || []);
  const others = (q.matches || []).filter((m) => m.relevant && m.product_id !== q.product)
    .map((m) => productById(m.product_id)?.name).filter(Boolean);

  const actions = [];
  if (q.status === "new" || q.status === "opened") {
    actions.push(`<button class="btn small" data-act="answered" title="답변을 달았으면 눌러주세요">✓ 답변완료</button>`);
    actions.push(`<button class="btn small" data-act="skipped" title="답변하지 않을 질문">제외</button>`);
  } else {
    actions.push(`<button class="btn small" data-act="opened">할 일로</button>`);
  }
  if (meta.ai.enabled) {
    actions.push(`<button class="btn small" data-act="draft">${q.draft ? "초안 보기" : "AI 초안"}</button>`);
  }

  return `
  <article class="card ${low ? "low" : ""} ${q.status === "opened" ? "opened" : ""}" data-id="${esc(q.doc_id)}" style="${color ? `--c:${esc(color)}` : ""}">
    <div class="card-main">
      <a class="title" href="${esc(q.url)}" target="_blank" rel="noopener" data-open>
        ${isNew ? '<span class="new-badge">NEW</span>' : ""}${highlight(q.title, terms)}
      </a>
      ${snippet ? `<p class="snippet">${highlight(snippet, terms)}</p>` : ""}
      <div class="meta">
        ${p ? `<span class="badge product">${esc(p.name)}</span>` : `<span class="badge">관련도 낮음${lowProduct ? " · " + esc(lowProduct.name) : ""}</span>`}
        ${cats.map((c) => `<span class="badge">${esc(c)}</span>`).join("")}
        ${ansBadge}
        ${q.reward ? `<span class="badge">내공 ${q.reward}</span>` : ""}
        <span>${time}</span>
        <span title="관련도 점수">점수 ${Math.round(bestMatch.score || q.score || 0)}</span>
        ${others.length ? `<span>· ${esc(others.join(", "))}에도 해당</span>` : ""}
        ${q.status === "answered" ? "<b>답변완료</b>" : q.status === "skipped" ? "<b>제외함</b>" : ""}
      </div>
      <div class="draft" hidden>
        <textarea spellcheck="false">${esc(q.draft || "")}</textarea>
        <div class="row">
          <button class="btn small primary" data-act="copy-open">복사하고 질문 열기</button>
          <button class="btn small" data-act="copy">복사</button>
          <button class="btn small" data-act="regen">다시 작성</button>
          <span class="hint">AI 초안입니다. 내용을 확인·수정한 뒤 직접 등록하세요.</span>
        </div>
      </div>
    </div>
    <div class="actions">${actions.join("")}</div>
  </article>`;
}

// ---------------------------------------------------------------- 동작
async function setStatus(card, status) {
  const id = card.dataset.id;
  await api(`/api/questions/${id}/status`, { status });
  if (status === "opened" && state.status === "todo") {
    card.classList.add("opened");
  } else if (state.status !== "all") {
    card.remove();
  }
  if (status === "answered") toast("답변완료로 표시했습니다");
  if (status === "skipped") toast("제외했습니다");
  if (status === "opened" && state.status !== "todo") toast("할 일로 되돌렸습니다");
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
    const data = await api(`/api/questions/${card.dataset.id}/draft`, {});
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
    if (!card.classList.contains("opened") && state.status === "todo") {
      api(`/api/questions/${card.dataset.id}/status`, { status: "opened" }).then(() => card.classList.add("opened")).catch(() => {});
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
      await copyText(card.querySelector(".draft textarea").value);
      toast("초안을 복사했습니다");
      if (act === "copy-open") {
        window.open(card.querySelector(".title").href, "_blank", "noopener");
        if (state.status === "todo") api(`/api/questions/${card.dataset.id}/status`, { status: "opened" }).catch(() => {});
      }
    }
  } catch (e) {
    toast(e.message);
  }
});

$("#collect-btn").addEventListener("click", async () => {
  try {
    const r = await api("/api/collect", {});
    toast(r.started ? "수집을 시작했습니다" : "이미 수집 중입니다");
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
    listTimer = setTimeout(loadList, key === "q" ? 250 : 0);
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
