"use strict";

const $ = (sel) => document.querySelector(sel);
let data = null;
let current = "";
let pollTimer = null;
let removedOpen = false;  // [직접 뺀 검색어] 칸을 펼쳐 둔 상태 (다시 그려도 유지)

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function num(n) { return Number(n).toLocaleString("ko-KR"); }
function relTime(iso) {
  if (!iso) return "";
  const sec = (Date.now() - new Date(iso).getTime()) / 1000;
  if (sec < 60) return "방금";
  if (sec < 3600) return `${Math.floor(sec / 60)}분 전`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}시간 전`;
  return `${Math.floor(sec / 86400)}일 전`;
}
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
  data = await api("/api/keywords");
  if (!current || !data.products.find((p) => p.id === current)) {
    try { current = localStorage.getItem("jisikin.kwProduct") || ""; } catch (e) { /* 무시 */ }
    if (!data.products.find((p) => p.id === current)) current = data.products[0]?.id || "";
  }
  render();
  const busy = data.running || data.pending.length;
  const waiting = data.products.some((p) => p.seeds.length && !p.generated_at);  // 수집이 끝나면 만들 예정
  clearTimeout(pollTimer);
  if (busy || waiting) pollTimer = setTimeout(load, busy ? 3000 : 10000);
}

function render() {
  const s = data.sources;
  const src = (ok, name, hint) => `<span class="src ${ok ? "on" : ""}" title="${esc(hint)}">${ok ? "✓" : "–"} ${name}</span>`;
  $("#kw-sources").innerHTML = [
    src(s.autocomplete, "네이버 자동완성", "사람들이 실제로 치는 검색어"),
    src(true, "지역·의도 조합", "설정의 regions, 추천/원인/병원 등"),
    src(s.ai, "AI 추천", s.ai ? "Claude 가 질문형 검색어를 추측" : "설정 → API 키에 Claude 키를 넣으면 사용"),
    src(s.searchad, "월간 검색수", s.searchad ? "네이버 검색광고 API" : "설정 → 검색광고 API 키를 넣으면 실제 검색수로 순위를 매깁니다"),
  ].join("") + `<span class="muted" style="margin-left:6px">지금 확인 중인 검색어: 총 ${data.total_enabled}개 · ${data.exposure_hours ? data.exposure_hours + "시간마다" : "자동 확인 꺼짐"}</span>`;

  $("#kw-tabs").innerHTML = data.products.map((p) => {
    const on = p.auto.filter((k) => k.enabled).length + p.config_keywords.length;
    return `<button class="tab ${p.id === current ? "active" : ""}" data-product="${esc(p.id)}" style="--c:${esc(p.color)}">
      <span class="dot"></span>${esc(p.name)} <span class="n">${on}</span></button>`;
  }).join("");

  const p = data.products.find((x) => x.id === current);
  if (!p) { $("#kw-body").innerHTML = ""; return; }
  const busy = data.running || data.pending.includes(p.id);
  const enabled = p.auto.filter((k) => k.enabled).length;
  const hasVolume = p.auto.some((k) => k.pc != null || k.mobile != null);

  // 직접 끈 검색어(고정 + 꺼짐)는 표에서 빼서 맨 아래 접힌 칸으로
  const removed = p.auto.filter((k) => k.user_set && !k.enabled);
  const shown = p.auto.filter((k) => !(k.user_set && !k.enabled));
  const row = (k) => {
    const vol = (k.pc != null || k.mobile != null)
      ? `<b>${num((k.pc || 0) + (k.mobile || 0))}</b> <span class="muted">(PC ${num(k.pc || 0)} · 모바일 ${num(k.mobile || 0)})</span>`
      : `<span class="muted">-</span>`;
    const exp = k.exposure == null ? '<span class="muted">확인 전</span>' : k.exposure ? `<b>${k.exposure}개</b>` : '<span class="muted">없음</span>';
    return `<tr class="${k.enabled ? "" : "off"}">
      <td><input type="checkbox" data-kw="${esc(k.keyword)}" ${k.enabled ? "checked" : ""}></td>
      <td><a href="https://search.naver.com/search.naver?query=${encodeURIComponent(k.keyword)}" target="_blank" rel="noopener">${esc(k.keyword)}</a>
        ${k.user_set ? '<span class="badge" title="직접 켜거나 끈 검색어는 다시 생성해도 유지됩니다">고정</span>' : ""}</td>
      <td>${vol}</td>
      <td>${k.sources.map((x) => `<span class="badge">${esc(x)}</span>`).join(" ")}</td>
      <td>${exp}</td>
    </tr>`;
  };
  const rows = shown.map(row).join("");
  const head = "<tr><th>사용</th><th>검색어</th><th>월간 검색수</th><th>출처</th><th>지식iN 노출</th></tr>";

  $("#kw-body").innerHTML = `
    <div class="panel kw-form">
      <label>메인 키워드 <span class="muted">(쉼표로 여러 개, 최대 30개)</span>
        <input id="kw-seeds" value="${esc(p.seeds.join(", "))}" placeholder="예: 건선, 모공각화증">
      </label>
      <label>켜 둘 검색어 수
        <input id="kw-max" type="number" min="1" max="100" value="${p.max}">
      </label>
      <label title="검색광고 API 로 월간 검색수(PC+모바일)를 알 때, 이보다 적은 검색어는 켜지 않습니다">최소 월간 검색수
        <input id="kw-min" type="number" min="0" step="10" value="${p.min_volume}" ${data.sources.searchad ? "" : "disabled"}>
      </label>
      <button class="btn primary" id="kw-save" ${busy ? "disabled" : ""}>${busy ? "생성 중…" : "저장하고 자동 생성"}</button>
      <div class="muted kw-meta">
        ${busy ? "<b>검색어를 만드는 중입니다… (1분 정도, 끝나면 상위노출 확인까지 이어서 합니다)</b>" :
          p.generated_at ? `마지막 생성 ${relTime(p.generated_at)}${p.used.length ? ` (사용: ${esc(p.used.join(" · "))})` : ""} · 7일마다 자동으로 다시 생성` :
          p.seeds.length ? "곧 자동으로 만듭니다 (수집이 끝난 뒤 차례로)" : "아직 생성 전"}
        ${!busy && p.generated_at && data.sources.searchad && !p.used.includes("검색광고") && !p.last_error
          ? "<br>검색광고 키가 등록되어 월간 검색수로 곧 다시 만듭니다." : ""}
        ${p.last_error ? `<br><span class="err">일부 실패: ${esc(p.last_error)}</span>` : ""}
      </div>
    </div>

    <div class="panel">
      <div class="kw-head2">
        <h2>자동 생성된 검색어 <span class="muted">켜짐 ${enabled} / 전체 ${shown.length}</span></h2>
        <span class="kw-add">
          <input id="kw-add" placeholder="검색어 직접 추가">
          <button class="btn small" id="kw-add-btn">추가</button>
        </span>
      </div>
      ${p.auto.length ? `
      <p class="muted" style="font-size:13px;margin:0 0 8px">${hasVolume ? `월간 검색수가 많은 순입니다. 월 ${num(p.min_volume)}회 미만은 자동으로 꺼 둡니다.` : "월간 검색수 없이 추정 점수 순입니다. <b>[설정]에 검색광고 API 키를 넣으면 실제 검색수로 정렬하고 검색량 낮은 검색어를 자동으로 뺍니다.</b>"} 체크를 끄면 확인에서 빠지고 맨 아래 [직접 뺀 검색어]로 옮겨집니다.</p>
      <div class="table-wrap"><table class="runs kw-table">
        ${head}
        ${rows}
      </table></div>` : `<div class="empty small">메인 키워드를 넣고 [저장하고 자동 생성]을 누르세요.</div>`}
    </div>

    ${removed.length ? `
    <details class="panel" id="kw-removed" ${removedOpen ? "open" : ""}>
      <summary>직접 뺀 검색어 ${removed.length}개 <span class="muted">— 다시 생성해도 켜지지 않아요. 체크하면 다시 위로 올라갑니다</span></summary>
      <div class="table-wrap"><table class="runs kw-table">${head}${removed.map(row).join("")}</table></div>
    </details>` : ""}

    ${p.config_keywords.length ? `
    <details class="panel">
      <summary>설정 파일(config.yaml)에서 직접 넣은 검색어 ${p.config_keywords.length}개 — 항상 확인</summary>
      <div class="kw-config">${p.config_keywords.map((k) =>
        `<span class="badge">${esc(k.keyword)}${k.exposure == null ? "" : k.exposure ? ` · ${k.exposure}개` : " · 없음"}</span>`).join(" ")}</div>
    </details>` : ""}`;
}

document.addEventListener("click", async (ev) => {
  const tab = ev.target.closest(".tab");
  if (tab) {
    current = tab.dataset.product;
    try { localStorage.setItem("jisikin.kwProduct", current); } catch (e) { /* 무시 */ }
    render();
    return;
  }
  if (ev.target.id === "kw-save") {
    try {
      const r = await api("/api/keywords/seeds", {
        product: current, seeds: $("#kw-seeds").value, max: $("#kw-max").value, min_volume: $("#kw-min").value,
      });
      toast(r.seeds.length ? "저장했습니다. 검색어를 만드는 중입니다…" : "메인 키워드를 비웠습니다");
      await load();
    } catch (e) { toast(e.message); }
    return;
  }
  if (ev.target.id === "kw-add-btn") {
    const kw = $("#kw-add").value.trim();
    if (!kw) return;
    try {
      await api("/api/keywords/add", { product: current, keyword: kw });
      toast("추가했습니다");
      await load();
    } catch (e) { toast(e.message); }
  }
});

document.addEventListener("change", async (ev) => {
  const box = ev.target.closest("input[data-kw]");
  if (!box) return;
  try {
    await api("/api/keywords/toggle", { product: current, keyword: box.dataset.kw, enabled: box.checked });
    const p = data.products.find((x) => x.id === current);
    const k = p.auto.find((x) => x.keyword === box.dataset.kw);
    if (k) { k.enabled = box.checked; k.user_set = true; }
    if (!box.checked) toast(`'${box.dataset.kw}' 을(를) 맨 아래 [직접 뺀 검색어]로 옮겼어요`);
    data.total_enabled += box.checked ? 1 : -1;
    render();
  } catch (e) {
    box.checked = !box.checked;
    toast(e.message);
  }
});

document.addEventListener("toggle", (ev) => {
  if (ev.target.id === "kw-removed") removedOpen = ev.target.open;
}, true);

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && ev.target.id === "kw-add") $("#kw-add-btn").click();
  if (ev.key === "Enter" && ev.target.id === "kw-seeds") $("#kw-save").click();
});

load().catch((e) => { $("#kw-body").innerHTML = `<div class="empty">불러오지 못했습니다: ${esc(e.message)}</div>`; });
