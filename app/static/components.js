// The richer pieces of the kit: choosing several records, locations, scanning barcodes, signatures, step-by-step
// forms, the spreadsheet view and trees. Every piece works from data-* attributes the templates put on the page.
(function () {
  "use strict";
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const csrf = () => $('meta[name="csrf-token"]')?.content || "";
  const postJSON = (url, body) => fetch(url, {method: "POST", headers: {"Content-Type": "application/json", "X-CSRFToken": csrf()}, body: JSON.stringify(body)});
  function toast(text, kind = "success") {
    let box = $(".toasts");
    if (!box) { box = document.createElement("div"); box.className = "toasts"; document.body.appendChild(box); }
    const el = document.createElement("div");
    el.className = `toast ${kind}`; el.textContent = text; box.appendChild(el);
    setTimeout(() => el.remove(), 4500);
  }

  // --- choosing several records or people: filter the chips ------------------------------------------------------------
  document.addEventListener("input", (ev) => {
    if (!ev.target.matches(".chip-filter")) return;
    const q = ev.target.value.trim().toLowerCase();
    $$(".chip-opt", ev.target.closest("[data-filter-chips]")).forEach((chip) => {
      chip.hidden = !!q && !chip.textContent.toLowerCase().includes(q) && !$("input", chip).checked;
    });
  });

  // --- a colour: the picker and the text box stay in step -----------------------------------------------------------------
  document.addEventListener("input", (ev) => {
    const picker = ev.target.closest("[data-color-for]");
    if (picker) { const box = picker.parentElement.querySelector(`input[name="${picker.dataset.colorFor}"]`); if (box) box.value = picker.value; }
  });

  // --- a link that depends on another field (a contact of the chosen customer) -----------------------------------------
  $$("select[data-match-field]").forEach((select) => {
    const form = select.closest("form");
    const source = form && form.querySelector(`[name="${select.dataset.matchField}"]`);
    if (!source) return;
    const apply = () => {
      const want = source.value;
      Array.from(select.options).forEach((o) => { if (o.value) { o.dataset.matchHidden = want && o.dataset.match !== want ? "1" : ""; o.hidden = o.dataset.matchHidden === "1"; } });
      if (select.selectedOptions[0]?.hidden) select.value = "";
    };
    source.addEventListener("change", apply); apply();
  });

  // --- a location: where the device is now -------------------------------------------------------------------------------
  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-locate]");
    if (!btn) return;
    const input = $("input", btn.parentElement);
    if (!navigator.geolocation) { toast("This browser can't tell where it is; type the latitude and longitude.", "error"); return; }
    btn.disabled = true;
    navigator.geolocation.getCurrentPosition((pos) => {
      input.value = `${pos.coords.latitude.toFixed(6)}, ${pos.coords.longitude.toFixed(6)}`;
      btn.disabled = false; toast("Location filled in.");
    }, (err) => { btn.disabled = false; toast(`Couldn't get the location: ${err.message}`, "error"); }, {enableHighAccuracy: true, timeout: 15000});
  });

  // --- scanning a barcode or QR code with the camera ---------------------------------------------------------------------
  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-scan]");
    if (!btn) return;
    const input = $("input", btn.parentElement);
    if (!("BarcodeDetector" in window) || !navigator.mediaDevices?.getUserMedia) {
      input.focus(); toast("This browser can't scan with the camera; a USB or Bluetooth scanner types straight into the box.", "error"); return;
    }
    const detector = new window.BarcodeDetector();
    const shade = document.createElement("div");
    shade.className = "scan-shade";
    shade.innerHTML = '<div class="scan-box"><video playsinline muted></video><p>Point the camera at the code</p><button type="button" class="btn">Cancel</button></div>';
    document.body.appendChild(shade);
    const video = $("video", shade);
    let stream, live = true;
    const stop = () => { live = false; stream?.getTracks().forEach((t) => t.stop()); shade.remove(); };
    $("button", shade).addEventListener("click", stop);
    try {
      stream = await navigator.mediaDevices.getUserMedia({video: {facingMode: "environment"}});
      video.srcObject = stream; await video.play();
      const look = async () => {
        if (!live) return;
        try {
          const codes = await detector.detect(video);
          if (codes.length) { input.value = codes[0].rawValue; stop(); toast("Scanned."); input.dispatchEvent(new Event("change", {bubbles: true})); return; }
        } catch (e) { /* the next frame may work */ }
        requestAnimationFrame(look);
      };
      look();
    } catch (e) { stop(); toast(`The camera isn't available: ${e.message}`, "error"); }
  });

  // --- signatures ---------------------------------------------------------------------------------------------------------
  $$("[data-signature]").forEach((box) => {
    const canvas = $("canvas", box), out = $("input", box), pen = canvas.getContext("2d");
    let drawing = false, drawn = false;
    const fit = () => { const ratio = canvas.width / canvas.getBoundingClientRect().width || 1; return ratio; };
    pen.lineWidth = 2.4; pen.lineCap = "round"; pen.lineJoin = "round";
    pen.strokeStyle = getComputedStyle(document.body).getPropertyValue("--text").trim() || "#111";
    const point = (e) => { const r = canvas.getBoundingClientRect(), k = fit(); return [(e.clientX - r.left) * k, (e.clientY - r.top) * k]; };
    canvas.addEventListener("pointerdown", (e) => { drawing = true; canvas.setPointerCapture(e.pointerId); pen.beginPath(); pen.moveTo(...point(e)); });
    canvas.addEventListener("pointermove", (e) => { if (!drawing) return; pen.lineTo(...point(e)); pen.stroke(); drawn = true; });
    const end = () => { if (!drawing) return; drawing = false; if (drawn) out.value = canvas.toDataURL("image/png"); };
    canvas.addEventListener("pointerup", end); canvas.addEventListener("pointercancel", end);
    $("[data-sig-clear]", box).addEventListener("click", () => { pen.clearRect(0, 0, canvas.width, canvas.height); out.value = ""; drawn = false; });
  });

  // --- step-by-step forms --------------------------------------------------------------------------------------------------
  $$("form[data-wizard]").forEach((form) => {
    const steps = $$("[data-step]", form);
    if (steps.length < 2) return;
    const actions = $(".form-actions", form);
    const bar = document.createElement("ol");
    bar.className = "stepper";
    bar.innerHTML = steps.map((s, i) => `<li data-go="${i}"><span>${i + 1}</span>${esc(s.dataset.step)}</li>`).join("");
    form.insertBefore(bar, steps[0]);
    const nav = document.createElement("div");
    nav.className = "form-actions wizard-nav";
    nav.innerHTML = '<button type="button" class="btn" data-back>Back</button><button type="button" class="btn primary" data-next>Next</button>';
    actions.parentNode.insertBefore(nav, actions);
    let at = 0;
    const show = (n) => {
      at = Math.max(0, Math.min(n, steps.length - 1));
      steps.forEach((s, i) => (s.hidden = i !== at));
      $$("li", bar).forEach((li, i) => { li.classList.toggle("on", i === at); li.classList.toggle("done", i < at); });
      $("[data-back]", nav).hidden = at === 0;
      $("[data-next]", nav).hidden = at === steps.length - 1;
      actions.hidden = at !== steps.length - 1;
      window.scrollTo({top: form.offsetTop - 80, behavior: "smooth"});
    };
    const missing = () => $$("label.field", steps[at]).filter((l) => $(".req", l) && !l.closest("[hidden]") && !l.closest(".is-hidden"))
      .filter((l) => { const i = $("input:not([type=hidden]), select, textarea", l); return i && !i.value && i.type !== "checkbox"; });
    $("[data-next]", nav).addEventListener("click", () => {
      const empty = missing();
      if (empty.length) { empty.forEach((l) => l.classList.add("has-error")); toast(`Fill in ${$("span", empty[0]).textContent.replace("*", "").trim()} first.`, "error"); return; }
      show(at + 1);
    });
    $("[data-back]", nav).addEventListener("click", () => show(at - 1));
    bar.addEventListener("click", (e) => { const li = e.target.closest("[data-go]"); if (li && +li.dataset.go <= at) show(+li.dataset.go); });
    const firstError = steps.findIndex((s) => $(".has-error, .field-error", s));
    show(firstError > 0 ? firstError : 0);
  });

  // --- the spreadsheet view ------------------------------------------------------------------------------------------------
  const sheet = $("table[data-sheet]");
  if (sheet) {
    const rows = () => $$("tbody tr", sheet);
    const cellAt = (r, c) => rows()[r]?.querySelectorAll("td")[c];
    const where = (td) => [rows().indexOf(td.closest("tr")), Array.from(td.parentElement.children).indexOf(td)];
    const move = (td, dr, dc) => {
      let [r, c] = where(td);
      for (let i = 0; i < 400; i++) {
        r += dr; c += dc;
        const next = cellAt(r, c);
        if (!next) return;
        if (next.classList.contains("cell")) { next.focus(); return; }
        if (!dr && !dc) return;
      }
    };
    const options = (td) => JSON.parse(td.dataset.options || "null");
    const shownFor = (td, value) => {
      const opts = options(td);
      if (opts && !Array.isArray(opts)) return opts[value] ?? value;
      if (td.dataset.type === "checkbox") return value ? "✓" : "";
      return value;
    };
    function editor(td, typed) {
      const type = td.dataset.type, opts = options(td);
      let raw = td.dataset.raw ? JSON.parse(td.dataset.raw) : "";
      let el;
      if (opts) {
        el = document.createElement("select");
        const entries = Array.isArray(opts) ? opts.map((o) => [o, o]) : Object.entries(opts);
        el.innerHTML = '<option value=""></option>' + entries.map(([v, label]) => `<option value="${esc(v)}" ${String(v).toLowerCase() === String(raw).toLowerCase() ? "selected" : ""}>${esc(label)}</option>`).join("");
      } else if (type === "checkbox") {
        el = document.createElement("select");
        el.innerHTML = `<option value="1" ${raw ? "selected" : ""}>Yes</option><option value="" ${raw ? "" : "selected"}>No</option>`;
      } else if (type === "long_text" || type === "address") {
        el = document.createElement("textarea"); el.rows = 3; el.value = raw ?? "";
      } else {
        el = document.createElement("input");
        el.type = type === "date" ? "date" : type === "datetime" ? "datetime-local" : "text";
        el.value = type === "percent" && typeof raw === "number" ? `${Math.round(raw * 10000) / 100}%` : type === "datetime" ? String(raw || "").slice(0, 16) : (raw ?? "");
        if (typed !== undefined && el.type === "text") el.value = typed;
      }
      return el;
    }
    async function commit(td, value) {
      const tr = td.closest("tr");
      if (tr.hasAttribute("data-new")) {  // the new row: gather values, add the record once something's in it
        const values = JSON.parse(tr.dataset.values || "{}");
        values[td.dataset.field] = value;
        tr.dataset.values = JSON.stringify(values);
        td.dataset.raw = JSON.stringify(value); td.textContent = shownFor(td, value);
        return true;
      }
      const res = await postJSON(`${sheet.dataset.sheet}/${tr.dataset.id}/set`, {field: td.dataset.field, value});
      const body = await res.json().catch(() => ({}));
      if (!res.ok) { toast(body.error || "Couldn't save that.", "error"); return false; }
      td.dataset.raw = JSON.stringify(value); td.textContent = body.shown === "—" ? "" : body.shown;
      td.classList.add("saved"); setTimeout(() => td.classList.remove("saved"), 900);
      (body.ran || []).forEach((m) => toast(m, "auto"));
      return true;
    }
    async function addNew(tr) {
      const values = JSON.parse(tr.dataset.values || "{}");
      if (!Object.values(values).some((v) => v !== "" && v !== null)) return;
      const res = await postJSON(sheet.dataset.quick, values);
      const body = await res.json().catch(() => ({}));
      if (!res.ok) { toast(body.error || "Couldn't add it.", "error"); return; }
      toast("Added."); window.location.reload();
    }
    function open(td, typed) {
      if (td.classList.contains("editing")) return;
      const before = td.innerHTML, el = editor(td, typed);
      td.classList.add("editing"); td.innerHTML = ""; td.appendChild(el); el.focus();
      if (typed !== undefined && el.setSelectionRange && el.type === "text") el.setSelectionRange(el.value.length, el.value.length);
      let done = false;
      const close = (restore) => { if (done) return; done = true; td.classList.remove("editing"); if (restore) td.innerHTML = before; };
      const save = async (dr, dc) => {
        if (done) return;
        const value = el.value;
        done = true; td.classList.remove("editing");
        if (!(await commit(td, value))) td.innerHTML = before;
        td.focus(); if (dr || dc) move(td, dr, dc);
      };
      el.addEventListener("keydown", (e) => {
        e.stopPropagation();
        if (e.key === "Escape") { e.preventDefault(); close(true); td.focus(); }
        else if (e.key === "Enter" && !(el.tagName === "TEXTAREA" && e.shiftKey)) { e.preventDefault(); save(1, 0); }
        else if (e.key === "Tab") { e.preventDefault(); save(0, e.shiftKey ? -1 : 1); }
      });
      el.addEventListener("change", () => { if (el.tagName === "SELECT") save(0, 0); });
      el.addEventListener("blur", () => setTimeout(() => { if (!done) save(0, 0); }, 120));
    }
    sheet.addEventListener("keydown", (e) => {
      const td = e.target.closest("td.cell");
      if (!td || e.target !== td || td.classList.contains("editing")) return;  // keys typed into an open editor are its own
      const keys = {ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1]};
      if (keys[e.key]) { e.preventDefault(); move(td, ...keys[e.key]); }
      else if (e.key === "Tab") { e.preventDefault(); move(td, 0, e.shiftKey ? -1 : 1); }
      else if (e.key === "Enter" || e.key === "F2") { e.preventDefault(); open(td); }
      else if (e.key === "Backspace" || e.key === "Delete") { e.preventDefault(); commit(td, ""); }
      else if (e.key.length === 1 && !e.metaKey && !e.ctrlKey) { e.preventDefault(); open(td, e.key); }
    });
    sheet.addEventListener("dblclick", (e) => { const td = e.target.closest("td.cell"); if (td) open(td); });
    sheet.addEventListener("focusout", (e) => {  // leaving the new row adds it
      const tr = e.target.closest("tr[data-new]");
      setTimeout(() => { if (tr && !tr.contains(document.activeElement)) addNew(tr); }, 200);
    });
    sheet.addEventListener("paste", async (e) => {  // a block copied from Excel or Sheets fills the cells from here
      const td = e.target.closest("td.cell");
      if (!td || td.classList.contains("editing")) return;
      e.preventDefault();
      const lines = (e.clipboardData.getData("text/plain") || "").replace(/\r/g, "").split("\n").filter((l, i, all) => l !== "" || i < all.length - 1);
      const [r0, c0] = where(td);
      let saved = 0, made = 0;
      for (let i = 0; i < lines.length; i++) {
        const cells = lines[i].split("\t");
        const tr = rows()[r0 + i];
        if (tr && !tr.hasAttribute("data-new")) {
          for (let j = 0; j < cells.length; j++) {
            const target = cellAt(r0 + i, c0 + j);
            if (target?.classList.contains("cell") && await commit(target, cells[j].trim())) saved++;
          }
        } else {
          const header = rows().find((x) => x.hasAttribute("data-new"));
          const values = {};
          for (let j = 0; j < cells.length; j++) {
            const target = header?.querySelectorAll("td")[c0 + j];
            if (target?.dataset.field) values[target.dataset.field] = cells[j].trim();
          }
          const res = await postJSON(sheet.dataset.quick, values);
          if (res.ok) made++; else toast(((await res.json().catch(() => ({}))).error) || `Row ${i + 1} wasn't added.`, "error");
        }
      }
      toast(`Pasted ${saved} cell${saved === 1 ? "" : "s"}${made ? ` and added ${made} row${made === 1 ? "" : "s"}` : ""}.`);
      if (made) setTimeout(() => window.location.reload(), 600);
    });
  }

  // --- tabs on a record page, printing, and moving to the previous / next record with [ and ] ----------------------------
  $$("[data-tabs]").forEach((bar) => bar.addEventListener("click", (e) => {
    const tab = e.target.closest("[data-tab]");
    if (!tab) return;
    e.preventDefault();
    $$("[data-tab]", bar).forEach((x) => x.classList.toggle("active", x === tab));
    $$("[data-tab-panel]").forEach((p) => (p.hidden = p.dataset.tabPanel !== tab.dataset.tab));
  }));
  document.addEventListener("click", (e) => { if (e.target.closest("[data-print]")) { e.preventDefault(); window.print(); } });
  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, textarea, select, [contenteditable]") || e.metaKey || e.ctrlKey || e.altKey) return;
    const link = e.key === "[" ? $("[data-key-prev]") : e.key === "]" ? $("[data-key-next]") : null;
    if (link) window.location = link.href;
  });

  // --- adding a linked record without leaving the form (a new customer while writing a quote): "+ New …" in its list ----------
  const quickAdd = (select) => {
    const conf = select.dataset, holder = select.closest(".link-pick") || select.parentElement;
    const next = holder.nextElementSibling;
    if (next && next.classList.contains("quick-add-box")) { next.querySelector("input").focus(); return; }
    const box = document.createElement("span");
    box.className = "quick-add-box";
    box.innerHTML = `<input type="text" placeholder="Name of the new ${esc(conf.singular.toLowerCase())}" aria-label="Name of the new ${esc(conf.singular.toLowerCase())}">`
      + '<button type="button" class="btn sm primary" data-qa-add>Add</button><button type="button" class="btn sm ghost" data-qa-cancel>Cancel</button>';
    holder.after(box);
    const input = box.querySelector("input");
    input.focus();
    const add = async () => {
      const name = input.value.trim();
      if (!name) { input.focus(); return; }
      const res = await postJSON(conf.quickLink, {[conf.titleField]: name});
      const body = await res.json().catch(() => ({}));
      if (!res.ok) { toast(`${body.error || "Couldn't add it"}. Use the full form for this one.`, "error"); window.open(conf.full, "_blank"); return; }
      const option = new Option(name, body.id, true, true);
      select.add(option); select.value = String(body.id); select.dataset.was = select.value;
      select.dispatchEvent(new Event("change", {bubbles: true}));
      box.remove();
      toast(`Added ${conf.singular.toLowerCase()} “${name}”.`);
    };
    box.querySelector("[data-qa-add]").addEventListener("click", add);
    box.querySelector("[data-qa-cancel]").addEventListener("click", () => box.remove());
    input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); add(); } if (ev.key === "Escape") box.remove(); });
  };
  // on the page, not each list: lines added to a form later work too
  document.addEventListener("focusin", (e) => {
    const select = e.target.closest?.("select[data-quick-link]");
    if (select && !select.selectedOptions[0]?.hasAttribute("data-quick-new")) select.dataset.was = select.value;
  });
  document.addEventListener("change", (e) => {
    const select = e.target.closest?.("select[data-quick-link]");
    if (!select) return;
    if (select.selectedOptions[0]?.hasAttribute("data-quick-new")) {  // back to what it showed, and the name box opens
      select.value = select.dataset.was || "";
      quickAdd(select);
    } else {
      select.dataset.was = select.value;
    }
  });

  // --- a linked record's list: it searches, and "+ New …" is there with what it finds ---------------------------------------
  // The real <select> stays (hidden) and is what's saved; what's shown is a button and, when open, a panel floating over the
  // page (so a table that scrolls sideways can't cut it off), with a search box, the matches, and "+ New …" at its foot.
  let openCombo = null;
  const pickValue = (select, value) => {
    select.value = value; select.dataset.was = value;
    select.dispatchEvent(new Event("input", {bubbles: true}));
    select.dispatchEvent(new Event("change", {bubbles: true}));
  };
  const createLinked = async (select, name) => {
    const conf = select.dataset;
    const res = await postJSON(conf.quickLink, {[conf.titleField]: name});
    const body = await res.json().catch(() => ({}));
    if (!res.ok) { toast(`${body.error || "Couldn't add it"}. Use the full form for this one.`, "error"); window.open(conf.full, "_blank"); return false; }
    select.add(new Option(name, body.id));
    pickValue(select, String(body.id));
    toast(`Added ${conf.singular.toLowerCase()} “${name}”.`);
    return true;
  };
  const closeCombo = () => {
    if (!openCombo) return;
    openCombo.panel.remove(); openCombo.button.setAttribute("aria-expanded", "false"); openCombo = null;
  };
  const comboLabel = (select, button) => {
    const o = select.selectedOptions[0], chosen = o && o.value;
    button.querySelector(".combo-text").textContent = chosen ? o.text : (select.closest(".lines-editor") ? "" : "—");
    button.classList.toggle("empty", !chosen);
  };
  const openPanel = (select, button, typed = "") => {
    closeCombo();
    const conf = select.dataset, thing = (conf.singular || "").toLowerCase();
    const panel = document.createElement("div");
    panel.className = "combo-panel"; panel.setAttribute("role", "listbox");
    panel.innerHTML = '<input type="search" class="combo-find" autocomplete="off" aria-label="Find"><div class="combo-list"></div>'
      + (conf.quickLink ? '<button type="button" class="combo-item combo-new"></button>' : "");
    document.body.append(panel);
    const find = panel.querySelector(".combo-find"), list = panel.querySelector(".combo-list"), add = panel.querySelector(".combo-new");
    const count = Array.from(select.options).filter((o) => o.value).length;
    find.placeholder = count > 7 ? `Find among ${count}…` : "Find…";
    let items = [], active = 0;
    const mark = () => { items.forEach((b, i) => b.classList.toggle("active", i === active)); items[active]?.scrollIntoView({block: "nearest"}); };
    const render = () => {
      const q = find.value.trim(), lower = q.toLowerCase();
      const shown = Array.from(select.options).filter((o) => o.value && o.dataset.matchHidden !== "1" && (!lower || o.text.toLowerCase().includes(lower)));
      list.innerHTML = ""; items = [];
      const item = (text, value, cls = "") => {
        const b = document.createElement("button");
        b.type = "button"; b.className = `combo-item ${cls}`; b.textContent = text; b.setAttribute("role", "option");
        b.addEventListener("mousedown", (e) => e.preventDefault());  // the search box keeps the focus
        b.addEventListener("click", () => { pickValue(select, value); closeCombo(); button.focus(); });
        list.append(b); items.push(b);
      };
      if (!lower) item("—", "", "combo-none");
      shown.slice(0, 300).forEach((o) => item(o.text, o.value, o.value === select.value ? "on" : ""));
      const note = (text) => { const d = document.createElement("div"); d.className = "combo-note"; d.textContent = text; list.append(d); };
      if (shown.length > 300) note(`${shown.length - 300} more: type to narrow it down`);
      if (lower && !shown.length) note(`Nothing called “${q}”`);
      if (add) { add.textContent = q ? `+ New ${thing} “${q}”` : `+ New ${thing}…`; items.push(add); }
      active = lower ? (shown.length ? 0 : items.length - 1) : Math.max(0, items.findIndex((b) => b.classList.contains("on")));
      mark();
    };
    add?.addEventListener("mousedown", (e) => e.preventDefault());
    add?.addEventListener("click", async () => {
      const q = find.value.trim();
      if (!q) { find.placeholder = `Name of the new ${thing}`; find.focus(); add.textContent = "Type its name, then press Enter"; return; }
      add.disabled = true;
      if (await createLinked(select, q)) { closeCombo(); button.focus(); } else add.disabled = false;
    });
    find.addEventListener("input", render);
    find.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(items.length - 1, active + 1); mark(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(0, active - 1); mark(); }
      else if (e.key === "Enter") { e.preventDefault(); (find.value.trim() && add && !items.some((b) => b !== add) ? add : items[active])?.click(); }
      else if (e.key === "Escape") { e.preventDefault(); closeCombo(); button.focus(); }
      else if (e.key === "Tab") closeCombo();
    });
    const place = () => {  // under its button, or above it when there's no room below
      const r = button.getBoundingClientRect(), width = Math.max(r.width, 280), below = window.innerHeight - r.bottom;
      panel.style.width = `${width}px`;
      panel.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - width - 8))}px`;
      if (below < 320 && r.top > below) { panel.style.top = ""; panel.style.bottom = `${window.innerHeight - r.top + 4}px`; }
      else { panel.style.bottom = ""; panel.style.top = `${r.bottom + 4}px`; }
    };
    place();
    openCombo = {select, button, panel, place};
    button.setAttribute("aria-expanded", "true");
    find.value = typed; render(); find.focus();
  };
  const enhanced = new WeakSet();
  const enhance = (select) => {
    enhanced.add(select);
    const copied = select.nextElementSibling;  // a line row copied from the one before brings that row's button: not this one's
    if (copied?.classList.contains("combo-button")) copied.remove();
    select.hidden = true;
    const button = document.createElement("button");
    button.type = "button"; button.className = "combo-button";
    button.setAttribute("aria-haspopup", "listbox"); button.setAttribute("aria-expanded", "false");
    button.innerHTML = '<span class="combo-text"></span>';
    select.after(button);
    comboLabel(select, button);
    select.addEventListener("change", () => comboLabel(select, button));
    button.addEventListener("click", (e) => { e.preventDefault(); openCombo?.select === select ? closeCombo() : openPanel(select, button); });
    button.addEventListener("keydown", (e) => {
      if (["ArrowDown", "Enter", " "].includes(e.key)) { e.preventDefault(); openPanel(select, button); }
      else if (e.key.length === 1 && !e.metaKey && !e.ctrlKey && !e.altKey) { e.preventDefault(); openPanel(select, button, e.key); }
    });
  };
  const enhanceAll = (root) => (root.matches?.("select[data-searchable]") ? [root] : []).concat($$("select[data-searchable]", root))
    .forEach((s) => { if (!enhanced.has(s)) enhance(s); });
  enhanceAll(document);
  new MutationObserver((changes) => changes.forEach((c) => c.addedNodes.forEach((n) => { if (n.nodeType === 1) enhanceAll(n); })))
    .observe(document.body, {childList: true, subtree: true});
  document.addEventListener("mousedown", (e) => {
    if (openCombo && !openCombo.panel.contains(e.target) && !openCombo.button.contains(e.target)) closeCombo();
  });
  window.addEventListener("scroll", (e) => { if (openCombo && !openCombo.panel.contains(e.target)) openCombo.place(); }, true);
  window.addEventListener("resize", () => openCombo?.place());

  // --- ⌘K / Ctrl+K: jump to any page, table, action or record ---------------------------------------------------------------
  const jumpUrl = $("[data-jump]")?.dataset.jump;
  if (jumpUrl) {
    let box = null, timer = null;
    const close = () => { box?.remove(); box = null; };
    const open = () => {
      if (box) return;
      box = document.createElement("div");
      box.className = "palette-shade";
      box.innerHTML = '<div class="palette" role="dialog" aria-label="Jump to"><input type="search" placeholder="Jump to a page, table or record…" aria-label="Jump to"><ul></ul></div>';
      document.body.appendChild(box);
      const input = $("input", box), list = $("ul", box);
      let active = 0, items = [];
      const draw = () => { list.innerHTML = items.map((x, i) => `<li class="${i === active ? "on" : ""}"><a href="${esc(x.url)}"><span>${esc(x.label)}</span><span class="faint tiny">${esc(x.kind)}</span></a></li>`).join("") || '<li class="faint small pad">Nothing matches.</li>'; };
      const load = async () => { const res = await fetch(`${jumpUrl}?q=${encodeURIComponent(input.value)}`); items = (await res.json()).items || []; active = 0; draw(); };
      input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(load, 120); });
      input.addEventListener("keydown", (e) => {
        if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(active + 1, items.length - 1); draw(); }
        else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(active - 1, 0); draw(); }
        else if (e.key === "Enter" && items[active]) { window.location = items[active].url; }
        else if (e.key === "Escape") close();
      });
      box.addEventListener("click", (e) => { if (e.target === box) close(); });
      input.focus(); load();
    };
    document.addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); box ? close() : open(); } });
    $$("[data-jump-open]").forEach((b) => b.addEventListener("click", (e) => { e.preventDefault(); open(); }));
  }

  // --- trees: open or close every branch -----------------------------------------------------------------------------------
  document.addEventListener("click", (ev) => {
    const all = ev.target.closest("[data-tree-all], [data-tree-none]");
    if (!all) return;
    ev.preventDefault();
    $$(".tree details").forEach((d) => (d.open = all.hasAttribute("data-tree-all")));
  });
})();
