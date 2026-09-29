"use strict";

const $ = (sel) => document.querySelector(sel);
let data = null;
let current = "";

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function day(iso) { return iso ? iso.slice(0, 10) : ""; }
let toastTimer = null;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2500);
}
async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json", "X-Jisikin": "1" }, body: JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  let d = {};
  try { d = await res.json(); } catch (e) { /* 무시 */ }
  if (!res.ok) throw new Error(d.error || `요청 실패 (${res.status})`);
  return d;
}

async function load() {
  data = await api("/api/examples");
  if (!current || !data.products.find((p) => p.id === current)) {
    try { current = localStorage.getItem("jisikin.exProduct") || ""; } catch (e) { /* 무시 */ }
    if (!data.products.find((p) => p.id === current)) current = data.products[0]?.id || "";
  }
  render();
}

function cardHtml(ex) {
  const src = ex.source === "manual"
    ? '<span class="badge">직접 추가</span>'
    : ex.edited ? '<span class="badge">직원이 고쳐서 올림</span>' : '<span class="badge">AI 초안 그대로</span>';
  const who = ex.created_by_name ? ` · ${esc(ex.created_by_name)}` : "";
  const title = ex.title
    ? (ex.url ? `<a href="${esc(ex.url)}" target="_blank" rel="noopener">${esc(ex.title)}</a>` : esc(ex.title))
    : '<span class="muted">(질문 없이 답변만)</span>';
  return `
  <article class="card ex-card ${ex.starred ? "starred" : ""}" data-id="${ex.id}">
    <div class="card-main">
      <div class="ex-title">${title}</div>
      ${ex.question ? `<p class="snippet">${esc(ex.question)}</p>` : ""}
      <textarea class="ex-answer" spellcheck="false">${esc(ex.answer)}</textarea>
      <div class="meta">${src}<span>${day(ex.created_at)}${who}</span><span>${ex.answer.length.toLocaleString("ko-KR")}자</span></div>
    </div>
    <div class="actions">
      <button class="btn small ${ex.starred ? "primary" : ""}" data-act="star">${ex.starred ? "⭐ 사용 중" : "☆ 예시로 사용"}</button>
      <button class="btn small" data-act="save" hidden>저장</button>
      <button class="btn small" data-act="delete">삭제</button>
    </div>
  </article>`;
}

function render() {
  $("#ex-note").innerHTML = data.ai ? "" :
    '<p class="err" style="font-size:13px;margin:8px 0 0">Claude API 키가 없어 AI 초안이 꺼져 있습니다. [설정] → API 키에서 넣으면 여기의 ⭐ 예시가 초안에 쓰입니다.</p>';

  $("#ex-tabs").innerHTML = data.products.map((p) => `
    <button class="tab ${p.id === current ? "active" : ""}" data-product="${esc(p.id)}" style="--c:${esc(p.color)}">
      <span class="dot"></span>${esc(p.name)} <span class="n">⭐ ${p.starred}/${data.max}</span></button>`).join("");

  const p = data.products.find((x) => x.id === current);
  if (!p) { $("#ex-body").innerHTML = ""; return; }
  const mine = data.items.filter((x) => x.product === p.id);
  const starred = mine.filter((x) => x.starred);
  const rest = mine.filter((x) => !x.starred);
  const full = p.starred >= data.max;

  $("#ex-body").innerHTML = `
    <div class="panel" style="--c:${esc(p.color)}">
      <h2>⭐ AI 초안이 참고하는 예시 <span class="muted">${starred.length} / ${data.max}</span></h2>
      ${starred.length ? starred.map(cardHtml).join("") :
        `<div class="empty small">아직 없습니다. 아래 '직원이 올린 답변'에서 ⭐를 달거나, 잘 쓴 답변을 직접 추가하세요.
          <br>예시가 없어도 초안은 [설정]의 답변 가이드대로 만들어집니다.</div>`}
    </div>

    <details class="panel" ${starred.length ? "" : "open"}>
      <summary><b>잘 쓴 답변 직접 추가</b> <span class="muted">— 예전에 채택된 답변 등을 붙여넣으세요</span></summary>
      <div class="ex-form">
        <input id="ex-title" placeholder="질문 제목 (선택)" maxlength="200">
        <textarea id="ex-question" rows="2" placeholder="질문 내용 (선택)" maxlength="1000"></textarea>
        <textarea id="ex-new" rows="8" placeholder="답변 (필수, 20자 이상)" maxlength="5000"></textarea>
        <div><button class="btn primary small" id="ex-add">추가</button>
          <span class="muted" style="font-size:12px">${full ? `⭐가 ${data.max}개라 후보로만 추가됩니다.` : "추가하면 바로 ⭐ 예시로 쓰입니다."}</span></div>
      </div>
    </details>

    <div class="panel">
      <h2>직원이 올린 답변 <span class="muted">${rest.length}개 · 최근 순</span></h2>
      ${rest.length ? rest.map(cardHtml).join("") :
        '<div class="empty small">직원이 AI 초안으로 답변하고 [✓ 답변완료]를 누르면 여기에 쌓입니다.</div>'}
    </div>`;
}

document.addEventListener("click", async (ev) => {
  const tab = ev.target.closest(".tab");
  if (tab) {
    current = tab.dataset.product;
    try { localStorage.setItem("jisikin.exProduct", current); } catch (e) { /* 무시 */ }
    render();
    return;
  }
  if (ev.target.id === "ex-add") {
    try {
      const r = await api("/api/examples/add", {
        product: current, title: $("#ex-title").value, question: $("#ex-question").value, answer: $("#ex-new").value,
      });
      toast(r.starred ? "추가했습니다 (⭐ 예시로 사용)" : "후보로 추가했습니다");
      await load();
    } catch (e) { toast(e.message); }
    return;
  }
  const btn = ev.target.closest("[data-act]");
  const card = ev.target.closest(".ex-card");
  if (!btn || !card) return;
  const id = card.dataset.id;
  const ex = data.items.find((x) => String(x.id) === id);
  try {
    if (btn.dataset.act === "star") {
      await api(`/api/examples/${id}/star`, { starred: !ex.starred });
      toast(ex.starred ? "⭐를 뺐습니다" : "⭐ 예시로 사용합니다");
      await load();
    } else if (btn.dataset.act === "save") {
      await api(`/api/examples/${id}/update`, { answer: card.querySelector(".ex-answer").value });
      toast("저장했습니다");
      await load();
    } else if (btn.dataset.act === "delete") {
      if (!confirm("이 답변을 지울까요?")) return;
      await api(`/api/examples/${id}/delete`, {});
      toast("지웠습니다");
      await load();
    }
  } catch (e) { toast(e.message); }
});

document.addEventListener("input", (ev) => {
  if (!ev.target.classList.contains("ex-answer")) return;
  const card = ev.target.closest(".ex-card");
  const ex = data.items.find((x) => String(x.id) === card.dataset.id);
  card.querySelector('[data-act="save"]').hidden = ev.target.value === ex.answer;
});

load().catch((e) => { $("#ex-body").innerHTML = `<div class="empty">불러오지 못했습니다: ${esc(e.message)}</div>`; });
