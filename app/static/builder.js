// The builder chat: send a message (with any attached spreadsheets or documents), watch the AI work, and show the
// result in the ERP preview on the right.
(function () {
  "use strict";
  const root = document.querySelector(".builder");
  if (!root) return;
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const list = document.getElementById("messages");
  const prompt = document.getElementById("prompt");
  const frame = document.getElementById("frame");
  const addr = document.getElementById("addr");
  const newtab = document.getElementById("newtab");
  const send = document.getElementById("send");
  const picker = document.getElementById("files");
  const attachedBox = document.getElementById("attached");
  let busy = false, attached = [];

  const post = (url, body) => fetch(url, {method: "POST", headers: {"Content-Type": "application/json", "X-CSRFToken": csrf}, body: JSON.stringify(body || {})})
    .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); });

  function framePath() { try { const l = frame.contentWindow.location; return l.pathname + l.search; } catch (e) { return ""; } }
  function go(url) { if (url) frame.src = url; }
  frame.addEventListener("load", () => { const p = framePath(); addr.textContent = p; newtab.href = p; });
  document.getElementById("back").onclick = () => { try { frame.contentWindow.history.back(); } catch (e) {} };
  document.getElementById("reload").onclick = () => { try { frame.contentWindow.location.reload(); } catch (e) { frame.src = frame.src; } };

  function showAttached() {
    attachedBox.innerHTML = attached.map((f, i) => `<span class="filechip">${f.name.replace(/[&<>]/g, "")}<button type="button" data-unattach="${i}" aria-label="Remove">×</button></span>`).join("");
    attachedBox.hidden = !attached.length;
  }
  function attach(files) { attached = attached.concat(Array.from(files)).slice(0, 5); showAttached(); prompt.focus(); }
  picker.addEventListener("change", () => { attach(picker.files); picker.value = ""; });
  attachedBox.addEventListener("click", (ev) => { const b = ev.target.closest("[data-unattach]"); if (b) { attached.splice(Number(b.dataset.unattach), 1); showAttached(); } });
  const chat = document.querySelector(".chat");
  ["dragenter", "dragover"].forEach((e) => chat.addEventListener(e, (ev) => { ev.preventDefault(); chat.classList.add("dropping"); }));
  ["dragleave", "drop"].forEach((e) => chat.addEventListener(e, () => chat.classList.remove("dropping")));
  chat.addEventListener("drop", (ev) => { ev.preventDefault(); if (ev.dataTransfer.files.length) attach(ev.dataTransfer.files); });

  function scrollDown() { list.scrollTop = list.scrollHeight; }
  function place(html, id) {
    const tpl = document.createElement("template");
    tpl.innerHTML = html.trim();
    const node = tpl.content.firstElementChild;
    const old = id && list.querySelector(`.msg[data-id="${id}"]`);
    if (old) {
      const keep = old.querySelector(".thinking[data-scroll-end]");
      const wasAtEnd = !keep || keep.scrollTop + keep.clientHeight >= keep.scrollHeight - 8;
      old.replaceWith(node);
      const fresh = node.querySelector(".thinking[data-scroll-end]");
      if (fresh && wasAtEnd) fresh.scrollTop = fresh.scrollHeight;
    } else {
      list.appendChild(node);
    }
    tick();
    return node;
  }
  function tick() {
    list.querySelectorAll("[data-started]").forEach((el) => {
      const s = Math.max(0, Math.round((Date.now() - Date.parse(el.dataset.started)) / 1000));
      el.textContent = s ? `${s}s` : "";
    });
  }
  setInterval(tick, 1000);

  async function watch(id) {
    busy = true; send.disabled = true;
    const chip = document.querySelector(".ai-chip");
    if (chip) chip.lastChild.textContent = "Working…";
    let seen = null;
    for (;;) {
      await new Promise((r) => setTimeout(r, 900));
      let m;
      try { m = await fetch(root.dataset.poll + id).then((r) => r.json()); } catch (e) { continue; }
      const atEnd = list.scrollTop + list.clientHeight >= list.scrollHeight - 40;
      place(m.html, m.id);
      if (atEnd) scrollDown();
      if (m.version_after && m.version_after !== seen && m.status !== "done" && m.status !== "failed") {  // a part is saved: show it
        if (seen !== null || !framePath().includes("/t/")) { if (m.navigate && seen === null) go(m.navigate); else try { frame.contentWindow.location.reload(); } catch (e) {} }
        seen = m.version_after;
      }
      if (m.status === "done" || m.status === "failed") {
        if (m.version_after) document.getElementById("version").textContent = m.version_after;
        if (m.navigate) go(m.navigate); else try { frame.contentWindow.location.reload(); } catch (e) {}
        frame.classList.remove("flash-pulse"); void frame.offsetWidth; frame.classList.add("flash-pulse");
        break;
      }
    }
    busy = false; send.disabled = false; prompt.focus();
    if (chip) chip.lastChild.textContent = "AI ready";
  }

  async function submit(text) {
    text = (text || "").trim();
    if ((!text && !attached.length) || busy) return;
    document.getElementById("welcome")?.remove();
    const data = new FormData();
    data.append("text", text); data.append("page", framePath());
    attached.forEach((f) => data.append("files", f));
    send.disabled = true;
    const res = await fetch(root.dataset.send, {method: "POST", headers: {"X-CSRFToken": csrf}, body: data});
    const body = await res.json().catch(() => ({}));
    if (!res.ok) { send.disabled = false; window.erpToast?.(body.error || "That couldn't be sent.", "error"); return; }
    prompt.value = ""; attached = []; showAttached();
    place(body.user.html, body.user.id);
    place(body.reply.html, body.reply.id);
    scrollDown();
    watch(body.reply.id);
  }

  document.getElementById("composer").addEventListener("submit", (ev) => { ev.preventDefault(); submit(prompt.value); });
  prompt.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); submit(prompt.value); } });
  prompt.addEventListener("input", () => { prompt.style.height = "auto"; prompt.style.height = Math.min(prompt.scrollHeight, 220) + "px"; });

  list.addEventListener("click", async (ev) => {
    const starter = ev.target.closest("[data-starter]");
    if (starter) { submit(starter.textContent); return; }
    const again = ev.target.closest("[data-starter-text]");
    if (again) { submit(again.dataset.starterText); return; }
    if (ev.target.closest("[data-pick-file]")) { picker.click(); return; }
    const goBtn = ev.target.closest("[data-go]");
    if (goBtn) { go(goBtn.dataset.go); return; }
    const undo = ev.target.closest("[data-undo]");
    if (undo) {
      undo.disabled = true;
      const send = (all) => fetch(undo.dataset.undo, {method: "POST", headers: {"Content-Type": "application/json", "X-CSRFToken": csrf}, body: JSON.stringify({all})})
        .then(async (r) => ({status: r.status, body: await r.json().catch(() => ({}))})).catch(() => ({status: 0, body: {}}));
      let got = await send(false);
      if (got.status === 409 && got.body.confirm) {  // an older change: what came after it goes too, so asked first
        if (!confirm(got.body.confirm)) { undo.disabled = false; return; }
        got = await send(true);
      }
      if (got.status !== 200) { undo.disabled = false; alert(got.body.error || "That couldn't be undone just now: try again, or use Set up › Versions."); return; }
      const m = got.body;
      place(m.html, m.id); scrollDown();
      if (m.version_after) document.getElementById("version").textContent = m.version_after;
      try { frame.contentWindow.location.reload(); } catch (e) {}
      return;
    }
    const retry = ev.target.closest("[data-retry]");
    if (retry) { const m = await post(retry.dataset.retry); place(m.html, m.id); watch(m.id); return; }
    const row = ev.target.closest("tr[data-open]");
    if (row) go(row.dataset.open);
  });

  showAttached();
  scrollDown();
  const pending = [...list.querySelectorAll('.msg.ai[data-status="queued"], .msg.ai[data-status="thinking"], .msg.ai[data-status="waiting_ai"]')].pop();
  if (pending) watch(pending.dataset.id);
  prompt.focus();
})();
