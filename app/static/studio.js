/* The data model and workflow studios (app/studio.py): the design drawn as diagrams and changed by hand. Every change
   goes to the server, is checked like the builder's and saved as a version; the answer says what changed and which
   version it was made from, so it can be taken back. */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const NS = "http://www.w3.org/2000/svg";
  const csrf = () => $('meta[name="csrf-token"]')?.content || "";
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const icon = (name, cls = "") => `<svg class="i ${cls}" aria-hidden="true"><use href="/static/icons.svg#i-${name}"></use></svg>`;
  const html = (s) => { const t = document.createElement("template"); t.innerHTML = s.trim(); return t.content.firstElementChild; };
  const node = (tag, attrs = {}, parent = null) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (parent) parent.appendChild(n);
    return n;
  };
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  async function post(url, body) {
    let res, data = {};
    try {
      res = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json", "X-CSRFToken": csrf()}, body: JSON.stringify(body || {})});
      data = await res.json();
    } catch (e) { return {error: "That didn't reach the server: check the connection and try again"}; }
    if (!res.ok && !data.error) data.error = res.status === 403 ? "Only an admin can change how the app works" : "Something went wrong: try again";
    return data;
  }
  async function get(url) {
    try { const res = await fetch(url, {headers: {Accept: "application/json"}}); return res.ok ? await res.json() : null; } catch (e) { return null; }
  }
  // A colour from the page's theme, as the canvas and a downloaded picture need it (not a CSS variable).
  function colour(name) {
    const probe = document.createElement("span");
    probe.style.color = `var(${name})`;
    document.body.appendChild(probe);
    const c = getComputedStyle(probe).color;
    probe.remove();
    return c;
  }

  // --- what a change said, and taking it back --------------------------------------------------------------------------
  function statusLine(box, lines, kind = "good", undo = null) {
    if (!box) return;
    box.innerHTML = "";
    if (!lines || !lines.length) return;
    const el = html(`<div class="studio-said ${kind}">${icon(kind === "bad" ? "triangle-alert" : "circle-check")}<div class="grow">${lines.slice(0, 3).map((l) => `<div>${esc(l)}</div>`).join("")}${lines.length > 3 ? `<div class="faint">and ${lines.length - 3} more</div>` : ""}</div></div>`);
    if (undo) {
      const b = html(`<button type="button" class="btn sm">${icon("undo-2")}Take it back</button>`);
      b.addEventListener("click", undo);
      el.appendChild(b);
    }
    const close = html(`<button type="button" class="btn sm ghost icon" aria-label="Close">${icon("x")}</button>`);
    close.addEventListener("click", () => { box.innerHTML = ""; });
    el.appendChild(close);
    box.appendChild(el);
    if (kind !== "bad") setTimeout(() => { if (el.isConnected) el.classList.add("fading"); }, 9000);
  }

  // --- asking the AI, from inside a studio -----------------------------------------------------------------------------
  function askBox(onDone) {
    const form = $("[data-studio-ask]");
    if (!form) return;
    const said = $(".studio-ask-said", form);
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const input = form.elements.text;
      const text = input.value.trim();
      if (!text) { input.focus(); return; }
      const button = $("button", form);
      button.disabled = true; input.disabled = true;
      said.hidden = false; said.className = "studio-ask-said small"; said.innerHTML = `<span class="spin"></span> Working on it: the AI is changing your app…`;
      const data = new FormData();
      data.append("text", text); data.append("page", location.pathname + location.search);
      let m;
      try {
        const res = await fetch(form.dataset.send, {method: "POST", headers: {"X-CSRFToken": csrf()}, body: data});
        m = (await res.json()).reply;
      } catch (e) { m = null; }
      if (!m) { said.textContent = "That didn't reach the AI: try again."; button.disabled = false; input.disabled = false; return; }
      const started = Date.now();
      while (m && !["done", "failed"].includes(m.status) && Date.now() - started < 30 * 60 * 1000) {
        await new Promise((r) => setTimeout(r, 1500));
        const next = await get(form.dataset.poll + m.id);
        if (next) m = next;
        if (m.status === "waiting_ai") said.innerHTML = `<span class="spin"></span> Waiting for the AI to be free…`;
      }
      const reply = html(`<div>${m.html || ""}</div>`);
      $$(".actions, button, form", reply).forEach((x) => x.remove());
      const words = (reply.textContent || "").replace(/\s+/g, " ").trim();
      said.className = `studio-ask-said small ${m.status === "failed" ? "bad-text" : ""}`;
      said.textContent = words.slice(0, 600) || (m.status === "done" ? "Done." : "That didn't work: try saying it another way.");
      button.disabled = false; input.disabled = false;
      if (m.status === "done") { input.value = ""; onDone(); }
    });
  }

  // =================================================================================================================
  // The data model
  // =================================================================================================================
  function dataStudio(root) {
    const canEdit = !!root.dataset.canEdit;
    const canvas = $(".studio-canvas", root), svg = $(".studio-svg", root), panel = $(".studio-panel", root);
    const status = $(".studio-status", root);
    let model = JSON.parse($("#studio-model").textContent);
    const undoStack = [];
    const W = 248, HEAD = 40, ROW = 24, PAD = 8;
    let view = {x: 40, y: 30, k: 1};
    let places = {};
    let selected = null;  // {table} or {table, field}
    let colours = {};
    const layer = node("g", {}, svg);
    const linksLayer = node("g", {class: "links"}, layer);
    const boxesLayer = node("g", {class: "boxes"}, layer);

    const tables = () => model.tables;
    const tableOf = (key) => tables().find((t) => t.key === key);
    const height = (t) => HEAD + t.fields.length * ROW + PAD;

    function themeColours() {
      colours = {panel: colour("--panel"), head: colour("--panel-2"), border: colour("--border-strong"), text: colour("--text"),
                 muted: colour("--muted"), faint: colour("--faint"), accent: colour("--accent"), accentWeak: colour("--accent-weak"),
                 line: colour("--faint"), warn: colour("--warn"), bg: colour("--bg")};
    }

    // Laid out the first time: what's linked to sits to the left of what links to it (customers, then orders, then lines).
    function layout(fresh) {
      const rank = {};
      const visiting = new Set();
      const depth = (t) => {
        if (rank[t.key] !== undefined) return rank[t.key];
        if (visiting.has(t.key)) return 0;
        visiting.add(t.key);
        const targets = t.fields.filter((f) => f.link && f.link !== t.key && !f.multiple).map((f) => tableOf(f.link)).filter(Boolean);
        rank[t.key] = targets.length ? 1 + Math.max(...targets.map(depth)) : 0;
        visiting.delete(t.key);
        return rank[t.key];
      };
      tables().forEach(depth);
      const columns = {};
      tables().forEach((t) => (columns[rank[t.key]] = columns[rank[t.key]] || []).push(t));
      const out = {};
      const total = tables().reduce((n, t) => n + height(t) + 44, 0);
      const r = canvas.getBoundingClientRect();
      const tallest = Math.max(560, Math.sqrt(total * (W + 110) * Math.max(0.5, r.height / Math.max(r.width, 1))));  // the canvas's shape
      let col = 0;
      Object.keys(columns).sort((a, b) => a - b).forEach((c) => {
        let y = 0;
        columns[c].sort((a, b) => (a.module || "").localeCompare(b.module || "") || a.label.localeCompare(b.label)).forEach((t) => {
          if (y > 0 && y + height(t) > tallest) { col += 1; y = 0; }
          out[t.key] = [col * (W + 110), y];
          y += height(t) + 44;
        });
        col += 1;
      });
      places = fresh ? out : {...out, ...(model.positions || {})};
    }

    function draw() {
      themeColours();
      boxesLayer.innerHTML = ""; linksLayer.innerHTML = "";
      tables().forEach((t) => {
        if (!places[t.key]) places[t.key] = [Math.max(0, ...Object.values(places).map((p) => p[0])) + W + 110, 0];
        drawBox(t);
      });
      drawLinks();
      apply();
    }

    function drawBox(t) {
      const [x, y] = places[t.key];
      const g = node("g", {class: "box", transform: `translate(${x},${y})`, "data-table": t.key, tabindex: "0", role: "button",
                           "aria-label": `${t.label}: ${plural(t.fields.length, "field", "fields")}`}, boxesLayer);
      const on = selected && selected.table === t.key;
      node("rect", {x: 0, y: 0, width: W, height: height(t), rx: 9, fill: colours.panel, stroke: on ? colours.accent : colours.border, "stroke-width": on ? 2 : 1, class: "box-frame"}, g);
      node("path", {d: `M0 9a9 9 0 0 1 9-9h${W - 18}a9 9 0 0 1 9 9v${HEAD - 9}h-${W}z`, fill: on ? colours.accentWeak : colours.head}, g);
      node("line", {x1: 0, y1: HEAD, x2: W, y2: HEAD, stroke: colours.border}, g);
      const title = node("text", {x: 12, y: 18, "font-size": 13.5, "font-weight": 650, fill: colours.text}, g);
      title.textContent = clip(t.label, 26);
      const sub = node("text", {x: 12, y: 32, "font-size": 11, fill: colours.faint}, g);
      sub.textContent = (t.combines.length ? `Put together from ${plural(t.combines.length, "list", "lists")} · ` : t.embedded ? "Lines inside another list · " : "") + plural(t.count, "record", "records");
      if (canEdit) {
        const plus = node("g", {class: "link-handle", transform: `translate(${W - 22},${HEAD / 2})`, "data-handle": t.key}, g);
        node("circle", {r: 9, fill: colours.panel, stroke: colours.accent}, plus);
        node("path", {d: "M-4 0h8M0 -4v8", stroke: colours.accent, "stroke-width": 1.6}, plus);
        const tip = node("title", {}, plus); tip.textContent = `Drag onto another list to link ${t.label} to it`;
      }
      t.fields.forEach((f, i) => {
        const fy = HEAD + i * ROW;
        const row = node("g", {class: "row", "data-field": f.key, transform: `translate(0,${fy})`}, g);
        const sel = selected && selected.table === t.key && selected.field === f.key;
        node("rect", {x: 1, y: 1, width: W - 2, height: ROW - 1, fill: sel ? colours.accentWeak : "transparent", class: "row-bg"}, row);
        const name = node("text", {x: 12, y: 16, "font-size": 12.5, fill: f.calculated ? colours.muted : colours.text, "font-weight": f.title ? 650 : 400}, row);
        name.textContent = clip(f.label, 19) + (f.required ? " *" : "");
        const kind = node("text", {x: W - 12, y: 16, "font-size": 11, fill: f.link ? colours.accent : colours.faint, "text-anchor": "end"}, row);
        kind.textContent = clip(f.link ? `${f.multiple ? "several " : ""}${(tableOf(f.link) || {}).singular || f.kind}` : f.kind, 16);
        if (f.link) node("circle", {cx: W, cy: ROW / 2, r: 3.2, fill: colours.accent}, row);
      });
    }

    function clip(s, n) { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; }

    function drawLinks() {
      linksLayer.innerHTML = "";
      model.links.forEach((l) => {
        const from = tableOf(l.from), to = tableOf(l.to);
        if (!from || !to || !places[l.from] || !places[l.to]) return;
        const i = from.fields.findIndex((f) => f.key === l.field);
        const [fx, fy] = places[l.from], [tx, ty] = places[l.to];
        const y1 = fy + HEAD + i * ROW + ROW / 2, y2 = ty + HEAD / 2;
        const right = tx > fx + W / 2;
        const x1 = right ? fx + W : fx, x2 = right ? tx : tx + W;
        const self = l.from === l.to;
        const bend = Math.max(40, Math.abs(x2 - x1) / 2);
        const d = self ? `M${fx + W} ${y1} C${fx + W + 60} ${y1} ${fx + W + 60} ${y2} ${fx + W} ${y2}`
                       : `M${x1} ${y1} C${x1 + (right ? bend : -bend)} ${y1} ${x2 + (right ? -bend : bend)} ${y2} ${x2} ${y2}`;
        const hot = selected && (selected.table === l.from || selected.table === l.to);
        node("path", {d, fill: "none", stroke: hot ? colours.accent : colours.line, "stroke-width": hot ? 2 : 1.3, "stroke-dasharray": l.many ? "5 4" : "",
                      "data-from": l.from, "data-to": l.to, class: "link-line"}, linksLayer);
        node("circle", {cx: self ? fx + W : x2, cy: y2, r: 3.6, fill: hot ? colours.accent : colours.line}, linksLayer);
      });
      (model.combined || []).forEach((l) => {  // a combined list and each list it's put together from
        if (!places[l.from] || !places[l.to]) return;
        const [fx, fy] = places[l.from], [tx, ty] = places[l.to];
        const right = tx > fx + W / 2;
        const x1 = right ? fx + W : fx, x2 = right ? tx : tx + W, y1 = fy + HEAD / 2, y2 = ty + HEAD / 2;
        const bend = Math.max(40, Math.abs(x2 - x1) / 2);
        const hot = selected && (selected.table === l.from || selected.table === l.to);
        const p = node("path", {d: `M${x1} ${y1} C${x1 + (right ? bend : -bend)} ${y1} ${x2 + (right ? -bend : bend)} ${y2} ${x2} ${y2}`, fill: "none",
                                stroke: colours.accent, "stroke-width": hot ? 2.4 : 1.6, "stroke-dasharray": "2 4", "stroke-linecap": "round", opacity: hot ? 1 : 0.7}, linksLayer);
        node("title", {}, p).textContent = `${(tableOf(l.to) || {}).label} is put together from ${(tableOf(l.from) || {}).label}`;
      });
    }

    function apply() { layer.setAttribute("transform", `translate(${view.x},${view.y}) scale(${view.k})`); }

    function bounds() {
      const xs = tables().map((t) => places[t.key][0]), ys = tables().map((t) => places[t.key][1]);
      if (!xs.length) return {x: 0, y: 0, w: 400, h: 300};
      const x = Math.min(...xs), y = Math.min(...ys);
      return {x, y, w: Math.max(...tables().map((t) => places[t.key][0] + W)) - x, h: Math.max(...tables().map((t) => places[t.key][1] + height(t))) - y};
    }
    function fit(least = 0.15) {
      // Everything in view; or, at the start, no smaller than can be read, from the top left (the rest is a drag away).
      const b = bounds(), r = canvas.getBoundingClientRect();
      const whole = Math.min((r.width - 60) / (b.w + 60), (r.height - 70) / (b.h + 60));
      const k = Math.min(1.1, Math.max(least, whole));
      view = k > whole ? {k, x: 64 - b.x * k, y: 24 - b.y * k} : {k, x: (r.width - b.w * k) / 2 - b.x * k, y: Math.max(20, (r.height - b.h * k) / 2 - b.y * k)};
      apply();
    }
    function zoom(by, cx, cy) {
      const r = canvas.getBoundingClientRect();
      cx = cx ?? r.width / 2; cy = cy ?? r.height / 2;
      const k = Math.min(2.5, Math.max(0.2, view.k * by));
      view = {k, x: cx - (cx - view.x) * (k / view.k), y: cy - (cy - view.y) * (k / view.k)};
      apply();
    }

    // Moving around, moving lists, and linking one to another by dragging its +.
    let drag = null;
    const point = (ev) => { const r = canvas.getBoundingClientRect(); return [ev.clientX - r.left, ev.clientY - r.top]; };
    const toDiagram = ([px, py]) => [(px - view.x) / view.k, (py - view.y) / view.k];
    canvas.addEventListener("pointerdown", (ev) => {
      if (ev.button !== 0 || ev.target.closest(".studio-tools")) return;
      const handle = ev.target.closest("[data-handle]");
      const box = ev.target.closest(".box");
      const p = point(ev);
      if (handle && canEdit) {
        const [x, y] = toDiagram(p);
        drag = {kind: "link", from: handle.dataset.handle, line: node("path", {fill: "none", stroke: colours.accent, "stroke-width": 2, "stroke-dasharray": "4 3"}, layer), x, y};
      } else if (box) {
        const key = box.dataset.table;
        drag = {kind: "box", key, start: p, at: [...places[key]], moved: false, row: ev.target.closest("[data-field]")?.dataset.field};
      } else {
        drag = {kind: "pan", start: p, at: [view.x, view.y]};
        canvas.classList.add("panning");
      }
      canvas.setPointerCapture(ev.pointerId);
    });
    canvas.addEventListener("pointermove", (ev) => {
      if (!drag) return;
      const p = point(ev);
      if (drag.kind === "pan") { view.x = drag.at[0] + p[0] - drag.start[0]; view.y = drag.at[1] + p[1] - drag.start[1]; apply(); }
      else if (drag.kind === "box") {
        const dx = (p[0] - drag.start[0]) / view.k, dy = (p[1] - drag.start[1]) / view.k;
        if (!drag.moved && Math.hypot(dx, dy) < 4) return;
        drag.moved = true;
        places[drag.key] = [Math.round(drag.at[0] + dx), Math.round(drag.at[1] + dy)];
        $(`.box[data-table="${CSS.escape(drag.key)}"]`, boxesLayer).setAttribute("transform", `translate(${places[drag.key][0]},${places[drag.key][1]})`);
        drawLinks();
      } else if (drag.kind === "link") {
        const [x, y] = toDiagram(p);
        drag.line.setAttribute("d", `M${drag.x} ${drag.y} L${x} ${y}`);
        $$(".box", boxesLayer).forEach((b) => b.classList.toggle("drop", b.dataset.table !== drag.from && b.contains(document.elementFromPoint(ev.clientX, ev.clientY))));
      }
    });
    canvas.addEventListener("pointerup", async (ev) => {
      const d = drag; drag = null;
      canvas.classList.remove("panning");
      if (!d) return;
      if (d.kind === "box") {
        if (d.moved) savePlaces();
        else select(d.key, d.row);
      } else if (d.kind === "link") {
        d.line.remove();
        $$(".box.drop", boxesLayer).forEach((b) => b.classList.remove("drop"));
        const target = document.elementFromPoint(ev.clientX, ev.clientY)?.closest(".box");
        if (target && target.dataset.table !== d.from) {
          const made = await change({do: "link", from: d.from, to: target.dataset.table});
          if (made) {
            const t = tableOf(d.from);
            const f = t && t.fields[t.fields.length - 1];
            select(d.from, f && f.link === target.dataset.table ? f.key : null);
          }
        }
      }
    });
    canvas.addEventListener("wheel", (ev) => {
      ev.preventDefault();
      if (ev.ctrlKey || ev.metaKey) { const [x, y] = point(ev); zoom(Math.exp(-ev.deltaY / 300), x, y); }
      else { view.x -= ev.deltaX; view.y -= ev.deltaY; apply(); }
    }, {passive: false});
    canvas.addEventListener("keydown", (ev) => {
      const box = ev.target.closest?.(".box");
      if (box && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); select(box.dataset.table); }
      if (ev.key === "+" || ev.key === "=") zoom(1.2);
      if (ev.key === "-") zoom(1 / 1.2);
    });
    let placing;
    function savePlaces() {
      if (!canEdit) return;
      clearTimeout(placing);
      placing = setTimeout(() => post(root.dataset.place, {positions: places}), 400);
    }
    $$("[data-zoom]", root).forEach((b) => b.addEventListener("click", () => {
      if (b.dataset.zoom === "fit") fit(); else zoom(b.dataset.zoom === "in" ? 1.25 : 0.8);
    }));
    $("[data-tidy]", root)?.addEventListener("click", async () => {
      layout(true); draw(); fit(0.6);
      await post(root.dataset.place, {reset: true});
      savePlaces();
    });
    $("[data-download]", root)?.addEventListener("click", download);
    const undoButton = $("[data-undo-last]", root);
    undoButton?.addEventListener("click", takeBack);

    function download() {
      const b = bounds(), m = 30;
      const copy = svg.cloneNode(true);
      copy.setAttribute("xmlns", NS);
      copy.setAttribute("width", b.w + 2 * m); copy.setAttribute("height", b.h + 2 * m);
      copy.setAttribute("viewBox", `0 0 ${b.w + 2 * m} ${b.h + 2 * m}`);
      copy.setAttribute("font-family", getComputedStyle(document.body).fontFamily);
      copy.querySelector("g").setAttribute("transform", `translate(${m - b.x},${m - b.y})`);
      copy.querySelectorAll(".link-handle").forEach((x) => x.remove());
      node("rect", {x: 0, y: 0, width: "100%", height: "100%", fill: colours.bg}, copy).parentNode.insertBefore(copy.lastChild, copy.firstChild);
      const img = new Image();
      img.onload = () => {
        const c = document.createElement("canvas");
        c.width = (b.w + 2 * m) * 2; c.height = (b.h + 2 * m) * 2;
        const ctx = c.getContext("2d"); ctx.scale(2, 2); ctx.drawImage(img, 0, 0);
        const a = document.createElement("a");
        a.download = "data-model.png"; a.href = c.toDataURL("image/png"); a.click();
      };
      img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(new XMLSerializer().serializeToString(copy));
    }

    // --- changing it -----------------------------------------------------------------------------------------------
    async function change(body) {
      statusLine(status, ["Saving…"], "info");
      const done = await post(root.dataset.change, body);
      if (done.error) { statusLine(status, [done.error], "bad"); return false; }
      undoStack.push(done.before);
      if (undoButton) undoButton.disabled = false;
      model = done.model;
      if (selected && !tableOf(selected.table)) selected = null;
      if (selected && selected.field && !tableOf(selected.table).fields.some((f) => f.key === selected.field)) selected.field = null;
      draw(); showPanel();
      statusLine(status, done.said, "good", takeBack);
      return true;
    }
    async function takeBack() {
      const to = undoStack.pop();
      if (to === undefined) return;
      if (undoButton) undoButton.disabled = !undoStack.length;
      const done = await post(root.dataset.undo, {to});
      if (done.error) { statusLine(status, [done.error], "bad"); return; }
      await reload();
      statusLine(status, ["Taken back: it's as it was before that change"], "good");
    }
    async function reload() {
      const fresh = await get(root.dataset.json);
      if (!fresh) return;
      model = fresh;
      if (selected && !tableOf(selected.table)) selected = null;
      draw(); showPanel();
    }

    function select(table, field = null) {
      selected = table ? {table, field} : null;
      history.replaceState(null, "", table ? `?open=${encodeURIComponent(table)}` : location.pathname);
      draw(); showPanel();
      if (window.matchMedia("(max-width: 900px)").matches) panel.scrollIntoView({behavior: "smooth", block: "start"});
    }

    // --- the panel beside the diagram --------------------------------------------------------------------------------
    function showPanel() {
      panel.innerHTML = "";
      if (!selected) return panel.appendChild(overview());
      const t = tableOf(selected.table);
      const f = selected.field && t.fields.find((x) => x.key === selected.field);
      panel.appendChild(f ? fieldPanel(t, f) : tablePanel(t));
    }

    function overview() {
      const box = html(`<div class="stack">
        <div><h2 class="panel-title">${icon("network")}Your lists</h2>
          <p class="small muted">${plural(tables().length, "list", "lists")}, ${plural(model.links.length, "link", "links")} between them. A line joins a list to the one it points at: an order to its customer, a line to its order.</p></div>
        <ul class="studio-list">${tables().map((t) => `<li><button type="button" data-pick="${esc(t.key)}">${icon(t.icon)}<span class="grow">${esc(t.label)}</span><span class="faint tiny">${plural(t.fields.length, "field", "fields")}</span></button></li>`).join("")}</ul>
        ${canEdit ? `<p class="small faint">${icon("info")} Every change here is checked first and saved as a new version: <b>Take it back</b> undoes it. Records are never deleted by a change here.</p>` : ""}
      </div>`);
      $$("[data-pick]", box).forEach((b) => b.addEventListener("click", () => select(b.dataset.pick)));
      return box;
    }

    function tablePanel(t) {
      const sections = model.sections || [];
      const box = html(`<div class="stack">
        <div class="row"><button type="button" class="btn sm ghost" data-back>${icon("chevron-left")}All lists</button>
          ${t.embedded ? "" : `<a class="btn sm ghost" href="${esc(root.dataset.openTable.replace("__table__", t.key))}">Open the list${icon("external-link")}</a>`}
          <a class="btn sm ghost" href="${esc(root.dataset.workflows)}?table=${encodeURIComponent(t.key)}">${icon("workflow")}Its stages and rules</a></div>
        <div><h2 class="panel-title">${icon(t.icon)}${esc(t.label)}</h2><p class="small muted">${plural(t.count, "record", "records")}${t.embedded ? " · filled in inside another record" : ""}</p>
          ${t.combines.length ? `<p class="small">${icon("layers")} Put together from ${esc(t.combines.map((k) => (tableOf(k) || {label: k}).label).join(", "))}, and kept up to date as they change: nobody adds to it by hand. Ask the AI to change what goes into it.</p>` : ""}</div>
        ${canEdit ? `<form class="stack" data-form="table">
          <label class="field"><span>Name of the list</span><input type="text" name="label" value="${esc(t.label)}" required maxlength="80"></label>
          <label class="field"><span>One of them is called</span><input type="text" name="singular" value="${esc(t.singular)}" maxlength="60"></label>
          ${sections.length ? `<label class="field"><span>In the menu under</span><select name="section">${sections.map((s) => `<option value="${esc(s.key)}" ${s.key === t.module ? "selected" : ""}>${esc(s.label)}</option>`).join("")}</select></label>` : ""}
          <button class="btn sm">Save</button></form>` : ""}
        <div><div class="label">Fields${canEdit ? ` <span class="faint tiny">· drag to put them in order</span>` : ""}</div>
          <ul class="studio-fields" data-fields>${t.fields.map((f) => `<li ${canEdit ? 'draggable="true"' : ""} data-key="${esc(f.key)}">
            ${canEdit ? icon("grip-vertical", "grip") : ""}<button type="button" data-field="${esc(f.key)}"><span class="grow"><b>${esc(f.label)}</b>${f.required ? ' <span class="req">*</span>' : ""}${f.title ? ' <span class="tag">name</span>' : ""}${f.stage ? ' <span class="tag">stages</span>' : ""}
            <div class="faint tiny">${esc(f.kind)}${f.detail ? " · " + esc(f.detail) : ""}</div></span>${icon("chevron-right")}</button></li>`).join("")}</ul></div>
        ${canEdit ? addFieldForm(t) : ""}
        ${canEdit ? `<details class="danger-zone"><summary class="small">Delete this list</summary>
          <p class="small muted">${t.count ? `Its ${plural(t.count, "record goes", "records go")} to the bin with it, and come${t.count === 1 ? "s" : ""} back if you take it back.` : "It has no records."}</p>
          <button type="button" class="btn sm danger" data-delete-table>${icon("trash-2")}Delete ${esc(t.label)}</button></details>` : ""}
      </div>`);
      $("[data-back]", box).addEventListener("click", () => select(null));
      $$("[data-field]", box).forEach((b) => b.addEventListener("click", () => select(t.key, b.dataset.field)));
      $("[data-form=table]", box)?.addEventListener("submit", (ev) => {
        ev.preventDefault();
        const fd = new FormData(ev.target);
        change({do: "update_table", table: t.key, label: fd.get("label"), singular: fd.get("singular"), section: fd.get("section") || ""});
      });
      wireAddField(box, t);
      $("[data-delete-table]", box)?.addEventListener("click", () => {
        if (confirm(`Delete the list ${t.label}?${t.count ? ` Its ${t.count} records go to the bin with it.` : ""}`)) change({do: "delete_table", table: t.key});
      });
      if (canEdit) sortable($("[data-fields]", box), (keys) => change({do: "order_fields", table: t.key, keys}));
      return box;
    }

    function addFieldForm(t) {
      return `<details class="add-field" ${t.fields.length < 2 ? "open" : ""}><summary class="btn sm">${icon("plus")}Add a field</summary>
        <form class="stack card card-body" data-form="add-field">
          <label class="field"><span>Name</span><input type="text" name="label" required maxlength="80" placeholder="Phone, Due date, Customer…"></label>
          <label class="field"><span>It keeps</span><select name="type">${model.types.map((x) => `<option value="${x.type}">${esc(x.type === "link" ? "A link to another list" : x.label)}</option>`).join("")}</select></label>
          <label class="field" data-when="select multi_select" hidden><span>The choices, one on each line</span><textarea name="options" rows="4" placeholder="New&#10;In progress&#10;Done"></textarea></label>
          <label class="field" data-when="link" hidden><span>Which list</span><select name="link_to">${tables().map((x) => `<option value="${esc(x.key)}">${esc(x.label)}</option>`).join("")}</select></label>
          <label class="check" data-when="link" hidden><input type="checkbox" name="multiple"> Several of them, not just one</label>
          <label class="field" data-when="formula" hidden><span>Worked out as</span><input type="text" name="expression" placeholder="quantity * price"><span class="help">Use the other fields' names: ${esc(t.fields.filter((f) => ["number", "money", "percent", "formula", "rollup"].includes(f.type)).map((f) => f.key).slice(0, 6).join(", ") || "quantity, price")}</span></label>
          <label class="check"><input type="checkbox" name="required"> It must be filled in</label>
          <button class="btn primary sm">Add it</button></form></details>`;
    }
    function wireAddField(box, t) {
      const form = $("[data-form=add-field]", box);
      if (!form) return;
      const showFor = () => $$("[data-when]", form).forEach((el) => { el.hidden = !el.dataset.when.split(" ").includes(form.elements.type.value); });
      form.elements.type.addEventListener("change", showFor); showFor();
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const fd = new FormData(form);
        const made = await change({do: "add_field", table: t.key, label: fd.get("label"), type: fd.get("type"), options: fd.get("options"),
                                   link_to: fd.get("link_to"), to: fd.get("link_to"), multiple: !!fd.get("multiple"), expression: fd.get("expression"), required: !!fd.get("required")});
        if (made) { const fresh = tableOf(t.key); select(t.key, fresh.fields[fresh.fields.length - 1].key); }
      });
    }

    function fieldPanel(t, f) {
      const family = [["text", "long_text", "rich_text"], ["number", "money"], ["date", "datetime"]].find((x) => x.includes(f.type));
      // what it can change to: any kind while nothing's filled in; once something is, only a kind those values fit
      const kinds = f.calculated ? [] : model.types.filter((x) => x.type === f.type || !x.type.match(/formula|autonumber/)
        && (f.filled ? !!family && family.includes(x.type) : true));
      const numeric = ["number", "percent", "formula", "rollup"].includes(f.type);
      const box = html(`<div class="stack">
        <div class="row"><button type="button" class="btn sm ghost" data-back>${icon("chevron-left")}${esc(t.label)}</button></div>
        <div><h2 class="panel-title">${esc(f.label)}</h2><p class="small muted">${esc(f.kind)}${f.detail ? " · " + esc(f.detail) : ""}</p>
          ${f.ai ? `<p class="small"><span class="ai-tag">${icon("sparkles")}AI</span> The AI fills this in.</p>` : ""}</div>
        ${!canEdit ? `<dl class="small">${f.help ? `<dt>Help</dt><dd>${esc(f.help)}</dd>` : ""}<dt>Must be filled in</dt><dd>${f.required ? "Yes" : "No"}</dd></dl>` : `
        <form class="stack" data-form="field">
          <label class="field"><span>Name</span><input type="text" name="label" value="${esc(f.label)}" required maxlength="80"></label>
          ${kinds.length > 1 ? `<label class="field"><span>It keeps</span><select name="type">${kinds.map((x) => `<option value="${x.type}" ${x.type === f.type ? "selected" : ""}>${esc(x.label)}</option>`).join("")}</select>
            <span class="help">${f.filled ? `${plural(f.filled, "record has", "records have")} one, so only a kind they already fit.` : "Any kind, while nothing is filled in yet."}</span></label>`
            : f.filled && !f.calculated ? `<p class="help">${plural(f.filled, "record has", "records have")} one, so it stays ${esc(f.kind.toLowerCase())}: add a new field for another kind.</p>` : ""}
          ${f.type === "formula" ? `<label class="field"><span>Worked out as</span><input type="text" name="expression" value="${esc(f.expression)}"></label>` : ""}
          <label class="field"><span>Help shown under it</span><input type="text" name="help" value="${esc(f.help)}" maxlength="200" placeholder="What to put here"></label>
          ${f.calculated ? "" : `<label class="check"><input type="checkbox" name="required" ${f.required ? "checked" : ""}> It must be filled in</label>`}
          ${["text", "email", "phone", "url", "number", "barcode"].includes(f.type) ? `<label class="check"><input type="checkbox" name="unique" ${f.unique ? "checked" : ""}> No two can be the same</label>` : ""}
          ${["text", "number", "phone", "email", "barcode"].includes(f.type) ? `<label class="check"><input type="checkbox" name="sensitive" ${f.sensitive ? "checked" : ""}> Keep it private: shown hidden, never sent to the AI</label>` : ""}
          ${f.link ? `<label class="check"><input type="checkbox" name="multiple" ${f.multiple ? "checked" : ""}> Several of them, not just one</label>` : ""}
          ${numeric ? `<div class="row"><label class="field grow"><span>Decimal places</span><input name="decimals" type="number" min="0" max="6" value="${f.decimals ?? ""}"></label>
            <label class="field grow"><span>Unit</span><input type="text" name="unit" value="${esc(f.unit)}" maxlength="12" placeholder="kg, hrs"></label></div>` : ""}
          ${f.type === "money" ? `<label class="field"><span>Currency</span><input type="text" name="currency" value="${esc(f.currency)}" maxlength="3" placeholder="The app's own"></label>` : ""}
          ${["text", "number", "formula", "rollup", "barcode", "autonumber"].includes(f.type) ? `<div class="row"><label class="field grow"><span>Shown before it</span><input type="text" name="before" value="${esc(f.before)}" maxlength="12" placeholder="ABN, #"></label>
            <label class="field grow"><span>Shown after it</span><input type="text" name="after" value="${esc(f.after)}" maxlength="12" placeholder="pcs, km"></label></div>` : ""}
          ${f.signs !== null && f.signs !== undefined ? `<label class="field"><span>Colour it</span><select name="signs">${[["", "No colour"], ["normal", "Green above nothing, red below"], ["inverted", "Red above nothing, green below (costs, discounts)"]].map(([v, l]) => `<option value="${v}" ${v === f.signs ? "selected" : ""}>${l}</option>`).join("")}</select></label>` : ""}
          ${f.type === "checkbox" ? `<div class="row"><label class="field grow"><span>Yes shows as</span><input type="text" name="yes_label" value="${esc(f.yes_label)}" maxlength="30" placeholder="Yes"></label>
            <label class="field grow"><span>No shows as</span><input type="text" name="no_label" value="${esc(f.no_label)}" maxlength="30" placeholder="No"></label></div>` : ""}
          ${f.ai_fillable && !f.calculated ? `<fieldset class="ai-trust stack"><label class="check"><input type="checkbox" name="ai_on" ${f.ai_instructions ? "checked" : ""}> <span class="ai-tag">${icon("sparkles")}AI</span> The AI fills this in</label>
            <label class="field" data-ai-said ${f.ai_instructions ? "" : "hidden"}><span>What the AI is asked</span><textarea name="ai" rows="3" placeholder="Summarise the notes in one sentence">${esc(f.ai_instructions)}</textarea>
            <span class="help">It fills it in when a record is added or what it's made from changes; people can still change it.</span></label></fieldset>` : ""}
          ${!f.calculated && !["select", "multi_select", "link", "user", "file", "image", "signature"].includes(f.type) ? `<label class="field"><span>Filled in to start with</span><input type="text" name="default" value="${esc(f.default ?? "")}" maxlength="200"></label>` : ""}
          <button class="btn primary sm">Save</button></form>
        ${["select", "multi_select"].includes(f.type) ? choices(t, f) : ""}
        ${f.title ? "" : `<button type="button" class="btn sm ghost" data-title>${icon("tag")}Name each ${esc(t.singular.toLowerCase())} by this</button>`}
        <details class="danger-zone"><summary class="small">Remove this field</summary>
          <p class="small muted">What's filled in is kept, and comes back if you take it back.</p>
          <button type="button" class="btn sm danger" data-remove>${icon("trash-2")}Remove ${esc(f.label)}</button></details>`}
      </div>`);
      $("[data-back]", box).addEventListener("click", () => select(t.key));
      const form = $("[data-form=field]", box);
      form?.addEventListener("submit", (ev) => {
        ev.preventDefault();
        const fd = new FormData(form);
        const body = {do: "update_field", table: t.key, key: f.key, label: fd.get("label"), help: fd.get("help")};
        if (form.elements.type) body.type = fd.get("type");
        if (form.elements.required) body.required = !!fd.get("required");
        if (form.elements.unique) body.unique = !!fd.get("unique");
        if (form.elements.sensitive) body.sensitive = !!fd.get("sensitive");
        if (form.elements.multiple) body.multiple = !!fd.get("multiple");
        if (form.elements.expression) body.expression = fd.get("expression");
        if (form.elements.decimals) body.decimals = fd.get("decimals") === "" ? null : Number(fd.get("decimals"));
        if (form.elements.unit) body.unit = fd.get("unit");
        if (form.elements.currency) body.currency = fd.get("currency").toUpperCase();
        if (form.elements.default) body.default = fd.get("default");
        for (const k of ["before", "after", "yes_label", "no_label", "signs"]) if (form.elements[k]) body[k] = fd.get(k);
        if (form.elements.ai_on) body.ai = fd.get("ai_on") ? (fd.get("ai") || "").trim() || "Fill this in from the record" : "";
        change(body);
      });
      $("[name=ai_on]", box)?.addEventListener("change", (ev) => { $("[data-ai-said]", box).hidden = !ev.target.checked; });
      $("[data-title]", box)?.addEventListener("click", () => change({do: "update_table", table: t.key, title_field: f.key}));
      $("[data-remove]", box)?.addEventListener("click", () => { if (confirm(`Remove ${f.label} from ${t.label}?`)) change({do: "remove_field", table: t.key, key: f.key}); });
      wireChoices(box, t, f);
      return box;
    }

    function choices(t, f) {
      return `<div class="stack"><div class="label">Choices <span class="faint tiny">· drag to put them in order</span></div>
        <ul class="studio-choices" data-choices>${f.options.map((o) => `<li draggable="true" data-key="${esc(o)}">${icon("grip-vertical", "grip")}
          <input type="text" value="${esc(o)}" data-old="${esc(o)}" aria-label="Choice ${esc(o)}" maxlength="80">
          <label class="choice-colour pill ${esc((f.colours || {})[o] || "slate")}" title="Its colour"><span class="sr-only">Colour of ${esc(o)}</span>
            <select data-colour="${esc(o)}">${model.colours.map((c) => `<option value="${c}" ${c === (f.colours || {})[o] ? "selected" : ""}>${c === "slate" ? "Grey" : c[0].toUpperCase() + c.slice(1)}</option>`).join("")}</select></label>
          <button type="button" class="btn sm ghost icon" data-drop="${esc(o)}" aria-label="Remove ${esc(o)}" title="Remove">${icon("x")}</button></li>`).join("")}</ul>
        <form class="row" data-form="choice"><input type="text" name="choice" placeholder="Another choice" maxlength="80" aria-label="Another choice"><button class="btn sm">${icon("plus")}Add</button></form>
        <p class="help">Renaming a choice renames it on every record that has it, and everywhere your app uses it.</p></div>`;
    }
    function wireChoices(box, t, f) {
      const list = $("[data-choices]", box);
      if (!list) return;
      $$("input[data-old]", list).forEach((input) => input.addEventListener("change", () => {
        const now = input.value.trim();
        if (now && now !== input.dataset.old) change({do: "rename_option", table: t.key, key: f.key, old: input.dataset.old, new: now});
      }));
      $$("[data-drop]", list).forEach((b) => b.addEventListener("click", () => change({do: "update_field", table: t.key, key: f.key, options: f.options.filter((o) => o !== b.dataset.drop)})));
      $$("[data-colour]", list).forEach((sel) => sel.addEventListener("change", () => change({do: "update_field", table: t.key, key: f.key,
        option_colors: {...(f.option_colors || {}), [sel.dataset.colour]: sel.value}})));
      $("[data-form=choice]", box).addEventListener("submit", (ev) => {
        ev.preventDefault();
        const v = ev.target.elements.choice.value.trim();
        if (v) change({do: "update_field", table: t.key, key: f.key, options: [...f.options, v]});
      });
      sortable(list, (keys) => change({do: "update_field", table: t.key, key: f.key, options: keys}));
    }

    // first draw (a list named in the address opened, as from its own page or from Workflows)
    layout(false);
    const asked = new URLSearchParams(location.search).get("open");
    selected = asked && tableOf(asked) ? {table: asked, field: null} : null;
    draw();
    showPanel();
    requestAnimationFrame(() => fit(0.6));
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", draw);
    $("[data-studio-new-table]")?.addEventListener("click", async () => {
      const label = prompt("What's the new list called? (Suppliers, Jobs, Assets…)");
      if (!label) return;
      const made = await change({do: "add_table", label});
      if (made) { const t = tables().find((x) => x.label.toLowerCase() === label.trim().toLowerCase()); if (t) select(t.key); }
    });
    askBox(reload);
  }

  // Dragging rows of a list into a new order (fields, choices, stages).
  function sortable(list, done) {
    if (!list) return;
    let dragged = null;
    list.addEventListener("dragstart", (ev) => { dragged = ev.target.closest("li"); ev.dataTransfer.effectAllowed = "move"; dragged.classList.add("dragging"); });
    list.addEventListener("dragover", (ev) => {
      if (!dragged) return;
      ev.preventDefault();
      const over = ev.target.closest("li");
      if (!over || over === dragged) return;
      const r = over.getBoundingClientRect();
      list.insertBefore(dragged, ev.clientY > r.top + r.height / 2 ? over.nextSibling : over);
    });
    list.addEventListener("dragend", () => {
      if (!dragged) return;
      dragged.classList.remove("dragging");
      dragged = null;
      const keys = $$(":scope > li", list).map((li) => li.dataset.key);
      if (keys.join("\u0000") !== list.dataset.was) done(keys);
    });
    list.dataset.was = $$(":scope > li", list).map((li) => li.dataset.key).join("\u0000");
  }

  // =================================================================================================================
  // Workflows
  // =================================================================================================================
  const STEP_ICONS = {notify: "bell", send_email: "mail", task: "list-todo", request_approval: "circle-check", update_record: "pencil",
    create_record: "file-plus", wait: "clock", assign: "user", branch: "split", generate_document: "file-text", ai_fill: "sparkles",
    ai_check: "sparkles", ai_extract: "sparkles", ai_create_record: "sparkles", webhook: "webhook", create_for_each: "copy",
    update_each: "pencil", delete_record: "trash-2", run_automation: "zap", copy_record: "copy", payment_link: "credit-card"};
  const FLOW_ICONS = {info: "info", form: "pencil", scan: "search", pick: "list-todo", photo: "camera", signature: "pencil", decide: "split", print: "printer"};

  function workflowStudio(root) {
    const canEdit = !!root.dataset.canEdit;
    const rail = $(".wf-rail", root), main = $(".wf-main", root), panel = $(".studio-panel", root), status = $(".studio-status", root);
    let data = JSON.parse($("#studio-model").textContent);
    let item = JSON.parse($("#studio-opened").textContent);
    let tried = null;  // what "try it" said, shown on the rule's steps
    const undoStack = [];

    const tableOf = (key) => data.tables.find((t) => t.key === key);
    const fieldsOf = (key) => (tableOf(key) || {fields: []}).fields;

    function drawRail() {
      rail.innerHTML = data.items.length ? data.items.map((g) => `<div class="wf-group"><div class="wf-group-head">${icon(g.icon)}${esc(g.label)}</div>
        ${g.items.map((i) => `<button type="button" class="wf-item ${item && i.id === itemId() ? "active" : ""}" data-open="${esc(i.id)}">
          ${icon(i.kind === "stages" ? "arrow-right" : i.kind === "rule" ? "zap" : "list-todo")}<span class="grow ellipsis">${esc(i.label)}</span>
          ${i.kind === "rule" && !i.on ? '<span class="tag">off</span>' : ""}</button>`).join("")}</div>`).join("")
        : `<p class="small muted pad">Nothing yet. ${canEdit ? "Add a rule, or stages to a list." : ""}</p>`;
      $$("[data-open]", rail).forEach((b) => b.addEventListener("click", () => open(b.dataset.open)));
    }
    const itemId = () => item ? `${item.kind}:${item.kind === "stages" ? item.table : item.key}` : "";

    async function open(id) {
      if (!id) { item = null; draw(); return; }
      const got = await get(`${root.dataset.item}?id=${encodeURIComponent(id)}`);
      if (!got) { statusLine(status, ["That isn't there any more"], "bad"); return; }
      item = got; tried = null;
      history.replaceState(null, "", `${root.dataset.here}?open=${encodeURIComponent(id)}`);
      draw();
    }

    async function change(body, id = itemId()) {
      statusLine(status, ["Saving…"], "info");
      const done = await post(root.dataset.change, {id, change: body});
      if (done.error) { statusLine(status, [done.error], "bad"); return false; }
      undoStack.push(done.before);
      data.items = done.items; data.tables = done.tables;
      item = done.item; tried = null;
      if (done.id) history.replaceState(null, "", `${root.dataset.here}?open=${encodeURIComponent(done.id)}`);
      draw();
      statusLine(status, done.said, "good", takeBack);
      return true;
    }
    async function takeBack() {
      const to = undoStack.pop();
      if (to === undefined) return;
      const done = await post(root.dataset.undo, {to});
      if (done.error) { statusLine(status, [done.error], "bad"); return; }
      location.reload();
    }

    function draw() {
      drawRail();
      main.innerHTML = ""; panel.innerHTML = "";
      if (!item) {
        main.appendChild(html(`<div class="card empty"><h3>${icon("workflow", "big")}<br>Choose something on the left</h3>
          <p class="muted">Each list's stages, each rule and each step-by-step flow is drawn here.${canEdit ? " Or start a new rule." : ""}</p></div>`));
        return;
      }
      if (item.kind === "rule") drawRule();
      else if (item.kind === "stages") drawStages();
      else drawFlow();
    }

    // --- a rule as a flowchart --------------------------------------------------------------------------------------
    function counts(c) {
      if (!c || !(c.ran || c.skipped || c.stopped)) return "";
      return `<div class="wf-counts">${c.ran ? `<span title="Times this step was done lately">${icon("check")}${c.ran}</span>` : ""}${c.skipped ? `<span title="Times it was skipped">${icon("arrow-right")}${c.skipped} skipped</span>` : ""}${c.stopped ? `<span class="bad-text" title="Times a run stopped here">${icon("triangle-alert")}${c.stopped} stopped</span>` : ""}</div>`;
    }
    function triedClass(at) {
      if (!tried || !tried.starts) return "";
      const s = tried.steps[at];
      return s ? `tried-${s.state}` : "tried-not_taken";
    }
    function stepsHtml(steps, into) {
      let out = "";
      steps.forEach((s, i) => {
        out += `${addHere(into, i)}<div class="wf-node wf-step ${triedClass(s.at)}" data-at="${esc(s.at)}" tabindex="0" role="button">
          <div class="wf-kind">${icon(STEP_ICONS[s.type] || "zap")}${esc(s.kind)}${s.ai ? ` <span class="ai-tag">${icon("sparkles")}AI</span>` : ""}</div>
          <div class="wf-text">${esc(s.text)}</div>
          ${s.ai ? `<div class="wf-cond">${icon(s.ai.review === "never" ? "zap" : "user")}${s.ai.manual ? "Switched off: a person does it instead" : esc(s.ai.words[0].toUpperCase() + s.ai.words.slice(1))}${s.ai.checked ? ` · right ${s.ai.right} of ${s.ai.checked}` : ""}</div>` : ""}
          ${s.ai_could && canEdit ? `<div class="wf-cond">${icon("sparkles")}The AI could do this</div>` : ""}
          ${s.only_if ? `<div class="wf-cond">${icon("filter")}Only if ${esc(s.only_if)}</div>` : ""}
          ${tried && tried.steps[s.at] && tried.steps[s.at].why ? `<div class="wf-why">${esc(tried.steps[s.at].why)}</div>` : ""}
          ${counts(s.counts)}</div>`;
        if (s.ways) {
          out += `<div class="wf-ways ${s.type === "request_approval" ? "side" : ""}">${s.ways.map((w) => `<div class="wf-way">
            <div class="wf-way-label" ${s.type === "branch" && w.n !== "o" && canEdit ? `data-way="${esc(s.at)}" data-n="${w.n}" role="button" tabindex="0"` : ""}>${esc(w.label)}</div>
            ${stepsHtml(w.steps, w.into)}${w.steps.length ? "" : `<div class="faint small wf-nothing">Nothing more</div>`}${canEdit ? addHere(w.into, w.steps.length, true) : ""}</div>`).join("")}
            ${s.type === "branch" && canEdit ? `<button type="button" class="wf-way-add" data-add-way="${esc(s.at)}">${icon("plus")}Another way</button>` : ""}</div>
            ${s.type === "request_approval" ? `<div class="wf-yes faint small">${icon("check")}If they say yes, it carries on</div>` : ""}`;
        }
      });
      return out;
    }
    function addHere(into, index, last = false) {
      if (!canEdit) return '<div class="wf-arrow"></div>';
      return `<div class="wf-arrow ${last ? "last" : ""}"><button type="button" class="wf-plus" data-into="${esc(into)}" data-index="${index}" title="Add a step here" aria-label="Add a step here">${icon("plus")}</button></div>`;
    }

    function drawRule() {
      const r = item;
      main.appendChild(html(`<div class="wf-head">
        <div class="grow"><h2>${icon("zap")}${esc(r.name)}</h2>
          <p class="small muted">${r.runs.total ? `Ran ${plural(r.runs.total, "time", "times")} lately${r.runs.failed ? `, <span class="bad-text">${r.runs.failed} stopped with a problem</span>` : ""}` : "It hasn't run yet"}</p></div>
        <div class="row wrap">${r.table ? `<button type="button" class="btn sm" data-try-it>${icon("play")}Try it on a record</button>` : ""}
          ${canEdit ? `<button type="button" class="switch ${r.on ? "on" : ""}" data-toggle title="${r.on ? "Turn off" : "Turn on"}" aria-label="${r.on ? "Turn off" : "Turn on"}"></button>` : `<span class="tag">${r.on ? "on" : "off"}</span>`}</div></div>`));
      if (tried) {
        main.appendChild(html(`<div class="wf-tried ${tried.starts ? "good" : "bad"}">${icon(tried.starts ? "play" : "circle-x")}<div class="grow">
          ${tried.starts ? `<b>For “${esc(tried.title)}”</b>, it would do the steps lit up below. Nothing was actually done.` : `<b>It wouldn't start for “${esc(tried.title)}”:</b> ${esc(tried.why.join("; "))}.`}
          ${tried.assumed.length ? `<div class="small muted">Tried as if this had just happened: ${esc(tried.assumed.join("; "))}.</div>` : ""}</div>
          <button type="button" class="btn sm ghost" data-untry>Clear</button></div>`));
      }
      const chart = html(`<div class="wf-chart ${r.on ? "" : "off"}">
        <div class="wf-node wf-when" data-edit="when" tabindex="0" role="button"><div class="wf-kind">${icon("zap")}When</div><div class="wf-text">${esc(r.when.replace(/^When /, ""))}</div></div>
        ${r.only_if.length || canEdit ? `<div class="wf-arrow"></div><div class="wf-node wf-if ${r.only_if.length ? "" : "ghost"}" data-edit="if" tabindex="0" role="button"><div class="wf-kind">${icon("filter")}Only if</div>
          <div class="wf-text">${r.only_if.length ? esc(r.only_if.join(", and ")) : "Every time (click to add a condition)"}</div></div>` : ""}
        ${stepsHtml(r.steps, "")}${addHere("", r.steps.length, true)}<div class="wf-end">${icon("circle-check")}Done</div></div>`);
      main.appendChild(chart);
      $("[data-toggle]", main)?.addEventListener("click", () => change({do: "set_enabled", on: !r.on}));
      $("[data-try-it]", main)?.addEventListener("click", () => tryPanel());
      $("[data-untry]", main)?.addEventListener("click", () => { tried = null; draw(); });
      $$(".wf-step", chart).forEach((n) => activate(n, () => stepPanel(n.dataset.at)));
      activate($("[data-edit=when]", chart), whenPanel);
      const cond = $("[data-edit=if]", chart);
      if (cond) activate(cond, conditionsPanel);
      $$("[data-into]", chart).forEach((b) => b.addEventListener("click", (ev) => { ev.stopPropagation(); newStepPanel(b.dataset.into, Number(b.dataset.index)); }));
      $$("[data-add-way]", chart).forEach((b) => b.addEventListener("click", () => wayPanel(b.dataset.addWay, null)));
      $$("[data-way]", chart).forEach((b) => activate(b, () => wayPanel(b.dataset.way, Number(b.dataset.n))));
      rulePanel();
    }
    function activate(el, fn) {
      if (!el) return;
      el.addEventListener("click", fn);
      el.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); fn(); } });
    }
    const findStep = (steps, at) => {
      for (const s of steps) {
        if (s.at === at) return s;
        for (const w of s.ways || []) { const f = findStep(w.steps, at); if (f) return f; }
      }
      return null;
    };

    function rulePanel() {
      const r = item;
      panel.innerHTML = "";
      panel.appendChild(html(`<div class="stack"><h2 class="panel-title">${icon("zap")}About this rule</h2>
        ${canEdit ? `<form class="stack" data-form="rename"><label class="field"><span>Name</span><input type="text" name="name" value="${esc(r.name)}" required maxlength="80"></label><button class="btn sm">Save</button></form>` : ""}
        <p class="small muted">Click any box to see it in full${canEdit ? " or change it; a + adds a step there" : ""}.${r.runs.total ? " The numbers on a step say how often it ran in the rule's last runs." : ""}</p>
        ${r.can_run ? `<button type="button" class="btn sm" data-run-now>${icon("play")}Run it now</button>` : ""}
        ${r.hook ? `<div><div class="label">${icon("webhook")}Its address for other systems</div><p class="small muted">Your web shop or a form tool starts this rule by sending its data here. Keep it private.</p>
          <div class="row"><input type="text" class="keybox grow small" readonly value="${esc(r.hook)}" aria-label="Webhook address"><button class="btn sm" type="button" data-copy-hook>${icon("copy")}Copy</button></div></div>` : ""}
        ${r.recent.length ? `<div><div class="label">The last times it ran</div><ul class="timeline-list">${r.recent.map((run) => `<li>
          <div class="row"><span class="status ${esc(run.status)}">${esc(run.said)}</span><span class="small grow ellipsis">${esc(run.trigger)}</span><span class="faint tiny nowrap">${esc(run.when)}</span></div>
          ${run.steps.length ? `<ul class="steps">${run.steps.map((x) => `<li class="${x.skipped ? "skipped" : ""}">${esc(x.text)}</li>`).join("")}</ul>` : ""}
          ${run.error ? `<div class="small bad-text mt-1">${esc(run.error)}</div>` : ""}</li>`).join("")}</ul>
          <a class="small" href="${esc(root.dataset.allRuns)}#${esc(r.key)}">Every rule's recent runs</a></div>` : ""}
        ${canEdit ? `<details class="danger-zone"><summary class="small">Delete this rule</summary><button type="button" class="btn sm danger" data-delete>${icon("trash-2")}Delete ${esc(r.name)}</button></details>` : ""}</div>`));
      $("[data-form=rename]", panel)?.addEventListener("submit", (ev) => { ev.preventDefault(); change({do: "rename", name: ev.target.elements.name.value}); });
      $("[data-delete]", panel)?.addEventListener("click", () => { if (confirm(`Delete the rule “${r.name}”? What it already did stays done.`)) change({do: "remove_rule"}); });
      $("[data-copy-hook]", panel)?.addEventListener("click", () => { navigator.clipboard?.writeText(r.hook); statusLine(status, ["Copied the address"], "good"); });
      $("[data-run-now]", panel)?.addEventListener("click", async () => {
        statusLine(status, ["Running it…"], "info");
        try {
          const res = await fetch(root.dataset.runNow.replace("__key__", encodeURIComponent(r.key)), {method: "POST", headers: {"X-CSRFToken": csrf()}, body: new URLSearchParams()});
          if (!res.ok) throw new Error();
          await open(itemId());
          statusLine(status, ["It ran: its runs are below"], "good");
        } catch (e) { statusLine(status, ["It couldn't be run now: try again"], "bad"); }
      });
    }

    // Who, values and conditions: the pieces the step forms are made of.
    const whoSelect = (name, value) => `<select name="${name}">${item.who.map((w) => `<option value="${esc(w.value)}" ${w.value === value ? "selected" : ""}>${esc(w.label)}</option>`).join("")}
      ${value && !item.who.some((w) => w.value === value) ? `<option value="${esc(value)}" selected>${esc(value)}</option>` : ""}</select>`;
    const insertable = (table) => fieldsOf(table).map((f) => `<option value="{{record.${esc(f.key)}}}">${esc(f.label)}</option>`).join("");
    function textWithInserts(name, label, value, long = false, table = item.table) {
      return `<label class="field"><span>${label}</span>${long ? `<textarea name="${name}" rows="4">${esc(value || "")}</textarea>` : `<input type="text" name="${name}" value="${esc(value || "")}">`}
        ${table ? `<select class="insert" data-insert="${name}" aria-label="Put in a detail of the record"><option value="">+ Put in a detail…</option>${insertable(table)}</select>` : ""}</label>`;
    }
    function wireInserts(scope) {
      $$("[data-insert]", scope).forEach((sel) => sel.addEventListener("change", () => {
        const target = sel.form.elements[sel.dataset.insert];
        const at = target.selectionStart ?? target.value.length;
        target.value = target.value.slice(0, at) + sel.value + target.value.slice(at);
        sel.value = ""; target.focus();
      }));
    }
    function valuesRows(table, values) {
      const fields = fieldsOf(table).filter((f) => f.stored);
      const rows = Object.entries(values || {});
      if (!rows.length) rows.push(["", ""]);
      return `<div class="stack" data-values>${rows.map(([k, v]) => valueRow(fields, k, v)).join("")}</div>
        <button type="button" class="btn sm ghost" data-add-value>${icon("plus")}Another field</button>`;
    }
    function valueRow(fields, k, v) {
      const f = fields.find((x) => x.key === k);
      const input = f && f.options.length ? `<select name="value">${f.options.map((o) => `<option ${o === v ? "selected" : ""}>${esc(o)}</option>`).join("")}${v && !f.options.includes(v) ? `<option selected>${esc(v)}</option>` : ""}</select>`
                                          : `<input type="text" name="value" value="${esc(typeof v === "object" ? JSON.stringify(v) : v ?? "")}" placeholder="A value, or {{record.field}}">`;
      return `<div class="row value-row"><select name="field" aria-label="Field"><option value="">Field…</option>${fields.map((x) => `<option value="${esc(x.key)}" ${x.key === k ? "selected" : ""}>${esc(x.label)}</option>`).join("")}</select>
        <span class="faint">=</span>${input}<button type="button" class="btn sm ghost icon" data-drop-row aria-label="Remove">${icon("x")}</button></div>`;
    }
    function wireValues(scope, table) {
      const box = $("[data-values]", scope);
      if (!box) return;
      const fields = fieldsOf(table).filter((f) => f.stored);
      $("[data-add-value]", scope).addEventListener("click", () => { box.appendChild(html(valueRow(fields, "", ""))); wireRows(); });
      const wireRows = () => {
        $$("[data-drop-row]", box).forEach((b) => { b.onclick = () => b.closest(".value-row").remove(); });
        $$("select[name=field]", box).forEach((s) => { s.onchange = () => { const row = s.closest(".value-row"); row.replaceWith(html(valueRow(fields, s.value, ""))); wireRows(); }; });
      };
      wireRows();
    }
    const readValues = (scope) => Object.fromEntries($$(".value-row", scope).map((row) => [row.querySelector("[name=field]").value, row.querySelector("[name=value]").value]).filter(([k]) => k));

    function conditionRows(table, conditions) {
      const rows = (conditions || []).filter((c) => !["changed", "changed_to"].includes(c.op));
      return `<div class="stack" data-conds>${rows.map((c) => condRow(table, c)).join("")}</div>
        <button type="button" class="btn sm ghost" data-add-cond>${icon("plus")}Add a condition</button>`;
    }
    function condRow(table, c = {}) {
      const fields = fieldsOf(table);
      const f = fields.find((x) => x.key === c.field);
      const op = data.ops.find((o) => o.op === (c.op || "equals")) || data.ops[0];
      const value = Array.isArray(c.value) ? c.value.join(", ") : (c.value ?? "");
      const input = !op.value ? "" : f && f.options.length && ["equals", "not_equals"].includes(op.op)
        ? `<select name="value">${f.options.map((o) => `<option ${o === value ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`
        : `<input type="text" name="value" value="${esc(value)}" placeholder="Value">`;
      return `<div class="row wrap cond-row"><select name="field" aria-label="Field"><option value="">Field…</option>${fields.map((x) => `<option value="${esc(x.key)}" ${x.key === c.field ? "selected" : ""}>${esc(x.label)}</option>`).join("")}</select>
        <select name="op" aria-label="Test">${data.ops.map((o) => `<option value="${o.op}" ${o.op === op.op ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select>
        ${input}<button type="button" class="btn sm ghost icon" data-drop-row aria-label="Remove">${icon("x")}</button></div>`;
    }
    function wireConds(scope, table) {
      const box = $("[data-conds]", scope);
      if (!box) return;
      const wireRows = () => {
        $$("[data-drop-row]", box).forEach((b) => { b.onclick = () => b.closest(".cond-row").remove(); });
        $$(".cond-row", box).forEach((row) => {
          const redo = () => { const c = readCond(row); row.replaceWith(html(condRow(table, c))); wireRows(); };
          row.querySelector("[name=field]").onchange = redo;
          row.querySelector("[name=op]").onchange = redo;
        });
      };
      $("[data-add-cond]", scope).addEventListener("click", () => { box.appendChild(html(condRow(table, {}))); wireRows(); });
      wireRows();
    }
    const readCond = (row) => {
      const c = {field: row.querySelector("[name=field]").value, op: row.querySelector("[name=op]").value};
      const v = row.querySelector("[name=value]");
      if (v) c.value = c.op === "one_of" ? v.value.split(",").map((x) => x.trim()).filter(Boolean) : v.value;
      return c;
    };
    const readConds = (scope) => $$(".cond-row", scope).map(readCond).filter((c) => c.field);

    function whenPanel() {
      const r = item, tr = r.trigger;
      const tables = data.tables;
      panel.innerHTML = "";
      const box = html(`<div class="stack"><h2 class="panel-title">${icon("zap")}When it starts</h2><p class="small muted">${esc(r.when)}</p>
        ${canEdit ? `<form class="stack" data-form="when">
          <label class="field"><span>It starts when</span><select name="type">
            ${[["record_created", "a record is added"], ["record_updated", "a record is changed"], ["record_created_or_updated", "a record is added or changed"],
               ["record_deleted", "a record is deleted"], ["button", "someone presses its button"], ["schedule", "on a schedule"]].map(([v, l]) => `<option value="${v}" ${v === tr.type ? "selected" : ""}>${l}</option>`).join("")}
            ${["email_received", "file_added", "webhook_received"].includes(tr.type) ? `<option value="${tr.type}" selected>${esc(r.when)}</option>` : ""}</select></label>
          <label class="field" data-for="record"><span>In the list</span><select name="table">${tables.map((t) => `<option value="${esc(t.key)}" ${t.key === tr.table ? "selected" : ""}>${esc(t.label)}</option>`).join("")}</select></label>
          <label class="field" data-for="button"><span>The button says</span><input type="text" name="label" value="${esc(tr.label || "Run")}" maxlength="40"></label>
          <div class="row" data-for="schedule"><label class="field grow"><span>Every</span><select name="every">${["hour", "day", "week", "month"].map((e) => `<option ${e === tr.every ? "selected" : ""}>${e}</option>`).join("")}</select></label>
            <label class="field grow"><span>At</span><input name="at" type="time" value="${esc(tr.at || "08:00")}"></label></div>
          <button class="btn primary sm">Save</button></form>` : ""}</div>`);
      panel.appendChild(box);
      const form = $("[data-form=when]", box);
      if (!form) return;
      const show = () => {
        const t = form.elements.type.value;
        $$("[data-for]", form).forEach((el) => {
          el.hidden = !(el.dataset.for === "record" ? ["record_created", "record_updated", "record_created_or_updated", "record_deleted", "button"].includes(t) : el.dataset.for === t);
        });
      };
      form.elements.type.addEventListener("change", show); show();
      form.addEventListener("submit", (ev) => {
        ev.preventDefault();
        const t = form.elements.type.value;
        const trigger = {...(["email_received", "file_added", "webhook_received"].includes(t) ? tr : {}), type: t};
        if (t !== "schedule" && !["email_received", "file_added", "webhook_received"].includes(t)) trigger.table = form.elements.table.value;
        if (t === "button") trigger.label = form.elements.label.value;
        if (t === "schedule") { trigger.every = form.elements.every.value; trigger.at = form.elements.at.value; }
        change({do: "set_trigger", trigger});
      });
    }

    function conditionsPanel() {
      const r = item;
      panel.innerHTML = "";
      const waits = r.conditions.filter((c) => ["changed", "changed_to"].includes(c.op));
      const box = html(`<div class="stack"><h2 class="panel-title">${icon("filter")}Only if</h2>
        <p class="small muted">${r.only_if.length ? "It only goes on when all of these are true." : "It goes on every time it starts."}</p>
        ${canEdit && r.table ? `<form class="stack" data-form="conds">${conditionRows(r.table, r.conditions)}
          ${waits.length ? `<p class="help">It also waits for: ${esc(r.when)} (that's part of when it starts).</p>` : ""}
          <button class="btn primary sm">Save</button></form>` : `<ul class="small">${r.only_if.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>`}</div>`);
      panel.appendChild(box);
      const form = $("[data-form=conds]", box);
      if (!form) return;
      wireConds(form, r.table);
      form.addEventListener("submit", (ev) => { ev.preventDefault(); change({do: "set_conditions", conditions: [...waits, ...readConds(form)]}); });
    }

    // A step's form: the settings people change most, for each kind of step. Anything else is the AI's to change.
    function stepForm(type, s = {}) {
      const r = item;
      const tables = data.tables;
      switch (type) {
        case "notify": return `<label class="field"><span>Who's told</span>${whoSelect("to", s.to || "admin")}</label>${textWithInserts("message", "What it says", s.message, true)}`;
        case "send_email": return `${textWithInserts("to", "To (an address, or a detail of the record)", s.to)}${textWithInserts("subject", "Subject", s.subject)}${textWithInserts("body", "Message", s.body, true)}`;
        case "task": return `<label class="field"><span>Who does it</span>${whoSelect("to", s.to || "editor")}</label>${textWithInserts("message", "What they're asked to do", s.message, true)}
          <label class="field"><span>How urgent</span><select name="priority">${["", "low", "normal", "high", "urgent"].map((p) => `<option value="${p}" ${p === (s.priority || "") ? "selected" : ""}>${p ? p[0].toUpperCase() + p.slice(1) : "Normal"}</option>`).join("")}</select></label>`;
        case "request_approval": return `<label class="field"><span>Who approves</span>${whoSelect("approver", s.approver || "admin")}</label>${textWithInserts("message", "What they're asked", s.message || "Please approve this.", true)}`;
        case "update_record": return `<div class="label">Set these on the ${esc((tableOf(r.table) || {singular: "record"}).singular.toLowerCase())}</div>${valuesRows(r.table, s.values)}`;
        case "create_record": return `<label class="field"><span>Add it to</span><select name="table" data-new-table>${tables.map((t) => `<option value="${esc(t.key)}" ${t.key === s.table ? "selected" : ""}>${esc(t.label)}</option>`).join("")}</select></label>
          <div class="label">With</div><div data-values-for>${valuesRows(s.table || tables[0].key, s.values)}</div><p class="help">Fields with the same name are copied across by themselves.</p>`;
        case "wait": return `<div class="row"><label class="field grow"><span>Days</span><input name="days" type="number" min="0" value="${s.days || ""}"></label>
          <label class="field grow"><span>Hours</span><input name="hours" type="number" min="0" value="${s.hours || ""}"></label><label class="field grow"><span>Minutes</span><input name="minutes" type="number" min="0" value="${s.minutes || ""}"></label></div>
          <label class="field"><span>Or until the date in</span><select name="until"><option value="">—</option>${fieldsOf(r.table).filter((f) => ["date", "datetime"].includes(f.type)).map((f) => `<option value="${esc(f.key)}" ${f.key === s.until ? "selected" : ""}>${esc(f.label)}</option>`).join("")}</select></label>`;
        case "assign": return `<label class="field"><span>Fill in the person in</span><select name="field">${fieldsOf(r.table).filter((f) => f.type === "user").map((f) => `<option value="${esc(f.key)}" ${f.key === s.field ? "selected" : ""}>${esc(f.label)}</option>`).join("")}</select></label>
          <label class="field"><span>Taking turns among</span>${whoSelect("from", s.from || "editor")}</label>`;
        case "branch": return `<p class="small muted">Records go the first way whose conditions they meet; the rest go “anything else”.</p>
          <label class="field"><span>Name of the first way</span><input type="text" name="label" placeholder="Big orders"></label><div class="label">It goes this way when</div>${conditionRows(r.table, [{}])}`;
        case "generate_document": return `<label class="field"><span>Make</span><select name="document">${item.documents.map((d) => `<option value="${esc(d.key)}" ${d.key === s.document ? "selected" : ""}>${esc(d.label)}</option>`).join("")}</select></label>`;
        case "ai_fill": case "ai_check": return `<label class="field"><span>${type === "ai_check" ? "Its verdict goes in" : "Fill in"}</span><select name="field">${fieldsOf(r.table).filter((f) => f.stored).map((f) => `<option value="${esc(f.key)}" ${f.key === s.field ? "selected" : ""}>${esc(f.label)}</option>`).join("")}</select></label>
          <label class="field"><span>What the AI is asked</span><textarea name="instructions" rows="4">${esc(s.instructions || "")}</textarea></label>`;
        case "ai_extract": return `<label class="field"><span>Read</span><select name="from">${[...fieldsOf(r.table).filter((f) => ["file", "image"].includes(f.type)).map((f) => [f.key, f.label]), ["email", "The email that started it"], ["file", "The file that started it"]].map(([k, l]) => `<option value="${esc(k)}" ${k === s.from ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label>
          <label class="field"><span>And fill in</span><select name="fields" multiple size="5">${fieldsOf(r.table).filter((f) => f.stored).map((f) => `<option value="${esc(f.key)}" ${(s.fields || []).includes(f.key) ? "selected" : ""}>${esc(f.label)}</option>`).join("")}</select></label>
          <label class="field"><span>What to look out for</span><textarea name="instructions" rows="3">${esc(s.instructions || "")}</textarea></label>`;
        case "ai_create_record": return `<label class="field"><span>Make a new one in</span><select name="table">${tables.map((t) => `<option value="${esc(t.key)}" ${t.key === s.table ? "selected" : ""}>${esc(t.label)}</option>`).join("")}</select></label>
          <label class="field"><span>What the AI is asked</span><textarea name="instructions" rows="3">${esc(s.instructions || "")}</textarea></label>`;
        case "webhook": return `<label class="field"><span>Send to the address</span><input type="text" name="url" value="${esc(s.url || "https://")}"></label>
          <label class="field"><span>As</span><select name="method">${["POST", "PUT", "PATCH", "GET"].map((m) => `<option ${m === s.method ? "selected" : ""}>${m}</option>`).join("")}</select></label>`;
        default: return "";
      }
    }
    function readStep(type, form, old = {}) {
      const v = (n) => form.elements[n] ? form.elements[n].value : undefined;
      const step = {type};
      const keep = (...names) => names.forEach((n) => { if (old[n] !== undefined) step[n] = old[n]; });
      switch (type) {
        case "notify": return {...step, to: v("to"), message: v("message")};
        case "send_email": keep("attach", "cc"); return {...step, to: v("to"), subject: v("subject"), body: v("body")};
        case "task": keep("fields", "flow", "not_by", "follow_up"); return {...step, to: v("to"), message: v("message"), priority: v("priority")};
        case "request_approval": keep("approvers", "mode", "not_by", "step_up", "follow_up"); return {...step, approver: v("approver"), message: v("message")};
        case "update_record": keep("target"); return {...step, values: readValues(form)};
        case "create_record": keep("copy_matching"); return {...step, table: v("table"), values: readValues(form)};
        case "wait": return {...step, days: Number(v("days")) || undefined, hours: Number(v("hours")) || undefined, minutes: Number(v("minutes")) || undefined, until: v("until") || undefined};
        case "assign": return {...step, field: v("field"), from: v("from")};
        case "branch": return {...step, paths: [{label: v("label"), when: readConds(form), steps: []}], otherwise: []};
        case "generate_document": keep("field", "target"); return {...step, document: v("document")};
        case "ai_fill": case "ai_check": keep("review", "reviewer", "reads", "notes_field", "use_knowledge"); return {...step, field: v("field"), instructions: v("instructions")};
        case "ai_extract": keep("review", "reviewer", "use_knowledge"); return {...step, from: v("from"), fields: Array.from(form.elements.fields.selectedOptions).map((o) => o.value), instructions: v("instructions")};
        case "ai_create_record": keep("review", "reviewer", "attach_to", "use_knowledge"); return {...step, table: v("table"), instructions: v("instructions")};
        case "webhook": keep("payload", "save", "app"); return {...step, url: v("url"), method: v("method")};
        default: return old;
      }
    }
    function wireStepForm(form, type) {
      wireInserts(form);
      if (type === "update_record") wireValues(form, item.table);
      if (type === "branch") wireConds(form, item.table);
      if (type === "create_record") {
        const sel = $("[data-new-table]", form), holder = $("[data-values-for]", form);
        wireValues(form, sel.value);
        sel.addEventListener("change", () => { holder.innerHTML = valuesRows(sel.value, {}); wireValues(form, sel.value); });
      }
    }

    function stepPanel(at) {
      const s = findStep(item.steps, at);
      if (!s) return;
      panel.innerHTML = "";
      const formHtml = canEdit && s.editable && s.type !== "branch" ? stepForm(s.type, s.step) : "";
      const box = html(`<div class="stack"><button type="button" class="btn sm ghost" data-back>${icon("chevron-left")}About this rule</button>
        <h2 class="panel-title">${icon(STEP_ICONS[s.type] || "zap")}${esc(s.kind)}</h2><p class="small muted">${esc(s.text)}</p>
        ${s.only_if ? `<p class="small">${icon("filter")} Only if ${esc(s.only_if)}</p>` : ""}
        ${s.ai ? `<div class="ai-trust stack"><div class="small">${s.ai.checked ? `People checked it ${plural(s.ai.checked, "time", "times")}: right ${s.ai.right} (${Math.round((s.ai.share || 0) * 100)}%).` : "Nobody has checked its work yet."}${s.ai.unchecked ? ` ${s.ai.unchecked} done without a check.` : ""}</div>
          ${s.ai.could_trust ? `<p class="small good-text">${icon("circle-check")} It's been right ${s.ai.right} of ${s.ai.checked} times: it could run on its own when it's sure.</p>` : ""}
          ${canEdit && !s.ai.manual ? `<label class="field"><span>Who checks its work</span><select data-review>${[["always", "A person, every time"], ["when_unsure", "A person, when the AI isn't sure"], ["never", "Nobody: it runs on its own"]].map(([v, l]) => `<option value="${v}" ${v === s.ai.review ? "selected" : ""}>${l}</option>`).join("")}</select></label>` : ""}
          ${s.ai.manual ? `<p class="small warn-text">${icon("user")} Switched off: a person does this job instead, as a task.</p>` : ""}
          <div class="row wrap">${canEdit ? `<button type="button" class="btn sm" data-ai-on="${s.ai.manual ? 1 : 0}">${icon("power")}${s.ai.manual ? "Switch the AI back on" : "Switch the AI off: a person does it"}</button>` : ""}
            <a class="btn sm ghost" href="${esc(root.dataset.aiPage)}?open=${encodeURIComponent(item.key + "-" + s.at)}#ai-${encodeURIComponent(item.key + "-" + s.at)}">${icon("play")}Try it on your records</a></div></div>` : ""}
        ${s.ai_could && canEdit ? `<details class="ai-trust"><summary class="btn sm">${icon("sparkles")}Let the AI do this</summary><form class="stack mt-1" data-form="take-over">
          <p class="small muted">The AI fills in ${esc(s.ai_could.field)} instead, and whoever does it now checks every one to begin with.</p>
          <label class="field"><span>What the AI is asked</span><textarea name="instructions" rows="3" placeholder="What to put in ${esc(s.ai_could.field)}, and how"></textarea></label>
          <button class="btn primary sm">Let the AI do it</button></form></details>` : ""}
        ${formHtml ? `<form class="stack" data-form="step">${formHtml}<button class="btn primary sm">Save</button></form>` : ""}
        ${canEdit && s.type === "branch" ? `<p class="small muted">Click a way's name in the diagram to change when records go that way, or <b>Another way</b> to add one.</p>` : ""}
        ${canEdit && !formHtml && s.type !== "branch" ? `<p class="small muted">${icon("sparkles")} This kind of step is changed by asking the AI, in the box at the top.</p>` : ""}
        ${canEdit ? `<div class="row wrap"><button type="button" class="btn sm" data-move="-1">${icon("arrow-up")}Earlier</button><button type="button" class="btn sm" data-move="1">${icon("arrow-down")}Later</button>
          <button type="button" class="btn sm danger" data-remove>${icon("trash-2")}Remove</button></div>` : ""}</div>`);
      panel.appendChild(box);
      $("[data-back]", box).addEventListener("click", rulePanel);
      $("[data-review]", box)?.addEventListener("change", (ev) => change({do: "set_review", at, mode: ev.target.value}));
      $("[data-ai-on]", box)?.addEventListener("click", (ev) => change({do: "set_ai_on", at, on: ev.currentTarget.dataset.aiOn === "1"}));
      $("[data-form=take-over]", box)?.addEventListener("submit", (ev) => { ev.preventDefault(); change({do: "ai_take_over", at, instructions: ev.target.elements.instructions.value}); });
      const form = $("[data-form=step]", box);
      if (form) {
        wireStepForm(form, s.type);
        form.addEventListener("submit", (ev) => {
          ev.preventDefault();
          change({do: "update_step", at, step: readStep(s.type, form, s.step)});
        });
      }
      $$("[data-move]", box).forEach((b) => b.addEventListener("click", () => change({do: "move_step", at, by: Number(b.dataset.move)})));
      $("[data-remove]", box)?.addEventListener("click", () => { if (confirm("Remove this step?" + (s.ways ? " The steps under it go too." : ""))) change({do: "remove_step", at}); });
      $$(".wf-node.selected", main).forEach((n) => n.classList.remove("selected"));
      $(`.wf-step[data-at="${CSS.escape(at)}"]`, main)?.classList.add("selected");
    }

    function newStepPanel(into, index) {
      panel.innerHTML = "";
      const box = html(`<div class="stack"><button type="button" class="btn sm ghost" data-back>${icon("chevron-left")}About this rule</button>
        <h2 class="panel-title">${icon("plus")}Add a step</h2>
        <label class="field"><span>It should</span><select name="type" data-type>${data.steps.map((s) => `<option value="${s.type}">${esc(s.label)}</option>`).join("")}</select></label>
        <form class="stack" data-form="new-step"></form></div>`);
      panel.appendChild(box);
      $("[data-back]", box).addEventListener("click", rulePanel);
      const form = $("[data-form=new-step]", box), pick = $("[data-type]", box);
      const show = () => { form.innerHTML = stepForm(pick.value) + `<button class="btn primary sm">Add it</button>`; wireStepForm(form, pick.value); };
      pick.addEventListener("change", show); show();
      form.addEventListener("submit", (ev) => { ev.preventDefault(); change({do: "add_step", into, index, step: readStep(pick.value, form)}); });
    }

    function wayPanel(at, n) {
      const s = findStep(item.steps, at);
      const way = n === null ? null : s.step.paths[n];
      panel.innerHTML = "";
      const box = html(`<div class="stack"><button type="button" class="btn sm ghost" data-back>${icon("chevron-left")}About this rule</button>
        <h2 class="panel-title">${icon("split")}${way ? "A way" : "Another way"}</h2>
        <form class="stack" data-form="way"><label class="field"><span>Name</span><input type="text" name="label" value="${esc(way ? way.label : "")}" placeholder="Big orders"></label>
          <div class="label">Records go this way when</div>${conditionRows(item.table, way ? way.when : [{}])}
          <button class="btn primary sm">Save</button></form>
        ${way ? `<button type="button" class="btn sm danger" data-remove>${icon("trash-2")}Remove this way and its steps</button>` : ""}</div>`);
      panel.appendChild(box);
      $("[data-back]", box).addEventListener("click", rulePanel);
      const form = $("[data-form=way]", box);
      wireConds(form, item.table);
      form.addEventListener("submit", (ev) => { ev.preventDefault(); change({do: way ? "update_way" : "add_way", at, n, label: form.elements.label.value, when: readConds(form)}); });
      $("[data-remove]", box)?.addEventListener("click", () => change({do: "remove_way", at, n}));
    }

    function tryPanel() {
      panel.innerHTML = "";
      const t = tableOf(item.table);
      const box = html(`<div class="stack"><button type="button" class="btn sm ghost" data-back>${icon("chevron-left")}About this rule</button>
        <h2 class="panel-title">${icon("play")}Try it on a record</h2>
        <p class="small muted">See what it would do for one of your ${esc(t ? t.label.toLowerCase() : "records")}, step by step. Nothing is changed and nobody is told.</p>
        <input type="search" placeholder="Find one by name" data-find aria-label="Find a record"><ul class="studio-list" data-found></ul></div>`);
      panel.appendChild(box);
      $("[data-back]", box).addEventListener("click", rulePanel);
      const find = $("[data-find]", box), found = $("[data-found]", box);
      const load = async () => {
        const got = await get(`${root.dataset.records}?table=${encodeURIComponent(item.table)}&q=${encodeURIComponent(find.value)}`);
        found.innerHTML = (got && got.items.length) ? got.items.map((r) => `<li><button type="button" data-rec="${r.id}">${icon("play")}<span class="grow">${esc(r.title)}</span></button></li>`).join("")
                                                   : `<li class="faint small pad">None found</li>`;
        $$("[data-rec]", found).forEach((b) => b.addEventListener("click", async () => {
          const res = await post(root.dataset.try, {id: itemId(), record: b.dataset.rec});
          if (res.error) { statusLine(status, [res.error], "bad"); return; }
          tried = {...res, title: b.textContent.trim()};
          draw();
          tryPanel();
        }));
      };
      let wait;
      find.addEventListener("input", () => { clearTimeout(wait); wait = setTimeout(load, 250); });
      load();
    }

    // --- a list's stages ------------------------------------------------------------------------------------------
    function drawStages() {
      // Top to bottom, so any number of stages fits: the next stage straight below, moves further on curving round the
      // right, moves back round the left, and a move from many stages (Cancel) one arrow in from the right.
      const s = item, BX = 170, BW = 200, BH = 54, GAP = 50, TOP = 24;
      const n = s.stages.length;
      const yOf = (name) => TOP + s.stages.findIndex((x) => x.name === name) * (BH + GAP);
      const head = html(`<div class="wf-head"><div class="grow"><h2>${icon("arrow-right")}${esc(s.label)}</h2>
        <p class="small muted">Each ${esc(s.singular.toLowerCase())} moves through these, in ${esc(s.field_label)}. The arrows are the buttons people press to move one on; click one to change who can press it.</p></div></div>`);
      main.appendChild(head);
      const width = BX + BW + 260, height = TOP * 2 + n * BH + (n - 1) * GAP;
      const wrap = html(`<div class="wf-stage-wrap"><svg class="wf-stage-svg" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img" aria-label="${esc(s.label)}"></svg></div>`);
      main.appendChild(wrap);
      const g = $("svg", wrap);
      const c = {panel: colour("--panel"), border: colour("--border-strong"), text: colour("--text"), faint: colour("--faint"), accent: colour("--accent"),
                 goodWeak: colour("--good-weak"), badWeak: colour("--bad-weak"), muted: colour("--muted")};
      const defs = node("defs", {}, g);
      const marker = node("marker", {id: "wf-arrowhead", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse"}, defs);
      node("path", {d: "M0 0L10 5L0 10z", fill: c.muted}, marker);
      const pill = (m, x, y, anchor = "middle", extra = "") => {
        const label = node("g", {class: "wf-move-label", "data-move": m.i, tabindex: 0, role: "button", "aria-label": `${m.label}${extra}`}, g);
        const text = (m.label.length > 20 ? m.label.slice(0, 19) + "…" : m.label) + extra;
        const w = text.length * 6.3 + 16;
        const left = anchor === "start" ? x : anchor === "end" ? x - w : x - w / 2;
        node("rect", {x: left, y: y - 11, width: w, height: 20, rx: 10, fill: c.panel, stroke: c.border}, label);
        const t = node("text", {x: left + w / 2, y: y + 3, "text-anchor": "middle", "font-size": 11, fill: c.text}, label);
        t.textContent = text;
      };
      const arrow = (d, m, dashed = false) => {
        const p = node("path", {d, fill: "none", stroke: c.muted, "stroke-width": 1.4, "marker-end": "url(#wf-arrowhead)", "stroke-dasharray": dashed ? "4 3" : "", class: "wf-move", "data-move": m.i}, g);
        node("title", {}, p).textContent = m.label;
      };
      const lanes = {right: 0, left: 0};
      s.moves.forEach((m) => {
        const ti = s.stages.findIndex((x) => x.name === m.to);
        const ty = yOf(m.to) + BH / 2;
        if (!m.from.length || m.from.length > 2) {  // from any stage, or from many: one arrow in from the right
          const x0 = BX + BW + 120;
          arrow(`M${x0} ${ty - 16} C${x0 - 50} ${ty - 16} ${BX + BW + 40} ${ty} ${BX + BW + 2} ${ty}`, m, true);
          pill(m, x0 + 4, ty - 16, "start", m.from.length ? ` · from ${m.from.length} stages` : " · from any stage");
          return;
        }
        m.from.forEach((from) => {
          const fi = s.stages.findIndex((x) => x.name === from);
          const fy = yOf(from) + BH / 2;
          if (ti === fi + 1) {  // the next one down
            arrow(`M${BX + BW / 2} ${yOf(from) + BH} L${BX + BW / 2} ${yOf(m.to) - 2}`, m);
            pill(m, BX + BW / 2 + 10, yOf(from) + BH + GAP / 2, "start");
          } else if (ti > fi) {  // further on: round the right
            lanes.right += 1;
            const cx = BX + BW + 34 + (ti - fi) * 12 + lanes.right * 8;
            arrow(`M${BX + BW} ${fy} C${cx} ${fy} ${cx} ${ty} ${BX + BW + 2} ${ty}`, m);
            pill(m, BX + BW + (cx - BX - BW) * 0.75 + 6, (fy + ty) / 2, "start");
          } else {  // back: round the left
            lanes.left += 1;
            const cx = BX - 34 - (fi - ti) * 12 - lanes.left * 8;
            arrow(`M${BX} ${fy} C${cx} ${fy} ${cx} ${ty} ${BX - 2} ${ty}`, m);
            pill(m, BX - (BX - cx) * 0.75 - 6, (fy + ty) / 2, "end");
          }
        });
      });
      s.stages.forEach((st) => {
        const sg = node("g", {class: "wf-stage", "data-stage": st.name, tabindex: 0, role: "button", transform: `translate(${BX},${yOf(st.name)})`}, g);
        const fill = st.no ? c.badWeak : st.ends ? c.goodWeak : c.panel;
        node("rect", {width: BW, height: BH, rx: 12, fill, stroke: st.start ? c.accent : c.border, "stroke-width": st.start ? 2 : 1}, sg);
        const name = node("text", {x: 14, y: 23, "font-size": 13.5, "font-weight": 650, fill: c.text}, sg);
        name.textContent = st.name.length > 22 ? st.name.slice(0, 21) + "…" : st.name;
        const sub = node("text", {x: 14, y: 41, "font-size": 11, fill: c.faint}, sg);
        sub.textContent = (st.start ? "Starts here · " : "") + plural(st.count, s.singular.toLowerCase(), s.plural.toLowerCase());
        if (st.rules.length) {
          const z = node("text", {x: BW - 12, y: 23, "font-size": 11, fill: c.accent, "text-anchor": "end"}, sg);
          z.textContent = `⚡ ${st.rules.length}`;
          node("title", {}, z).textContent = "Rules that run when one gets here: " + st.rules.map((r) => r.name).join(", ");
        }
      });
      $$(".wf-move-label", g).forEach((el) => activate(el, () => movePanel(Number(el.dataset.move))));
      $$(".wf-stage", g).forEach((el) => activate(el, () => stagePanel(el.dataset.stage)));
      stagesPanel();
    }

    function stagesPanel() {
      const s = item;
      panel.innerHTML = "";
      const box = html(`<div class="stack"><h2 class="panel-title">${icon("arrow-right")}${esc(s.label)}</h2>
        <a class="btn sm ghost" href="${esc(root.dataset.dataStudio)}?open=${encodeURIComponent(s.table)}">${icon("network")}Its fields, in the Data model</a>
        <div><div class="label">Stages${canEdit ? ` <span class="faint tiny">· drag to put them in order</span>` : ""}</div>
          <ul class="studio-fields" data-stages>${s.stages.map((st) => `<li ${canEdit ? 'draggable="true"' : ""} data-key="${esc(st.name)}">${canEdit ? icon("grip-vertical", "grip") : ""}
            <button type="button" data-stage="${esc(st.name)}"><span class="grow"><b>${esc(st.name)}</b>${st.start ? ' <span class="tag">start</span>' : ""}<div class="faint tiny">${plural(st.count, s.singular.toLowerCase(), s.plural.toLowerCase())}${st.rules.length ? ` · ${plural(st.rules.length, "rule", "rules")}` : ""}</div></span>${icon("chevron-right")}</button></li>`).join("")}</ul></div>
        ${canEdit ? `<form class="row" data-form="add-stage"><input type="text" name="name" placeholder="A new stage" maxlength="60" aria-label="A new stage" required><button class="btn sm">${icon("plus")}Add</button></form>` : ""}
        <div><div class="label">Moves</div><ul class="studio-list">${s.moves.map((m) => `<li><button type="button" data-move="${m.i}">${icon("arrow-right")}<span class="grow"><b>${esc(m.label)}</b>
          <div class="faint tiny">${m.from.length ? esc(m.from.join(", ")) : "Any stage"} → ${esc(m.to)}${m.who.length ? " · " + esc(m.who.join(", ")) : ""}</div></span></button></li>`).join("")}</ul>
          ${canEdit ? `<button type="button" class="btn sm" data-new-move>${icon("plus")}Add a move</button>` : ""}</div>
        ${canEdit ? `<details class="danger-zone"><summary class="small">Take the stages off</summary><p class="small muted">${esc(s.field_label)} stays as an ordinary choice, and nobody is held to the moves any more.</p>
          <button type="button" class="btn sm danger" data-off>Take the stages off</button></details>` : ""}</div>`);
      panel.appendChild(box);
      $$("[data-stage]", box).forEach((b) => b.addEventListener("click", () => stagePanel(b.dataset.stage)));
      $$("[data-move]", box).forEach((b) => b.addEventListener("click", () => movePanel(Number(b.dataset.move))));
      $("[data-new-move]", box)?.addEventListener("click", () => movePanel(null));
      $("[data-form=add-stage]", box)?.addEventListener("submit", (ev) => { ev.preventDefault(); change({do: "add_stage", name: ev.target.elements.name.value}); });
      $("[data-off]", box)?.addEventListener("click", () => { if (confirm("Take the stages off this list?")) change({do: "remove_stages"}); });
      if (canEdit) sortable($("[data-stages]", box), (names) => change({do: "order_stages", names}));
    }

    function stagePanel(name) {
      const s = item, st = s.stages.find((x) => x.name === name);
      if (!st) return;
      panel.innerHTML = "";
      const box = html(`<div class="stack"><button type="button" class="btn sm ghost" data-back>${icon("chevron-left")}${esc(s.label)}</button>
        <h2 class="panel-title">${esc(st.name)}</h2><p class="small muted">${plural(st.count, s.singular.toLowerCase(), s.plural.toLowerCase())} here now.</p>
        ${st.rules.length ? `<div><div class="label">When one gets here</div><ul class="studio-list">${st.rules.map((r) => `<li><button type="button" data-rule="${esc(r.key)}">${icon("zap")}<span class="grow">${esc(r.name)}</span>${r.on ? "" : '<span class="tag">off</span>'}</button></li>`).join("")}</ul></div>` : ""}
        ${st.set_by.length ? `<p class="small">${icon("zap")} Rules move records here too: ${esc(st.set_by.join(", "))}</p>` : ""}
        ${canEdit ? `<form class="stack" data-form="rename-stage"><label class="field"><span>Name</span><input type="text" name="name" value="${esc(st.name)}" required maxlength="60"></label>
          <span class="help">Renaming it renames it on every ${esc(s.singular.toLowerCase())} at this stage, and in every rule.</span><button class="btn sm">Save</button></form>
          <button type="button" class="btn sm" data-rule-new>${icon("zap")}Do something when one gets here</button>
          ${st.start ? "" : `<button type="button" class="btn sm" data-start>${icon("flag")}New ones start here</button>`}
          <details class="danger-zone"><summary class="small">Remove this stage</summary><button type="button" class="btn sm danger" data-remove>${icon("trash-2")}Remove ${esc(st.name)}</button></details>` : ""}</div>`);
      panel.appendChild(box);
      $("[data-back]", box).addEventListener("click", stagesPanel);
      $$("[data-rule]", box).forEach((b) => b.addEventListener("click", () => open(`rule:${b.dataset.rule}`)));
      $("[data-form=rename-stage]", box)?.addEventListener("submit", (ev) => { ev.preventDefault(); change({do: "rename_stage", old: st.name, new: ev.target.elements.name.value}); });
      $("[data-start]", box)?.addEventListener("click", () => change({do: "set_start", name: st.name}));
      $("[data-remove]", box)?.addEventListener("click", () => change({do: "remove_stage", name: st.name}));
      $("[data-rule-new]", box)?.addEventListener("click", () => change({do: "add_rule", name: `When ${s.singular.toLowerCase()} is ${st.name.toLowerCase()}`,
        trigger: {type: "record_updated", table: s.table, field: s.field, to: st.name},
        step: {type: "notify", to: "admin", message: `{{record}} is now ${st.name}`}}, ""));
    }

    function movePanel(i) {
      const s = item, m = i === null ? {from: [], to: s.stages[s.stages.length - 1].name, label: "", who_keys: [], requires_keys: []} : s.moves.find((x) => x.i === i);
      panel.innerHTML = "";
      const box = html(`<div class="stack"><button type="button" class="btn sm ghost" data-back>${icon("chevron-left")}${esc(s.label)}</button>
        <h2 class="panel-title">${icon("arrow-right")}${i === null ? "A new move" : esc(m.label)}</h2>
        ${i !== null && m.only_if ? `<p class="small">${icon("filter")} Only while ${esc(m.only_if)}</p>` : ""}
        ${canEdit ? `<form class="stack" data-form="move">
          <label class="field"><span>The button says</span><input type="text" name="label" value="${esc(i === null ? "" : m.label)}" maxlength="60" placeholder="Approve, Ship it, Send to client"></label>
          <fieldset class="field"><legend class="label">From</legend><div class="chips">${s.stages.map((st) => `<label class="check"><input type="checkbox" name="from" value="${esc(st.name)}" ${m.from.includes(st.name) ? "checked" : ""}> ${esc(st.name)}</label>`).join("")}</div>
            <span class="help">None ticked: from any stage.</span></fieldset>
          <label class="field"><span>To</span><select name="to">${s.stages.map((st) => `<option ${st.name === m.to ? "selected" : ""}>${esc(st.name)}</option>`).join("")}</select></label>
          <fieldset class="field"><legend class="label">Who can press it</legend><div class="chips">${s.who.map((w) => `<label class="check"><input type="checkbox" name="who" value="${esc(w.value)}" ${m.who_keys.includes(w.value) ? "checked" : ""}> ${esc(w.label)}</label>`).join("")}</div>
            <span class="help">None ticked: anyone who can change ${esc(s.plural.toLowerCase())}.</span></fieldset>
          <fieldset class="field"><legend class="label">Must be filled in first</legend><div class="chips">${s.fields.map((f) => `<label class="check"><input type="checkbox" name="requires" value="${esc(f.key)}" ${m.requires_keys.includes(f.key) ? "checked" : ""}> ${esc(f.label)}</label>`).join("")}</div></fieldset>
          <button class="btn primary sm">${i === null ? "Add it" : "Save"}</button></form>
          ${i === null ? "" : `<button type="button" class="btn sm danger" data-remove>${icon("trash-2")}Remove this move</button>`}` : `<p class="small">${m.from.length ? esc(m.from.join(", ")) : "Any stage"} → ${esc(m.to)}</p>`}</div>`);
      panel.appendChild(box);
      $("[data-back]", box).addEventListener("click", stagesPanel);
      const form = $("[data-form=move]", box);
      form?.addEventListener("submit", (ev) => {
        ev.preventDefault();
        const ticked = (n) => $$(`input[name=${n}]:checked`, form).map((x) => x.value);
        change({do: i === null ? "add_move" : "update_move", i, label: form.elements.label.value, from: ticked("from"), to: form.elements.to.value, who: ticked("who"), requires: ticked("requires")});
      });
      $("[data-remove]", box)?.addEventListener("click", () => change({do: "remove_move", i}));
    }

    // --- a step-by-step flow --------------------------------------------------------------------------------------
    function drawFlow() {
      const f = item;
      main.appendChild(html(`<div class="wf-head"><div class="grow"><h2>${icon("list-todo")}${esc(f.label)}</h2><p class="small muted">${esc(f.starts)}. One screen at a time; nothing is saved until the last one.</p></div></div>`));
      main.appendChild(html(`<div class="wf-chart">${f.steps.map((s, i) => `${i ? '<div class="wf-arrow"></div>' : ""}<div class="wf-node wf-screen" data-step="${esc(s.key)}" tabindex="0" role="button">
        <div class="wf-kind">${icon(FLOW_ICONS[s.kind] || "info")}Screen ${i + 1}</div><div class="wf-text"><b>${esc(s.label)}</b>${s.text ? `<div class="small muted">${esc(s.text.slice(0, 160))}</div>` : ""}</div>
        ${s.detail ? `<div class="wf-cond">${esc(s.detail)}</div>` : ""}</div>`).join("")}
        ${f.then.length ? `<div class="wf-arrow"></div><div class="wf-node wf-when"><div class="wf-kind">${icon("circle-check")}When it's done</div><div class="wf-text">${f.then.map((t) => esc(t)).join("<br>")}</div></div>` : ""}</div>`));
      $$(".wf-screen", main).forEach((n) => activate(n, () => screenPanel(n.dataset.step)));
      flowPanel();
    }
    function flowPanel() {
      const f = item;
      panel.innerHTML = "";
      const box = html(`<div class="stack"><h2 class="panel-title">${icon("list-todo")}${esc(f.label)}</h2>
        ${canEdit ? `<form class="stack" data-form="rename"><label class="field"><span>Name</span><input type="text" name="label" value="${esc(f.label)}" required maxlength="80"></label><button class="btn sm">Save</button></form>
          <details><summary class="btn sm">${icon("plus")}Add a screen</summary><form class="stack card card-body" data-form="add-screen">
            <label class="field"><span>It's for</span><select name="kind"><option value="info">Reading instructions</option><option value="form">Filling in fields</option><option value="photo">Taking a photo</option><option value="signature">Signing</option></select></label>
            <label class="field"><span>Title</span><input type="text" name="label" maxlength="80"></label>
            <label class="field"><span>Instructions</span><textarea name="text" rows="3"></textarea></label>
            <label class="field" data-kind="form photo signature"><span>Field${""}</span><select name="field" multiple size="5">${f.fields.map((x) => `<option value="${esc(x.key)}">${esc(x.label)}</option>`).join("")}</select></label>
            <button class="btn primary sm">Add it at the end</button></form></details>
          <details class="danger-zone"><summary class="small">Delete this flow</summary><button type="button" class="btn sm danger" data-delete>${icon("trash-2")}Delete ${esc(f.label)}</button></details>` : ""}</div>`);
      panel.appendChild(box);
      $("[data-form=rename]", box)?.addEventListener("submit", (ev) => { ev.preventDefault(); change({do: "rename", label: ev.target.elements.label.value}); });
      const add = $("[data-form=add-screen]", box);
      if (add) {
        const show = () => $$("[data-kind]", add).forEach((el) => { el.hidden = !el.dataset.kind.split(" ").includes(add.elements.kind.value); });
        add.elements.kind.addEventListener("change", show); show();
        add.addEventListener("submit", (ev) => {
          ev.preventDefault();
          const picked = Array.from(add.elements.field.selectedOptions).map((o) => o.value);
          change({do: "add_step", kind: add.elements.kind.value, label: add.elements.label.value, text: add.elements.text.value, fields: picked, field: picked[0]});
        });
      }
      $("[data-delete]", box)?.addEventListener("click", () => { if (confirm(`Delete the flow “${f.label}”?`)) change({do: "remove_flow"}); });
    }
    function screenPanel(key) {
      const f = item, s = f.steps.find((x) => x.key === key);
      if (!s) return;
      panel.innerHTML = "";
      const box = html(`<div class="stack"><button type="button" class="btn sm ghost" data-back>${icon("chevron-left")}${esc(f.label)}</button>
        <h2 class="panel-title">${icon(FLOW_ICONS[s.kind] || "info")}${esc(s.label)}</h2>${s.detail ? `<p class="small muted">${esc(s.detail)}</p>` : ""}
        ${canEdit ? `<form class="stack" data-form="screen"><label class="field"><span>Title</span><input type="text" name="label" value="${esc(s.label)}" required maxlength="80"></label>
          <label class="field"><span>Instructions</span><textarea name="text" rows="4">${esc(s.text)}</textarea></label>
          ${s.kind === "form" ? `<label class="field"><span>Fields to fill in</span><select name="fields" multiple size="6">${f.fields.map((x) => `<option value="${esc(x.key)}" ${s.fields.includes(x.key) ? "selected" : ""}>${esc(x.label)}</option>`).join("")}</select></label>` : ""}
          <button class="btn primary sm">Save</button></form>
          <div class="row wrap"><button type="button" class="btn sm" data-move="-1">${icon("arrow-up")}Earlier</button><button type="button" class="btn sm" data-move="1">${icon("arrow-down")}Later</button>
          <button type="button" class="btn sm danger" data-remove>${icon("trash-2")}Remove</button></div>` : (s.text ? `<p class="small">${esc(s.text)}</p>` : "")}</div>`);
      panel.appendChild(box);
      $("[data-back]", box).addEventListener("click", flowPanel);
      const form = $("[data-form=screen]", box);
      form?.addEventListener("submit", (ev) => {
        ev.preventDefault();
        const body = {do: "update_step", step: key, label: form.elements.label.value, text: form.elements.text.value};
        if (form.elements.fields) body.fields = Array.from(form.elements.fields.selectedOptions).map((o) => o.value);
        change(body);
      });
      $$("[data-move]", box).forEach((b) => b.addEventListener("click", () => change({do: "move_step", step: key, by: Number(b.dataset.move)})));
      $("[data-remove]", box)?.addEventListener("click", () => { if (confirm("Remove this screen?")) change({do: "remove_step", step: key}); });
    }

    // --- starting something new -----------------------------------------------------------------------------------
    $("[data-wf-new-rule]")?.addEventListener("click", () => {
      item = null; draw();
      panel.innerHTML = "";
      const box = html(`<div class="stack"><h2 class="panel-title">${icon("zap")}A new rule</h2>
        <form class="stack" data-form="new-rule"><label class="field"><span>Name</span><input type="text" name="name" required maxlength="80" placeholder="Tell the manager about big orders"></label>
          <label class="field"><span>When a record in</span><select name="table">${data.tables.map((t) => `<option value="${esc(t.key)}">${esc(t.label)}</option>`).join("")}</select></label>
          <label class="field"><span>…</span><select name="type"><option value="record_created">is added</option><option value="record_updated">is changed</option><option value="record_created_or_updated">is added or changed</option><option value="button">has its button pressed</option></select></label>
          <p class="help">It starts by telling the admins; change that, and add steps, once it's made.</p>
          <button class="btn primary sm">Make it</button></form></div>`);
      panel.appendChild(box);
      $("[data-form=new-rule]", box).addEventListener("submit", (ev) => {
        ev.preventDefault();
        const fm = ev.target.elements, t = tableOf(fm.table.value);
        change({do: "add_rule", name: fm.name.value, trigger: {type: fm.type.value, table: fm.table.value, label: fm.type.value === "button" ? fm.name.value.slice(0, 40) : undefined},
                step: {type: "notify", to: "admin", message: `${fm.name.value}: {{record}}`}}, "");
        void t;
      });
    });
    $("[data-wf-new-stages]")?.addEventListener("click", () => {
      item = null; draw();
      panel.innerHTML = "";
      const without = data.tables.filter((t) => !t.stages);
      const box = html(`<div class="stack"><h2 class="panel-title">${icon("arrow-right")}Stages for a list</h2>
        <p class="small muted">Records then move from one stage to the next by buttons, and rules can act on each move.</p>
        ${without.length ? `<form class="stack" data-form="new-stages"><label class="field"><span>The list</span><select name="table">${without.map((t) => `<option value="${esc(t.key)}">${esc(t.label)}</option>`).join("")}</select></label>
          <label class="field"><span>Use the choices of</span><select name="field"><option value="">A new Status field</option></select></label>
          <label class="field" data-new><span>The stages, one on each line</span><textarea name="stages" rows="4">New\nIn progress\nDone</textarea></label>
          <button class="btn primary sm">Add the stages</button></form>` : `<p class="small">Every list has stages already.</p>`}</div>`);
      panel.appendChild(box);
      const form = $("[data-form=new-stages]", box);
      if (!form) return;
      const fill = () => {
        const t = tableOf(form.elements.table.value);
        form.elements.field.innerHTML = `<option value="">A new Status field</option>` + t.fields.filter((f) => f.type === "select").map((f) => `<option value="${esc(f.key)}">${esc(f.label)} (${esc(f.options.join(", "))})</option>`).join("");
        $("[data-new]", form).hidden = !!form.elements.field.value;
      };
      form.elements.table.addEventListener("change", fill);
      form.elements.field.addEventListener("change", () => { $("[data-new]", form).hidden = !!form.elements.field.value; });
      fill();
      form.addEventListener("submit", (ev) => {
        ev.preventDefault();
        change({do: "add_stages", table: form.elements.table.value, field: form.elements.field.value, stages: form.elements.stages.value}, `stages:${form.elements.table.value}`);
      });
    });

    draw();
    askBox(() => location.reload());
  }

  document.addEventListener("DOMContentLoaded", () => {
    const root = $("[data-studio]");
    if (!root) return;
    if (root.dataset.studio === "data") dataStudio(root);
    else workflowStudio(root);
  });
})();
