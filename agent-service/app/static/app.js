/* SafeGuard WhatsApp Copilot — Frontend Application Logic */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const tokenKey = "wa-agent-token";
const themeKey = "wa-agent-theme";
const voiceIdKey = "wa-voice-id"; // persisted voice selection — 5 Why: drift per person because no persistence
const voiceFallbackBadgeId = "voiceFallbackBadge";

const state = {
  pending: [],
  pollTimers: [],
  allChats: [],
  activeFilter: "all",
  activeNav: "conversations",
  auditEvents: []
};

/* ---------- utilities ---------- */

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s ?? "";
  return d.innerHTML;
}

function relTime(epochSeconds) {
  const delta = epochSeconds - Date.now() / 1000;
  const m = Math.round(delta / 60);
  if (Math.abs(m) < 1) return "just now";
  return m > 0 ? `in ${m}m` : `${-m}m ago`;
}

function toast(msg, kind = "ok", ms = 4200) {
  const icons = { ok: "✓", bad: "✕", warn: "⚠" };
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `<span>${icons[kind] || ""}</span><span>${esc(msg)}</span>`;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), ms);
}

function headers() {
  return {
    Authorization: "Bearer " + localStorage.getItem(tokenKey),
    "Content-Type": "application/json",
  };
}

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: headers() });
  if (res.status === 401) {
    localStorage.removeItem(tokenKey);
    $("#auth").showModal();
    throw new Error("unauthorized — service token required");
  }
  let body = {};
  try { body = await res.json(); } catch { /* empty body */ }
  return { status: res.status, body };
}

/* ---------- auth ---------- */

async function autoFetchToken() {
  try {
    const res = await fetch("/pair-token", { cache: "no-store" });
    if (res.ok) {
      const { token } = await res.json();
      if (token) {
        localStorage.setItem(tokenKey, token);
        return true;
      }
    }
  } catch { /* manual fallback */ }
  return false;
}

function saveToken() {
  const t = $("#tokenInput").value.trim();
  if (!t) return;
  localStorage.setItem(tokenKey, t);
  $("#auth").close();
  boot();
}

/* ---------- theme ---------- */

function applyTheme() {
  const theme = localStorage.getItem(themeKey) || "light";
  document.documentElement.dataset.theme = theme;
  const themeBtnIcon = $("#themeBtn span");
  if (theme === "dark") {
    document.documentElement.classList.add("dark");
    document.documentElement.classList.remove("light");
    if (themeBtnIcon) themeBtnIcon.textContent = "light_mode";
  } else {
    document.documentElement.classList.add("light");
    document.documentElement.classList.remove("dark");
    if (themeBtnIcon) themeBtnIcon.textContent = "dark_mode";
  }
}

function toggleTheme() {
  const cur = (document.documentElement.dataset.theme || "light") === "light" ? "dark" : "light";
  localStorage.setItem(themeKey, cur);
  applyTheme();
}

/* ---------- chat rendering ---------- */

function formatMarkdown(text) {
  if (!text) return "";
  let html = esc(text);
  // Code blocks
  html = html.replace(/```(\w+)?\n([\s\S]*?)```/g, (m, lang, code) => {
    return `<div class="relative my-2 rounded-xl overflow-hidden border border-slate-300/60 shadow-sm"><div class="flex justify-between items-center bg-slate-100 px-3 py-1 text-[10px] font-label-md text-slate-600 border-b border-slate-200"><span>${lang || "code"}</span><button type="button" class="text-primary font-semibold hover:underline" onclick="copySnippet(this)">Copy</button></div><pre class="bg-white/80 p-2.5 text-xs font-label-md overflow-x-auto text-slate-800 m-0"><code>${code}</code></pre></div>`;
  });
  // Inline code
  html = html.replace(/`([^`]+)`/g, '<code class="bg-slate-100 text-slate-800 px-1.5 py-0.5 rounded border border-slate-200 text-[11.5px] font-label-md">$1</code>');
  // Bold
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong class="font-bold text-slate-900">$1</strong>');
  // Italic
  html = html.replace(/\*([^*]+)\*/g, '<em class="italic text-slate-800">$1</em>');
  // Bullet items
  html = html.replace(/^\s*[-*•]\s+(.*)$/gm, '<div class="flex items-start gap-2 my-0.5"><span class="text-primary text-xs">•</span><span>$1</span></div>');
  return html;
}

function copySnippet(btn) {
  const code = btn.closest(".relative")?.querySelector("code")?.textContent || "";
  if (!code) return;
  navigator.clipboard.writeText(code);
  toast("Code snippet copied!", "ok");
}

function bubble(cls, html, rawText = "") {
  const el = document.createElement("div");
  el.className = `bubble ${cls}`;

  // Add distinct header to AI Copilot responses
  let contentHtml = html;
  if (cls.includes("msg-agent") && !cls.includes("typing")) {
    contentHtml = `
      <div class="flex items-center justify-between pb-1.5 mb-1.5 border-b border-slate-100 select-none">
        <div class="flex items-center gap-1.5 text-xs font-bold text-primary">
          <span class="material-symbols-outlined text-[15px]">auto_awesome</span>
          <span>AI Copilot</span>
        </div>
        <span class="text-[10px] font-label-md text-slate-400">assistant</span>
      </div>
      <div class="msg-content text-slate-900">${html}</div>
    `;
  }
  
  el.innerHTML = contentHtml;

  // Add interactive quick action bar to Copilot agent responses
  if (cls.includes("msg-agent") && !cls.includes("typing") && rawText) {
    const actBar = document.createElement("div");
    actBar.className = "copilot-action-bar";
    actBar.innerHTML = `
      <button type="button" class="copilot-btn" onclick="insertCopilotToDraft(${JSON.stringify(rawText).replace(/"/g, '&quot;')})">
        <span class="material-symbols-outlined text-xs">edit_note</span> Use in WhatsApp
      </button>
      <button type="button" class="copilot-btn" onclick="copyMessageText(${JSON.stringify(rawText).replace(/"/g, '&quot;')})">
        <span class="material-symbols-outlined text-xs">content_copy</span> Copy
      </button>
    `;
    el.appendChild(actBar);
  }

  const log = $("#chatlog");
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

function system(text) { bubble("msg-system", esc(text)); }

function typing(on) {
  if (on) {
    const t = document.createElement("div");
    t.className = "bubble msg-agent typing"; t.id = "typing";
    t.innerHTML = "<span class='text-xs text-primary font-label-md animate-pulse flex items-center gap-2'><span class='material-symbols-outlined text-sm animate-spin'>sync</span> Copilot analyzing conversation stream...</span>";
    $("#chatlog").appendChild(t);
    $("#chatlog").scrollTop = $("#chatlog").scrollHeight;
  } else {
    $("#typing")?.remove();
  }
}

function insertCopilotToDraft(text) {
  const draft = $("#draftMsg");
  if (!draft) return;
  draft.value = text;
  autoGrow(draft);
  draft.focus();
  toast("Inserted into WhatsApp message draft", "ok");
}

/* ---------- agent status line (stage events) ---------- */

const TOOL_LABELS = {
  search_chats: (a) => `Searching contacts${a?.query ? ` “${String(a.query).slice(0, 32)}”` : ""}`,
  get_messages: (a) => `Reading chat${a?.chat_jid ? ` · ${String(a.chat_jid).split("@")[0].slice(-10)}` : ""}`,
  list_chats: () => "Loading recent chats",
  send_message: (a) => `Preparing message → ${a?.recipient || ""}`.slice(0, 60),
  delete_message: (a) => `Preparing delete in ${a?.chat_jid || "chat"}`.slice(0, 60),
  initiate_audio_call: (a) => `Audio call → ${a?.recipient || ""}`.slice(0, 60),
  initiate_video_call: (a) => `Video call → ${a?.recipient || ""}`.slice(0, 60),
};

const STAGE_LABELS = {
  understanding: () => "Understanding request",
  understood: (d) => `Plan: ${String(d.detail || "").replace(/\s+/g, " ").trim().slice(0, 50)}…`,
  thinking: () => "Analyzing intent & parameters",
  proposing: (d) => `Awaiting approval for ${TOOL_LABELS[d.tool]?.({}) || d.tool || "action"}`,
};

let statusEl = null;
let statusTimer = null;
let statusStart = 0;
let stepsEl = null;

function ensureStatusLine() {
  if (!statusEl) {
    statusEl = document.createElement("div");
    statusEl.className = "agent-status";
    statusEl.id = "agentStatus";
    statusEl.innerHTML =
      '<span class="spin"></span><span class="status-text">Processing</span>' +
      '<span class="lat status-timer">0.0s</span>';
    const log = $("#chatlog");
    log.appendChild(statusEl);
    stepsEl = document.createElement("div");
    stepsEl.className = "turn-steps";
    log.appendChild(stepsEl);
    statusStart = Date.now();
    statusTimer = setInterval(() => {
      const t = ((Date.now() - statusStart) / 1000).toFixed(1);
      const el = statusEl?.querySelector(".status-timer");
      if (el) el.textContent = `${t}s`;
    }, 100);
    $("#chatlog").scrollTop = $("#chatlog").scrollHeight;
  }
  return statusEl;
}

function setStatus(label) {
  const el = ensureStatusLine();
  el.querySelector(".status-text").textContent = label;
  el.style.display = "";
  $("#chatlog").scrollTop = $("#chatlog").scrollHeight;
}

function addStepChip(text) {
  const chip = document.createElement("span");
  chip.className = "step-chip";
  chip.textContent = text;
  stepsEl?.appendChild(chip);
  $("#chatlog").scrollTop = $("#chatlog").scrollHeight;
}

function clearStatus() {
  if (statusTimer) { clearInterval(statusTimer); statusTimer = null; }
  statusEl?.remove();
  statusEl = null;
}

function applyStage(data) {
  typing(false);
  const phase = data.phase;
  if (phase === "understood") {
    addStepChip(STAGE_LABELS.understood(data));
    setStatus("Planning next step");
    return;
  }
  if (phase === "thinking" && data.round > 1) {
    addStepChip(`Round ${data.round - 1}`);
  }
  const fn = STAGE_LABELS[phase];
  setStatus(fn ? fn(data) : phase);
}

function handleToolStage(data) {
  typing(false);
  const label = (TOOL_LABELS[data.tool] || (() => data.tool))(data.args || {});
  addStepChip(label);
  setStatus(label);
}

/* ---------- approval card HTML ---------- */

function approvalCardHTML(a, inChat = false) {
  const isDelete = a.tool === "delete_message";
  const isAudioCall = a.tool === "initiate_audio_call";
  const isVideoCall = a.tool === "initiate_video_call";
  const isCall = isAudioCall || isVideoCall;
  const hasWarnings = (a.warnings || []).length > 0;
  const borderColor = hasWarnings ? "border-error" : isDelete ? "border-secondary" : isCall ? "border-emerald-500" : "border-primary";
  const severityTag = hasWarnings
    ? `<span class="inline-flex items-center gap-1 bg-error-container text-error text-[10px] font-label-md px-2 py-0.5 rounded-full"><span class="material-symbols-outlined text-[13px]">warning</span> Warning</span>`
    : `<span class="inline-flex items-center gap-1 bg-primary/10 text-primary border border-primary/20 text-[10px] font-label-md px-2 py-0.5 rounded-full"><span class="material-symbols-outlined text-[13px]">verified</span> Verified</span>`;

  let msgContent = a.args?.message || a.args?.text || "";
  if (isAudioCall) msgContent = `📞 Audio call → ${a.args?.recipient || "contact"}`;
  else if (isVideoCall) msgContent = `🎥 Video call → ${a.args?.recipient || "contact"}`;
  else if (!msgContent) msgContent = (a.args ? JSON.stringify(a.args) : "");

  return `
    <div class="bg-white/80 backdrop-blur-xl rounded-2xl p-4 md:p-5 flex flex-col gap-3 border border-slate-300/60 relative overflow-hidden group shadow-md" id="ac-${esc(a.actionId)}">
      <div class="absolute top-0 left-0 w-1.5 h-full ${borderColor}"></div>
      
      <div class="flex items-start justify-between gap-3">
        <div class="flex items-center gap-3">
          <div class="w-10 h-10 rounded-xl ${isCall ? "bg-emerald-50 border-emerald-200 text-emerald-600" : "bg-primary/10 border-primary/20 text-primary"} flex items-center justify-center font-bold text-xs border">
            <span class="material-symbols-outlined text-lg">${isDelete ? "delete" : isVideoCall ? "videocam" : isAudioCall ? "call" : "send"}</span>
          </div>
          <div>
            <h3 class="font-headline-md text-sm font-semibold text-on-surface">${esc(a.args?.recipient || a.args?.chat_jid || "WhatsApp Contact")}</h3>
            <div class="flex items-center gap-2 mt-0.5">
              <span class="bg-slate-100 text-on-surface-variant px-2 py-0.5 rounded font-label-md text-[10px] uppercase font-semibold">Action: ${esc(a.tool)}</span>
              <span class="text-on-surface-variant font-label-md text-xs">•</span>
              <span class="text-secondary font-label-md text-[11px] flex items-center gap-1 font-medium">
                <span class="material-symbols-outlined text-[13px]">schedule</span> Expires ${relTime(a.expiresAt)}
              </span>
            </div>
          </div>
        </div>
        ${severityTag}
      </div>

      <div class="bg-slate-50/90 p-3.5 rounded-xl border border-slate-200/80 ml-0 md:ml-12 mt-1 relative text-xs font-body-md text-on-surface leading-relaxed">
        ${isDelete ? `<p class="line-through italic text-on-surface-variant mb-1 font-semibold text-error">Target message will be permanently deleted.</p>` : isCall ? `<p class="italic text-emerald-700 mb-1 font-semibold">${isVideoCall ? "🎥 Video call" : "📞 Audio call"} — requires approval before dialing. Linked-device may be simulated.</p>` : ""}
        <p class="${isDelete ? 'line-through text-on-surface-variant' : isCall ? 'font-semibold text-emerald-700' : ''}">${esc(msgContent)}</p>
      </div>

      ${(a.warnings || []).map(w => `<div class="text-warn text-xs flex items-center gap-1.5 ml-0 md:ml-12"><span class="material-symbols-outlined text-sm">error</span><span>${esc(w)}</span></div>`).join("")}

      <details class="args ml-0 md:ml-12 text-xs">
        <summary class="cursor-pointer text-on-surface-variant hover:text-primary font-medium">View raw arguments payload</summary>
        <pre class="mono bg-white p-2 rounded-lg border border-slate-200 mt-1 text-[11px]">${esc(JSON.stringify(a.args ?? a, null, 2))}</pre>
      </details>

      <div class="flex items-center justify-end gap-2.5 mt-2 rowbtns">
        <button class="px-3.5 py-1.5 rounded-xl text-xs font-semibold text-error hover:bg-error-container transition-colors border border-transparent hover:border-error/20"
                onclick="decide('${esc(a.actionId)}', false, this)">Reject</button>
        <button class="px-5 py-1.5 rounded-xl text-xs font-bold bg-gradient-to-r from-primary to-primary-container text-white hover:brightness-105 transition-all shadow-[0_4px_14px_rgba(37,99,235,0.25)]"
                onclick="decide('${esc(a.actionId)}', true, this)">Approve &amp; Execute</button>
      </div>
    </div>`;
}

function handleAgentResponse(body) {
  switch (body.type) {
    case "answer": {
      const formatted = formatMarkdown(body.text || "(empty answer)");
      const el = bubble("msg-agent", formatted, body.text || "");
      attachLatency(el, body.processTimeMs);
      // Grounded badge — proves real WhatsApp data, not hallucination (15yrs: cite sources)
      if(body.citations && body.citations.length){
        const cite = body.citations[0];
        const chatJid = cite.chat_jid || (cite.chats && cite.chats[0]?.jid) || "";
        const cnt = cite.count || cite.sample_ids?.length || (cite.chats?.length) || 0;
        const badge = document.createElement("div");
        badge.className = "text-[10px] font-label-md text-emerald-600 bg-emerald-50 border border-emerald-200 px-2 py-0.5 rounded-full inline-flex items-center gap-1 mt-1";
        let label = "Grounded • real";
        if(chatJid) label = `Grounded • ${chatJid.split("@")[0]} • ${cnt} msgs`;
        else if(cnt) label = `Grounded • ${cnt} chats`;
        badge.innerHTML = `<span class="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>${esc(label)}`;
        el.appendChild(badge);
      } else if(body.grounded===false){
        const warn = document.createElement("div");
        warn.className = "text-[10px] font-label-md text-amber-600 bg-amber-50 border border-amber-200 px-2 py-0.5 rounded-full inline-flex items-center gap-1 mt-1";
        warn.innerHTML = `<span class="w-1.5 h-1.5 rounded-full bg-amber-500"></span>Not grounded — no WhatsApp tool used`;
        el.appendChild(warn);
      }
      break;
    }
    case "approval_required": {
      state.pending.push(body);
      const card = document.createElement("div");
      card.className = "approval-card";
      card.id = `ac-chat-${body.actionId}`;
      card.innerHTML = approvalCardHTML(body, true);
      $("#chatlog").appendChild(card);
      $("#chatlog").scrollTop = $("#chatlog").scrollHeight;
      toast("Action proposed — approval required", "warn");
      refreshApprovals();
      break;
    }
    case "blocked":
      bubble("msg-error", "Refused: " + esc(body.reason));
      break;
    default:
      bubble("msg-error", "error: " + esc(body.message || JSON.stringify(body)));
  }
}

function attachLatency(el, ms) {
  if (el && typeof ms === "number") {
    const tag = document.createElement("span");
    tag.className = "lat";
    tag.textContent = `${Math.round(ms)}ms`;
    el.appendChild(tag);
  }
}

/* ---------- streaming send ---------- */

async function* sseEvents(res) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let event = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (data) yield { event, data: JSON.parse(data) };
    }
  }
}

async function sendStreaming(text) {
  const res = await fetch("/agents/whatsapp/stream", {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ message: text }),
  });
  if (res.status === 401) {
    localStorage.removeItem(tokenKey);
    $("#auth").showModal();
    throw new Error("unauthorized — service token required");
  }
  if (!res.ok || !(res.headers.get("content-type") || "").includes("text/event-stream")) {
    throw new Error("stream unavailable");
  }

  let liveEl = null;
  let liveText = "";
  let sawTerminal = false;

  for await (const { event, data } of sseEvents(res)) {
    switch (event) {
      case "delta": {
        clearStatus();
        liveText += data.text || "";
        if (!liveEl) liveEl = bubble("msg-agent", "");
        const contentEl = liveEl.querySelector(".msg-content");
        if (contentEl) {
          contentEl.innerHTML = formatMarkdown(liveText);
        } else {
          liveEl.innerHTML = formatMarkdown(liveText);
        }
        $("#chatlog").scrollTop = $("#chatlog").scrollHeight;
        break;
      }
      case "tool": {
        handleToolStage(data);
        break;
      }
      case "stage": {
        applyStage(data);
        break;
      }
      case "meta":
        break;
      default: {
        sawTerminal = true;
        clearStatus();
        typing(false);
        if (stepsEl && stepsEl.childElementCount) {
          stepsEl.classList.add("done");
        }
        if (event === "answer" && liveEl) {
          const finalTxt = data.text || liveText;
          const contentEl = liveEl.querySelector(".msg-content");
          if (contentEl) {
            contentEl.innerHTML = formatMarkdown(finalTxt);
          } else {
            liveEl.innerHTML = formatMarkdown(finalTxt);
          }
          attachLatency(liveEl, data.processTimeMs);
          // Grounded badge for streamed answer
          if(data.citations && data.citations.length){
            const cite=data.citations[0];
            const chatJid=cite.chat_jid || (cite.chats && cite.chats[0]?.jid) || "";
            const cnt=cite.count || cite.sample_ids?.length || (cite.chats?.length) || 0;
            const badge=document.createElement("div");
            badge.className="text-[10px] font-label-md text-emerald-600 bg-emerald-50 border border-emerald-200 px-2 py-0.5 rounded-full inline-flex items-center gap-1 mt-1";
            let label=chatJid?`Grounded • ${chatJid.split("@")[0]} • ${cnt} msgs`:`Grounded • ${cnt}`;
            badge.innerHTML=`<span class="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>${esc(label)}`;
            liveEl.appendChild(badge);
          }
          
          // Action bar for streamed response
          let actBar = liveEl.querySelector(".copilot-action-bar");
          if (!actBar) {
            actBar = document.createElement("div");
            actBar.className = "copilot-action-bar";
            actBar.innerHTML = `
              <button type="button" class="copilot-btn" onclick="insertCopilotToDraft(${JSON.stringify(finalTxt).replace(/"/g, '&quot;')})">
                <span class="material-symbols-outlined text-xs">edit_note</span> Use in WhatsApp
              </button>
              <button type="button" class="copilot-btn" onclick="copyMessageText(${JSON.stringify(finalTxt).replace(/"/g, '&quot;')})">
                <span class="material-symbols-outlined text-xs">content_copy</span> Copy
              </button>
            `;
            liveEl.appendChild(actBar);
          }
          $("#chatlog").scrollTop = $("#chatlog").scrollHeight;
        } else {
          handleAgentResponse(data);
        }
        break;
      }
    }
  }
  if (!sawTerminal) {
    clearStatus();
    typing(false);
    bubble("msg-error", "Connection interrupted before response completed.");
  }
}

async function send(ev) {
  ev?.preventDefault();
  const ta = $("#msg");
  const text = ta.value.trim();
  if (!text) return;
  ta.value = "";
  autoGrow(ta);
  bubble("msg-user", esc(text));
  typing(true);
  try {
    await sendStreaming(text);
  } catch {
    try {
      const { body } = await api("/agents/whatsapp", {
        method: "POST",
        body: JSON.stringify({ message: text }),
      });
      typing(false);
      handleAgentResponse(body);
    } catch (e2) {
      typing(false);
      bubble("msg-error", esc(e2.message));
    }
  }
}

function autoGrow(ta) {
  if (!ta) return;
  ta.style.height = "auto";
  const h = Math.min(180, Math.max(22, ta.scrollHeight));
  ta.style.height = `${h}px`;
}

/* ---------- decisions ---------- */

async function decide(actionId, approved, btn) {
  if (btn) btn.disabled = true;
  try {
    const { status, body } = await api(`/agents/whatsapp/approve/${actionId}`, {
      method: "POST",
      body: JSON.stringify({ approved }),
    });
    if (status === 200 && body.status === "executed") {
      const isSimulated = !!(body.result?.simulated || body.result?.bridge?.simulated);
      const callType = body.result?.bridge?.call_type || "";
      if(isSimulated && callType){
        toast(`${callType} call logged — WhatsApp Web cannot place calls directly, please dial on phone`, "warn", 6000);
      } else if(isSimulated){
        toast(`Logged — WhatsApp Web call not supported, please dial manually`, "warn", 6000);
      } else {
        toast(`Executed on WhatsApp ✓`, "ok");
      }
      $(`#ac-${actionId}`)?.remove();
      $(`#ac-chat-${actionId}`)?.remove();
      loadChatList();
      if (chatState.jid) refreshConversation();
    } else if (body.status === "rejected") {
      toast("Rejected — action cancelled", "ok");
      $(`#ac-${actionId}`)?.remove();
      $(`#ac-chat-${actionId}`)?.remove();
    } else if (body.status === "failed") {
      toast(`Execution failed: ${body.detail || "unknown error"}`, "bad", 7000);
    } else {
      toast(`${body.status || "error"}${body.detail ? " — " + body.detail : ""}`, "bad", 7000);
      if (btn) btn.disabled = false;
    }
    refreshApprovals();
  } catch (e) {
    toast(e.message, "bad");
    if (btn) btn.disabled = false;
  }
}

/* ---------- approvals dashboard ---------- */

async function refreshApprovals() {
  try {
    const { body } = await api("/agents/whatsapp/actions?status=pending");
    const list = body.actions || [];
    state.pending = list;
    
    // Update badge in Nav rail
    const badge = $("#approvalsBadge");
    if (badge) {
      badge.textContent = list.length;
      badge.classList.toggle("hidden", list.length === 0);
    }
    const countText = $("#approvalsCountText");
    if (countText) countText.textContent = `${list.length} Pending`;

    const wrap = $("#approvalsList");
    if (!wrap) return;

    if (!list.length) {
      wrap.innerHTML = `
        <div class="empty text-center py-20 bg-[#111b21] rounded-2xl border border-[#2a3942]">
          <span class="material-symbols-outlined text-5xl text-primary/40 block mb-3">verified</span>
          <h3 class="font-headline-md text-base text-on-surface font-semibold">No Pending Actions</h3>
          <p class="text-xs text-on-surface-variant mt-1">When the agent proposes a message send or delete, it will wait for your sign-off here.</p>
        </div>`;
      return;
    }

    wrap.innerHTML = list.map(a => approvalCardHTML(a, false)).join("");
  } catch { /* auth handled */ }
}

/* ---------- audit log view ---------- */

const EV_BADGES = {
  action_executed: `<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-primary/10 text-primary font-label-sm border border-primary/20"><span class="w-1.5 h-1.5 rounded-full bg-primary"></span> Executed</span>`,
  action_approved: `<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-primary/10 text-primary font-label-sm border border-primary/20"><span class="w-1.5 h-1.5 rounded-full bg-primary"></span> Approved</span>`,
  action_created: `<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-secondary/10 text-secondary font-label-sm border border-secondary/20"><span class="w-1.5 h-1.5 rounded-full bg-secondary"></span> Proposed</span>`,
  request_received: `<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-surface-container-highest text-on-surface font-label-sm border border-[#2a3942]"><span class="w-1.5 h-1.5 rounded-full bg-tertiary"></span> Request</span>`,
  request_completed: `<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-primary/10 text-primary font-label-sm border border-primary/20"><span class="w-1.5 h-1.5 rounded-full bg-primary"></span> Completed</span>`,
  action_failed: `<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-error/10 text-error font-label-sm border border-error/20"><span class="w-1.5 h-1.5 rounded-full bg-error"></span> Failed</span>`,
  action_rejected: `<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-error/10 text-error font-label-sm border border-error/20"><span class="w-1.5 h-1.5 rounded-full bg-error"></span> Rejected</span>`,
  action_expired: `<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-warn/10 text-amber-400 font-label-sm border border-amber-400/20"><span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span> Expired</span>`,
};

async function refreshAudit() {
  try {
    const { body } = await api("/audit?limit=80");
    const events = body.events || [];
    state.auditEvents = events;
    const wrap = $("#auditList");
    if (!wrap) return;

    if (!events.length) {
      wrap.innerHTML = `<tr><td colspan="4" class="p-8 text-center text-on-surface-variant">No audit events recorded yet.</td></tr>`;
      return;
    }

    wrap.innerHTML = events.map((e, idx) => {
      const badge = EV_BADGES[e.event] || `<span class="font-label-sm text-on-surface-variant">${esc(e.event)}</span>`;
      const timeStr = new Date(e.ts * 1000).toLocaleString();
      const detailStr = JSON.stringify(e.detail || {});
      const summary = detailStr.length > 70 ? detailStr.slice(0, 67) + "…" : detailStr;

      return `
        <tr class="hover:bg-slate-100/80 transition-colors">
          <td class="p-3.5 whitespace-nowrap font-label-md text-on-surface-variant text-[11px]">${esc(timeStr)}</td>
          <td class="p-3.5 whitespace-nowrap">${badge}</td>
          <td class="p-3.5 font-label-md text-on-surface text-[11px] max-w-xs truncate">${esc(summary)}</td>
          <td class="p-3.5 text-right whitespace-nowrap">
            <button class="text-primary hover:text-primary-fixed transition-colors font-label-md text-[11px] font-semibold"
                    onclick="viewEventDetail(${idx})">View Details</button>
          </td>
        </tr>`;
    }).join("");
  } catch { /* auth handled */ }
}

function viewEventDetail(idx) {
  const ev = state.auditEvents[idx];
  if (!ev) return;
  $("#eventDetailJson").textContent = JSON.stringify(ev, null, 2);
  $("#eventDetailModal").showModal();
}

/* ---------- health pills ---------- */

function setDot(id, level, label) {
  const dot = $(`#${id} .dot`);
  if (dot) {
    dot.className = "dot " + (level === "ok" ? "ok" : level === "warn" ? "warn pulse" : "bad");
  }
  const txt = $(`#${id} span:last-child`);
  if (txt) txt.textContent = label;
}

function updateHealthUI(h) {
  const b = h.bridge || {};
    const waLevel = b.logged_in === true ? "ok" : b.up ? "warn" : "bad";
    setDot("pillWa", waLevel, b.logged_in ? "Bridge: Connected" : b.up ? "Bridge: Pair QR" : "Bridge: Offline");
    
    const pairLink = $("#pairLink");
    if (pairLink) pairLink.style.display = b.logged_in === true ? "none" : (b.up ? "inline-flex" : "none");
    
    setDot("pillLlm", h.llm?.configured ? "ok" : "bad", `LLM: ${h.llm?.model || "Set"}`);
  setDot("pillDb", h.archive?.available ? "ok" : "bad", h.archive?.available ? "DB: Synced" : "DB: Disconnected");
}

async function refreshHealth() {
  try {
    const res = await fetch("/health");
    updateHealthUI(await res.json());
  } catch {
    ["pillWa", "pillLlm", "pillDb"].forEach((id) => setDot(id, "bad", "Offline"));
  }
}

/* ---------- view navigation ---------- */

function switchNavTab(targetView) {
  state.activeNav = targetView;
  
  // Highlight active Nav Rail icon
  document.querySelectorAll("#mainNav .nav-btn").forEach(btn => {
    const active = btn.dataset.target === targetView;
    btn.classList.toggle("active-rail", active);
    btn.classList.toggle("text-on-surface-variant", !active);
    btn.classList.toggle("hover:bg-white/[0.06]", !active);
  });

  // Toggle View Panels
  ["conversations", "approvals", "audit"].forEach(v => {
    const el = $(`#view-${v}`);
    if (el) el.classList.toggle("hidden", v !== targetView);
  });

  if (targetView === "approvals") refreshApprovals();
  if (targetView === "audit") { refreshAudit(); renderAuditFromStore(); }
  if (targetView === "conversations") {
    if (!state.allChats.length) loadChatList();
  }
}

/* ---------- chats view (whatsapp style) ---------- */

const chatState = { jid: null, name: null, timer: null };

function sortChatsList(list) {
  return list.sort((a, b) => {
    const ta = a.last_message_time ? new Date(String(a.last_message_time).replace(" ", "T")).getTime() : 0;
    const tb = b.last_message_time ? new Date(String(b.last_message_time).replace(" ", "T")).getTime() : 0;
    return tb - ta;
  });
}

function bumpChatInRoster(jid, lastMessage, time, isIncoming = false) {
  if (!jid) return;
  state.allChats = state.allChats || [];
  const idx = state.allChats.findIndex(c => c.jid === jid);
  const now = time || new Date().toISOString();
  if (idx !== -1) {
    const chat = { ...state.allChats[idx] };
    if (lastMessage) chat.last_message = lastMessage;
    chat.last_message_time = now;
    if (isIncoming && chatState.jid !== jid) {
      chat.unread_count = (chat.unread_count || 0) + 1;
    }
    state.allChats.splice(idx, 1);
    state.allChats.unshift(chat);
  } else {
    // If not found in current loaded list, reload authoritatively
    loadChatList();
    return;
  }
  renderChatRoster(state.allChats);
}

async function loadChatList() {
  const q = ($("#chatSearch")?.value || "").trim();
  try {
    const { body } = await api(`/agents/whatsapp/chats?limit=80&q=${encodeURIComponent(q)}`);
    const list = sortChatsList(body.chats || []);
    state.allChats = list;
    renderChatRoster(list);
  } catch { /* auth handled */ }
}

function renderChatRoster(list) {
  const wrap = $("#chatListItems");
  if (!wrap) return;

  let filtered = list;
  if (state.activeFilter === "unread") {
    filtered = list.filter(c => (c.unread_count || 0) > 0);
  } else if (state.activeFilter === "groups") {
    filtered = list.filter(c => c.jid && c.jid.includes("@g.us"));
  }

  if (!filtered.length) {
    wrap.innerHTML = `<div class="empty text-xs text-center py-10 text-on-surface-variant">No chats match.</div>`;
    return;
  }

  wrap.innerHTML = filtered.map(c => {
    const label = c.name || c.jid.split("@")[0];
    const isSel = chatState.jid === c.jid;
    const isGroup = c.jid && c.jid.includes("@g.us");
    const avatarTxt = esc(label.replace(/[^a-z0-9 ]/gi, "").trim().slice(0, 2) || "#");

    return `
      <div class="clitem ${isSel ? "sel" : ""}" role="option"
           data-jid="${esc(c.jid)}" data-name="${esc(label)}"
           onclick="openChat('${esc(c.jid)}', this)">
        ${avatarHTML(c.jid, c.name || label)}
        <div class="min-w-0 flex-1">
          <div class="flex justify-between items-baseline mb-0.5">
            <h4 class="text-xs font-semibold text-on-surface truncate">${esc(label)}</h4>
            <span class="text-[10px] text-on-surface-variant font-label-md">${c.last_message_time ? fmtTime(c.last_message_time) : ""}</span>
          </div>
          <div class="flex items-center justify-between text-on-surface-variant text-[11px]">
            <p class="truncate font-body-md">${esc(c.last_message || (isGroup ? "Group Chat" : c.jid))}</p>
            ${(c.unread_count || 0) > 0 ? `<span class="bg-primary text-white font-bold text-[10px] w-4 h-4 rounded-full flex items-center justify-center shadow-sm">${c.unread_count}</span>` : ""}
          </div>
        </div>
      </div>`;
  }).join("");
}

/* ---------- interactive reply & quote state ---------- */

let replyState = { sender: null, text: null };

function quoteMessage(sender, text) {
  replyState = { sender, text };
  const banner = $("#replyPreview");
  if (banner) {
    $("#replySender").textContent = `Replying to ${sender || "Message"}`;
    $("#replyText").textContent = text;
    banner.classList.remove("hidden");
  }
  const draft = $("#draftMsg");
  draft?.focus();
}

function cancelReply() {
  replyState = { sender: null, text: null };
  const banner = $("#replyPreview");
  if (banner) banner.classList.add("hidden");
}

function copyMessageText(text) {
  if (!text) return;
  navigator.clipboard.writeText(text);
  toast("Copied message text to clipboard", "ok");
}

function askCopilotAbout(sender, text) {
  const prompt = `Regarding the message from ${sender || "the contact"}: "${text}" — please analyze or suggest next steps.`;
  askCopilot(prompt);
}

async function proposeDeleteMessage(msgId, chatJid) {
  if (!msgId || !chatJid) return;
  try {
    const { status, body } = await api("/agents/whatsapp/delete", {
      method: "POST",
      body: JSON.stringify({ chat_jid: chatJid, message_id: msgId }),
    });
    if (body.type === "approval_required") {
      state.pending.push({ ...body });
      toast("Delete request submitted for approval", "warn");
      refreshApprovals();
    } else {
      toast(body.reason || "Could not propose deletion", "bad");
    }
  } catch (e) { toast(e.message, "bad"); }
}

/* ---------- interactive emojis & quick chips ---------- */

function toggleEmojiTray() {
  const tray = $("#emojiPopover");
  if (tray) tray.classList.toggle("hidden");
}

function insertEmoji(char) {
  const draft = $("#draftMsg");
  if (!draft) return;
  const start = draft.selectionStart || draft.value.length;
  const end = draft.selectionEnd || draft.value.length;
  draft.value = draft.value.substring(0, start) + char + draft.value.substring(end);
  draft.selectionStart = draft.selectionEnd = start + char.length;
  autoGrow(draft);
  draft.focus();
  $("#emojiPopover")?.classList.add("hidden");
}

function quickPrompt(type) {
  if (!chatState.jid) {
    toast("Select a chat first", "warn");
    return;
  }
  const name = chatState.name || chatState.jid;
  const prompt = `${type} for the current WhatsApp conversation with ${name}.`;
  askCopilot(prompt);
}

function askCopilot(promptText) {
  switchNavTab("conversations");
  const msgInput = $("#msg");
  if (msgInput) {
    msgInput.value = promptText;
    autoGrow(msgInput);
    send();
  }
}

function summarizeCurrentChat() {
  if (!chatState.jid) {
    toast("Select a chat to summarize", "warn");
    return;
  }
  askCopilot(`Please summarize all recent messages and key takeaways for WhatsApp contact ${chatState.name || chatState.jid}.`);
}

function scrollConvoToBottom(smooth = false) {
  const log = $("#msgLog");
  if (!log) return;
  log.scrollTo({ top: log.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  $("#scrollBottomBtn")?.classList.add("hidden");
}

async function openChat(jid, el) {
  chatState.jid = jid;
  chatState.name = el?.dataset?.name || jid;
  document.querySelectorAll(".clitem.sel").forEach((n) => n.classList.remove("sel"));
  el?.classList.add("sel");

  const chatObj = (state.allChats || []).find(c => c.jid === jid);
  if (chatObj && chatObj.unread_count) {
    chatObj.unread_count = 0;
    renderChatRoster(state.allChats);
  }

  
  const titleEl = $("#convoTitle");
  if (titleEl) titleEl.textContent = chatState.name;
  
  const subEl = $("#convoSub");
  if (subEl) {
    subEl.innerHTML = `<span class="w-1.5 h-1.5 rounded-full bg-primary animate-pulse"></span><p class="text-xs text-on-surface-variant font-label-md">${esc(jid)}</p>`;
  }
  
  const avEl = $("#convoAvatar");
  if (avEl) avEl.outerHTML = avatarHTML(jid, chatState.name).replace("<span", '<span id="convoAvatar"');
  
  if (evtSock && evtSock.connected) {
    if (window.__subscribedChat && window.__subscribedChat !== jid) {
      evtSock.emit("unsubscribe_chat", { jid: window.__subscribedChat });
    }
    evtSock.emit("subscribe_chat", { jid });
    window.__subscribedChat = jid;
  }
  $("#sendForm").style.display = "flex";
  $("#quickChips")?.classList.remove("hidden");
  cancelReply();
  await refreshConversation();
  scrollConvoToBottom(false);
}

async function refreshConversation() {
  if (!chatState.jid) return;
  try {
    const { body } = await api(
      `/agents/whatsapp/chats/messages?chat_jid=${encodeURIComponent(chatState.jid)}&limit=120`
    );
    renderMessages(body.messages || [], body.reactions || []);
  } catch { /* auth handled */ }
}

function fmtTime(ts) {
  const d = new Date(String(ts).replace(" ", "T"));
  return isNaN(d) ? esc(ts) : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function dayLabel(ts) {
  const d = new Date(String(ts).replace(" ", "T"));
  if (isNaN(d)) return "";
  const today = new Date().toDateString();
  const yest = new Date(Date.now() - 864e5).toDateString();
  if (d.toDateString() === today) return "Today";
  if (d.toDateString() === yest) return "Yesterday";
  return d.toLocaleDateString();
}

/* ---------- interactive message reactions & animations ---------- */

/* Real WhatsApp reactions: one per message, tap same to clear.
   Goes through /agents/whatsapp/react -> Go bridge /api/react. */

const reactionsStore = {}; // msgKey -> {messageId, containerEl}

async function toggleReaction(msgKeyOrId, emoji) {
  // Accept either raw message id or the sanitized UI key
  let m = (window.__msgIndex || {})[msgKeyOrId];
  if (!m) {
    for (const [id, cand] of Object.entries(window.__msgIndex || {})) {
      if (`k-${String(id).replace(/[^a-zA-Z0-9_-]/g, "")}` === msgKeyOrId) { m = cand; break; }
    }
  }
  if (!m || !m.id || !chatState.jid) return;

  const current = m.my_reaction || "";
  const next = current === emoji ? "" : emoji; // toggle off / replace
  m.my_reaction = next || null;                // optimistic single-reaction state
  renderMyReactionUI(m);

  try {
    const res = await api("/agents/whatsapp/react", {
      method: "POST",
      body: JSON.stringify({
        chat_jid: chatState.jid,
        message_id: m.id,
        emoji: next,
        sender_jid: !m.from_me && m.sender ? m.sender : undefined,
      }),
    });
    if (res.status !== 200) {
      toast(res.body.detail || "Reaction failed", "bad");
      m.my_reaction = current || null;          // roll back on failure
      renderMyReactionUI(m);
      return;
    }
    toast(next ? `Reacted ${next}` : "Reaction removed", "ok");
    setTimeout(() => { if (chatState.jid) refreshConversation(); }, 900);
  } catch (e) {
    toast(e.message || "Reaction failed", "bad");
    m.my_reaction = current || null;
    renderMyReactionUI(m);
  }
}

function renderMyReactionUI(m) {
  const msgKey = `k-${String(m.id).replace(/[^a-zA-Z0-9_-]/g, "")}`;
  reactionsStore[msgKey] = { messageId: m.id };
  const container = $(`#reactions-${msgKey}`);
  if (!container) return;
  container.innerHTML = m.my_reaction
    ? `<button type="button" class="reaction-badge mine" ` +
      `title="Tap to remove" onclick="toggleReaction('${esc(m.id)}', '${esc(m.my_reaction)}')">` +
      `<span>${esc(m.my_reaction)}</span></button>`
    : "";
}

function handleMsgDoubleClick(msgKey, el) {
  // Spawn floating heart burst particle
  const burst = document.createElement("div");
  burst.className = "heart-burst";
  burst.textContent = "❤️";
  el.appendChild(burst);
  setTimeout(() => burst.remove(), 800);

  toggleReaction(msgKey, "❤️");
}

function formatMsgContent(rawText) {
  if (!rawText) return "";
  let text = esc(rawText);
  
  // Parse quoted reply format: > "quoted..."\nrest or > quoted...\nrest
  let quoteHtml = "";
  const quoteMatch = text.match(/^&gt;\s*&quot;([\s\S]*?)&quot;\n+([\s\S]*)$/);
  if (quoteMatch) {
    quoteHtml = `<div class="wa-quote-box">${quoteMatch[1]}</div>`;
    text = quoteMatch[2];
  } else {
    const lineQuoteMatch = text.match(/^&gt;\s*([\s\S]*?)\n+([\s\S]*)$/);
    if (lineQuoteMatch) {
      quoteHtml = `<div class="wa-quote-box">${lineQuoteMatch[1]}</div>`;
      text = lineQuoteMatch[2];
    }
  }

  // URL detection
  const urlRegex = /(https?:\/\/[^\s]+)/g;
  text = text.replace(urlRegex, (url) => {
    return `<a href="${url}" target="_blank" rel="noopener noreferrer" class="underline font-medium hover:text-primary transition-colors inline-flex items-center gap-0.5" onclick="event.stopPropagation()">${url} <span class="material-symbols-outlined text-[10px]">north_east</span></a>`;
  });
  return quoteHtml + text;
}


/* ---------- media rendering ---------- */

/* ---------- profile photo avatars (initials fallback behind image) ---------- */

function avatarHTML(jid, name, extraCls) {
  const initials = esc(name.replace(/[^a-z0-9 ]/gi, "").trim().slice(0, 2).toUpperCase() || "#");
  // status/newsletters can't have profile photos — skip the network entirely
  if (!jid || /@(newsletter|broadcast)$/i.test(jid)) {
    return '<span class="avatar-wrap ' + (extraCls || "") + '">' +
      '<div class="avatar">' + initials + '</div></span>';
  }
  const url = "/agents/whatsapp/avatar?jid=" + encodeURIComponent(jid || "") +
    "&token=" + encodeURIComponent(localStorage.getItem(tokenKey) || "");
  return '<span class="avatar-wrap ' + (extraCls || "") + '">' +
    '<div class="avatar">' + initials + '</div>' +
    '<img class="avatar-img" loading="lazy" src="' + url + '" alt="" ' +
    'onerror="this.remove()"></span>';
}

function mediaUrl(m) {
  return "/agents/whatsapp/media?chat_jid=" + encodeURIComponent(chatState.jid || "") +
    "&message_id=" + encodeURIComponent(m.id || "") +
    "&token=" + encodeURIComponent(localStorage.getItem(tokenKey) || "");
}

function expiredChip(el, label) {
  const span = document.createElement("span");
  span.className = "wa-expired";
  span.textContent = `⚠ ${label} unavailable (may have expired)`;
  el.replaceWith(span);
}

function fetchChip(m, label) {
  const mid = String(m.id || "").replace(/'/g, "");
  return `<button type="button" class="wa-fetch" ` +
    `onclick="loadMediaNow(this, '${mid}')">` +
    `<span class="material-symbols-outlined">cloud_download</span> ${label} · tap to fetch</button>` +
    (m.content && m.content.trim() ? `<div class="wa-caption">${esc(m.content.trim())}</div>` : "");
}

function loadMediaNow(btn, mid) {
  const m = (window.__msgIndex || {})[mid];
  if (!m) { expiredChip(btn, "Media"); return; }
  const real = buildMediaTag({ ...m, resolvable: true });
  btn.replaceWith(real);
}

function openMediaZoom(url) {
  if (!url) return;
  const overlay = $("#mediaZoomOverlay");
  const img = $("#mediaZoomImg");
  if (!overlay || !img) return;
  img.src = url;
  overlay.style.display = "flex";
  overlay.classList.remove("hidden");
  document.body.classList.add("media-zoom-active");
}

function closeMediaZoom(e) {
  // If clicked directly on the enlarged image itself, do not close
  if (e && e.target && e.target.id === "mediaZoomImg") return;
  if (e && e.stopPropagation) e.stopPropagation();
  const overlay = $("#mediaZoomOverlay");
  if (!overlay) return;
  overlay.classList.add("hidden");
  overlay.style.display = "none";
  document.body.classList.remove("media-zoom-active");
  const img = $("#mediaZoomImg");
  if (img) img.src = "";
}

function buildMediaTag(m) {
  const url = mediaUrl(m);
  const cap = m.content && m.content.trim()
    ? `<div class="wa-caption">${esc(m.content.trim())}</div>` : "";
  const err = (label) => ` onerror="expiredChip(this, '${label}')"`;
  switch ((m.type || "").toLowerCase()) {
    case "image":
      return `<div class="wa-media-wrap group" title="Click to view full photo" onclick="event.stopPropagation(); openMediaZoom('${url}')">` +
        `<img class="wa-media" loading="lazy" src="${url}" alt="image"${err("Image")}>` +
        `<div class="wa-media-hover-hint"><span class="material-symbols-outlined text-[16px]">fullscreen</span></div>` +
        `</div>${cap}`;
    case "sticker":
      return `<div class="wa-media-wrap group" title="Click to view sticker" onclick="event.stopPropagation(); openMediaZoom('${url}')">` +
        `<img class="wa-sticker" loading="lazy" src="${url}" alt="sticker"${err("Sticker")}>` +
        `</div>`;
    case "video":
      return `<video class="wa-media" controls preload="metadata" src="${url}"` +
        `${err("Video")}></video>${cap}`;
    case "audio":
      return `<div class="wa-audio"><span class="material-symbols-outlined">graphic_eq</span>` +
        `<audio controls preload="none" src="${url}"${err("Voice note")}></audio></div>`;
    case "document": {
      const fname = esc((m.filename || "document").split("/").pop());
      return `<a class="wa-doc" href="${url}" download="${fname}"` +
        `${err("Document")}><span class="material-symbols-outlined">description</span> ` +
        `${fname}</a>${cap}`;
    }
    case "reaction":
      return `<span class="wa-react">${esc(m.content || "👍")}</span>`;
    default:
      return null;
  }
}

function mediaBody(m) {
  if (m.resolvable === false && m.id) return fetchChip(m, prettyType(m.type));
  return buildMediaTag(m);
}

function prettyType(t) {
  return { image: "Image", sticker: "Sticker", video: "Video",
           audio: "Voice note", document: "Document" }[(t || "").toLowerCase()] || "Media";
}

let lastDay = "";

function msgRow(m, idx) {
  const cls = m.from_me ? "me" : "them";
  let daysep = "";
  const day = dayLabel(m.time);
  if (day && day !== lastDay) {
    daysep = `<div class="wa-daysep">${day}</div>`;
    lastDay = day;
  }
  const senderLine =
    !m.from_me && chatState.name && chatState.jid.includes("@g.us")
      ? `<div class="wa-sender">${esc(m.sender || "Participant")}</div>`
      : "";
  const bodyText = m.content || "";
  const media = !m.deleted && m.type && m.type !== "text" ? mediaBody(m) : null;
  const body = m.deleted
    ? (bodyText
        ? `<span class="italic text-on-surface-variant opacity-70">${esc(bodyText)} <span>(deleted)</span></span>`
        : `<span class="italic text-on-surface-variant opacity-70">This message was deleted</span>`)
    : (media !== null ? media : formatMsgContent(bodyText));

  const senderName = m.from_me
    ? "You"
    : (m.sender_name || chatState.contacts?.[m.sender] || m.sender || chatState.name || "Contact");
  const msgId = m.id || `idx-${idx}`;
  const msgKey = `k-${String(m.id || idx).replace(/[^a-zA-Z0-9_-]/g, "")}`;
  const rawBodyEscaped = JSON.stringify(bodyText).replace(/"/g, '&quot;');
  const senderEscaped = JSON.stringify(senderName).replace(/"/g, '&quot;');

  // Interactive Hover Action Toolbar with Quick Emoji Reactions
  const actionToolbar = !m.deleted ? `
    <div class="wa-msg-actions">
      <div class="reaction-picker-mini">
        <button type="button" class="reaction-item-mini" title="React 👍" onclick="toggleReaction('${esc(m.id)}', '👍')">👍</button>
        <button type="button" class="reaction-item-mini" title="React ❤️" onclick="toggleReaction('${esc(m.id)}', '❤️')">❤️</button>
        <button type="button" class="reaction-item-mini" title="React 😂" onclick="toggleReaction('${esc(m.id)}', '😂')">😂</button>
        <button type="button" class="reaction-item-mini" title="React 🙏" onclick="toggleReaction('${esc(m.id)}', '🙏')">🙏</button>
        <button type="button" class="reaction-item-mini" title="React 🔥" onclick="toggleReaction('${esc(m.id)}', '🔥')">🔥</button>
      </div>
      <button type="button" class="wa-action-btn" title="Copy text" onclick="copyMessageText(${rawBodyEscaped})">
        <span class="material-symbols-outlined text-[14px]">content_copy</span>
      </button>
      <button type="button" class="wa-action-btn" title="Quote / Reply" onclick="quoteMessage(${senderEscaped}, ${rawBodyEscaped})">
        <span class="material-symbols-outlined text-[14px]">reply</span>
      </button>
      <button type="button" class="wa-action-btn" title="Ask AI Copilot" onclick="askCopilotAbout(${senderEscaped}, ${rawBodyEscaped})">
        <span class="material-symbols-outlined text-[14px]">auto_awesome</span>
      </button>
      ${m.from_me && m.id ? `
        <button type="button" class="wa-action-btn hover:text-error" title="Propose Delete" onclick="proposeDeleteMessage('${esc(m.id)}', '${esc(chatState.jid)}')">
          <span class="material-symbols-outlined text-[14px]">delete</span>
        </button>
      ` : ""}
    </div>
  ` : "";

  return `${daysep}<div class="wa-row ${cls}"><div class="wa-msg-container" id="msgwrap-${msgKey}">${actionToolbar}<div class="wa-msg ${m.type === "sticker" ? "wa-msg-sticker" : ""}" ondblclick="handleMsgDoubleClick('${msgKey}', this)">${senderLine}${body}<span class="wa-time">${fmtTime(m.time)}${m.from_me ? '<span class="wa-ticks"> ✓✓</span>' : ""}</span></div><div class="msg-reactions" id="reactions-${msgKey}"></div></div></div>`;
}

function renderMessages(messages, reactions) {
  lastDay = "";
  window.__msgIndex = {};
  messages.forEach((m) => {
    if (m.id) {
      window.__msgIndex[m.id] = m;
      const k2 = `k-${String(m.id).replace(/[^a-zA-Z0-9_-]/g, "")}`;
      reactionsStore[k2] = { messageId: m.id };
    }
  });
  reactions = reactions || [];
  const log = $("#msgLog");
  const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 200;

  let html = "";
  let ri = 0;
  for (const m of messages) {
    while (ri < reactions.length &&
           String(reactions[ri].time) <= String(m.time)) {
      const r = reactions[ri++];
      html += `<div class="wa-revrow"><span class="wa-revchip">` +
        `${esc(r.content || "👍")} <b>${esc(r.sender_name || r.sender || "")}</b></span></div>`;
    }
    lastDay = ""; // msgRow manages its own day separators via shared state
    html += msgRow(m);
  }
  while (ri < reactions.length) {
    const r = reactions[ri++];
    html += `<div class="wa-revrow"><span class="wa-revchip">` +
      `${esc(r.content || "👍")} <b>${esc(r.sender_name || r.sender || "")}</b></span></div>`;
  }

  log.innerHTML = html;

  messages.forEach((m) => {
    const msgKey = `k-${String(m.id || "").replace(/[^a-zA-Z0-9_-]/g, "")}`;
    if (reactionsStore[msgKey]) renderReactionsUI(msgKey);
  });

  if (nearBottom || messages.length < 120) log.scrollTop = log.scrollHeight;
}

async function sendDraft(ev) {
  ev.preventDefault();
  const ta = $("#draftMsg");
  let text = ta.value.trim();
  if (!text || !chatState.jid) return;

  // Include quoted reply if active
  if (replyState.text) {
    text = `> "${replyState.text}"\n${text}`;
  }

  const currentJid = chatState.jid;
  ta.value = ""; 
  autoGrow(ta);
  cancelReply();

  // Optimistically bump contact to the top of the people list with latest snippet
  bumpChatInRoster(currentJid, text, new Date().toISOString(), false);

  try {
    const { status, body } = await api("/agents/whatsapp/send", {
      method: "POST",
      body: JSON.stringify({ recipient: currentJid, message: text }),
    });
    if (body.type === "approval_required") {
      state.pending.push({ ...body });
      toast("Proposed send created — requires approval", "warn");
      refreshApprovals();
    } else {
      toast(body.reason || "Could not propose send", "bad", 6000);
    }
  } catch (e) { toast(e.message, "bad"); }
  setTimeout(loadChatList, 600);
}

/* ---------- central reactive store (React-style state management) ---------- */

function createStore(initial) {
  const state = { ...initial };
  const subs = new Set();
  return {
    get: () => state,
    set(patch) {
      Object.assign(state, patch);
      subs.forEach((fn) => {
        try { fn(state, patch); } catch (err) { console.error("[store]", err); }
      });
    },
    subscribe(fn) { subs.add(fn); return () => subs.delete(fn); },
  };
}

/* Single source of truth for live data — every view renders from this. */
const Store = createStore({
  health: {},
  pendingCount: 0,
  auditEvents: [],
  incomingChat: null,
  connected: false,
});

Store.subscribe((state) => {
  if (Object.keys(state.health).length) updateHealthUI(toHealthSnapshot(state.health));
});

Store.subscribe((state, patch) => {
  if ("pendingCount" in patch) {
    const badge = $("#approvalsBadge");
    if (badge) {
      badge.textContent = state.pendingCount;
      badge.style.display = state.pendingCount ? "inline-block" : "none";
    }
  }
  if ("auditEvents" in patch && state.activeNav === "audit") renderAuditFromStore();
  if ("incomingChat" in patch && patch.incomingChat === chatState.jid) refreshConversation();
});

let evtSock = null;
let sseFailures = 0;
const pollTimers = [];

function startLegacyPolling() {
  if (pollTimers.length) return;
  console.warn("[live] socket unavailable — falling back to polling");
  system("Live socket unavailable — using periodic refresh.");
  pollTimers.push(setInterval(refreshHealth, 15000));
  pollTimers.push(setInterval(refreshApprovals, 9000));
  pollTimers.push(setInterval(() => {
    if (state.activeNav === "conversations" && chatState.jid) refreshConversation();
  }, 8000));
  pollTimers.push(setInterval(() => {
    if (state.activeNav === "conversations" && !($("#chatSearch")?.value || "").trim()) loadChatList();
  }, 5000));
  pollTimers.push(setInterval(() => {
    if (state.activeNav === "audit") refreshAudit();
  }, 15000));
}

function initLiveEvents() {
  const token = localStorage.getItem(tokenKey) || "";
  evtSock = io("/", { auth: { token }, transports: ["websocket", "polling"] });

  evtSock.on("connect", () => {
    sseFailures = 0;
    Store.set({ connected: true });
    if (chatState.jid) evtSock.emit("subscribe_chat", { jid: chatState.jid });
  });
  evtSock.on("disconnect", () => Store.set({ connected: false }));
  evtSock.on("connect_error", () => {
    sseFailures += 1;
    if (sseFailures >= 4) startLegacyPolling();
  });

  evtSock.on("health", (snap) => Store.set({ health: snap || {} }));

  evtSock.on("audit", (d) => {
    const ev = d.event || "";
    const audit = Store.get().auditEvents;
    Store.set({
      auditEvents: [{ ts: d.ts, event: d.event, detail: d.detail || {}, user_id: d.user_id },
        ...audit].slice(0, 200),
    });
    if (ev.startsWith("action_")) {
      // approval set changed → authoritative refetch (cheap, rare)
      refreshApprovals();
    }
  });

  evtSock.on("voice_activity", (step) => {
    if (step) renderVoiceActivityStep(step);
  });

  evtSock.on("incoming", (d) => {
    if (!d.chat_jid) return;
    if (d.chat_jid === chatState.jid) {
      Store.set({ incomingChat: d.chat_jid });
      refreshConversation();
    }
    bumpChatInRoster(d.chat_jid, d.preview || "New message", new Date().toISOString(), true);
    loadChatList();
  });

  // Background roster freshness poll
  setInterval(() => {
    if (state.activeNav === "conversations" && !($("#chatSearch")?.value || "").trim()) {
      loadChatList();
    }
  }, 5000);
}

function renderAuditFromStore() {
  const wrap = $("#auditList");
  if (!wrap) return;
  const events = Store.get().auditEvents.slice(0, 60);
  wrap.innerHTML = events
    .map((e2) => {
      const cls = EV_CLASS[e2.event] || "ev-info";
      const detail = Object.keys(e2.detail || {}).length
        ? `<div class="tagline mono" style="margin-top:2px">${esc(JSON.stringify(e2.detail)).slice(0, 140)}</div>`
        : "";
      return `<div class="audit-ev">
        <span class="ev ${cls}">${esc(e2.event)}</span>
        <span style="flex:1">${detail}</span>
        <time>${new Date((e2.ts || Date.now()/1000) * 1000).toLocaleTimeString()}</time>
      </div>`;
    })
    .join("");
}

function boot() {
  applyTheme();
  refreshHealth();
  refreshApprovals();
  loadChatList();
  initLiveEvents();
  const av = new URLSearchParams(location.search).get("autovoice");
  if (av === "1" || av === "tab") {
    setTimeout(() => {
      try {
        if (av === "tab") {
          switchCopilotTab("voice");
        }
        openVoiceModal();
      } catch {}
    }, 600);
  }
  updateWakeWordUI(isWakeEnabled);
  if (isWakeEnabled) {
    setTimeout(() => { startAmbientWakeListener(); }, 800);
  }
  system("Live updates active — approvals, chats and audit stream in without refresh.");
}

document.addEventListener("DOMContentLoaded", async () => {
  applyTheme();

  // Navigation rail clicks
  document.querySelectorAll("#mainNav .nav-btn[data-target]").forEach(btn => {
    btn.addEventListener("click", () => switchNavTab(btn.dataset.target));
  });

  // Filter buttons in chat roster
  document.querySelectorAll(".chat-filter-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".chat-filter-btn").forEach(b => {
        b.classList.remove("active", "bg-primary/10", "border-primary/25", "text-primary", "shadow-[0_2px_8px_rgba(37,99,235,0.12)]");
        b.classList.add("bg-white/40", "border-slate-300/40", "text-on-surface-variant");
      });
      btn.classList.add("active", "bg-primary/10", "border-primary/25", "text-primary", "shadow-[0_2px_8px_rgba(37,99,235,0.12)]");
      btn.classList.remove("bg-white/40", "border-slate-300/40", "text-on-surface-variant");
      state.activeFilter = btn.dataset.filter;
      renderChatRoster(state.allChats);
    });
  });

  $("#themeBtn")?.addEventListener("click", toggleTheme);
  $("#saveToken")?.addEventListener("click", saveToken);
  $("#composer")?.addEventListener("submit", send);
  $("#sendForm")?.addEventListener("submit", sendDraft);

  // Search input & clear button
  let searchTimer;
  const searchInput = $("#chatSearch");
  const searchClear = $("#chatSearchClear");
  
  searchInput?.addEventListener("input", (e) => {
    const val = e.target.value;
    if (searchClear) searchClear.classList.toggle("hidden", !val);
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadChatList, 250);
  });

  searchClear?.addEventListener("click", () => {
    if (searchInput) {
      searchInput.value = "";
      searchClear.classList.add("hidden");
      loadChatList();
      searchInput.focus();
    }
  });

  // Scroll detection on msgLog for Floating Scroll-To-Bottom button
  const msgLogEl = $("#msgLog");
  msgLogEl?.addEventListener("scroll", () => {
    const scrollBottomBtn = $("#scrollBottomBtn");
    if (!scrollBottomBtn) return;
    const isUp = msgLogEl.scrollHeight - msgLogEl.scrollTop - msgLogEl.clientHeight > 180;
    scrollBottomBtn.classList.toggle("hidden", !isUp);
  });

  // Global Keyboard Shortcuts
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      switchNavTab("conversations");
      searchInput?.focus();
    }
    if ((e.altKey && e.key.toLowerCase() === "v") || ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "v")) {
      e.preventDefault();
      openVoiceModal();
    }
    if (e.key === "Escape") {
      closeMediaZoom();
      closeVoiceModal();
      cancelReply();
      $("#emojiPopover")?.classList.add("hidden");
      $("#auth")?.close();
      $("#eventDetailModal")?.close();
    }
  });

  $("#msg")?.addEventListener("input", (e) => autoGrow(e.target));
  $("#msg")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) send(e);
  });

  $("#draftMsg")?.addEventListener("input", (e) => autoGrow(e.target));
  $("#draftMsg")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) sendDraft(e);
  });

  // Voice selects sync (single source of truth + persistence) — 5 Why: drift per person due to no localStorage
  const voiceSel = $("#voiceSelect");
  const voicePaneSel = $("#voiceSelectPane");
  if (voiceSel && voicePaneSel) {
    const syncVoice = (src, dst) => { if (src.value){ dst.value = src.value; setPersistedVoiceId(src.value); } };
    voiceSel.addEventListener("change", () => syncVoice(voiceSel, voicePaneSel));
    voicePaneSel.addEventListener("change", () => syncVoice(voicePaneSel, voiceSel));
    // Init from persisted or server default (health) on boot — keeps what you choose
    const persisted = getPersistedVoiceId();
    if(persisted){
      voiceSel.value = persisted;
      voicePaneSel.value = persisted;
    }
  }
  // Voice orb accessibility: keyboard operable
  ["voiceCircle", "voiceCirclePane"].forEach(id=>{
    const el=document.getElementById(id);
    if(el){
      el.setAttribute("role","button");
      el.setAttribute("tabindex","0");
      el.setAttribute("aria-label","Toggle voice listening");
      el.addEventListener("keydown", (e)=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); toggleVoiceListening(); }});
    }
  });

  if (!localStorage.getItem(tokenKey)) {
    const ok = await autoFetchToken();
    if (ok) {
      boot();
    } else {
      $("#auth").showModal();
    }
  } else {
    boot();
  }
});

/* ==========================================================================
   Wiki Wiki Live Voice Agent (Ultron-Inspired Wake-Word & Voice Orchestrator)
   ========================================================================== */

const WAKE_WORD = "uhu";
const WAKE_ENABLED_KEY = "wa-wake-enabled";
let isWakeEnabled = localStorage.getItem(WAKE_ENABLED_KEY) !== "false"; // default enabled
let ambientWakeRecognition = null;
let isAmbientListening = false;

const VOICE_SILENCE_MS = 1200; // adaptive: 1.2s after final, 3s fallback for interim-only
const VOICE_SILENCE_FALLBACK_MS = 3000;
const VOICE_LANG_KEY = "wa-voice-lang";
function getVoiceLang(){ try{ return localStorage.getItem(VOICE_LANG_KEY) || "en-US"; }catch{ return "en-US"; } }
function setVoiceLang(l){ try{ localStorage.setItem(VOICE_LANG_KEY,l);}catch{} }
// Persisted voice_id — Why 5: no persistence caused per-person drift; now survives reload/device
function getPersistedVoiceId(){ try{ return localStorage.getItem(voiceIdKey) || ""; }catch{ return ""; } }
function setPersistedVoiceId(id){ try{ if(id) localStorage.setItem(voiceIdKey, id); }catch{} }
function getSelectedVoiceId(){
  // 5 Why verified: DOM value last, persisted first, server default fallback (no hardcode drift)
  const persisted = getPersistedVoiceId();
  if(persisted) return persisted;
  const dom = $("#voiceSelect")?.value || $("#voiceSelectPane")?.value || "";
  if(dom) return dom;
  return ""; // let server use settings.elevenlabs_voice_id (JBF George) — single source of truth
}
// Tightened: whole-utterance yes/no with word boundaries, no substring shadowing
const CONFIRM_YES_RE = /^\s*(yes|yeah+|yep|sure|go\s*ahead|do\s*it|confirm|proceed|ok(?:ay)?|haan|ha)\s*[.!?]?\s*$/i;
const CONFIRM_NO_RE  = /^\s*(no|nope|cancel|don'?t|stop|abort|reject|nahi|nah)\s*[.!?]?\s*$/i;
// Helper for strict variant checks inside utterances
function isYesUtterance(s){ const t=s.trim().toLowerCase(); return CONFIRM_YES_RE.test(t) || /^\s*yes\b/i.test(t) && t.split(/\s+/).length <= 3; }
function isNoUtterance(s){ const t=s.trim().toLowerCase(); return CONFIRM_NO_RE.test(t) || /^\s*no\b/i.test(t) && t.split(/\s+/).length <= 3; }

let voiceRecognition = null;
let isVoiceListeningIntent = false; // user wants mic on (intent)
let isRecognizing = false;          // engine actually running
let currentVoiceAudio = null;
let speechDebounceTimer = null;
let currentVoiceActionId = null;
let pendingConfirmation = null;
let isVoiceSpeaking = false;
let isHandlingCommand = false;
let awaitingConfirmation = false;
let confirmationTimeout = null;
let approvalCountdownTimer = null;

function updateWakeWordUI(active) {
  const btn = $("#wakeWordToggleBtn");
  const txt = $("#wakeWordStatusText");
  const icon = $("#wakeWordIcon");
  const pillBtn = $("#pillWakeToggleBtn");
  const pillIcon = $("#pillWakeIcon");

  const isOn = (active !== undefined) ? !!active : isWakeEnabled;
  if (btn) {
    if (isOn) {
      btn.className = "px-2 py-1 rounded-lg bg-cyan-500/25 text-cyan-300 border border-cyan-500/50 text-[11px] font-semibold flex items-center gap-1 hover:bg-cyan-500/35 transition-all cursor-pointer shadow-[0_0_10px_rgba(6,182,212,0.3)]";
      if (txt) txt.textContent = "Wake: ON";
      if (icon) icon.textContent = "hearing";
    } else {
      btn.className = "px-2 py-1 rounded-lg bg-slate-800 text-slate-400 border border-slate-700 text-[11px] font-semibold flex items-center gap-1 hover:bg-slate-700 transition-all cursor-pointer";
      if (txt) txt.textContent = "Wake: OFF";
      if (icon) icon.textContent = "hearing_disabled";
    }
  }
  if (pillBtn) {
    if (isOn) {
      pillBtn.className = "p-1.5 rounded-full bg-cyan-500/20 text-cyan-300 hover:bg-cyan-500/30 transition-all shadow-[0_0_8px_rgba(6,182,212,0.3)]";
      if (pillIcon) pillIcon.textContent = "hearing";
    } else {
      pillBtn.className = "p-1.5 rounded-full bg-white/10 text-slate-400 hover:text-white transition-all";
      if (pillIcon) pillIcon.textContent = "hearing_disabled";
    }
  }
}

function startApprovalCountdown(expiresAt){
  clearInterval(approvalCountdownTimer);
  const countEl = $("#voiceActionCount");
  if(!expiresAt || !countEl) return;
  const expiry = new Date(expiresAt).getTime();
  if(isNaN(expiry)) return;
  function tick(){
    const now = Date.now();
    const sec = Math.max(0, Math.round((expiry - now)/1000));
    countEl.textContent = sec > 0 ? `Expires in ${sec}s` : "Expiring…";
    if(sec <= 0) clearInterval(approvalCountdownTimer);
  }
  tick();
  approvalCountdownTimer = setInterval(tick, 1000);
}
function stopApprovalCountdown(){
  clearInterval(approvalCountdownTimer);
  approvalCountdownTimer = null;
  const countEl = $("#voiceActionCount");
  if(countEl) countEl.textContent = "Idle";
}
let voiceAnalyser = null; // legacy alias, now mic analyser
let playbackAnalyser = null;
let voiceVisualizerRaf = null;

let micStream = null;
let micSourceNode = null;
let activeSphereAnalyser = null;

// For backward-compat: some older code references isVoiceListening
let isVoiceListening = false;
Object.defineProperty(window, 'isVoiceListening', {
  get(){ return isVoiceListeningIntent; },
  set(v){ isVoiceListeningIntent = !!v; isVoiceListening = !!v; }
});

function ensureVoiceAudioCtx() {
  if (!voiceAudioCtx) {
    voiceAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
  }
  if (voiceAudioCtx.state === "suspended") {
    voiceAudioCtx.resume();
  }
  return voiceAudioCtx;
}

function getSphereAnalyser() {
  if (!voiceAnalyser) {
    voiceAnalyser = voiceAudioCtx.createAnalyser();
    voiceAnalyser.fftSize = 64;
    voiceAnalyser.smoothingTimeConstant = 0.8;
  }
  return voiceAnalyser;
}
function getMicAnalyser(){
  return getSphereAnalyser();
}
function getPlaybackAnalyser(){
  if (!playbackAnalyser) {
    playbackAnalyser = voiceAudioCtx.createAnalyser();
    playbackAnalyser.fftSize = 64;
    playbackAnalyser.smoothingTimeConstant = 0.8;
  }
  return playbackAnalyser;
}
function setActiveAnalyser(an){
  activeSphereAnalyser = an;
  if(an) startSphereLoop();
}

function switchCopilotTab(tab) {
  const chatTab = $("#copilotTabChat");
  const voiceTab = $("#copilotTabVoice");
  const chatView = $("#copilotChatView");
  const voiceView = $("#copilotVoiceView");

  if (tab === "voice") {
    chatTab?.classList.remove("bg-white", "text-primary", "shadow-sm");
    chatTab?.classList.add("text-slate-600");
    voiceTab?.classList.add("bg-gradient-to-r", "from-cyan-500", "to-blue-600", "text-white", "shadow-sm");
    voiceTab?.classList.remove("text-slate-600");

    chatView?.classList.add("hidden");
    voiceView?.classList.remove("hidden");
    voiceView?.classList.add("flex");

    loadVoiceList();
    if (!wakeGreeted) {
      wakeGreeted = true;
      setTimeout(() => {
        appendVoiceTranscript("agent", WAKE_GREETING);
        speakWithBrowser(WAKE_GREETING);
      }, 350);
    }
    if (!isVoiceListeningIntent) {
      toggleVoiceListening();
    }
  } else {
    voiceTab?.classList.remove("bg-gradient-to-r", "from-cyan-500", "to-blue-600", "text-white", "shadow-sm");
    voiceTab?.classList.add("text-slate-600");
    chatTab?.classList.add("bg-white", "text-primary", "shadow-sm");
    chatTab?.classList.remove("text-slate-600");

    voiceView?.classList.add("hidden");
    voiceView?.classList.remove("flex");
    chatView?.classList.remove("hidden");
  }
}

function startSphereLoop() {
  cancelAnimationFrame(voiceVisualizerRaf);
  if (!activeSphereAnalyser) return;
  const freqData = new Uint8Array(activeSphereAnalyser.frequencyBinCount);
  const circles = [$("#voiceCircle"), $("#voiceCirclePane")].filter(Boolean);
  const auras = [$("#voiceAmbientAura"), $("#voiceAmbientAuraPane")].filter(Boolean);
  const pillOrbCore = document.querySelector(".pill-orb-core");

  function loop() {
    if (!activeSphereAnalyser) return;
    activeSphereAnalyser.getByteFrequencyData(freqData);
    let sum = 0;
    for (let i = 0; i < freqData.length; i++) sum += freqData[i];
    const energy = Math.min(1.0, sum / freqData.length / 75);

    const scale = 1.0 + energy * 0.42;
    const hue = ((Date.now() / 25 + energy * 200) % 360).toFixed(1);

    circles.forEach(c => {
      c.style.transform = `scale(${scale.toFixed(3)})`;
      c.style.filter = `hue-rotate(${hue}deg)`;
    });

    auras.forEach(a => {
      a.style.transform = `scale(${(1.1 + energy * 0.45).toFixed(3)})`;
      a.style.opacity = `${(0.6 + energy * 0.4).toFixed(2)}`;
    });

    if (pillOrbCore) {
      pillOrbCore.style.transform = `scale(${(1.0 + energy * 0.35).toFixed(3)})`;
    }
    voiceVisualizerRaf = requestAnimationFrame(loop);
  }
  loop();
}

/** Route AI playback through analyser; returns false when context can't run */
async function attachPlaybackAnalyser(audioEl) {
  try {
    const ctx = ensureVoiceAudioCtx();
    await ctx.resume();
    if (ctx.state !== "running") return false;
    const src = ctx.createMediaElementSource(audioEl);
    const analyser = getPlaybackAnalyser();
    src.connect(analyser);
    analyser.connect(ctx.destination);
    setActiveAnalyser(analyser);
    return true;
  } catch (err) {
    console.warn("[voice] playback analyser unavailable:", err);
    return false;
  }
}

/** Live mic amplitude while listening (no destination: no echo). */
async function startMicVisualizer() {
  try {
    const ctx = ensureVoiceAudioCtx();
    await ctx.resume();
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 }
    });
    micSourceNode = ctx.createMediaStreamSource(micStream);
    const analyser = getMicAnalyser();
    micSourceNode.connect(analyser);
    setActiveAnalyser(analyser);
  } catch (err) {
    console.warn("[voice] mic visualizer unavailable:", err);
  }
}

function stopMicVisualizer() {
  try { micStream && micStream.getTracks().forEach((t) => t.stop()); } catch {}
  micStream = null;
  try { micSourceNode && micSourceNode.disconnect(); } catch {}
  micSourceNode = null;
  if (!isVoiceSpeaking) {
    activeSphereAnalyser = null;
    resetSphereScale();
  } else {
    // keep playback analyser active
    activeSphereAnalyser = playbackAnalyser || voiceAnalyser;
    if (activeSphereAnalyser) startSphereLoop();
  }
}

function resetSphereScale() {
  if (voiceVisualizerRaf) {
    cancelAnimationFrame(voiceVisualizerRaf);
    voiceVisualizerRaf = null;
  }
  const circles = [$("#voiceCircle"), $("#voiceCirclePane")].filter(Boolean);
  const auras = [$("#voiceAmbientAura"), $("#voiceAmbientAuraPane")].filter(Boolean);
  const pillOrbCore = document.querySelector(".pill-orb-core");

  circles.forEach(c => {
    c.style.transform = "";
    c.style.filter = "";
  });
  auras.forEach(a => {
    a.style.transform = "";
    a.style.opacity = "";
  });
  if (pillOrbCore) {
    pillOrbCore.style.transform = "";
  }
}

function setVoiceOrbState(stateName, statusText, iconName = "mic") {
  const circles = [$("#voiceCircle"), $("#voiceCirclePane")].filter(Boolean);
  const stageWraps = [$("#voiceStageWrap"), $("#voiceStageWrapPane")].filter(Boolean);
  const badges = [$("#voiceStatusText"), $("#voicePaneStatusText")].filter(Boolean);
  const icons = [$("#voiceStatusIcon"), $("#voicePaneStatusIcon")].filter(Boolean);
  const micBtns = [$("#voiceMicToggleBtn"), $("#voicePaneMicBtn")].filter(Boolean);
  const micLabels = [$("#voiceMicLabel"), $("#voicePaneMicLabel")].filter(Boolean);
  const pillStatus = $("#pillStatusText");
  const pillSub = $("#pillSubText");
  const pillMicIcon = $("#pillMicIcon");
  const spinner = $("#voiceSpinnerIcon");
  const staticIcon = $("#voiceStaticIcon");

  circles.forEach(c => { c.className = `gemini-main-sphere state-${stateName}`; });
  stageWraps.forEach(w => { w.className = `gemini-sphere-wrap ${stateName}`; });
  badges.forEach(b => { b.textContent = statusText; });
  icons.forEach(i => { i.textContent = iconName; });
  if (pillStatus) pillStatus.textContent = statusText;

  if (spinner && staticIcon) {
    if (stateName === "thinking") {
      spinner.style.display = "inline-block";
      staticIcon.style.display = "none";
    } else {
      spinner.style.display = "none";
      staticIcon.style.display = "inline-block";
    }
  }

  // Button active state reflects *intent* not transient speaking
  const listeningActive = (stateName === "listening" || stateName === "confirming");
  micBtns.forEach(b => b.classList.toggle("active", listeningActive));
  micLabels.forEach(l => { l.textContent = listeningActive ? "Listening..." : (stateName === "speaking" ? "Speaking..." : "Start Talking"); });

  if (pillMicIcon) {
    pillMicIcon.textContent = listeningActive ? "mic" : "mic_off";
  }

  if (stateName !== "speaking" && stateName !== "listening" && stateName !== "confirming") {
    // idle/thinking: keep scale reset unless visualizer is active
    if (!activeSphereAnalyser) resetSphereScale();
  }
}

function stepChipLabel(step) {
  const ev = step.event || "";
  const d = step.data || {};
  if (ev === "tool") return "Using " + String(d.tool || "tool").replace(/_/g, " ");
  if (ev === "stage") {
    const phase = d.phase || "";
    if (phase === "analyzing") return "Analyzing request";
    if (phase === "understanding") return "Understanding intent";
    if (phase === "thinking") return "Reasoning (round " + (d.round || 1) + ")";
    if (phase === "synthesizing") return "Synthesizing voice";
    if (phase === "ready") return "Speaking response";
    if (d.text) return String(d.text).slice(0, 60);
  }
  if (ev === "approval_required") return "Approval needed: " + (d.tool || "action");
  return ev || "Activity";
}

function appendPaneStep(step) {
  const pane = $("#voicePaneTranscript");
  if (!pane) return;
  const emptyP = $("#voicePaneEmpty");
  if (emptyP) emptyP.remove();
  const div = document.createElement("div");
  div.className = "text-cyan-300/90 font-mono text-[10.5px] truncate";
  div.textContent = "• " + stepChipLabel(step);
  pane.appendChild(div);
  pane.scrollTop = pane.scrollHeight;
}

function renderVoiceActivityStep(step) {
  if (!step || step.event === "delta") return;
  appendPaneStep(step);
  const container = $("#voiceLiveSteps");
  const empty = $("#voiceStepsEmpty");
  if (empty) empty.remove();
  if (!container) return;

  const ev = step.event || "";
  const data = step.data || {};
  let icon = "bolt";
  let label = "Activity";
  let cls = "";

  if (ev === "tool" || ev === "stage" && data.phase === "running_tool") {
    icon = "build";
    cls = "tool";
    label = `Tool: ${data.tool || data.name || "executing"}`;
    if (data.args) label += ` (${esc(JSON.stringify(data.args)).slice(0, 45)})`;
  } else if (ev === "supervisor") {
    icon = "psychology";
    label = `Intent: ${data.intent || "analyzing"}`;
  } else if (ev === "stage") {
    icon = "sync";
    label = data.text || `Phase: ${data.phase || "processing"}`;
  } else if (ev === "approval_required") {
    icon = "gavel";
    cls = "action";
    label = `Approval Required: ${data.tool || "WhatsApp action"}`;
  } else if (ev === "ready") {
    icon = "volume_up";
    cls = "done";
    label = "Spoken response ready";
  }

  const el = document.createElement("div");
  el.className = `voice-step-chip ${cls}`;
  el.innerHTML = `
    <span class="material-symbols-outlined text-[13px]">${icon}</span>
    <span class="truncate flex-1">${esc(label)}</span>
    <time class="text-[9px] opacity-60 font-mono">${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time>
  `;
  container.appendChild(el);
  container.scrollTop = container.scrollHeight;

  const countBadge = $("#voiceActionCount");
  if (countBadge) countBadge.textContent = `${container.children.length} steps`;
  const pillSub = $("#pillSubText");
  if (pillSub) pillSub.textContent = label.slice(0, 32);
}

function appendVoiceTranscript(role, text) {
  const boxes = [$("#voiceTranscriptBox"), $("#voicePaneTranscript")].filter(Boolean);
  const empties = [$("#voiceEmptyTranscript"), $("#voicePaneEmpty")].filter(Boolean);
  empties.forEach(e => e && e.remove());
  if (!boxes.length || !text) return;

  const isUser = role === "user";
  boxes.forEach(box => {
    const row = document.createElement("div");
    row.className = `flex gap-2 ${isUser ? "justify-end" : "justify-start"}`;
    row.innerHTML = `
      <div class="px-3 py-1.5 rounded-xl max-w-[85%] ${
        isUser ? "bg-cyan-600/30 text-cyan-200 border border-cyan-500/30" : "bg-slate-800/90 text-slate-200 border border-slate-700"
      }">
        <span class="font-semibold text-[10px] block opacity-75 mb-0.5">${isUser ? "You" : "Voice Agent"}</span>
        <p class="leading-relaxed">${esc(text)}</p>
      </div>
    `;
    box.appendChild(row);
    box.scrollTop = box.scrollHeight;
  });
}

// Interim transcript overlay (ephemeral, not committed)
function showInterimTranscript(text) {
  // Show in pane transcript as faint italic line, replace previous interim
  const pane = $("#voicePaneTranscript");
  if (!pane) return;
  let el = document.getElementById("voiceInterimLine");
  if (!text) {
    el?.remove();
    return;
  }
  if (!el) {
    el = document.createElement("div");
    el.id = "voiceInterimLine";
    el.className = "text-slate-400 italic text-[11px] truncate px-1";
    pane.appendChild(el);
  }
  el.textContent = "… " + text;
  pane.scrollTop = pane.scrollHeight;
}

function pauseRecognition() {
  if (!voiceRecognition || !isRecognizing) return;
  try {
    // Prevent onend auto-restart while paused
    isRecognizing = false;
    voiceRecognition.stop();
  } catch {}
}

function resumeRecognitionIfNeeded() {
  if (!isVoiceListeningIntent) return;
  if (isVoiceSpeaking || isHandlingCommand) return;
  if (isRecognizing) return;
  if (!voiceRecognition) {
    voiceRecognition = initVoiceRecognition();
    if (!voiceRecognition) return;
  }
  try {
    isRecognizing = true;
    // Keep intent sync
    isVoiceListening = true;
    isVoiceListeningIntent = true;
    voiceRecognition.start();
    // ensure orb shows listening (unless confirming)
    if (awaitingConfirmation) {
      setVoiceOrbState("confirming", "Say yes or no...", "help");
    } else {
      setVoiceOrbState("listening", "Listening to you...", "mic");
    }
    startMicVisualizer();
  } catch (e) {
    console.warn("[voice] resume start failed", e);
    isRecognizing = false;
  }
}

function initVoiceRecognition() {
  const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRec) {
    toast("Speech Recognition not supported in this browser. Please use Chrome or Edge.", "warn");
    return null;
  }
  const rec = new SpeechRec();
  rec.continuous = true;
  rec.interimResults = true;
  rec.lang = getVoiceLang();
  // Keep lang in sync if user changes it elsewhere
  try {
    window.addEventListener("storage", (e) => {
      if (e.key === VOICE_LANG_KEY && e.newValue) rec.lang = e.newValue;
    });
  } catch {}

  rec.onstart = () => {
    isRecognizing = true;
    isVoiceListening = true;
    isVoiceListeningIntent = true;
    if (awaitingConfirmation) {
      setVoiceOrbState("confirming", "Listening for yes or no...", "help");
    } else {
      setVoiceOrbState("listening", "Listening to you...", "mic");
    }
    if (!isVoiceSpeaking) startMicVisualizer();
  };

  let _finalBuffer = "";
  let _interimBuffer = "";
  rec.onresult = (event) => {
    if (isVoiceSpeaking || isHandlingCommand) return;
    // Use resultIndex to avoid reprocessing old results — 5Why fix for duplicate triggers
    let newFinal = "";
    let newInterim = "";
    for (let i = event.resultIndex; i < event.results.length; ++i) {
      const res = event.results[i];
      const transcript = res[0].transcript;
      if (res.isFinal) newFinal += transcript + " ";
      else newInterim += transcript + " ";
    }
    if (newFinal) _finalBuffer = (_finalBuffer + " " + newFinal).trim();
    _interimBuffer = newInterim.trim();
    // Also reconstruct combined for display, but prefer buffered final
    const combined = (_finalBuffer + " " + _interimBuffer).trim();
    const displayText = _interimBuffer || _finalBuffer;
    if (displayText) {
      showInterimTranscript(displayText);
      if (!awaitingConfirmation) {
        setVoiceOrbState("listening", `Hearing: "${displayText.slice(0,32)}…"`, "mic");
      }
    }

    if (combined) {
      clearTimeout(speechDebounceTimer);
      // Adaptive silence: fast path when we have a final, slow fallback otherwise
      const hasFinal = !!_finalBuffer;
      const silenceMs = hasFinal ? VOICE_SILENCE_MS : VOICE_SILENCE_FALLBACK_MS;
      speechDebounceTimer = setTimeout(() => {
        showInterimTranscript("");
        const toHandle = (_finalBuffer || combined).trim();
        // Reset buffers before handling to avoid double-send
        _finalBuffer = "";
        _interimBuffer = "";
        if (!toHandle) return;
        if (awaitingConfirmation) {
          handleVoiceConfirmation(toHandle);
        } else {
          handleVoiceCommand(toHandle);
        }
        try { rec.stop(); } catch {}
      }, silenceMs);
    }
  };
  // Keep wrapper for external clear: allow handleVoiceCommand to reset buffers
  rec._resetBuffers = () => { _finalBuffer = ""; _interimBuffer = ""; };

  rec.onerror = (err) => {
    console.warn("[voice] rec error:", err);
    // no-speech is benign — keep listening intent but reflect paused orb
    if (err.error === "no-speech" || err.error === "audio-capture") {
      // Keep intent; will restart onend if needed
      return;
    }
    // For aborted/network errors, show idle but keep intent for restart
    if (!isVoiceListeningIntent) {
      setVoiceOrbState("idle", "Microphone paused. Tap to talk.", "mic_off");
      isRecognizing = false;
    }
  };

  rec.onend = () => {
    isRecognizing = false;
    showInterimTranscript("");
    // Half-duplex: don't restart while speaking or handling
    if (isVoiceSpeaking || isHandlingCommand) {
      setVoiceOrbState(isVoiceSpeaking ? "speaking" : "thinking", isVoiceSpeaking ? "Speaking..." : "Processing...", isVoiceSpeaking ? "volume_up" : "psychology");
      return;
    }
    if (isVoiceListeningIntent) {
      // Auto-restart listening (continuous) — but with small delay to avoid tight loop
      setTimeout(() => {
        if (isVoiceListeningIntent && !isVoiceSpeaking && !isHandlingCommand && !isRecognizing) {
          try {
            isRecognizing = true;
            rec.start();
          } catch {}
        } else if (!isVoiceListeningIntent) {
          setVoiceOrbState("idle", "Tap circle or mic to speak", "mic");
          stopMicVisualizer();
        }
      }, 400);
    } else {
      setVoiceOrbState("idle", "Tap circle or mic to speak", "mic");
      stopMicVisualizer();
    }
  };

  rec.onspeechstart = () => {
    // User started speaking — ensure we interrupt any playback (barge-in)
    if (isVoiceSpeaking) {
      interruptVoicePlayback(false); // false => don't reset to idle, keep listening
    }
  };

  return rec;
}

function initAmbientWakeListener() {
  const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRec) return null;
  const rec = new SpeechRec();
  rec.continuous = true;
  rec.interimResults = true;
  rec.lang = getVoiceLang();

  rec.onstart = () => {
    isAmbientListening = true;
    updateWakeWordUI(true);
  };

  rec.onresult = (event) => {
    if (isVoiceSpeaking || isHandlingCommand || isVoiceListeningIntent) return;
    for (let i = event.resultIndex; i < event.results.length; ++i) {
      const res = event.results[i];
      const transcript = (res[0]?.transcript || "").trim();
      const lower = transcript.toLowerCase();
      // Match "uhu", "oohu", "oo-hoo", "hey uhu", "ok uhu", "wiki wiki"
      const match = lower.match(/\b(?:hey\s+|ok\s+|yo\s+)?(uhu|oohu|oo-hoo|uhoo|uh-hu|wiki[\s-]*wiki)\b/i);
      if (match) {
        const matchIdx = match.index + match[0].length;
        const trailing = transcript.slice(matchIdx).replace(/^[\s,:\-\.!]+/, "").trim();
        console.log("[wake] Uhu wake word detected. Trailing command:", trailing);
        try { rec.stop(); } catch {}
        isAmbientListening = false;
        handleWakeWordTrigger(trailing);
        break;
      }
    }
  };

  rec.onerror = (err) => {
    if (err.error === "no-speech" || err.error === "audio-capture") return;
    if (err.error === "not-allowed" || err.error === "service-not-allowed") {
      console.warn("[wake] mic permission not granted for ambient wake listener");
      isAmbientListening = false;
    }
  };

  rec.onend = () => {
    isAmbientListening = false;
    if (isWakeEnabled && !isVoiceSpeaking && !isHandlingCommand && !isVoiceListeningIntent) {
      setTimeout(() => {
        if (isWakeEnabled && !isVoiceSpeaking && !isHandlingCommand && !isVoiceListeningIntent && !isAmbientListening) {
          try {
            isAmbientListening = true;
            rec.start();
          } catch {}
        }
      }, 700);
    }
  };

  return rec;
}

function startAmbientWakeListener() {
  if (!isWakeEnabled) return;
  if (isVoiceSpeaking || isHandlingCommand || isVoiceListeningIntent) return;
  if (!ambientWakeRecognition) {
    ambientWakeRecognition = initAmbientWakeListener();
  }
  if (ambientWakeRecognition && !isAmbientListening) {
    try {
      isAmbientListening = true;
      ambientWakeRecognition.start();
    } catch {}
  }
  updateWakeWordUI(true);
}

function stopAmbientWakeListener() {
  if (ambientWakeRecognition && isAmbientListening) {
    isAmbientListening = false;
    try { ambientWakeRecognition.stop(); } catch {}
  }
  updateWakeWordUI(false);
}

function toggleWakeWordListener() {
  isWakeEnabled = !isWakeEnabled;
  localStorage.setItem(WAKE_ENABLED_KEY, isWakeEnabled ? "true" : "false");
  if (isWakeEnabled) {
    startAmbientWakeListener();
    toast('Uhu wake-word enabled: say "uhu" anytime', "ok", 4000);
  } else {
    stopAmbientWakeListener();
    toast("Uhu wake-word listener disabled", "warn", 3000);
  }
  updateWakeWordUI(isWakeEnabled);
}

function handleWakeWordTrigger(trailingCommand) {
  openVoiceModal(true); // skip generic greeting

  const cmd = (trailingCommand || "").trim();
  if (cmd && cmd.length >= 2) {
    toast(`Uhu: "${cmd}"`, "ok", 3000);
    appendVoiceTranscript("user", `uhu ${cmd}`);
    try { if ($("#chatlog")) bubble("msg-user", esc(`uhu ${cmd}`)); } catch {}
    handleVoiceCommand(cmd);
  } else {
    const ultronAck = "Yes boss? Uhu online.";
    appendVoiceTranscript("agent", ultronAck);
    try { if ($("#chatlog")) bubble("msg-agent", formatMarkdown(ultronAck), ultronAck); } catch {}
    setVoiceOrbState("speaking", "Yes boss? Uhu online.", "volume_up");
    speakWithBrowser(ultronAck, () => {
      isVoiceListeningIntent = true;
      isVoiceListening = true;
      resumeRecognitionIfNeeded();
      setVoiceOrbState("listening", "Listening to you, boss...", "mic");
    });
  }
}

async function handleVoiceCommand(text) {
  const trimmed = (text || "").trim();
  if (!trimmed) return;
  if (trimmed.length < 2) return;
  if (handleVoiceCommand._lastText === trimmed && Date.now() - (handleVoiceCommand._lastAt||0) < 5000) {
    return;
  }
  handleVoiceCommand._lastText = trimmed;
  handleVoiceCommand._lastAt = Date.now();

  appendVoiceTranscript("user", trimmed);
  // Mirror to main chatlog for unified normal-conversation history
  try { if ($("#chatlog")) bubble("msg-user", esc(trimmed)); } catch {}

  const stepsContainer = $("#voiceLiveSteps");
  if (stepsContainer) stepsContainer.innerHTML = "";
  $("#voiceApprovalCard")?.classList.add("hidden");
  pendingConfirmation = null;
  currentVoiceActionId = null;
  if (confirmationTimeout) { clearTimeout(confirmationTimeout); confirmationTimeout=null; }

  renderVoiceActivityStep({ event: "stage", data: { phase: "received", text: `Voice Input: "${trimmed.slice(0,60)}"` } });

  isHandlingCommand = true;
  pauseRecognition();
  stopMicVisualizer();
  setVoiceOrbState("thinking", "Agent reasoning & executing tools...", "psychology");
  showInterimTranscript("");

  const selectedVoice = getSelectedVoiceId() || ""; // empty → server default JBF George (single source of truth)
  const chatJid = (typeof chatState !== 'undefined' && chatState.jid) ? chatState.jid : null;

  // Try streaming first for low-latency, fallback to batch
  let usedStream = false;
  try {
    const streamResult = await streamVoiceChat(trimmed, chatJid, selectedVoice);
    if (streamResult && streamResult.handled) {
      usedStream = true;
      return;
    }
  } catch (e) {
    console.warn("[voice] stream failed, fallback to chat:", e);
  }
  if (usedStream) return;

  try {
    const { status, body } = await api("/agents/voice/chat", {
      method: "POST",
      body: JSON.stringify({ message: trimmed, chat_jid: chatJid, voice_id: selectedVoice }),
    });

    if (body && body.steps && Array.isArray(body.steps)) {
      body.steps.forEach(st => renderVoiceActivityStep(st));
    }

    const answer = (body && body.text) ? body.text : "I have completed your request.";
    const type = body ? body.type : "answer";

    if (type === "approval_required" && body.payload) {
      const payload = body.payload;
      const actionId = payload.actionId || payload.action_id;
      const tool = payload.tool || "action";
      const args = payload.args || {};
      let summary = payload.reason || answer;
      if (!summary || summary === "Done.") {
        if (tool === "send_message") {
          const recip = args.recipient || args.jid || "contact";
          const msg = args.message || "";
          summary = `Send "${msg.slice(0,80)}" to ${recip}`;
        } else if (tool === "delete_message") {
          summary = `Delete message in ${args.chat_jid || "chat"}`;
        } else if (tool === "initiate_audio_call") {
          summary = `Audio call to ${args.recipient || "contact"}`;
        } else if (tool === "initiate_video_call") {
          summary = `Video call to ${args.recipient || "contact"}`;
        } else {
          summary = `${tool}`;
        }
      }

      currentVoiceActionId = actionId;
      pendingConfirmation = { actionId, tool, summary, expiresAt: payload.expiresAt };

      const approvalCard = $("#voiceApprovalCard");
      if (approvalCard) {
        const t = $("#voiceActionType");
        const s = $("#voiceActionSummary");
        if (t) {
          if (tool === "send_message") t.textContent = "WhatsApp Send";
          else if (tool === "initiate_audio_call") t.textContent = "Audio Call";
          else if (tool === "initiate_video_call") t.textContent = "Video Call";
          else t.textContent = tool;
        }
        if (s) s.textContent = summary;
        approvalCard.classList.remove("hidden");
      }
      renderVoiceActivityStep({ event: "approval_required", data: { tool } });
      refreshApprovals();
      toast("Voice action proposed — say yes or no", "warn");
      appendVoiceTranscript("agent", answer);
      try{ if($("#chatlog")) bubble("msg-agent", formatMarkdown(answer), answer); }catch{}
      const msUntilExpiry = payload.expiresAt ? (new Date(payload.expiresAt).getTime() - Date.now()) : 20000;
      const timeoutMs = Math.min(Math.max(msUntilExpiry - 2000, 15000), 120000);
      const onPromptDone = () => {
        isHandlingCommand = false;
        awaitingConfirmation = true;
        isVoiceListeningIntent = true;
        if (confirmationTimeout) clearTimeout(confirmationTimeout);
        confirmationTimeout = setTimeout(() => {
          if (awaitingConfirmation) {
            awaitingConfirmation = false;
            pendingConfirmation = null;
            stopApprovalCountdown();
            appendVoiceTranscript("agent", "Confirmation timed out. Task cancelled.");
            try{ if($("#chatlog")) bubble("msg-agent", formatMarkdown("Confirmation timed out. Task cancelled."), "Confirmation timed out."); }catch{}
            speakWithBrowser("Confirmation timed out. Task cancelled.");
            setVoiceOrbState("idle", "Tap circle or mic to speak", "mic");
            $("#voiceApprovalCard")?.classList.add("hidden");
          }
        }, timeoutMs);
        startApprovalCountdown(payload.expiresAt || new Date(Date.now()+timeoutMs).toISOString());
        resumeRecognitionIfNeeded();
        setVoiceOrbState("confirming", "Say YES to execute or NO to cancel", "help");
        showInterimTranscript("");
        renderVoiceActivityStep({ event: "stage", data: { phase: "awaiting_confirmation", text: "Awaiting your Yes/No..." } });
      };

      if (body.audio_base64) {
        await playVoiceAudio(body.audio_base64, onPromptDone);
      } else {
        speakWithBrowser(answer, onPromptDone);
      }
      return;
    }

    appendVoiceTranscript("agent", answer);
    try{ if($("#chatlog")) bubble("msg-agent", formatMarkdown(answer), answer); }catch{}

    // Premium ElevenLabs audio when provided; otherwise browser TTS fallback
    // isHandlingCommand stays true until playback finishes, then we resume
    const onSpeakDone = () => {
      isHandlingCommand = false;
      awaitingConfirmation = false;
      // Resume listening after agent finishes speaking — not while speaking (echo guard)
      if (isVoiceListeningIntent) {
        resumeRecognitionIfNeeded();
      } else {
        setVoiceOrbState("idle", "Tap circle or mic to speak", "mic");
      }
    };

    if (body && body.audio_base64) {
      await playVoiceAudio(body.audio_base64, onSpeakDone);
    } else {
      speakWithBrowser(answer, onSpeakDone);
    }

  } catch (err) {
    appendVoiceTranscript("agent", `Error: ${err.message}`);
    try{ if($("#chatlog")) bubble("msg-error", esc(err.message)); }catch{}
    setVoiceOrbState("idle", "Connection error. Tap to retry.", "error");
    isHandlingCommand = false;
    if (isVoiceListeningIntent) resumeRecognitionIfNeeded();
  }
}

async function streamVoiceChat(message, chatJid, voiceId){
  const token = localStorage.getItem(tokenKey) || "";
  const headers = { Authorization: "Bearer " + token, "Content-Type": "application/json" };
  const res = await fetch("/agents/voice/stream", { method:"POST", headers, body: JSON.stringify({ message, chat_jid: chatJid, voice_id: voiceId }) });
  if(!res.ok || !(res.headers.get("content-type")||"").includes("text/event-stream")){
    throw new Error("voice stream unavailable");
  }
  let voiceResult = null;
  let audioChunks = [];
  let sawTerminal = false;
  let usedBrowserFallback = false;
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf="";
  // Helper to process SSE blocks
  async function processBlock(block){
    let ev="message", dataStr="";
    for(const line of block.split("\n")){
      if(line.startsWith("event:")) ev=line.slice(6).trim();
      else if(line.startsWith("data:")) dataStr+=line.slice(5).trim();
    }
    if(!dataStr) return;
    let data; try{ data=JSON.parse(dataStr);}catch{ return; }
    switch(ev){
      case "stage":
      case "tool":
        renderVoiceActivityStep({event: ev, data});
        break;
      case "delta":
        // not rendering delta for voice, but could show live tokens if needed
        break;
      case "voice_result":{
        sawTerminal=true;
        voiceResult=data;
        const answer=data.text||"";
        const type=data.type||"answer";
        appendVoiceTranscript("agent", answer);
        try{ if($("#chatlog")) bubble("msg-agent", formatMarkdown(answer), answer); }catch{}
        if(type==="approval_required" && data.payload){
          const payload=data.payload;
          const actionId=payload.actionId||payload.action_id;
          const tool=payload.tool||"action";
          const args=payload.args||{};
          let summary=payload.reason||answer;
          if(!summary||summary==="Done."){
            if(tool==="send_message"){ const recip=args.recipient||"contact"; const msg=args.message||""; summary=`Send "${msg.slice(0,80)}" to ${recip}`; }
            else if(tool==="delete_message"){ summary=`Delete message in ${args.chat_jid||"chat"}`; }
            else if(tool==="initiate_audio_call"){ summary=`Audio call to ${args.recipient||"contact"}`; }
            else if(tool==="initiate_video_call"){ summary=`Video call to ${args.recipient||"contact"}`; }
            else summary=tool;
          }
          currentVoiceActionId=actionId;
          pendingConfirmation={ actionId, tool, summary, expiresAt: data.expiresAt || payload.expiresAt };
          const card=$("#voiceApprovalCard");
          if(card){
            const t=$("#voiceActionType"), s=$("#voiceActionSummary");
            if(t){
              if(tool==="send_message") t.textContent="WhatsApp Send";
              else if(tool==="initiate_audio_call") t.textContent="Audio Call";
              else if(tool==="initiate_video_call") t.textContent="Video Call";
              else t.textContent=tool;
            }
            if(s) s.textContent=summary;
            card.classList.remove("hidden");
          }
          renderVoiceActivityStep({event:"approval_required", data:{tool}});
          refreshApprovals();
          toast("Voice action proposed — say yes or no","warn");
          // Prepare confirmation timer from server expiresAt
          const msUntilExpiry = pendingConfirmation.expiresAt ? (new Date(pendingConfirmation.expiresAt).getTime() - Date.now()) : 20000;
          const timeoutMs = Math.min(Math.max(msUntilExpiry - 2000, 15000), 120000);
          // Will be handled after audio playback
          voiceResult._timeoutMs = timeoutMs;
        }
        // Render steps if included
        if(data.payload && data.payload.steps) {/* already via stage events */}
        break;
      }
      case "audio_chunk":{
        if(data.b64){
          audioChunks.push({index: data.index||0, b64: data.b64, isLast: !!data.isLast});
        } else if(data.fallback==="browser" && voiceResult){
          usedBrowserFallback = true;
        }
        if(data.isLast){
          // Trigger playback after last chunk queued
          // But we may still be waiting for more chunks; handle after loop
        }
        break;
      }
      case "error":{
        appendVoiceTranscript("agent", `Error: ${data.message||"voice stream error"}`);
        break;
      }
      case "done":
        break;
    }
  }
  while(true){
    const {done, value} = await reader.read();
    if(done) break;
    buf+=decoder.decode(value,{stream:true});
    let idx;
    while((idx=buf.indexOf("\n\n"))!==-1){
      const block=buf.slice(0,idx);
      buf=buf.slice(idx+2);
      await processBlock(block);
    }
  }
  // After stream, handle audio playback + state transitions
  if(!sawTerminal) throw new Error("stream ended without voice_result");
  const isApproval = voiceResult.type==="approval_required";
  const answerText = voiceResult.text||"";
  // Play audio chunks sequentially if available — 5 Why: silent browser fallback looked like voice drift
  async function playChunksSequentially(){
    if(audioChunks.length===0){
      if(usedBrowserFallback) toast("Using browser voice — ElevenLabs unavailable or quota. Your chosen voice is kept for next time.", "warn", 6000);
      // show fallback badge via status
      if(usedBrowserFallback) setVoiceOrbState("speaking", "Browser voice…", "volume_up");
      return new Promise(resolve=>{
        speakWithBrowser(answerText, resolve);
      });
    }
    audioChunks.sort((a,b)=>a.index-b.index);
    for(const ch of audioChunks){
      await new Promise(resolve=>{
        playVoiceAudio(ch.b64, resolve);
      });
    }
  }
  if(isApproval){
    await playChunksSequentially();
    // Enter confirming state after prompt spoken
    isHandlingCommand=false;
    awaitingConfirmation=true;
    isVoiceListeningIntent=true;
    const timeoutMs = voiceResult._timeoutMs || 20000;
    if(confirmationTimeout) clearTimeout(confirmationTimeout);
    confirmationTimeout=setTimeout(()=>{
      if(awaitingConfirmation){
        awaitingConfirmation=false;
        pendingConfirmation=null;
        stopApprovalCountdown();
        appendVoiceTranscript("agent","Confirmation timed out. Task cancelled.");
        try{ if($("#chatlog")) bubble("msg-agent", formatMarkdown("Confirmation timed out. Task cancelled."), "Confirmation timed out."); }catch{}
        speakWithBrowser("Confirmation timed out. Task cancelled.");
        setVoiceOrbState("idle","Tap circle or mic to speak","mic");
        $("#voiceApprovalCard")?.classList.add("hidden");
      }
    }, timeoutMs);
    startApprovalCountdown(pendingConfirmation?.expiresAt || new Date(Date.now()+timeoutMs).toISOString());
    resumeRecognitionIfNeeded();
    setVoiceOrbState("confirming","Say YES to execute or NO to cancel","help");
    showInterimTranscript("");
    renderVoiceActivityStep({event:"stage", data:{phase:"awaiting_confirmation", text:"Awaiting your Yes/No..."}});
  } else {
    await playChunksSequentially();
    try{ if($("#chatlog") && answerText) bubble("msg-agent", formatMarkdown(answerText), answerText); }catch{}
    isHandlingCommand=false;
    awaitingConfirmation=false;
    if(isVoiceListeningIntent) resumeRecognitionIfNeeded();
    else setVoiceOrbState("idle","Tap circle or mic to speak","mic");
  }
  return {handled:true, voiceResult, audioChunks};
}

async function handleVoiceConfirmation(text) {
  const raw = (text || "").trim();
  if (!raw) return;
  // Only first sentence matters for yes/no; take up to 20 chars lowercased?
  const lower = raw.toLowerCase().trim();
  appendVoiceTranscript("user", raw);
  showInterimTranscript("");

  // Clear debounce timer for confirmation
  clearTimeout(speechDebounceTimer);

  // Strict yes/no detection: prefix word-boundary, no substring shadowing (e.g., "yesterday" ≠ yes)
  // 5 Why improved: also handle Hindi "bhej do", "kar do", "send kar do" as yes intent
  const yesMatch = CONFIRM_YES_RE.test(lower);
  const noMatch = CONFIRM_NO_RE.test(lower);
  const firstToken = lower.split(/[\s,.!?]+/).filter(Boolean)[0] || "";
  const yesTokens = new Set(["yes","yeah","yep","haan","ha","haanji","hanji","ok","okay","sure","bhej","bhejo","bhejdo","kardo","kar","karo","send","confirm","proceed","go","theek","accha"]);
  const noTokens = new Set(["no","nope","nahi","nah","cancel","stop","abort","reject","ruko","mat"]);
  // Additional intent signals for Hindi action phrases
  const hasHindiYesIntent = /(bhej\s*(do|de|dijiye)?|kar\s*(do|de|dijiye)?|send\s*(kar|kardo)?)/i.test(lower);
  let decision = null;
  if (yesMatch && noMatch) {
    decision = null;
  } else if (yesMatch && !noMatch) {
    decision = true;
  } else if (noMatch && !yesMatch) {
    decision = false;
  } else if (yesTokens.has(firstToken) || hasHindiYesIntent) {
    // If awaiting confirmation and user says "bhej do" etc without yes word, still treat as yes
    // But ensure not mixed with no
    if (!noMatch) decision = true;
  } else if (noTokens.has(firstToken)) {
    decision = false;
  }

  if (decision === null) {
    // Unclear — ask again via voice
    const reprompt = "I didn't catch that. Please say yes to approve or no to cancel.";
    appendVoiceTranscript("agent", reprompt);
    renderVoiceActivityStep({ event: "stage", data: { phase: "awaiting_confirmation", text: "Heard unclear reply, reprompting..." } });
    // Keep awaitingConfirmation true, speak reprompt then resume
    const onDone = () => {
      // stay in confirming, resume listening
      resumeRecognitionIfNeeded();
      setVoiceOrbState("confirming", "Say YES or NO", "help");
    };
    // Use browser TTS for reprompt (quick)
    speakWithBrowser(reprompt, onDone);
    return;
  }

  // We have a clear yes/no — stop confirmation mode
  awaitingConfirmation = false;
  stopApprovalCountdown();
  if (confirmationTimeout) { clearTimeout(confirmationTimeout); confirmationTimeout = null; }
  const pending = pendingConfirmation;
  const actionId = pending ? pending.actionId : currentVoiceActionId;
  const pendingToolForCall = pending?.tool || null;
  pendingConfirmation = null;
  $("#voiceApprovalCard")?.classList.add("hidden");

  // Prevent handling as normal command
  isHandlingCommand = true;
  pauseRecognition();
  setVoiceOrbState("thinking", decision ? "Approving and executing..." : "Cancelling...", "psychology");

  if (!actionId) {
    appendVoiceTranscript("agent", "No pending action found to confirm.");
    speakWithBrowser("No pending action found.", () => {
      isHandlingCommand = false;
      resumeRecognitionIfNeeded();
    });
    return;
  }

  try {
    await decideVoiceAction(decision, actionId, pendingToolForCall);
    // decideVoiceAction will speak result and resume listening via its callback
  } catch (err) {
    toast(`Failed to execute: ${err.message}`, "bad");
    appendVoiceTranscript("agent", `Failed: ${err.message}`);
    isHandlingCommand = false;
    resumeRecognitionIfNeeded();
  }
}

async function decideVoiceAction(approved, actionIdOverride, pendingToolOverride) {
  const actionId = actionIdOverride || currentVoiceActionId;
  if (!actionId) return;
  const pendingTool = pendingToolOverride || pendingConfirmation?.tool || null;
  const wasCall = pendingTool === "initiate_audio_call" || pendingTool === "initiate_video_call";
  const isVideo = pendingTool === "initiate_video_call";
  const card = $("#voiceApprovalCard");
  currentVoiceActionId = null;
  if (card) card.classList.add("hidden");

  // Keep handling flag
  isHandlingCommand = true;
  pauseRecognition();

  try {
    // Direct approve to capture simulated flag for calls — 5 Why: hallucination if we claim real call when simulated
    const {status, body} = await api(`/agents/whatsapp/approve/${actionId}`, {
      method: "POST",
      body: JSON.stringify({ approved }),
    });
    let msg, spoken, isSimulated=false;
    if(status===200 && body.status==="executed"){
      isSimulated = !!(body.result?.simulated || body.result?.bridge?.simulated);
      if(wasCall && approved){
        const callType = isVideo ? "video" : "audio";
        if(isSimulated){
          msg = `Call logged — WhatsApp Web cannot place ${callType} calls directly ✓`;
          spoken = `${callType} call logged. WhatsApp Web cannot place calls directly, please dial ${pendingConfirmation?.summary?.split("to")[1] || "the contact"} on your phone.`;
        } else {
          msg = `${callType} call initiated on WhatsApp ✓`;
          spoken = `Initiating ${callType} call now.`;
        }
      } else {
        msg = approved ? "Action approved and executed on WhatsApp ✓" : "Action rejected ✕";
        spoken = approved ? "Executed successfully on WhatsApp." : "Cancelled. Task not executed.";
      }
      renderVoiceActivityStep({ event: "ready", data: { text: msg } });
      appendVoiceTranscript("agent", spoken);
      try{ if($("#chatlog")) bubble("msg-agent", formatMarkdown(spoken), spoken); }catch{}
      refreshApprovals();
      try{ loadChatList(); if(chatState.jid) refreshConversation(); }catch{}
      stopApprovalCountdown();
      const onDone = () => {
        isHandlingCommand = false;
        if (isVoiceListeningIntent) resumeRecognitionIfNeeded();
        else setVoiceOrbState("idle", "Tap circle or mic to speak", "mic");
      };
      speakWithBrowser(spoken, onDone);
      // Also toast the bridge message for transparency
      if(wasCall && isSimulated) toast(body.result?.bridge?.message || "Call logged for manual dial", "warn", 6000);
      else if(approved) toast(msg, "ok");
      return;
    } else if(body.status==="rejected"){
      msg = "Action rejected ✕";
      spoken = "Cancelled. Task not executed.";
      renderVoiceActivityStep({ event: "ready", data: { text: msg } });
      appendVoiceTranscript("agent", spoken);
      try{ if($("#chatlog")) bubble("msg-agent", formatMarkdown(spoken), spoken); }catch{}
      refreshApprovals();
      stopApprovalCountdown();
      speakWithBrowser(spoken, () => {
        isHandlingCommand = false;
        if (isVoiceListeningIntent) resumeRecognitionIfNeeded();
        else setVoiceOrbState("idle", "Tap circle or mic to speak", "mic");
      });
      return;
    } else {
      throw new Error(body.detail || body.message || "Approve failed");
    }
  } catch (err) {
    toast(`Failed to execute: ${err.message}`, "bad");
    appendVoiceTranscript("agent", `Execution failed: ${err.message}`);
    try{ if($("#chatlog")) bubble("msg-error", esc(err.message)); }catch{}
    stopApprovalCountdown();
    isHandlingCommand = false;
    if (isVoiceListeningIntent) resumeRecognitionIfNeeded();
  }
}

async function playVoiceAudio(b64Audio, onDone) {
  // Half-duplex: ensure mic is off while playing
  pauseRecognition();
  stopMicVisualizer();
  if ("speechSynthesis" in window) window.speechSynthesis.cancel();
  if (currentVoiceAudio) {
    try { currentVoiceAudio.pause(); currentVoiceAudio.currentTime = 0; } catch {}
    currentVoiceAudio = null;
  }

  isVoiceSpeaking = true;
  setVoiceOrbState("speaking", "Speaking...", "volume_up");
  $("#voiceStopBtn")?.classList.remove("hidden");
  $("#pillStopBtn")?.classList.remove("hidden");

  return new Promise((resolve) => {
    currentVoiceAudio = new Audio("data:audio/mpeg;base64," + b64Audio);

    let doneCalled = false;
    const doDone = (cb) => {
      if (doneCalled) return;
      doneCalled = true;
      resetSphereScale();
      $("#voiceStopBtn")?.classList.add("hidden");
      $("#pillStopBtn")?.classList.add("hidden");
      isVoiceSpeaking = false;
      currentVoiceAudio = null;
      if (typeof cb === 'function') cb();
      else if (typeof onDone === 'function') onDone();
      resolve();
    };

    currentVoiceAudio.onended = () => doDone();
    currentVoiceAudio.onerror = () => {
      setVoiceOrbState("idle", "Playback error", "error");
      doDone();
    };

    attachPlaybackAnalyser(currentVoiceAudio).then(routed => {
      if (routed) startSphereLoop();
    });

    currentVoiceAudio.play().catch((e) => {
      console.warn("[voice] audio play rejected:", e);
      resetSphereScale();
      $("#voiceStopBtn")?.classList.add("hidden");
      $("#pillStopBtn")?.classList.add("hidden");
      isVoiceSpeaking = false;
      setVoiceOrbState("idle", "Tap circle or mic to speak", "mic");
      doDone();
    });

    // Also hook onDone if caller passed it and audio ends — already handled via onended
  });
}

function shortenForSpeech(text) {
  const clean = (text || "").replace(/\s+/g, " ").trim();
  if (clean.length <= 280) return clean;
  const sentences = clean.split(/(?<=[.!?])\s+/);
  let out = "";
  for (const sen of sentences) {
    if ((out + " " + sen).trim().length > 280 && out) break;
    out += (out ? " " : "") + sen;
    if (out.length > 280) break;
  }
  return (out || clean.slice(0, 277) + "...").trim();
}

function speakWithBrowser(text, onDone) {
  if (!("speechSynthesis" in window)) {
    setVoiceOrbState("idle", "Ready", "mic");
    if (typeof onDone === 'function') onDone();
    return;
  }
  // Half-duplex: pause recognition while speaking to prevent echo self-trigger
  pauseRecognition();
  stopMicVisualizer();
  if (currentVoiceAudio) {
    try { currentVoiceAudio.pause(); currentVoiceAudio.currentTime = 0; } catch {}
    currentVoiceAudio = null;
  }
  window.speechSynthesis.cancel();
  isVoiceSpeaking = true;
  const utter = new SpeechSynthesisUtterance(shortenForSpeech(text));
  utter.rate = 1.05;
  // Prefer a natural voice if available
  try {
    const voices = window.speechSynthesis.getVoices();
    const pref = voices.find(v => v.name.includes("Google") && v.lang.startsWith("en")) || voices[0];
    if (pref) utter.voice = pref;
  } catch {}

  utter.onstart = () => {
    $("#voiceStopBtn")?.classList.remove("hidden");
    $("#pillStopBtn")?.classList.remove("hidden");
    setVoiceOrbState("speaking", "Speaking...", "volume_up");
    // No mic visualizer during TTS — analyser will be driven by playback if we had audio element,
    // but browser TTS has no element, so sphere stays in speaking CSS animation
  };
  const finish = () => {
    $("#voiceStopBtn")?.classList.add("hidden");
    $("#pillStopBtn")?.classList.add("hidden");
    isVoiceSpeaking = false;
    if (typeof onDone === 'function') onDone();
    else {
      if (isVoiceListeningIntent && !isHandlingCommand && !awaitingConfirmation) {
        resumeRecognitionIfNeeded();
      } else if (awaitingConfirmation) {
        resumeRecognitionIfNeeded();
        setVoiceOrbState("confirming", "Say YES or NO", "help");
      } else {
        setVoiceOrbState("idle", "Tap circle or mic to speak", "mic");
      }
    }
  };
  utter.onend = finish;
  utter.onerror = () => {
    $("#voiceStopBtn")?.classList.add("hidden");
    $("#pillStopBtn")?.classList.add("hidden");
    isVoiceSpeaking = false;
    setVoiceOrbState("idle", "Playback error", "error");
    if (typeof onDone === 'function') onDone();
  };
  // Ensure voices loaded (Chrome lazy)
  if (window.speechSynthesis.getVoices().length === 0) {
    window.speechSynthesis.onvoiceschanged = () => {
      try { window.speechSynthesis.speak(utter); } catch {}
    };
    setTimeout(() => { try { window.speechSynthesis.speak(utter); } catch {} }, 250);
  } else {
    window.speechSynthesis.speak(utter);
  }
}

function interruptVoicePlayback(keepIntent) {
  const keepListening = (keepIntent === false) ? isVoiceListeningIntent : true;
  if ("speechSynthesis" in window) window.speechSynthesis.cancel();
  if (currentVoiceAudio) {
    try { currentVoiceAudio.pause(); currentVoiceAudio.currentTime = 0; } catch {}
    currentVoiceAudio = null;
  }
  resetSphereScale();
  isVoiceSpeaking = false;
  $("#voiceStopBtn")?.classList.add("hidden");
  $("#pillStopBtn")?.classList.add("hidden");
  if (keepListening && isVoiceListeningIntent && !isHandlingCommand) {
    resumeRecognitionIfNeeded();
    setVoiceOrbState(awaitingConfirmation ? "confirming" : "listening", awaitingConfirmation ? "Listening for yes/no..." : "Listening to you...", awaitingConfirmation ? "help" : "mic");
  } else if (!isVoiceListeningIntent) {
    setVoiceOrbState("idle", "Tap circle or mic to speak", "mic");
  }
}

function toggleVoiceListening() {
  if (!voiceRecognition) {
    voiceRecognition = initVoiceRecognition();
  }
  if (!voiceRecognition) return;

  if (isVoiceListeningIntent) {
    // Stop listening intent
    isVoiceListeningIntent = false;
    isVoiceListening = false;
    awaitingConfirmation = false;
    if (confirmationTimeout) { clearTimeout(confirmationTimeout); confirmationTimeout=null; }
    clearTimeout(speechDebounceTimer);
    showInterimTranscript("");
    try { voiceRecognition.stop(); } catch {}
    // isRecognizing will become false in onend
    stopMicVisualizer();
    interruptVoicePlayback(false);
    setVoiceOrbState("idle", "Microphone paused. Tap to talk.", "mic_off");
    if (isWakeEnabled) {
      setTimeout(() => { startAmbientWakeListener(); }, 500);
    }
  } else {
    // Start listening intent
    stopAmbientWakeListener();
    interruptVoicePlayback(false); // stop any TTS first (half-duplex)
    isVoiceListeningIntent = true;
    isVoiceListening = true;
    awaitingConfirmation = false;
    try {
      // Ensure fresh start
      isRecognizing = true;
      voiceRecognition.start();
    } catch (e) {
      // If already started, restart
      try { voiceRecognition.stop(); } catch {}
      setTimeout(() => { try { voiceRecognition.start(); isRecognizing=true; } catch {} }, 300);
    }
    startMicVisualizer();
    setVoiceOrbState("listening", "Listening to you...", "mic");
  }
}

async function loadVoiceList() {
  const sel = $("#voiceSelect");
  if (!sel || sel.children.length > 5) return;
  try {
    const { body } = await api("/agents/voice/voices");
    if (body.voices && body.voices.length) {
      const persisted = getPersistedVoiceId();
      const cur = persisted || sel.value;
      sel.innerHTML = body.voices
        .map(v => `<option value="${esc(v.voice_id)}">${esc(v.name)}</option>`)
        .join("");
      const paneSel = $("#voiceSelectPane");
      if (paneSel) paneSel.innerHTML = sel.innerHTML;
      // Keep what you choose (persisted) else keep cur else server default first
      const target = cur || (body.voices[0] && body.voices[0].voice_id) || "";
      if (target){
        sel.value = target;
        if(paneSel) paneSel.value = target;
        // If no persisted but we have a target from server default, persist it once
        if(!persisted && target) setPersistedVoiceId(target);
      }
    }
  } catch {}
}

let WAKE_GREETING = "Hello! How can I help with WhatsApp today?";
let wakeGreeted = false;

// Allow server to override wake greeting via /health
async function fetchWakeGreeting() {
  try {
    const res = await fetch("/health", { cache: "no-store" });
    const data = await res.json();
    if (data.voice && data.voice.wake_greeting) {
      WAKE_GREETING = String(data.voice.wake_greeting);
    }
  } catch {}
}
fetchWakeGreeting();

function openVoiceModal(skipGreeting = false) {
  const modal = $("#geminiVoiceModal");
  if (!modal) return;
  modal.classList.remove("hidden");
  $("#floatingVoicePill")?.classList.add("hidden");
  stopAmbientWakeListener();
  loadVoiceList();
  if (!isVoiceListeningIntent && !skipGreeting) {
    toggleVoiceListening();
  }

  if (!wakeGreeted && !skipGreeting) {
    wakeGreeted = true;
    setTimeout(() => {
      appendVoiceTranscript("agent", WAKE_GREETING);
      speakWithBrowser(WAKE_GREETING);
    }, 350);
  }
}

function minimizeVoiceModal() {
  $("#geminiVoiceModal")?.classList.add("hidden");
  $("#floatingVoicePill")?.classList.remove("hidden");
}

function expandVoiceModal() {
  $("#floatingVoicePill")?.classList.add("hidden");
  $("#geminiVoiceModal")?.classList.remove("hidden");
}

function closeVoiceModal(e) {
  if (e && e.stopPropagation) e.stopPropagation();
  const modal = $("#geminiVoiceModal");
  if (modal) modal.classList.add("hidden");
  // Full stop: cancel TTS, stop mic, clear confirmation
  if (confirmationTimeout) { clearTimeout(confirmationTimeout); confirmationTimeout=null; }
  stopApprovalCountdown();
  awaitingConfirmation = false;
  pendingConfirmation = null;
  clearTimeout(speechDebounceTimer);
  showInterimTranscript("");
  interruptVoicePlayback(false);
  stopMicVisualizer();
  if (voiceRecognition && isVoiceListeningIntent) {
    isVoiceListeningIntent = false;
    isVoiceListening = false;
    isRecognizing = false;
    try { voiceRecognition.stop(); } catch {}
  }
  setVoiceOrbState("idle", "Tap circle or mic to speak", "mic");
  if (isWakeEnabled) {
    setTimeout(() => { startAmbientWakeListener(); }, 500);
  }
}

