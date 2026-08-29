/* Live pairing page.
   - Token gate inline (stored to localStorage, same key as console)
   - QR fetched WITH Authorization header and rendered via blob URL
     (<img> tags cannot send bearer tokens)
   - Auto-reload on rotation; redirect to console once linked */
"use strict";

const $ = (s) => document.querySelector(s);
const TOKEN_KEY = "wa-agent-token";

const ROTATE_SECONDS = 60;
const RING_LEN = 2 * Math.PI * 19;

let lastGeneratedAt = 0;
let objectUrl = null;

function authHeaders() {
  return { Authorization: "Bearer " + localStorage.getItem(TOKEN_KEY) };
}

function ring(fraction) {
  const c = document.querySelector(".ring .fgc");
  c.style.strokeDasharray = String(RING_LEN);
  c.style.strokeDashoffset = String(RING_LEN * (1 - Math.max(0, Math.min(1, fraction))));
}

function setState(html) {
  $("#state").innerHTML = html;
}

function showTokenGate(show) {
  $("#tokenGate").style.display = show ? "" : "none";
}

async function saveToken() {
  const t = $("#tokenInput").value.trim();
  if (!t) return;
  localStorage.setItem(TOKEN_KEY, t);
  showTokenGate(false);
  poll();
}

async function loadQrImage() {
  const res = await fetch(`/agents/whatsapp/qr.png?t=${Date.now()}`, {
    headers: authHeaders(),
  });
  if (!res.ok) return false;
  const blob = await res.blob();
  if (objectUrl) URL.revokeObjectURL(objectUrl);
  objectUrl = URL.createObjectURL(blob);
  $("#qrimg").src = objectUrl;
  $("#qrimg").style.opacity = "1";
  return true;
}

let linkedRedirect = false;

async function autoFetchToken() {
  /* Loopback-only endpoint: the server hands its own token to local
     browsers so pairing needs zero terminal steps. */
  try {
    const res = await fetch("/pair-token", { cache: "no-store" });
    if (res.ok) {
      const { token } = await res.json();
      if (token) {
        localStorage.setItem(TOKEN_KEY, token);
        return true;
      }
    }
  } catch { /* fall back to manual entry */ }
  return false;
}

async function poll() {
  let token = localStorage.getItem(TOKEN_KEY);
  if (!token) {
    setState("Requesting access from local server…");
    const ok = await autoFetchToken();
    if (!ok) {
      showTokenGate(true);
      setState("Paste your service token to display the live QR.");
      ring(0);
      return;
    }
  }

  try {
    const res = await fetch("/agents/whatsapp/qr/meta", { headers: authHeaders() });
    if (res.status === 401) {
      localStorage.removeItem(TOKEN_KEY);
      const ok = await autoFetchToken();
      if (!ok) {
        showTokenGate(true);
        setState("That token was rejected — paste a valid one.");
        return;
      }
      return; // next tick proceeds with the fresh token
    }
    const meta = await res.json();

    if (meta.logged_in === true) {
      $("#scanline")?.remove();
      $("#frame").style.boxShadow = "0 14px 50px rgba(34,197,94,.35)";
      $("#qrimg").style.opacity = "0.22";
      setState('<span class="ok-big">✓ Phone linked successfully!</span><br><span class="tagline">Opening your console…</span>');
      if (!linkedRedirect) {
        linkedRedirect = true;
        setTimeout(() => (location.href = "/"), 2200);
      }
      return;
    }

    if (!meta.available) {
      setState("Waiting for the bridge to produce a QR…");
      ring(0);
      return;
    }

    if (meta.generated_at !== lastGeneratedAt) {
      lastGeneratedAt = meta.generated_at;
      await loadQrImage();
    }

    const age = Math.max(0, Math.floor(Date.now() / 1000 - lastGeneratedAt));
    ring(Math.max(0, ROTATE_SECONDS - age) / ROTATE_SECONDS);
    const remaining = Math.max(0, ROTATE_SECONDS - age);
    setState(
      remaining > 5
        ? `Point your phone at the code — new code in ${remaining}s`
        : "Refreshing code…"
    );
  } catch {
    setState("⚠ Bridge unreachable — is it running?");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.documentElement.dataset.theme = localStorage.getItem("wa-agent-theme") || "dark";
  const c = document.querySelector(".ring .fgc");
  if (c) {
    c.style.strokeDasharray = String(RING_LEN);
    c.style.strokeDashoffset = "0";
  }
  $("#saveToken").addEventListener("click", saveToken);
  $("#tokenInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") saveToken();
  });
  showTokenGate(false); // gate appears only if auto-token fails
  poll();
  setInterval(poll, 1500);
});
