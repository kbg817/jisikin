"use strict";

const $ = (sel) => document.querySelector(sel);
let data = null;
let current = "";
let channel = "kin"; // kin: 지식iN 답변 / youtube: 유튜브 댓글
// 채널마다 화면에 쓰는 말
const WORDS = {
  kin: { q: "질문", a: "답변", done: "✓ 답변 달았어요", where: "지식iN" },
  youtube: { q: "영상", a: "댓글", done: "✓ 댓글 달았어요", where: "유튜브" },
  cafe: { q: "카페 글", a: "댓글", done: "✓ 댓글 달았어요", where: "카페" },
};
const W = () => WORDS[channel];

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
  try { channel = localStorage.getItem("jisikin.exChannel") || channel; } catch (e) { /* 무시 */ }
  if (!data.channels.find((c) => c.id === channel)) channel = "kin";
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
    : `<span class="muted">(${W().q} 없이 ${W().a}만)</span>`;
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

  const w = W();
  $("#ex-channels").innerHTML = data.channels.map((c) => {
    const n = data.items.filter((x) => x.channel === c.id && x.starred).length;
    return `<button type="button" data-channel="${esc(c.id)}" class="${c.id === channel ? "on" : ""}" aria-selected="${c.id === channel}">
      <span class="pf ${({ kin: "kin", cafe: "cf" })[c.id] || "yt"} mini">${({ kin: "N", cafe: "C" })[c.id] || "▶"}</span>${esc(c.name)} <span class="muted">⭐ ${n}</span></button>`;
  }).join("");

  $("#ex-tabs").innerHTML = data.products.map((p) => `
    <button class="tab ${p.id === current ? "active" : ""}" data-product="${esc(p.id)}" style="--c:${esc(p.color)}">
      <span class="dot"></span>${esc(p.name)} <span class="n">⭐ ${p.starred[channel] || 0}/${data.max}</span></button>`).join("");

  const p = data.products.find((x) => x.id === current);
  if (!p) { $("#ex-body").innerHTML = ""; return; }
  const mine = data.items.filter((x) => x.product === p.id && x.channel === channel);
  const starred = mine.filter((x) => x.starred);
  const rest = mine.filter((x) => !x.starred);
  const full = (p.starred[channel] || 0) >= data.max;
  const hint = channel === "kin"
    ? "예전에 채택된 답변 등을 붙여넣으세요"
    : channel === "cafe"
      ? "반응이 좋았던 우리 카페 댓글(글쓴이가 고맙다고 한 것 등)을 붙여넣으세요"
      : "반응이 좋았던 우리 댓글(좋아요·답글이 많이 달린 것 등)을 붙여넣으세요";

  $("#ex-body").innerHTML = `
    <div class="panel" style="--c:${esc(p.color)}">
      <h2>⭐ ${w.where} ${w.a} 초안이 참고하는 예시 <span class="muted">${starred.length} / ${data.max}</span></h2>
      ${starred.length ? starred.map(cardHtml).join("") :
        `<div class="empty small">아직 없습니다. 아래 '직원이 올린 ${w.a}'에서 ⭐를 달거나, 잘 쓴 ${w.a}을 직접 추가하세요.
          <br>예시가 없어도 초안은 [설정]의 가이드대로 만들어집니다.</div>`}
    </div>

    <details class="panel" ${starred.length ? "" : "open"}>
      <summary><b>잘 쓴 ${w.a} 직접 추가</b> <span class="muted">— ${hint}</span></summary>
      <div class="ex-form">
        <input id="ex-title" placeholder="${w.q} 제목 (선택)" maxlength="200">
        <textarea id="ex-question" rows="2" placeholder="${w.q} 내용 (선택)" maxlength="1000"></textarea>
        <textarea id="ex-new" rows="${channel === "kin" ? 8 : 4}" placeholder="${w.a} (필수, 20자 이상)" maxlength="5000"></textarea>
        <div><button class="btn primary small" id="ex-add">추가</button>
          <span class="muted" style="font-size:12px">${full ? `⭐가 ${data.max}개라 후보로만 추가됩니다.` : "추가하면 바로 ⭐ 예시로 쓰입니다."}</span></div>
      </div>
    </details>

    <div class="panel">
      <h2>직원이 올린 ${w.a} <span class="muted">${rest.length}개 · 최근 순</span></h2>
      ${rest.length ? rest.map(cardHtml).join("") :
        `<div class="empty small">직원이 AI 초안으로 ${w.a}을 달고 [${w.done}]를 누르면 여기에 쌓입니다.</div>`}
    </div>`;
}

document.addEventListener("click", async (ev) => {
  const ch = ev.target.closest("[data-channel]");
  if (ch) {
    channel = ch.dataset.channel;
    try { localStorage.setItem("jisikin.exChannel", channel); } catch (e) { /* 무시 */ }
    render();
    return;
  }
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
        product: current, channel, title: $("#ex-title").value, question: $("#ex-question").value, answer: $("#ex-new").value,
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
      if (!confirm(`이 ${W().a}을 지울까요?`)) return;
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
