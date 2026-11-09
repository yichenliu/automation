// Everything interactive on the ERP's pages. No inline scripts anywhere (the pages are served with a strict CSP).
(function () {
  "use strict";
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const post = (url, body, json = true) => fetch(url, {
    method: "POST", headers: json ? {"Content-Type": "application/json", "X-CSRFToken": csrf} : {"X-CSRFToken": csrf},
    body: json ? JSON.stringify(body || {}) : body,
  });
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

  // --- toasts ---------------------------------------------------------------------------------------------------
  function toast(text, kind = "success") {
    let box = $(".toasts");
    if (!box) { box = document.createElement("div"); box.className = "toasts"; document.body.appendChild(box); }
    const t = document.createElement("div");
    t.className = `toast ${kind}`; t.textContent = text;
    box.appendChild(t);
    setTimeout(() => t.classList.add("gone"), 5000);
    setTimeout(() => t.remove(), 5600);
  }
  window.erpToast = toast;

  // --- clickable rows and cards, menus, flashes ------------------------------------------------------------------
  document.addEventListener("click", (ev) => {
    const dismiss = ev.target.closest("[data-dismiss]");
    if (dismiss) { dismiss.closest(".flash")?.remove(); return; }
    const menuBtn = ev.target.closest("[data-menu]");
    $$(".menu:not([hidden])").forEach((m) => { if (!menuBtn || m !== menuBtn.nextElementSibling) m.hidden = true; });
    if (menuBtn) { const m = menuBtn.nextElementSibling; m.hidden = !m.hidden; return; }
    const toggle = ev.target.closest("[data-toggle]");
    if (toggle) { $(toggle.dataset.toggle)?.classList.toggle("open"); return; }
    const copy = ev.target.closest("[data-copy]");
    if (copy) {
      const input = copy.parentElement.querySelector("input");
      navigator.clipboard?.writeText(input.value).then(() => toast("Copied")).catch(() => { input.select(); document.execCommand("copy"); });
      return;
    }
    const row = ev.target.closest("[data-open]");
    if (!row || ev.target.closest("a, button, input, select, textarea, label, form, .editing")) return;
    if (row.closest(".messages")) return; // the builder handles its own
    if (ev.metaKey || ev.ctrlKey) { window.open(row.dataset.open, "_blank"); return; }
    row.classList.add("opening"); // a slow page still shows the click took, so nobody clicks again
    window.location = row.dataset.open;
  });
  setTimeout(() => $$(".flash.success").forEach((f) => f.classList.add("fade")), 7000);

  document.addEventListener("change", (ev) => {
    if (ev.target.matches("[data-autosubmit]")) ev.target.form.requestSubmit ? ev.target.form.requestSubmit() : ev.target.form.submit();
  });
  document.addEventListener("submit", (ev) => {
    const form = ev.target;
    const asker = ev.submitter?.dataset.confirmText ? ev.submitter : form.dataset.confirmText ? form : null;
    if (asker && !confirm(asker.dataset.confirmText)) { ev.preventDefault(); return; }
    if (form.hasAttribute("data-busy")) {
      const btn = ev.submitter || form.querySelector("button");
      if (btn) { setTimeout(() => { btn.disabled = true; btn.classList.add("busy"); }, 0); }
      if (btn && form.dataset.busy === "download") {  // a download leaves the page where it is: the button comes back
        setTimeout(() => { btn.disabled = false; btn.classList.remove("busy"); }, 5000);
      }
    }
  });

  // --- keyboard -------------------------------------------------------------------------------------------------
  document.addEventListener("keydown", (ev) => {
    const typing = ev.target.closest("input, textarea, select, [contenteditable]");
    if (ev.key === "Escape") { $$(".menu:not([hidden])").forEach((m) => (m.hidden = true)); closeAssistant(); return; }
    if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter") { const f = $("#record-form"); if (f) { ev.preventDefault(); f.requestSubmit(); } return; }
    if (typing || ev.metaKey || ev.ctrlKey || ev.altKey) return;
    if (ev.key === "/") { const s = $(".side-search input, .search-box input"); if (s) { ev.preventDefault(); s.focus(); } }
    else if (ev.key === "n") $("[data-new]")?.click();
    else if (ev.key === "e") $("[data-edit]")?.click();
    else if (ev.key === ".") { ev.preventDefault(); openAssistant(); }
  });

  // --- board: drag cards between columns -------------------------------------------------------------------------
  const board = $(".board[data-set]");
  if (board && !board.hasAttribute("data-readonly")) {
    let dragged = null;
    board.addEventListener("dragstart", (ev) => { dragged = ev.target.closest(".bcard"); if (dragged) { dragged.classList.add("dragging"); ev.dataTransfer.effectAllowed = "move"; } });
    board.addEventListener("dragend", () => { dragged?.classList.remove("dragging"); $$(".lane", board).forEach((l) => l.classList.remove("over")); });
    $$(".lane", board).forEach((lane) => {
      lane.addEventListener("dragover", (ev) => { ev.preventDefault(); lane.classList.add("over"); });
      lane.addEventListener("dragleave", () => lane.classList.remove("over"));
      lane.addEventListener("drop", async (ev) => {
        ev.preventDefault(); lane.classList.remove("over");
        if (!dragged || dragged.closest(".lane") === lane) return;
        const from = dragged.closest(".lane");
        lane.querySelector(".lane-body").prepend(dragged);
        const res = await post(`${board.dataset.set}/${dragged.dataset.id}/set`, {field: board.dataset.field, value: lane.dataset.value});
        const body = await res.json().catch(() => ({}));
        if (!res.ok) { from.querySelector(".lane-body").prepend(dragged); toast(body.error || "That change couldn't be saved.", "error"); return; }
        (body.ran || []).forEach((m) => toast(m, "auto"));
        setTimeout(() => window.location.reload(), (body.ran || []).length ? 1200 : 150);
      });
    });
  }

  // --- edit in place ----------------------------------------------------------------------------------------------
  function editorFor(cell) {
    const type = cell.dataset.type;
    let raw = cell.dataset.raw ? JSON.parse(cell.dataset.raw) : "";
    if (Array.isArray(raw)) raw = raw.join(", ");
    let el;
    if (type === "select" || type === "user") {
      el = document.createElement("select");
      const opts = JSON.parse(cell.dataset.options || "[]");
      el.innerHTML = '<option value=""></option>' + (Array.isArray(opts) ? opts.map((o) => `<option ${String(o).toLowerCase() === String(raw).toLowerCase() ? "selected" : ""}>${esc(o)}</option>`).join("")
        : Object.entries(opts).map(([id, name]) => `<option value="${esc(id)}" ${String(id) === String(raw) ? "selected" : ""}>${esc(name)}</option>`).join(""));
    } else if (type === "checkbox") {
      el = document.createElement("select");
      el.innerHTML = `<option value="1" ${raw ? "selected" : ""}>Yes</option><option value="" ${raw ? "" : "selected"}>No</option>`;
    } else {
      el = document.createElement("input");
      el.type = type === "date" ? "date" : "text";
      el.value = type === "percent" && typeof raw === "number" ? `${Math.round(raw * 10000) / 100}%` : raw ?? "";
    }
    return el;
  }
  async function saveCell(cell, el) {
    const url = cell.dataset.set || `${cell.closest("table").dataset.table}/${cell.closest("tr").dataset.id}/set`;
    const res = await post(url, {field: cell.dataset.field, value: el.value});
    const body = await res.json().catch(() => ({}));
    if (!res.ok) { toast(body.error || "Couldn't save that.", "error"); el.focus(); return false; }
    (body.ran || []).forEach((m) => toast(m, "auto"));
    window.location.reload();
    return true;
  }
  document.addEventListener("dblclick", (ev) => {
    const cell = ev.target.closest(".editable");
    // a row that opens is opened by a click, double or not (people double-click): changing a value in place is the
    // spreadsheet view's, and the record's own page
    if (!cell || cell.classList.contains("editing") || cell.closest("tr[data-open]")) return;
    const before = cell.innerHTML;
    const el = editorFor(cell);
    cell.classList.add("editing"); cell.innerHTML = ""; cell.appendChild(el); el.focus();
    let done = false;
    const cancel = () => { if (!done) { done = true; cell.classList.remove("editing"); cell.innerHTML = before; } };
    el.addEventListener("keydown", async (e) => {
      if (e.key === "Escape") cancel();
      if (e.key === "Enter") { e.preventDefault(); done = true; if (!(await saveCell(cell, el))) done = false; }
    });
    el.addEventListener("change", async () => { if (el.tagName === "SELECT" || el.type === "date") { done = true; if (!(await saveCell(cell, el))) done = false; } });
    el.addEventListener("blur", () => setTimeout(cancel, 150));
  });

  // --- selecting rows for bulk changes ------------------------------------------------------------------------------
  const bulkForm = $("#bulk-form");
  if (bulkForm) {
    const bar = $(".bulkbar", bulkForm);
    const update = () => {
      const n = $$('input[name="ids"]:checked', bulkForm).length;
      bar.hidden = n === 0; $("[data-count]", bar).textContent = n;
    };
    bulkForm.addEventListener("change", (ev) => {
      if (ev.target.matches("[data-select-all]")) $$('input[name="ids"]', bulkForm).forEach((c) => (c.checked = ev.target.checked));
      if (ev.target.matches("[data-bulk-field]")) {
        const opt = ev.target.selectedOptions[0];
        const valueBox = $("[data-bulk-value]", bar);
        const type = opt?.dataset.type;
        let replacement;
        if (type === "select") {
          replacement = document.createElement("select");
          replacement.innerHTML = JSON.parse(opt.dataset.options || "[]").map((o) => `<option>${esc(o)}</option>`).join("");
        } else if (type === "checkbox") {
          replacement = document.createElement("select");
          replacement.innerHTML = '<option value="1">Yes</option><option value="">No</option>';
        } else {
          replacement = document.createElement("input");
          replacement.type = type === "date" ? "date" : "text";
          replacement.placeholder = "to…";
        }
        replacement.name = "value"; replacement.setAttribute("data-bulk-value", "");
        replacement.hidden = !opt?.value; valueBox.replaceWith(replacement);
        $("[data-bulk-set]", bar).hidden = !opt?.value;
      }
      update();
    });
  }

  // --- line items: new rows as you type, live totals, removing -----------------------------------------------------
  function tokenize(src) { return src.match(/\d+(?:\.\d+)?|[A-Za-z_]\w*|[()+\-*/,]/g) || []; }
  function evaluate(src, vars) {
    // + - * / and parentheses over numbers and field values; anything else leaves the cell for the server to work out
    const tokens = tokenize(src); let i = 0;
    const peek = () => tokens[i], next = () => tokens[i++];
    function factor() {
      const t = next();
      if (t === "(") { const v = expr(); next(); return v; }
      if (t === "-") return -factor();
      if (/^\d/.test(t)) return parseFloat(t);
      if (t === "round") { next(); const v = expr(); let d = 0; if (peek() === ",") { next(); d = expr(); } next(); const m = 10 ** d; return Math.round(v * m) / m; }
      if (/^[A-Za-z_]/.test(t)) { if (!(t in vars)) throw new Error("unknown"); return vars[t]; }
      throw new Error("bad");
    }
    function term() { let v = factor(); while (peek() === "*" || peek() === "/") { const op = next(); const r = factor(); v = op === "*" ? v * r : (r ? v / r : 0); } return v; }
    function expr() { let v = term(); while (peek() === "+" || peek() === "-") { const op = next(); const r = term(); v = op === "+" ? v + r : v - r; } return v; }
    const v = expr();
    if (i < tokens.length) throw new Error("left over");
    return v;
  }
  const money = new Intl.NumberFormat(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
  function recalc(row) {
    const vars = {};
    $$("input[data-key]", row).forEach((inp) => { const n = parseFloat(String(inp.value).replace(/[^0-9.\-]/g, "")); vars[inp.dataset.key] = isNaN(n) ? 0 : n; });
    $$(".calc-cell[data-expr]", row).forEach((cell) => {
      if (!cell.dataset.expr) return;
      try { const v = evaluate(cell.dataset.expr, vars); cell.textContent = cell.dataset.format === "money" ? "$" + money.format(v) : String(Math.round(v * 100) / 100); }
      catch (e) { /* the server works it out on save */ }
    });
  }
  $$(".lines-editor").forEach((table) => {
    const body = table.tBodies[0];
    $$("tr", body).forEach(recalc);
    body.addEventListener("input", (ev) => {
      const row = ev.target.closest("tr");
      recalc(row);
      if (row.hasAttribute("data-template")) {
        row.removeAttribute("data-template"); row.classList.remove("blank");
        const n = Number(table.dataset.next) + 1; table.dataset.next = n;
        const fresh = row.cloneNode(true);
        fresh.setAttribute("data-template", ""); fresh.classList.add("blank");
        $$("input, select", fresh).forEach((el) => {
          if (el.name) el.name = el.name.replace(/^(lines-[^-]+-)\d+-/, `$1${n}-`);
          if (el.type === "checkbox") el.checked = false; else if (!el.name?.endsWith("-id")) el.value = "";
        });
        $$(".calc-cell", fresh).forEach((c) => (c.textContent = ""));
        body.appendChild(fresh);
      }
    });
    body.addEventListener("change", (ev) => {
      if (ev.target.matches("[data-remove-line]")) ev.target.closest("tr").classList.toggle("removed", ev.target.checked);
      const pick = ev.target.closest("select[data-fill]");
      if (pick && pick.value) {  // a menu item picked: its name and price fill the line's empty boxes
        const row = pick.closest("tr"), fill = JSON.parse(pick.dataset.fill || "{}")[pick.value] || {};
        Object.entries(fill).forEach(([key, value]) => {
          const box = $$("input, select, textarea", row).find((el) => el.name && el.name.endsWith(`-${key}`));
          if (box && !box.value) { box.value = typeof value === "number" && box.inputMode === "decimal" ? value.toFixed(2) : value; box.dispatchEvent(new Event("input", {bubbles: true})); }
        });
        recalc(row);
      }
    });
  });

  // --- conditional fields: shown, hidden or locked while conditions hold; choices that follow another field ----------
  const conditional = (form) => {
    const valueOf = (name) => {
      const els = $$(`[name="${name}"]`, form);
      if (!els.length) return undefined;
      if (els[0].type === "checkbox" && els.length === 1) return els[0].checked;
      if (els[0].type === "checkbox" || els[0].type === "radio") return els.filter((e) => e.checked).map((e) => e.value);
      return els[0].value;
    };
    const textOf = (name) => {  // what a choice shows (a linked record's name), beside its value (its id)
      const el = $(`select[name="${name}"]`, form);
      return el && el.value !== "" && el.selectedOptions[0] ? el.selectedOptions[0].text.trim() : undefined;
    };
    const num = (x) => { const n = parseFloat(String(x ?? "").replace(/[^0-9.\-]/g, "")); return Number.isNaN(n) ? null : n; };
    const holds = (c) => {
      if (c.any) return c.any.some(holds);
      if (c.all) return c.all.every(holds);
      const v = valueOf(c.field);
      if (v === undefined) return true;  // not on this form: the server decides
      const list = Array.isArray(v) ? v.map((x) => String(x).toLowerCase()) : null;
      const s = Array.isArray(v) ? v.join(",") : String(v ?? "");
      const shown = (textOf(c.field) || "").toLowerCase();
      const want = String(c.value ?? "").toLowerCase();
      const is = (w) => (list ? list.includes(w) : s.toLowerCase() === w || (shown && shown === w));
      switch (c.op) {
        case "equals": return typeof v === "boolean" ? v === ["true", "yes", "1"].includes(want) : is(want);
        case "not_equals": return typeof v === "boolean" ? v !== ["true", "yes", "1"].includes(want) : !is(want);
        case "one_of": return (Array.isArray(c.value) ? c.value : [c.value]).map((x) => String(x).toLowerCase()).some(is);
        case "is_empty": return v === false || s === "";
        case "not_empty": return v === true || (s !== "" && v !== false);
        case "contains": return s.toLowerCase().includes(want) || shown.includes(want);
        case "greater_than": case "less_than": case "at_least": case "at_most": {
          const dated = /^\d{4}-\d{2}-\d{2}/.test(s) && /^\d{4}-\d{2}-\d{2}/.test(want);
          const a = dated ? s.slice(0, 10) : num(s), b = dated ? want.slice(0, 10) : num(want);
          if (a === null || b === null || s === "") return c.op === "less_than" || c.op === "at_most" ? s === "" : false;
          return { greater_than: a > b, less_than: a < b, at_least: a >= b, at_most: a <= b }[c.op];
        }
        default: return true;
      }
    };
    const conditions = (el, name) => (el.dataset[name] ? JSON.parse(el.dataset[name]) : null);
    const apply = () => {
      $$("[data-show-if], [data-hide-if]", form).forEach((el) => {
        const show = conditions(el, "showIf"), hide = conditions(el, "hideIf");
        el.hidden = (show && !show.every(holds)) || (hide && hide.every(holds)) || false;
      });
      $$("[data-readonly-if]", form).forEach((el) => {
        const locked = conditions(el, "readonlyIf").every(holds);
        el.classList.toggle("is-readonly", locked);
        $$("input, select, textarea", el).forEach((x) => { if (locked) x.setAttribute("tabindex", "-1"); else x.removeAttribute("tabindex"); });
      });
      $$("[data-options-by]", form).forEach((el) => {
        const by = JSON.parse(el.dataset.optionsBy);
        const parent = textOf(by.field) ?? valueOf(by.field);
        const key = Object.keys(by.options).find((k) => k.toLowerCase() === String(parent ?? "").toLowerCase());
        const allowed = key ? by.options[key].map((o) => o.toLowerCase()) : null;
        if (el.tagName === "SELECT") {
          Array.from(el.options).forEach((o) => { if (o.value === "") return; o.hidden = o.disabled = !!allowed && !allowed.includes(o.text.toLowerCase()); });
          if (el.selectedOptions[0] && el.selectedOptions[0].disabled) el.value = "";
        } else {
          $$(".chip-opt", el).forEach((chip) => {
            const box = $("input", chip), off = !!allowed && !allowed.includes(box.value.toLowerCase());
            chip.hidden = off; if (off) box.checked = false;
          });
        }
      });
    };
    form.addEventListener("input", apply); form.addEventListener("change", apply); apply();
  };
  $$("form").filter((f) => f.querySelector("[data-show-if], [data-hide-if], [data-readonly-if], [data-options-by]")).forEach(conditional);

  // --- Settings › AI: the models a provider has, offered in each purpose's model box --------------------------------
  $$("form[data-ai-models]").forEach((form) => {
    const slots = ["assistant", "builder", "fast", "embed"], asked = new Map();
    const load = async (slot) => {
      const select = form.elements[`${slot}_provider`], list = $(`#models-${slot}`, form), note = $(`[data-models-note="${slot}"]`, form);
      const provider = select ? select.value : "";
      if (!provider || provider === "none" || !list) return;
      const key = form.elements[`key_${provider}`], url = form.elements[`url_${provider}`];
      const body = new FormData();
      body.append("provider", provider);
      body.append("kind", slot === "embed" ? "embed" : "chat");
      if (key && key.value) body.append("key", key.value);
      if (url && url.value) body.append("url", url.value);
      const ask = [provider, slot === "embed", key && key.value, url && url.value].join("|");
      try {
        if (!asked.has(ask)) asked.set(ask, fetch(form.dataset.aiModels, { method: "POST", headers: { "X-CSRFToken": csrf }, body }).then((r) => r.json()));
        const d = await asked.get(ask);
        list.innerHTML = (d.models || []).map((m) => `<option value="${esc(m)}">`).join("");
        const input = form.elements[`${slot}_model`];
        if (input && slot !== "embed") input.placeholder = d.best ? `its best: ${d.best}` : "which model";
        if (note) note.textContent = d.error || (d.models && d.models.length ? `${d.models.length} to choose from with this key.` : "Type the model's name as the provider writes it.");
      } catch (e) { asked.clear(); /* the list only helps: the name can be typed */ }
    };
    slots.forEach((slot) => {
      const select = form.elements[`${slot}_provider`], input = form.elements[`${slot}_model`];
      if (select) select.addEventListener("change", () => load(slot));
      if (input) input.addEventListener("focus", () => load(slot));
    });
    form.addEventListener("change", (e) => { if (/^(key|url)_/.test(e.target.name || "")) slots.forEach(load); });
  });

  // --- trying an AI helper on a record before trusting it (nothing is saved) ------------------------------------
  $$("[data-ai-try]").forEach((button) => button.addEventListener("click", async () => {
    const form = button.closest("form"), out = $("[data-ai-result]", form);
    out.hidden = false;
    out.innerHTML = '<span class="faint">The AI is working on it… on your own AI this can take a minute.</span>';
    button.disabled = true;
    try {
      const r = await fetch(button.dataset.aiTry, { method: "POST", headers: { "X-CSRFToken": csrf }, body: new FormData(form) });
      const d = await r.json();
      if (!d.ok) { out.innerHTML = `<span class="bad-text">${esc(d.error || "That didn't work.")}</span>`; return; }
      const values = (d.values || []).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
      const found = (d.findings || []).length ? `<ul>${d.findings.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : "";
      out.innerHTML = `<div class="row mb-2"><span class="pill ${d.sure ? "green" : "amber"}">${d.sure ? "Sure" : "Not sure: a person would check this"}</span>`
        + (d.learned ? `<span class="faint small">used ${d.learned} past correction${d.learned === 1 ? "" : "s"}</span>` : "") + `</div>`
        + `<dl>${values}</dl>${found}` + (d.why ? `<p class="small muted mt-1">${esc(d.why)}</p>` : "");
    } catch (e) {
      out.innerHTML = '<span class="bad-text">Couldn\'t reach the server. Try again.</span>';
    } finally { button.disabled = false; }
  }));

  // --- the AI suggesting a field while the form is filled in (nothing saved; the person decides) ----------------
  $$("[data-suggest-field]").forEach((button) => button.addEventListener("click", async () => {
    const form = button.closest("form[data-suggest]"), key = button.dataset.suggestField;
    if (!form) return;
    const field = form.elements[key], label = button.closest("label");
    const note = label.querySelector(".suggest-why") || label.appendChild(Object.assign(document.createElement("small"), {className: "suggest-why"}));
    note.textContent = "The AI is reading what you've filled in…";
    button.disabled = true;
    const body = new FormData(form);
    if (form.dataset.record) body.append("_record", form.dataset.record);
    try {
      const r = await fetch(form.dataset.suggest.replace("__key__", encodeURIComponent(key)), {method: "POST", headers: {"X-CSRFToken": csrf}, body});
      const d = await r.json();
      if (!d.ok) { note.textContent = d.error || "Nothing to suggest yet."; return; }
      if (field) {
        if (field.type === "checkbox") field.checked = !!d.value;
        else field.value = Array.isArray(d.value) ? d.value.join(", ") : d.value;
        field.dispatchEvent(new Event("change", {bubbles: true}));
        field.classList.add("suggested");
      }
      note.textContent = (d.sure ? "Suggested: " : "A guess, check it: ") + (d.why || "");
    } catch (e) {
      note.textContent = "Couldn't reach the server. Try again.";
    } finally { button.disabled = false; }
  }));

  $$("[data-back]").forEach((b) => b.addEventListener("click", () => history.back()));

  // --- a list's filters, folded behind one button until wanted --------------------------------------------------------
  $$("[data-toggle-filters]").forEach((button) => button.addEventListener("click", () => {
    const box = button.parentElement.querySelector(".filters");
    if (!box) return;
    box.hidden = !box.hidden;
    button.setAttribute("aria-expanded", String(!box.hidden));
  }));

  // --- a table wider than the screen (line items on a phone) says there's more to the side ----------------------------
  function sideways() {
    $$(".table-wrap, .board").forEach((wrap) => {
      let hint = wrap.nextElementSibling?.classList.contains("scroll-hint") ? wrap.nextElementSibling : null;
      const more = wrap.scrollWidth > wrap.clientWidth + 4 && wrap.scrollLeft + wrap.clientWidth < wrap.scrollWidth - 4;
      if (more && !hint) {
        hint = document.createElement("div"); hint.className = "scroll-hint"; hint.textContent = "Swipe sideways to see the rest →";
        wrap.after(hint);
        wrap.addEventListener("scroll", sideways, {passive: true});
      }
      if (hint) hint.hidden = !more;
    });
  }
  sideways();
  window.addEventListener("resize", sideways);

  // --- one-line guides, shown until the person says they've got it -----------------------------------------------------
  $$("[data-hint]").forEach((hint) => {
    let seen = null;
    try { seen = localStorage.getItem("hint:" + hint.dataset.hint); } catch (e) { /* private window: show it */ }
    if (!seen) hint.hidden = false;
    $("[data-hint-close]", hint)?.addEventListener("click", () => {
      hint.hidden = true;
      try { localStorage.setItem("hint:" + hint.dataset.hint, "1"); } catch (e) { /* nothing to remember it in */ }
    });
  });

  // --- a sensitive value shown on request (the bank account), and hidden again --------------------------------------
  $$("[data-reveal]").forEach((button) => button.addEventListener("click", async () => {
    const out = button.parentElement.querySelector("[data-secret]");
    if (button.dataset.shown) { out.textContent = button.dataset.shown; delete button.dataset.shown; button.lastChild.textContent = "Show"; return; }
    const d = await post(button.dataset.reveal).then((r) => r.json()).catch(() => null);
    if (!d) return;
    button.dataset.shown = out.textContent; out.textContent = d.value || "—"; button.lastChild.textContent = "Hide";
  }));

  // --- dropping files ---------------------------------------------------------------------------------------------
  $$("[data-drop]").forEach((zone) => {
    const input = $('input[type="file"]', zone);
    const show = () => { const names = Array.from(input.files).map((f) => f.name).join(", "); const out = $(".chosen", zone); if (out) out.textContent = names; zone.classList.toggle("has", !!names); };
    ["dragenter", "dragover"].forEach((e) => zone.addEventListener(e, (ev) => { ev.preventDefault(); zone.classList.add("over"); }));
    ["dragleave", "drop"].forEach((e) => zone.addEventListener(e, () => zone.classList.remove("over")));
    zone.addEventListener("drop", (ev) => { ev.preventDefault(); input.files = ev.dataTransfer.files; show(); if (zone.hasAttribute("data-autoupload")) zone.submit(); });
    input.addEventListener("change", () => { show(); if (zone.hasAttribute("data-autoupload")) zone.submit(); });
  });

  // --- background jobs (imports, reading documents, reading Microsoft 365) ------------------------------------------
  $$("[data-job]").forEach((box) => {
    const tick = async () => {
      const s = await fetch(box.dataset.job).then((r) => r.json()).catch(() => null);
      if (s) {
        const stage = $("[data-job-stage]", box), bar = $("[data-job-bar]", box), pct = $("[data-job-pct]", box);
        if (stage) stage.textContent = s.status === "waiting_ai" ? "Waiting for your AI…" : (s.stage || "Working");
        if (bar) bar.style.width = Math.max(3, s.progress || 0) + "%";
        if (pct) pct.textContent = (s.progress || 0) + "%";
        if (s.status === "done" || s.status === "failed") { window.location.reload(); return; }
      }
      setTimeout(tick, 1500);
    };
    setTimeout(tick, 1200);
  });

  // --- the assistant ------------------------------------------------------------------------------------------------
  const panel = $("#assistant");
  let assistantLoaded = false, assistantBusy = false;
  function openAssistant() {
    if (!panel) return;
    panel.hidden = false; document.body.classList.add("assistant-open");
    $("#assistant-text")?.focus();
    if (!assistantLoaded) {
      assistantLoaded = true;
      fetch(panel.dataset.history).then((r) => r.json()).then((d) => {
        if (d.html) { $("#assistant-body").innerHTML = d.html; scrollAssistant(); }
        if (d.pending) watchAssistant(d.pending);
      });
    }
  }
  function closeAssistant() { if (panel && !panel.hidden) { panel.hidden = true; document.body.classList.remove("assistant-open"); } }
  function scrollAssistant() { const b = $("#assistant-body"); b.scrollTop = b.scrollHeight; }
  function placeAssistant(html, id) {
    const tpl = document.createElement("template"); tpl.innerHTML = html.trim();
    const node = tpl.content.firstElementChild;
    const old = id && $(`#assistant-body [data-id="${id}"]`);
    $(".assistant-hint")?.remove();
    if (old) old.replaceWith(node); else $("#assistant-body").appendChild(node);
    scrollAssistant();
  }
  async function watchAssistant(id) {
    assistantBusy = true;
    for (;;) {
      await new Promise((r) => setTimeout(r, 900));
      const m = await fetch(panel.dataset.poll + id).then((r) => r.json()).catch(() => null);
      if (!m) continue;
      placeAssistant(m.html, m.id);
      if (m.status === "done" || m.status === "failed") break;
    }
    assistantBusy = false;
  }
  async function ask(text) {
    if (!text.trim() || assistantBusy) return;
    $("#assistant-text").value = "";
    const res = await post(panel.dataset.url, {text, page: location.pathname});
    if (!res.ok) { toast("Couldn't send that.", "error"); return; }
    const d = await res.json();
    placeAssistant(d.user.html, d.user.id); placeAssistant(d.reply.html, d.reply.id);
    watchAssistant(d.reply.id);
  }
  if (panel) {
    document.addEventListener("click", async (ev) => {
      if (ev.target.closest("[data-assistant-open]")) { openAssistant(); return; }
      if (ev.target.closest("[data-assistant-close]")) { closeAssistant(); return; }
      const hint = ev.target.closest("[data-ask]");
      if (hint) { ask(hint.textContent); return; }
      const confirmBtn = ev.target.closest("[data-confirm]");
      if (confirmBtn) {
        confirmBtn.disabled = true;
        const d = await post(confirmBtn.dataset.confirm).then((r) => r.json());
        placeAssistant(d.html, d.id);
        if (d.navigate) setTimeout(() => (window.location = d.navigate), 600);
        return;
      }
      const dismissBtn = ev.target.closest("[data-dismiss-proposal]");
      if (dismissBtn) { const d = await post(dismissBtn.dataset.dismissProposal).then((r) => r.json()); placeAssistant(d.html, d.id); }
    });
    $("#assistant-form").addEventListener("submit", (ev) => { ev.preventDefault(); ask($("#assistant-text").value); });
    $("#assistant-text").addEventListener("keydown", (ev) => { if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); ask(ev.target.value); } });
  }
  window.openAssistant = openAssistant;
  // "Something not right here?" opened at the foot of a page: brought into view, not left below the fold
  document.addEventListener("toggle", (ev) => {
    if (ev.target.matches?.("details.not-right") && ev.target.open) ev.target.scrollIntoView({behavior: "smooth", block: "nearest"});
  }, true);
})();
