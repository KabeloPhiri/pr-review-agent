/* PR Workspace client. No framework, no build step.
 *
 * Every DOM node is built with createElement/textContent — never innerHTML —
 * because comment bodies are written by anyone who can comment on the pull
 * request. The SCM token lives in sessionStorage for this tab only and goes
 * out as X-SCM-Token; the server never stores it.
 *
 * Motion follows one rule: animate from the current on-screen value, carry
 * the gesture's velocity into the animation, and let anything be grabbed
 * again mid-flight. A small spring does all of it.
 */
(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
  const narrow = matchMedia("(max-width: 600px)");
  const phoneSized = matchMedia("(max-width: 700px)");
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  let uidCounter = 0;
  const uid = (prefix) => `${prefix}-${++uidCounter}`;

  function el(tag, props, ...kids) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "dataset") Object.assign(node.dataset, value);
      else if (key.startsWith("on") && typeof value === "function") {
        node.addEventListener(key.slice(2), value);
      } else node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid === null || kid === undefined || kid === false) continue;
      node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return node;
  }

  /* append()/replaceChildren() print a null as the text "null"; el() and
     this drop empty entries instead. */
  const nodes = (...kids) =>
    kids.flat(Infinity).filter((kid) => kid !== null && kid !== undefined && kid !== false);

  // -- storage: per tab for the token, per browser for conveniences ---------------

  function safeStorage(kind) {
    const memory = new Map();
    const backing = () => {
      try { return window[kind]; } catch { return null; }
    };
    return {
      get(key) {
        try { const s = backing(); return s ? s.getItem(key) : memory.get(key) ?? null; }
        catch { return memory.get(key) ?? null; }
      },
      set(key, value) {
        memory.set(key, value);
        try { backing()?.setItem(key, value); } catch { /* private mode: memory only */ }
      },
      remove(key) {
        memory.delete(key);
        try { backing()?.removeItem(key); } catch { /* ignore */ }
      },
    };
  }
  const session = safeStorage("sessionStorage");
  const local = safeStorage("localStorage");
  const Token = {
    key: (scm) => `prreview.token.${scm}`,
    get: (scm) => session.get(Token.key(scm)),
    set: (scm, value) => session.set(Token.key(scm), value),
    clear: (scm) => session.remove(Token.key(scm)),
  };

  // -- API -------------------------------------------------------------------------

  class ApiError extends Error {
    constructor(status, message, data) {
      super(message);
      this.status = status;
      this.data = data || {};
    }
  }

  async function api(method, path, { scm, body } = {}) {
    const headers = { Accept: "application/json" };
    const token = scm ? Token.get(scm) : null;
    if (token) headers["X-SCM-Token"] = token;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    let response;
    try {
      response = await fetch(path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "same-origin",
      });
    } catch {
      throw new ApiError(0, "The workspace could not be reached. Check your connection.");
    }
    if (response.status === 204) return {};
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new ApiError(response.status, data.error || `Request failed (${response.status})`, data);
    }
    return data;
  }

  const REF_KEYS = ["scm", "organization", "project", "repository", "pull_request_id",
    "base_url", "fixture_dir"];

  function refFromLocation() {
    const params = new URLSearchParams(location.search);
    const ref = {};
    for (const key of REF_KEYS) if (params.get(key)) ref[key] = params.get(key);
    ref.scm = ref.scm || "github";
    return ref;
  }
  const refQuery = (ref) =>
    new URLSearchParams(Object.entries(ref).filter(([, v]) => v)).toString();

  async function pollJob(jobId, scm, onStatus) {
    const deadline = Date.now() + 15 * 60 * 1000;
    while (Date.now() < deadline) {
      await wait(1500);
      const job = await api("GET", `/workspace/api/jobs/${encodeURIComponent(jobId)}`, { scm });
      onStatus?.(job.status);
      if (job.status === "completed" || job.status === "failed") return job;
    }
    throw new ApiError(0, "This is taking unusually long. Reload the pull request to see where it got to.");
  }

  // -- motion: one interruptible, velocity-aware spring --------------------------

  /* Response (seconds) and damping ratio, the two parameters Apple designs
     with, mapped onto stiffness and damping for a unit mass. */
  class Spring {
    constructor({ value = 0, response = 0.35, damping = 1, onUpdate } = {}) {
      Object.assign(this, { value, target: value, velocity: 0, response, damping, onUpdate });
      this.frame = 0;
      this.resolve = null;
    }
    to(target, { velocity, response, damping } = {}) {
      this.settle(false); // a retarget interrupts; the old promise says so
      this.target = target;
      if (velocity !== undefined) this.velocity = velocity;
      if (response) this.response = response;
      if (damping !== undefined) this.damping = damping;
      const done = new Promise((resolve) => { this.resolve = resolve; });
      if (reducedMotion.matches) {
        this.jump(target);
      } else if (!this.frame) {
        this.last = performance.now();
        this.frame = requestAnimationFrame((t) => this.tick(t));
      }
      return done;
    }
    jump(value) {
      this.stop();
      this.value = this.target = value;
      this.velocity = 0;
      this.onUpdate?.(value);
      this.settle(true);
    }
    stop() {
      if (this.frame) cancelAnimationFrame(this.frame);
      this.frame = 0;
    }
    settle(done) {
      const resolve = this.resolve;
      this.resolve = null;
      resolve?.(done);
    }
    tick(now) {
      let dt = Math.min((now - this.last) / 1000, 0.064);
      this.last = now;
      const stiffness = (2 * Math.PI / this.response) ** 2;
      const friction = (4 * Math.PI * this.damping) / this.response;
      while (dt > 0) {
        const h = Math.min(dt, 1 / 240);
        const accel = -stiffness * (this.value - this.target) - friction * this.velocity;
        this.velocity += accel * h;
        this.value += this.velocity * h;
        dt -= h;
      }
      if (Math.abs(this.velocity) < 4 && Math.abs(this.value - this.target) < 0.4) {
        this.frame = 0;
        this.value = this.target;
        this.velocity = 0;
        this.onUpdate?.(this.value);
        this.settle(true);
        return;
      }
      this.onUpdate?.(this.value);
      this.frame = requestAnimationFrame((t) => this.tick(t));
    }
  }

  /* Where a flick would come to rest: Apple's exponential-decay projection. */
  const project = (velocity, rate = 0.998) => ((velocity / 1000) * rate) / (1 - rate);
  /* Progressive resistance past a boundary instead of a hard stop. */
  const rubberband = (overshoot, dimension, c = 0.55) =>
    (overshoot * dimension * c) / (dimension + c * Math.abs(overshoot));

  class VelocityTracker {
    constructor() { this.samples = []; }
    reset() { this.samples = []; }
    add(value) {
      const now = performance.now();
      this.samples.push([now, value]);
      while (this.samples.length > 2 && now - this.samples[0][0] > 100) this.samples.shift();
    }
    velocity() {
      if (this.samples.length < 2) return 0;
      const [t0, v0] = this.samples[0];
      const [t1, v1] = this.samples[this.samples.length - 1];
      const dt = (t1 - t0) / 1000;
      return dt > 0 ? (v1 - v0) / dt : 0;
    }
  }

  // -- sheets ------------------------------------------------------------------------

  const FOCUSABLE = "button:not([disabled]), [href], input:not([disabled]), select, textarea, [tabindex]:not([tabindex='-1'])";

  /* A bottom sheet (modal, dimmed) or a side panel (parallel task, no scrim).
     Enters and leaves along the same edge; can be dragged down and flicked
     away, and grabbed again while it is still moving. */
  class Sheet {
    constructor({ title, side = false, onClose } = {}) {
      this.side = side;
      this.onClose = onClose;
      this.titleEl = el("h2", { id: uid("sheet-title"), text: title });
      this.body = el("div", { class: "sheet-body" });
      this.head = el("div", { class: "sheet-head" }, this.titleEl,
        el("button", { class: "btn small", type: "button", text: "Close", onclick: () => this.close() }));
      this.grabber = el("div", { class: "grabber", "aria-hidden": "true" });
      this.node = el("section", {
        class: side ? "sheet side" : "sheet",
        role: "dialog",
        "aria-modal": side ? "false" : "true",
        "aria-labelledby": this.titleEl.id,
        tabindex: "-1",
      }, this.grabber, this.head, this.body);
      this.scrim = side ? null : el("div", { class: "scrim", onclick: () => this.close() });
      this.isOpen = false;
      this.spring = new Spring({ response: 0.32, damping: 1, onUpdate: (v) => this.paint(v) });
      this.onKey = (event) => this.key(event);
      this.enableDrag(this.grabber);
      this.enableDrag(this.head);
    }

    setTitle(text) { this.titleEl.textContent = text; }
    get horizontal() { return this.side && !narrow.matches; }
    extent() { return (this.horizontal ? this.node.offsetWidth : this.node.offsetHeight) + 24; }

    paint(offset) {
      this.node.style.transform = this.horizontal
        ? `translate3d(${offset}px, 0, 0)` : `translate3d(0, ${offset}px, 0)`;
      if (this.scrim) {
        this.scrim.style.opacity = String(Math.min(1, Math.max(0, 1 - offset / this.extent())));
      }
    }

    open(trigger) {
      if (this.isOpen) return;
      this.isOpen = true;
      this.trigger = trigger || document.activeElement;
      const fresh = !this.node.isConnected;
      const host = $("#sheets");
      if (this.scrim) host.append(this.scrim);
      host.append(this.node);
      if (reducedMotion.matches) {
        this.paint(0);
        this.fade(1);
      } else {
        this.node.style.opacity = "";
        if (fresh) this.spring.jump(this.extent()); // start just off-screen
        this.spring.to(0); // from wherever it is now — mid-close included
      }
      document.addEventListener("keydown", this.onKey);
      if (!this.side) document.documentElement.style.overflow = "hidden";
      requestAnimationFrame(() => {
        const first = this.body.querySelector(FOCUSABLE);
        (first || this.node).focus({ preventScroll: true });
      });
    }

    async close({ velocity = 0 } = {}) {
      if (!this.isOpen) return;
      this.isOpen = false;
      document.removeEventListener("keydown", this.onKey);
      document.documentElement.style.overflow = "";
      let finished = true;
      if (reducedMotion.matches) {
        this.fade(0);
        await wait(200);
      } else {
        finished = await this.spring.to(this.extent(), { velocity });
      }
      if (!finished || this.isOpen) return; // grabbed or reopened mid-flight
      this.node.remove();
      this.scrim?.remove();
      if (this.trigger?.isConnected) this.trigger.focus({ preventScroll: true });
      this.onClose?.();
    }

    fade(to) {
      for (const node of [this.node, this.scrim]) {
        if (!node) continue;
        node.style.transition = "opacity 200ms ease";
        node.style.opacity = String(to === 1 ? 0 : 1);
        requestAnimationFrame(() => { node.style.opacity = String(to); });
      }
    }

    key(event) {
      // Only the topmost sheet answers: a confirm over the alignment panel
      // closes alone.
      if ($("#sheets").lastElementChild !== this.node) return;
      if (event.key === "Escape") {
        event.preventDefault();
        this.close();
      } else if (event.key === "Tab" && !this.side) {
        const items = [...this.node.querySelectorAll(FOCUSABLE)].filter((n) => n.offsetParent);
        if (!items.length) return;
        const first = items[0];
        const last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    }

    enableDrag(handle) {
      const tracker = new VelocityTracker();
      let start = null;
      handle.addEventListener("pointerdown", (event) => {
        if (event.button !== 0 || event.target.closest("button, a, input, textarea, select")) return;
        if (reducedMotion.matches) return;
        handle.setPointerCapture(event.pointerId);
        this.spring.stop(); // catch it where it is
        start = { pos: this.horizontal ? event.clientX : event.clientY, offset: this.spring.value };
        tracker.reset();
        tracker.add(start.offset);
      });
      handle.addEventListener("pointermove", (event) => {
        if (!start) return;
        const pos = this.horizontal ? event.clientX : event.clientY;
        let offset = start.offset + (pos - start.pos); // keeps the grab offset: 1:1 tracking
        if (offset < 0) offset = -rubberband(-offset, this.extent());
        this.spring.value = offset;
        this.paint(offset);
        tracker.add(offset);
      });
      const release = () => {
        if (!start) return;
        start = null;
        const velocity = tracker.velocity();
        const offset = this.spring.value;
        const projected = offset + project(velocity);
        // A real flick decides by direction; a slow drag by where it would land.
        const dismiss = Math.abs(velocity) > 300 ? velocity > 0 : projected > this.extent() * 0.5;
        if (dismiss) this.close({ velocity });
        else this.spring.to(0, { velocity });
      };
      handle.addEventListener("pointerup", release);
      handle.addEventListener("pointercancel", release);
    }
  }

  function confirmSheet({ title, message, confirmLabel, danger = false, extra = null, trigger }) {
    return new Promise((resolve) => {
      let answer = false;
      const sheet = new Sheet({ title, onClose: () => resolve(answer) });
      sheet.body.append(...nodes(
        el("p", { text: message }),
        extra,
        el("div", { class: "actions" },
          el("button", { class: "btn", type: "button", text: "Cancel", onclick: () => sheet.close() }),
          el("button", {
            class: danger ? "btn danger" : "btn primary",
            type: "button",
            text: confirmLabel,
            onclick: () => { answer = true; sheet.close(); },
          })),
      ));
      sheet.open(trigger);
    });
  }

  let toastTimer = 0;
  function toast(message) {
    const node = $("#toast");
    if (!node) return;
    node.textContent = message;
    node.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.classList.remove("show"), 3200);
  }

  // -- markdown-lite: the subset the bot and most people write --------------------

  const INLINE = /(`[^`\n]+`|\*\*[^*\n]+\*\*|(?<![\w])_[^_\n]+_(?![\w]))/g;

  function inline(text) {
    const nodes = [];
    let last = 0;
    for (const match of text.matchAll(INLINE)) {
      if (match.index > last) nodes.push(text.slice(last, match.index));
      const token = match[0];
      if (token.startsWith("`")) nodes.push(el("code", { text: token.slice(1, -1) }));
      else if (token.startsWith("**")) nodes.push(el("strong", { text: token.slice(2, -2) }));
      else nodes.push(el("em", { text: token.slice(1, -1) }));
      last = match.index + token.length;
    }
    if (last < text.length) nodes.push(text.slice(last));
    return nodes;
  }

  function markdown(source) {
    const root = el("div", { class: "md" });
    const text = (source || "")
      .replace(/<!--[\s\S]*?-->/g, "")
      .replace(/<\/?(details|summary|br|p|div|span|sub|sup)[^>]*>/gi, "")
      .replace(/\r\n/g, "\n")
      // Not trim(): leading indentation is what makes a suggestion a code block.
      .replace(/^(?:[ \t]*\n)+/, "")
      .trimEnd();
    const lines = text.split("\n");
    let i = 0;
    let paragraph = [];
    const flush = () => {
      if (paragraph.length) root.append(el("p", null, inline(paragraph.join(" "))));
      paragraph = [];
    };
    while (i < lines.length) {
      const line = lines[i];
      if (/^\s*```/.test(line)) {
        flush();
        const code = [];
        i += 1;
        while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
        i += 1;
        root.append(el("pre", null, el("code", { text: code.join("\n") })));
      } else if (/^( {4}|\t)/.test(line) && !paragraph.length) {
        const code = [];
        while (i < lines.length && (/^( {4}|\t)/.test(lines[i]) || (lines[i] === "" && /^( {4}|\t)/.test(lines[i + 1] || "")))) {
          code.push(lines[i++].replace(/^( {4}|\t)/, ""));
        }
        root.append(el("pre", null, el("code", { text: code.join("\n") })));
      } else if (/^\s*\|/.test(line)) {
        flush();
        const rows = [];
        while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(lines[i++]);
        const cells = (row) => row.trim().replace(/^\||\|$/g, "").split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
        const body = rows.filter((row) => !/^\s*\|?\s*:?-{2,}/.test(row));
        const [head, ...rest] = body;
        root.append(el("table", null,
          head ? el("thead", null, el("tr", null, cells(head).map((c) => el("th", null, inline(c))))) : null,
          el("tbody", null, rest.map((row) => el("tr", null, cells(row).map((c) => el("td", null, inline(c))))))));
      } else if (/^#{1,6}\s/.test(line)) {
        flush();
        root.append(el("h4", null, inline(line.replace(/^#+\s*/, ""))));
        i += 1;
      } else if (/^\s*[-*]\s+/.test(line)) {
        flush();
        const items = [];
        while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*[-*]\s+/, ""));
        root.append(el("ul", null, items.map((item) => el("li", null, inline(item)))));
      } else if (/^\s*>/.test(line)) {
        flush();
        const quote = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) quote.push(lines[i++].replace(/^\s*>\s?/, ""));
        root.append(el("blockquote", null, inline(quote.join(" "))));
      } else if (!line.trim()) {
        flush();
        i += 1;
      } else {
        paragraph.push(line.trim());
        i += 1;
      }
    }
    flush();
    return root;
  }

  // -- plain-language outcomes -----------------------------------------------------

  const REASONS = {
    apply_fixes_disabled: "This repository has not turned on applying fixes. A maintainer enables allow_apply_fixes on the target branch.",
    unauthorized_requester: "Only the pull request's author can apply suggestions.",
    fork_pull_request_unsupported: "Fixes can't be pushed to a pull request opened from a fork.",
    requester_unverified: "Your token could not be matched to an account. Connect it again.",
    apply_unsupported_for_scm: "Applying fixes is available for GitHub pull requests only.",
    comment_not_found_or_unsupported: "That suggestion could not be found. It may have been deleted.",
    not_a_bot_suggestion: "That comment isn't one of the reviewer's suggestions.",
    already_applied: "This suggestion has already been applied.",
    already_recorded: "This was already flagged as a false positive.",
    no_change_generated: "No change was needed. It may already be fixed on the branch.",
    file_changed_since_review: "The file kept changing while the fix was prepared. Try again once other pushes settle.",
    nothing_to_apply: "There are no open suggestions to apply.",
  };
  const reasonText = (code) =>
    REASONS[code] || (code && code.startsWith("failed: ") ? code.slice(8) : code || "Not applied.");

  const SEVERITY = { error: "Error", warning: "Warning", info: "Info" };
  const SEVERITY_ORDER = { error: 0, warning: 1, info: 2 };
  const VERDICTS = {
    aligned: "Aligned with the requirement",
    partial: "Partly aligned",
    not_aligned: "Not aligned",
    unclear: "Could not judge",
  };
  const CRITERION = { met: ["Met", "ok"], partial: ["Partly met", "warning"], missing: ["Missing", "error"] };

  // -- token -----------------------------------------------------------------------

  const SCM_NAMES = { github: "GitHub", fake: "the local fixture" };

  /* Resolves with the login once a working token is in this tab. */
  function connect(scm, { trigger, message } = {}) {
    return new Promise((resolve) => {
      const fake = scm === "fake";
      const input = el("input", {
        id: uid("token"),
        type: fake ? "text" : "password",
        autocomplete: "off",
        spellcheck: "false",
        autocapitalize: "off",
        placeholder: fake ? "dev" : "github_pat_...",
      });
      const error = el("p", { class: "field-error", role: "alert", text: message || "" });
      let login = null;
      const sheet = new Sheet({
        title: fake ? "Act as a fixture user" : "Connect to GitHub",
        onClose: () => resolve(login),
      });
      const submit = async (event) => {
        event.preventDefault();
        const value = input.value.trim();
        if (!value) {
          error.textContent = fake ? "Enter the login to act as." : "Paste a token to continue.";
          return;
        }
        Token.set(scm, value);
        error.textContent = "";
        try {
          const me = await api("GET", `/workspace/api/me?scm=${encodeURIComponent(scm)}`, { scm });
          login = me.login;
          if (!login) throw new ApiError(401, "That token does not belong to an account.");
          sheet.close();
        } catch (err) {
          Token.clear(scm);
          error.textContent = err.status === 401
            ? "That token was not accepted. Check it has not expired and try again."
            : err.message;
        }
      };
      sheet.body.append(el("form", { onsubmit: submit, novalidate: true },
        fake
          ? el("p", { text: "The fixture connector treats whatever you enter as your login. Use the pull request's author to try Apply." })
          : [
            el("p", { text: "Paste a GitHub personal access token. It is kept in this browser tab only and sent with each request; the server never stores it." }),
            el("ul", { class: "scopes" },
              el("li", { text: "Pull requests: read and write" }),
              el("li", { text: "Contents: read and write, to apply fixes" }),
              el("li", { text: "Commit statuses: read" })),
          ],
        el("div", { class: "field" }, el("label", { for: input.id, text: fake ? "Login" : "Token" }), input),
        error,
        el("div", { class: "actions" }, el("button", { class: "btn primary", type: "submit", text: "Connect" }))));
      sheet.open(trigger);
    });
  }

  async function signedIn(scm) {
    if (!Token.get(scm)) return connect(scm);
    try {
      const me = await api("GET", `/workspace/api/me?scm=${encodeURIComponent(scm)}`, { scm });
      if (me.login) return me.login;
    } catch (err) {
      if (err.status !== 401) throw err;
    }
    Token.clear(scm);
    return connect(scm, { message: "Your token has expired or was revoked. Connect again." });
  }

  function renderIdentity(scm, login) {
    const host = $("#identity");
    if (!host) return;
    host.replaceChildren();
    if (!login) {
      host.append(el("button", {
        class: "btn small",
        type: "button",
        text: `Connect ${SCM_NAMES[scm] || scm}`,
        onclick: async (event) => { if (await connect(scm, { trigger: event.currentTarget })) location.reload(); },
      }));
      return;
    }
    host.append(
      el("span", { class: "who", text: `@${login}` }),
      el("button", {
        class: "btn plain small",
        type: "button",
        text: "Forget token",
        onclick: () => { Token.clear(scm); location.reload(); },
      }),
    );
  }

  // -- the pull request view, shared by desktop and phone --------------------------

  class PrView {
    constructor(mode) {
      this.mode = mode; // "desktop" | "mobile"
      this.ref = refFromLocation();
      this.scm = this.ref.scm;
      this.data = null;
      this.alignment = null;
    }

    async start() {
      if (!this.ref.repository) {
        location.replace("/workspace");
        return;
      }
      $("#crumb-repo") && ($("#crumb-repo").textContent = this.ref.repository);
      $("#btn-align")?.addEventListener("click", (e) => this.openAlignment(e.currentTarget));
      $("#btn-apply-all")?.addEventListener("click", (e) => this.applyAll(e.currentTarget));
      $("#btn-phone")?.addEventListener("click", (e) => this.openPhone(e.currentTarget));
      let login = null;
      try {
        login = await signedIn(this.scm);
      } catch (err) {
        this.fail(err.message);
        return;
      }
      renderIdentity(this.scm, login);
      if (!login) {
        this.fail(`Connect ${SCM_NAMES[this.scm] || this.scm} to see this pull request.`);
        return;
      }
      await this.load();
    }

    fail(message) {
      $("#pr-title").textContent = "Pull request unavailable";
      $("#overview").replaceChildren(el("p", { class: "notice error", text: message }));
    }

    async load() {
      const scroll = window.scrollY;
      try {
        this.data = await api("GET", `/workspace/api/pr?${refQuery(this.ref)}`, { scm: this.scm });
      } catch (err) {
        if (err.status === 401) {
          Token.clear(this.scm);
          if (await connect(this.scm, { message: "Connect again to continue." })) return this.load();
        }
        this.fail(err.message);
        return;
      }
      this.render();
      window.scrollTo(0, scroll);
    }

    // -- derived data ---------------------------------------------------------------

    get views() { return this.data.comments; }
    get pr() { return this.data.pr; }

    isOpen(view) {
      return view.kind === "finding" && !view.applied && !view.flagged && !view.comment.is_closed;
    }

    applyBlock(view) {
      if (!view.finding?.suggestion) return "There is no suggested fix to apply.";
      if (!view.comment.comment_id) return "This suggestion has no comment id to apply.";
      if (this.data.apply_refusal) return this.refusalText();
      return null;
    }

    refusalText() {
      const code = this.data.apply_refusal;
      if (code === "unauthorized_requester") {
        return `Only the author, @${this.pr.author}, can apply suggestions. You are connected as @${this.data.viewer}.`;
      }
      return reasonText(code);
    }

    openSuggestions() {
      return this.views.filter((v) => this.isOpen(v) && !this.applyBlock(v));
    }

    // -- rendering ------------------------------------------------------------------

    render() {
      const pr = this.pr;
      document.title = `${pr.title || "Pull request"} - PR Workspace`;
      $("#pr-title").textContent = pr.title || "(untitled pull request)";
      $("#pr-meta").textContent = [
        `#${pr.ref.pull_request_id}`,
        pr.author && `by @${pr.author}`,
        pr.source_branch && `${shortBranch(pr.source_branch)} into ${shortBranch(pr.target_branch)}`,
      ].filter(Boolean).join("  ·  ");
      this.renderOverview();
      this.renderConversation();
      this.renderFiles();
      if (this.mode === "mobile") this.renderFindingList();
      const applyAll = $("#btn-apply-all");
      if (applyAll) {
        const count = this.data.apply_refusal ? 0 : this.openSuggestions().length;
        applyAll.replaceChildren("Apply all ", el("span", { class: "count", text: `(${count})` }));
        applyAll.disabled = count === 0;
        applyAll.title = this.data.apply_refusal ? this.refusalText()
          : count === 0 ? "No open suggestions" : `Push ${count} suggested fixes`;
      }
    }

    renderOverview() {
      const pr = this.pr;
      const counts = { error: 0, warning: 0, info: 0 };
      for (const view of this.views) if (this.isOpen(view)) counts[view.finding.severity] += 1;
      let adds = 0;
      let dels = 0;
      for (const file of this.data.diff.files) {
        for (const hunk of file.hunks) {
          for (const line of hunk.lines) {
            if (line.startsWith("+")) adds += 1;
            else if (line.startsWith("-")) dels += 1;
          }
        }
      }
      const fileCount = this.data.diff.files.length;
      const node = $("#overview");
      node.replaceChildren(...nodes(
        el("div", { class: "stats" },
          pr.is_draft ? el("span", { class: "badge", text: "Draft" }) : null,
          el("span", { class: "badge", text: `${fileCount} file${fileCount === 1 ? "" : "s"}` }),
          el("span", { class: "badge ok", text: `+${adds}` }),
          el("span", { class: "badge error", text: `-${dels}` }),
          ["error", "warning", "info"].map((s) => counts[s]
            ? el("span", { class: `badge ${s}`, text: `${counts[s]} ${SEVERITY[s].toLowerCase()}${counts[s] === 1 ? "" : "s"}` })
            : null),
          pr.url ? el("a", { class: "btn plain small", href: pr.url, target: "_blank", rel: "noopener noreferrer", text: "Open on GitHub" }) : null),
        pr.description ? el("div", { class: "description" }, markdown(pr.description)) : null,
        this.data.apply_refusal
          ? el("p", { class: this.data.apply_refusal === "apply_fixes_disabled" ? "notice" : "notice warn", text: this.refusalText() })
          : null,
      ));
    }

    renderConversation() {
      const host = $("#conversation-list");
      const general = this.views.filter((v) => !v.comment.file);
      const cards = general.map((v) => this.commentCard(v));
      if (this.mode === "mobile") {
        const inlineHuman = this.views.filter((v) => v.comment.file && v.kind === "human");
        if (inlineHuman.length) {
          cards.push(el("h3", { class: "section-title", text: "On the code" }));
          for (const v of inlineHuman) cards.push(this.commentCard(v, { where: true }));
        }
      }
      host.replaceChildren(...(cards.length ? cards : [el("p", { class: "muted", text: "No conversation yet." })]));
    }

    renderFindingList() {
      const host = $("#finding-list");
      const findings = this.views.filter((v) => v.kind === "finding");
      findings.sort((a, b) => (this.isOpen(b) - this.isOpen(a))
        || SEVERITY_ORDER[a.finding.severity] - SEVERITY_ORDER[b.finding.severity]);
      const open = findings.filter((v) => this.isOpen(v)).length;
      $("#count-findings").textContent = open ? String(open) : "";
      $("#swipe-hint").hidden = !findings.some((v) => this.isOpen(v));
      host.replaceChildren(...(findings.length
        ? findings.map((v) => (this.isOpen(v) ? this.swipeable(v) : this.findingCard(v)))
        : [el("p", { class: "muted", text: "The reviewer has no findings on this pull request." })]));
    }

    renderFiles() {
      const anchored = new Map();
      for (const view of this.views) {
        const { file, line } = view.comment;
        if (!file) continue;
        const key = `${file}\u0000${line ?? ""}`;
        if (!anchored.has(key)) anchored.set(key, []);
        anchored.get(key).push(view);
      }
      const list = $("#file-list");
      list?.replaceChildren();
      const cards = this.data.diff.files.map((file, index) => {
        const id = `file-${index}`;
        const comments = this.views.filter((v) => v.comment.file === file.path);
        if (list) {
          const open = comments.filter((v) => this.isOpen(v)).length;
          list.append(el("li", null, el("a", { href: `#${id}` },
            el("span", { text: file.path }),
            open ? el("span", { class: "badge warning", text: String(open) }) : null)));
        }
        return this.fileCard(file, id, anchored);
      });
      $("#file-diffs").replaceChildren(...(cards.length ? cards : [el("p", { class: "muted", text: "No changed files." })]));
    }

    fileCard(file, id, anchored) {
      let adds = 0;
      let dels = 0;
      const table = el("table", { class: "diff" });
      const used = new Set();
      for (const hunk of file.hunks) {
        table.append(el("tr", { class: "hunk" }, el("td", { colspan: 3, text: `@@ -${hunk.old_start},${hunk.old_lines} +${hunk.new_start},${hunk.new_lines} @@ ${hunk.header || ""}` })));
        let oldNo = hunk.old_start;
        let newNo = hunk.new_start;
        for (const raw of hunk.lines) {
          const marker = raw.slice(0, 1) || " ";
          const code = raw.slice(1);
          let row;
          if (marker === "+") {
            adds += 1;
            row = el("tr", { class: "add", dataset: { line: newNo } },
              el("td", { class: "ln" }), el("td", { class: "ln", text: newNo }), el("td", { class: "code", text: `+ ${code}` }));
            newNo += 1;
          } else if (marker === "-") {
            dels += 1;
            row = el("tr", { class: "del" },
              el("td", { class: "ln", text: oldNo }), el("td", { class: "ln" }), el("td", { class: "code", text: `- ${code}` }));
            oldNo += 1;
          } else if (marker === "\\") {
            continue;
          } else {
            row = el("tr", { dataset: { line: newNo } },
              el("td", { class: "ln", text: oldNo }), el("td", { class: "ln", text: newNo }), el("td", { class: "code", text: `  ${code}` }));
            oldNo += 1;
            newNo += 1;
          }
          table.append(row);
          const line = row.dataset.line;
          const key = `${file.path}\u0000${line}`;
          if (line && anchored.has(key) && !used.has(key)) {
            used.add(key);
            table.append(el("tr", { class: "thread" }, el("td", { colspan: 3 },
              el("div", { class: "stack" }, anchored.get(key).map((v) => this.commentCard(v))))));
          }
        }
      }
      const leftovers = this.views.filter((v) => v.comment.file === file.path
        && !used.has(`${file.path}\u0000${v.comment.line ?? ""}`));
      const extra = leftovers.length
        ? el("div", { class: "file-extra" },
          el("h4", { text: "Outdated, or on lines not shown" }),
          leftovers.map((v) => this.commentCard(v, { where: true })))
        : null;
      const head = [
        el("span", { class: "path", text: file.path }),
        file.change_type !== "edit" ? el("span", { class: "badge", text: file.change_type }) : null,
        el("span", { class: "delta" }, el("span", { class: "plus", text: `+${adds}` }), " ", el("span", { class: "minus", text: `-${dels}` })),
      ];
      const content = file.is_binary
        ? el("p", { class: "muted", style: null, text: "Binary or too large to show." })
        : el("div", { class: "diff-scroll" }, table);
      if (this.mode === "mobile") {
        return el("details", { class: "file-card mobile-file", id, dataset: { path: file.path } },
          el("summary", null, head), content, extra);
      }
      return el("section", { class: "file-card", id, dataset: { path: file.path } },
        el("div", { class: "file-head" }, head), content, extra);
    }

    commentCard(view, { where = false } = {}) {
      if (view.kind === "finding") return this.findingCard(view, { where });
      const c = view.comment;
      const bot = view.kind === "bot";
      return el("article", { class: bot ? "comment bot" : "comment" },
        el("header", null,
          el("span", { class: "author", text: bot ? "Reviewer" : c.author ? `@${c.author}` : "Someone" }),
          where && c.file ? el("span", { class: "muted", text: `${c.file}${c.line ? `:${c.line}` : ""}` }) : null,
          c.is_closed ? el("span", { class: "badge", text: "Outdated" }) : null),
        markdown(c.body));
    }

    findingCard(view, { where = true } = {}) {
      const f = view.finding;
      const c = view.comment;
      const open = this.isOpen(view);
      const state = view.applied ? ["Applied", "ok"] : view.flagged ? ["Dismissed", ""]
        : c.is_closed ? ["Outdated", ""] : null;
      const block = this.applyBlock(view);
      const card = el("article", {
        class: `finding sev-${f.severity}${open ? "" : " settled"}`,
        dataset: { commentId: c.comment_id || "" },
        "aria-label": `${SEVERITY[f.severity]}: ${f.rule_id}`,
      },
      el("header", null,
        el("span", { class: "sev", text: SEVERITY[f.severity] }),
        el("code", { class: "rule", text: f.rule_id }),
        state ? el("span", { class: `badge ${state[1]}`, text: state[0] }) : null,
        where ? el("span", { class: "where", text: `${f.file}${f.line ? `:${f.line}` : ""}` }) : null),
      el("div", { class: "message" }, markdown(f.message)),
      f.suggestion ? el("div", { class: "suggestion" }, el("h4", { text: "Suggestion" }), markdown(f.suggestion)) : null);
      if (open) {
        card.append(el("footer", null,
          el("button", {
            class: "btn primary small",
            type: "button",
            text: "Apply",
            disabled: Boolean(block),
            title: block || "Push this fix as a commit",
            onclick: (e) => this.applyOne(view, e.currentTarget),
          }),
          el("button", {
            class: "btn small",
            type: "button",
            text: "Dismiss",
            disabled: !c.comment_id || !this.data.viewer,
            title: "Flag as a false positive",
            onclick: (e) => this.dismiss(view, e.currentTarget),
          }),
          this.mode === "mobile" && f.line ? el("button", {
            class: "btn plain small",
            type: "button",
            text: "Show code",
            onclick: () => this.reveal(f.file, f.line),
          }) : null,
          block && f.suggestion && this.mode === "desktop" ? el("span", { class: "why", text: block }) : null));
      }
      return card;
    }

    /* Swipe right to apply, left to dismiss. Tracks the finger 1:1, commits
       on a ~10px horizontal intent, decides by flick direction or landing
       point, and hands the release velocity to a slightly bouncy spring —
       bounce is earned here because the gesture carried momentum. A commit
       still asks before pushing anything. */
    swipeable(view) {
      const card = this.findingCard(view, { where: true });
      const wrap = el("div", { class: "swipe" },
        el("div", { class: "under", "aria-hidden": "true" },
          el("span", { class: "apply", text: "Apply" }),
          el("span", { class: "dismiss", text: "Dismiss" })),
        card);
      if (reducedMotion.matches) return wrap;
      const under = wrap.firstChild;
      const canApply = !this.applyBlock(view);
      const canDismiss = Boolean(view.comment.comment_id && this.data.viewer);
      const spring = new Spring({
        response: 0.35,
        damping: 0.8,
        onUpdate: (x) => {
          card.style.transform = `translate3d(${x}px, 0, 0)`;
          wrap.classList.toggle("toward-apply", x > 0);
          wrap.classList.toggle("toward-dismiss", x < 0);
          under.style.opacity = String(Math.min(1, Math.abs(x) / 72));
        },
      });
      const tracker = new VelocityTracker();
      let start = null;
      card.addEventListener("pointerdown", (event) => {
        if (event.button !== 0 || event.target.closest("button, a, pre, code")) return;
        spring.stop();
        start = { x: event.clientX, y: event.clientY, base: spring.value, intent: null, id: event.pointerId };
        tracker.reset();
      });
      card.addEventListener("pointermove", (event) => {
        if (!start) return;
        const dx = event.clientX - start.x;
        const dy = event.clientY - start.y;
        if (!start.intent) {
          if (Math.hypot(dx, dy) < 10) return; // hysteresis before choosing a direction
          start.intent = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
          if (start.intent === "x") card.setPointerCapture(start.id);
        }
        if (start.intent !== "x") return;
        const width = wrap.offsetWidth;
        let x = start.base + dx;
        if ((x > 0 && !canApply) || (x < 0 && !canDismiss)) x = rubberband(x, width, 0.12);
        spring.value = x;
        spring.onUpdate(x);
        tracker.add(x);
      });
      const release = async () => {
        if (!start) return;
        const intent = start.intent;
        start = null;
        if (intent !== "x") return;
        const width = wrap.offsetWidth;
        const velocity = tracker.velocity();
        const x = spring.value;
        const projected = x + project(velocity);
        const commit = Math.abs(velocity) > 300
          ? Math.sign(velocity) === Math.sign(x) && Math.abs(projected) > width * 0.35
          : Math.abs(x) > width * 0.45;
        const toApply = x > 0;
        if (!commit || (toApply && !canApply) || (!toApply && !canDismiss)) {
          spring.to(0, { velocity });
          return;
        }
        spring.to(Math.sign(x) * width * 0.92, { velocity });
        const done = toApply
          ? await this.applyOne(view, card, { confirm: true })
          : await this.dismiss(view, card);
        if (!done) spring.to(0);
      };
      card.addEventListener("pointerup", release);
      card.addEventListener("pointercancel", () => { if (start) { start = null; spring.to(0); } });
      card.addEventListener("lostpointercapture", release);
      return wrap;
    }

    // -- actions --------------------------------------------------------------------

    body(extra = {}) {
      return { ...this.ref, ...extra };
    }

    commitLink(sha) {
      if (!sha) return null;
      const short = sha.slice(0, 7);
      return this.pr.url && this.scm === "github"
        ? el("a", { class: "sha", href: `${this.pr.url}/commits/${sha}`, target: "_blank", rel: "noopener noreferrer", text: short })
        : el("span", { class: "sha", text: short });
    }

    async applyOne(view, trigger, { confirm = false } = {}) {
      const f = view.finding;
      const where = `${f.file}${f.line ? `:${f.line}` : ""}`;
      if (confirm) {
        const ok = await confirmSheet({
          title: "Apply this suggestion?",
          message: `This pushes a commit fixing ${f.rule_id} in ${where} to ${shortBranch(this.pr.source_branch) || "the branch"}, as you.`,
          confirmLabel: "Apply",
          trigger,
        });
        if (!ok) return false;
      }
      return this.runJob({
        title: "Applying suggestion",
        detail: `${f.rule_id} in ${where}`,
        trigger,
        start: () => api("POST", "/workspace/api/apply", { scm: this.scm, body: this.body({ comment_id: view.comment.comment_id }) }),
        describe: (result) => result.applied
          ? { tone: "ok", headline: "Applied", lines: [el("p", null, "Pushed in commit ", this.commitLink(result.commit_sha), ".")] }
          : { tone: "warn", headline: "Not applied", lines: [el("p", { text: reasonText(result.reason) })] },
      });
    }

    async applyAll(trigger) {
      const items = this.openSuggestions();
      if (!items.length) return;
      const byFile = new Map();
      for (const v of items) byFile.set(v.finding.file, (byFile.get(v.finding.file) || 0) + 1);
      const ok = await confirmSheet({
        title: `Apply ${items.length} suggestion${items.length === 1 ? "" : "s"}?`,
        message: `This pushes one commit per file to ${shortBranch(this.pr.source_branch) || "the branch"}, as you. Each commit can be reverted like any other.`,
        confirmLabel: "Apply all",
        extra: el("ul", { class: "list" }, [...byFile].map(([file, n]) =>
          el("li", null, el("span", { class: "path", text: file }), el("span", { class: "muted", text: `${n} fix${n === 1 ? "" : "es"}` })))),
        trigger,
      });
      if (!ok) return false;
      return this.runJob({
        title: "Applying suggestions",
        detail: `${items.length} suggestion${items.length === 1 ? "" : "s"} in ${byFile.size} file${byFile.size === 1 ? "" : "s"}`,
        trigger,
        start: () => api("POST", "/workspace/api/apply/all", { scm: this.scm, body: this.body() }),
        describe: (result) => {
          const lines = [];
          const applied = result.applied_comment_ids || [];
          if (applied.length) {
            lines.push(el("p", null, `${applied.length} applied in `,
              (result.commit_shas || []).flatMap((sha, i) => [i ? ", " : "", this.commitLink(sha)]), "."));
          }
          if (result.reason) lines.push(el("p", { text: reasonText(result.reason) }));
          if (result.skipped?.length) {
            lines.push(el("h3", { text: `Not applied (${result.skipped.length})` }),
              el("ul", { class: "list" }, result.skipped.map((s) => el("li", null, el("span", { text: reasonText(s.reason) })))));
          }
          return { tone: result.applied ? "ok" : "warn", headline: result.applied ? "Applied" : "Nothing applied", lines };
        },
      });
    }

    /* One sheet that shows a background job from start to finish. */
    runJob({ title, detail, trigger, start, describe }) {
      return new Promise((resolve) => {
        let succeeded = false;
        const sheet = new Sheet({ title, onClose: () => { resolve(succeeded); if (succeeded) this.load(); } });
        const status = el("p", { class: "status-line", text: "Starting" });
        const bar = el("div", { class: "progress", role: "progressbar", "aria-label": "Working" });
        const out = el("div", { "aria-live": "polite" });
        const done = el("button", { class: "btn primary", type: "button", text: "Done", hidden: true, onclick: () => sheet.close() });
        sheet.body.append(el("p", { class: "muted", text: detail }), status, bar, out, el("div", { class: "actions" }, done));
        sheet.open(trigger);
        const finish = ({ tone, headline, lines }) => {
          bar.remove();
          status.textContent = headline;
          status.className = `status-line result-${tone}`;
          out.replaceChildren(...lines);
          done.hidden = false;
          done.focus({ preventScroll: true });
        };
        (async () => {
          try {
            let job = await start();
            if (job.job_id) {
              status.textContent = "Queued";
              job = await pollJob(job.job_id, this.scm, (s) => {
                status.textContent = s === "running" ? "Working on it" : "Queued";
              });
            }
            if (job.status === "failed") {
              finish({ tone: "error", headline: "Failed", lines: [el("p", { text: job.error || "Something went wrong." })] });
              return;
            }
            const outcome = describe(job.result || {});
            succeeded = outcome.tone === "ok";
            // The sheet shows the result; toast only if it was closed early.
            if (succeeded && !sheet.isOpen) toast(outcome.headline);
            finish(outcome);
            for (const w of job.result?.warnings || []) out.append(el("p", { class: "notice", text: w }));
          } catch (err) {
            finish({ tone: "error", headline: "Failed", lines: [el("p", { text: err.message })] });
          }
        })();
      });
    }

    async dismiss(view, trigger) {
      const reason = el("textarea", { id: uid("reason"), rows: 3, maxlength: 500, placeholder: "Optional: why is this wrong?" });
      const ok = await confirmSheet({
        title: "Dismiss as a false positive?",
        message: "This resolves the comment and records it against the rule, so the reviewer's quality figures improve.",
        confirmLabel: "Dismiss",
        danger: true,
        extra: el("div", { class: "field" }, el("label", { for: reason.id, text: "Reason" }), reason),
        trigger,
      });
      if (!ok) return false;
      try {
        const res = await api("POST", "/workspace/api/feedback", {
          scm: this.scm,
          body: this.body({ comment_id: view.comment.comment_id, reason: reason.value.trim() || null }),
        });
        const result = res.result || {};
        if (result.recorded) {
          toast("Dismissed");
          await this.load();
          return true;
        }
        toast(result.reason === "unauthorized_requester"
          ? "Only the author can dismiss here. Reviewers can reply /fp on the pull request."
          : reasonText(result.reason));
      } catch (err) {
        toast(err.message);
      }
      return false;
    }

    reveal(path, line) {
      const card = [...document.querySelectorAll(".file-card")].find((n) => n.dataset.path === path);
      if (!card) {
        toast(`${path} is not part of this diff.`);
        return;
      }
      if (this.mode === "mobile") {
        selectTab("files");
        card.open = true;
      }
      const row = line ? card.querySelector(`tr[data-line="${CSS.escape(String(line))}"]`) : null;
      const target = row || card;
      this.alignment?.sheet.side && narrow.matches && this.alignment.sheet.close();
      target.scrollIntoView({ block: "center", behavior: reducedMotion.matches ? "auto" : "smooth" });
      if (row) {
        row.classList.remove("flash");
        void row.offsetWidth;
        row.classList.add("flash");
      }
    }

    // -- alignment -------------------------------------------------------------------

    openAlignment(trigger) {
      if (!this.data) return;
      if (!this.alignment) this.alignment = new AlignmentPanel(this);
      this.alignment.sheet.open(trigger);
    }

    // -- phone handoff ---------------------------------------------------------------

    openPhone(trigger) {
      let code = null;
      let timer = 0;
      const qr = el("div", { class: "qr", role: "img", "aria-label": "QR code to open this pull request on your phone" });
      const countdown = el("p", { class: "countdown", "aria-live": "off" });
      const error = el("p", { class: "field-error", role: "alert" });
      const discard = () => {
        clearInterval(timer);
        if (code) api("DELETE", `/workspace/api/handoff/${encodeURIComponent(code)}`, { scm: this.scm }).catch(() => {});
        code = null;
      };
      const sheet = new Sheet({ title: "Continue on your phone", onClose: discard });
      const create = async () => {
        discard();
        error.textContent = "";
        qr.style.opacity = "0.25";
        try {
          const res = await api("POST", "/workspace/api/handoff", { scm: this.scm, body: this.body() });
          code = res.code;
          const doc = new DOMParser().parseFromString(res.svg, "text/html");
          const svg = doc.body.firstElementChild;
          qr.replaceChildren(svg ? document.importNode(svg, true) : "");
          qr.style.opacity = "1";
          const ends = Date.now() + res.expires_in * 1000;
          const tick = () => {
            const left = Math.max(0, Math.round((ends - Date.now()) / 1000));
            countdown.textContent = left
              ? `Expires in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`
              : "This code has expired. Make a new one.";
            if (!left) {
              clearInterval(timer);
              qr.style.opacity = "0.25";
              code = null;
            }
          };
          tick();
          timer = setInterval(tick, 1000);
        } catch (err) {
          error.textContent = err.message;
        }
      };
      sheet.body.append(
        el("p", { text: "Scan with your phone's camera to open this pull request there, ready to use." }),
        qr, countdown, error,
        el("p", { class: "muted", text: `Your phone must be signed in to Databricks as ${document.body.dataset.email}. The code works once, expires after two minutes, and never contains your token.` }),
        el("div", { class: "actions" }, el("button", { class: "btn", type: "button", text: "New code", onclick: create })),
      );
      sheet.open(trigger);
      create();
    }
  }

  class AlignmentPanel {
    constructor(view) {
      this.view = view;
      this.draftKey = `prreview.alignment.${view.ref.repository}#${view.ref.pull_request_id}`;
      this.sheet = new Sheet({ title: "Check alignment", side: true });
      this.intent = "feature";
      this.renderForm();
    }

    renderForm(error = "") {
      const intents = this.view.data.intents;
      const chips = el("div", { class: "chips", role: "radiogroup", "aria-label": "Kind of change" },
        intents.map((intent) => el("button", {
          class: "chip",
          type: "button",
          role: "radio",
          "aria-checked": String(intent.value === this.intent),
          tabindex: intent.value === this.intent ? "0" : "-1",
          text: intent.label,
          dataset: { value: intent.value },
          onclick: (e) => this.pick(chips, e.currentTarget.dataset.value),
          onkeydown: (e) => this.chipKeys(chips, e),
        })));
      const area = el("textarea", { id: uid("requirement"), rows: 9, maxlength: 20000, placeholder: "Paste the user story, ticket or acceptance criteria" });
      area.value = session.get(this.draftKey) || "";
      area.addEventListener("input", () => {
        session.set(this.draftKey, area.value);
        if (area.value.trim()) errorNode.textContent = "";
      });
      const post = el("input", { type: "checkbox", id: uid("post") });
      const errorNode = el("p", { class: "field-error", role: "alert", text: error });
      const form = el("form", {
        novalidate: true,
        onsubmit: (event) => {
          event.preventDefault();
          const requirement = area.value.trim();
          if (!requirement) {
            errorNode.textContent = "Paste the requirement to check against.";
            area.focus();
            return;
          }
          this.run(requirement, post.checked);
        },
      },
      el("p", { text: "Optional. Paste what this pull request is meant to deliver and the reviewer checks the diff against it." }),
      el("label", { text: "What kind of change is this?" }), chips,
      el("div", { class: "field" }, el("label", { for: area.id, text: "Requirement or acceptance criteria" }), area),
      el("label", { class: "check", for: post.id }, post, "Post the result as a comment on the pull request"),
      errorNode,
      el("div", { class: "actions" }, el("button", { class: "btn primary", type: "submit", text: "Check" })));
      this.sheet.setTitle("Check alignment");
      this.sheet.body.replaceChildren(form);
    }

    pick(group, value) {
      this.intent = value;
      for (const chip of group.children) {
        const on = chip.dataset.value === value;
        chip.setAttribute("aria-checked", String(on));
        chip.tabIndex = on ? 0 : -1;
      }
    }

    chipKeys(group, event) {
      const keys = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
      if (!(event.key in keys)) return;
      event.preventDefault();
      const chips = [...group.children];
      const next = chips[(chips.indexOf(event.currentTarget) + keys[event.key] + chips.length) % chips.length];
      this.pick(group, next.dataset.value);
      next.focus();
    }

    async run(requirement, postComment) {
      const status = el("p", { class: "status-line", text: "Reading the diff" });
      this.sheet.body.replaceChildren(status, el("div", { class: "progress", role: "progressbar", "aria-label": "Checking" }),
        el("p", { class: "muted", text: "This takes a little while on larger pull requests. You can close this panel; the result waits here." }));
      try {
        let job = await api("POST", "/workspace/api/alignment", {
          scm: this.view.scm,
          body: this.view.body({ intent: this.intent, requirement, post_comment: postComment }),
        });
        if (job.job_id) job = await pollJob(job.job_id, this.view.scm, () => { status.textContent = "Checking against the requirement"; });
        if (job.status === "failed") throw new Error(job.error || "The check failed.");
        this.renderResult(job.result);
        if (!this.sheet.isOpen) toast("Alignment check finished");
        if (job.result.comment_posted) this.view.load();
      } catch (err) {
        this.renderForm(err.message);
      }
    }

    renderResult(result) {
      this.sheet.setTitle("Alignment");
      const intents = Object.fromEntries(this.view.data.intents.map((i) => [i.value, i.label]));
      const nodes = [
        el("div", { class: `verdict ${result.verdict}` },
          el("h3", { text: VERDICTS[result.verdict] || result.verdict }),
          result.summary ? el("p", { text: result.summary }) : null),
      ];
      nodes.push(el("p", { class: result.intent_matches ? "muted" : "notice warn",
        text: result.intent_matches
          ? `Reads as a ${intents[result.intent]?.toLowerCase() || result.intent}, as declared.`
          : `Declared as ${intents[result.intent]?.toLowerCase() || result.intent}, but reads more like ${intents[result.detected_intent]?.toLowerCase() || "something else"}.` }));
      if (result.criteria?.length) {
        nodes.push(el("h3", { class: "section-title", text: "Requirement by requirement" }),
          el("ol", { class: "criteria" }, result.criteria.map((c) => {
            const [label, tone] = CRITERION[c.status] || [c.status, ""];
            return el("li", null,
              el("div", { class: "row" }, el("span", { class: "text", text: c.text }), el("span", { class: `badge ${tone}`, text: label })),
              c.note ? el("p", { class: "note", text: c.note }) : null,
              c.evidence?.length ? el("div", { class: "evidence" }, c.evidence.map((ref) => {
                const m = /^(.*?)(?::(\d+)(?:-\d+)?)?$/.exec(ref);
                return el("button", { type: "button", text: ref, onclick: () => this.view.reveal(m[1], m[2] && Number(m[2])) });
              })) : null);
          })));
      }
      if (result.unrelated_changes?.length) {
        nodes.push(el("h3", { class: "section-title", text: "Changes the requirement does not explain" }),
          el("ul", { class: "list" }, result.unrelated_changes.map((u) =>
            el("li", null, el("span", { class: "path", text: u.file }), el("span", { class: "muted", text: u.reason })))));
      }
      for (const w of result.warnings || []) nodes.push(el("p", { class: "notice", text: w }));
      if (result.comment_posted) nodes.push(el("p", { class: "muted", text: "Posted on the pull request." }));
      nodes.push(el("div", { class: "actions" }, el("button", { class: "btn", type: "button", text: "Check again", onclick: () => this.renderForm() })));
      this.sheet.body.replaceChildren(...nodes);
    }
  }

  function shortBranch(name) {
    return (name || "").replace(/^refs\/heads\//, "");
  }

  // -- mobile tabs ---------------------------------------------------------------------

  function selectTab(name) {
    for (const tab of document.querySelectorAll("[role=tab]")) {
      const on = tab.id === `tab-btn-${name}`;
      tab.setAttribute("aria-selected", String(on));
      tab.tabIndex = on ? 0 : -1;
      $(`#${tab.getAttribute("aria-controls")}`).hidden = !on;
    }
  }

  function initTabs() {
    const tabs = [...document.querySelectorAll("[role=tab]")];
    for (const tab of tabs) {
      tab.addEventListener("click", () => {
        selectTab(tab.id.replace("tab-btn-", ""));
        window.scrollTo({ top: 0 });
      });
      tab.addEventListener("keydown", (event) => {
        const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
        if (!step) return;
        const next = tabs[(tabs.indexOf(tab) + step + tabs.length) % tabs.length];
        selectTab(next.id.replace("tab-btn-", ""));
        next.focus();
      });
    }
    const toolbar = $(".toolbar");
    if (toolbar && "ResizeObserver" in window) {
      new ResizeObserver(() => {
        document.documentElement.style.setProperty("--toolbar-h", `${toolbar.offsetHeight}px`);
      }).observe(toolbar);
    }
  }

  // -- pages ---------------------------------------------------------------------------

  function initIndex() {
    const form = $("#picker");
    const scm = $("#scm");
    const repo = $("#repository");
    const number = $("#number");
    const fixture = $("#fixture_dir");
    const error = $("#picker-error");
    const remembered = local.get("prreview.workspace.last");
    if (remembered) {
      try {
        const last = JSON.parse(remembered);
        if ([...scm.options].some((o) => o.value === last.scm)) scm.value = last.scm;
        repo.value = last.repository || "";
      } catch { /* ignore */ }
    }
    const syncIdentity = () => {
      const current = scm.value;
      renderIdentity(current, null);
      if (!Token.get(current)) return;
      api("GET", `/workspace/api/me?scm=${encodeURIComponent(current)}`, { scm: current })
        .then((me) => { if (scm.value === current) renderIdentity(current, me.login); })
        .catch(() => {});
    };
    const syncFixture = () => {
      $("#fixture-field").hidden = scm.value !== "fake";
      if (scm.value === "fake" && !repo.value) repo.value = "platform";
    };
    scm.addEventListener("change", () => { syncFixture(); syncIdentity(); });
    syncFixture();
    syncIdentity();
    const target = phoneSized.matches ? "/workspace/m/pr" : "/workspace/pr";

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      error.textContent = "";
      const ref = { scm: scm.value, repository: repo.value.trim() };
      if (scm.value === "fake") ref.fixture_dir = fixture.value.trim();
      if (!ref.repository || (ref.scm === "github" && !/^[\w.-]+\/[\w.-]+$/.test(ref.repository))) {
        error.textContent = ref.scm === "github" ? "Enter the repository as owner/repo." : "Enter a repository.";
        repo.focus();
        return;
      }
      local.set("prreview.workspace.last", JSON.stringify({ scm: ref.scm, repository: ref.repository }));
      const login = await signedIn(ref.scm).catch((err) => { error.textContent = err.message; return null; });
      if (!login) return;
      renderIdentity(ref.scm, login);
      if (number.value.trim()) {
        location.href = `${target}?${refQuery({ ...ref, pull_request_id: number.value.trim() })}`;
        return;
      }
      const list = $("#pr-list");
      list.replaceChildren(el("li", { class: "muted", text: "Loading" }));
      $("#results").hidden = false;
      try {
        const { pulls } = await api("GET", `/workspace/api/pulls?${refQuery(ref)}`, { scm: ref.scm });
        list.replaceChildren(...(pulls.length ? pulls.map((pr) => el("li", null,
          el("a", { href: `${target}?${refQuery({ ...ref, pull_request_id: pr.number })}` },
            el("div", { class: "title", text: `${pr.title || "(untitled)"}` }),
            el("div", { class: "sub", text: [`#${pr.number}`, pr.author && `@${pr.author}`, pr.is_draft && "Draft",
              `${shortBranch(pr.source_branch)} into ${shortBranch(pr.target_branch)}`].filter(Boolean).join("  ·  ") })))) :
          [el("li", { class: "muted", text: "No open pull requests." })]));
      } catch (err) {
        list.replaceChildren(el("li", { class: "notice error", text: err.message }));
      }
    });
  }

  function initRedeem() {
    const params = new URLSearchParams(location.search);
    const code = params.get("code");
    // Keep the one-time code out of history and anything the page shares later.
    history.replaceState(null, "", location.pathname);
    const button = $("#btn-redeem");
    const error = $("#redeem-error");
    if (!code) {
      error.textContent = "This link has no code. Scan the QR code on your computer again.";
      button.disabled = true;
      return;
    }
    button.addEventListener("click", async () => {
      button.disabled = true;
      button.textContent = "Opening";
      try {
        const res = await api("POST", "/workspace/api/handoff/redeem", { body: { code } });
        Token.set(res.scm, res.token);
        location.replace(res.next);
      } catch (err) {
        error.textContent = err.message;
        button.textContent = "Continue";
      }
    });
  }

  const page = document.body.dataset.page;
  if (page === "index") initIndex();
  else if (page === "pr") new PrView("desktop").start();
  else if (page === "mobile") {
    initTabs();
    new PrView("mobile").start();
  } else if (page === "redeem") initRedeem();
})();
