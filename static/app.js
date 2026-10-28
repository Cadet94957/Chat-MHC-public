/* Chat MHC frontend: login, chat with SSE streaming, forms & laws browsers. */
(() => {
  const $ = (sel) => document.querySelector(sel);
  const state = { chatId: null, chats: [], sending: false };

  // ---------- tiny markdown renderer (bold, italics, code, lists, headings, links) ----------
  // Escapes quotes too, so escaped text is inert inside attribute values.
  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  // Only http(s) URLs may become links (blocks javascript:/data: schemes).
  const isSafeUrl = (u) => /^https?:\/\//i.test(u || "");
  function inline(s) {
    return s
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
               '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  }
  function renderMarkdown(md) {
    const lines = esc(md).split("\n");
    let html = "", list = null;
    const closeList = () => { if (list) { html += `</${list}>`; list = null; } };
    for (const raw of lines) {
      const line = raw.trimEnd();
      let m;
      if ((m = line.match(/^(#{1,4})\s+(.*)/))) {
        closeList();
        html += `<h3>${inline(m[2])}</h3>`;
      } else if ((m = line.match(/^[-*]\s+(.*)/))) {
        if (list !== "ul") { closeList(); html += "<ul>"; list = "ul"; }
        html += `<li>${inline(m[1])}</li>`;
      } else if ((m = line.match(/^(\d+)[.)]\s+(.*)/))) {
        // Keep the written number so steps split by paragraphs read 1, 2, 3.
        if (list !== "ol") { closeList(); html += `<ol start="${m[1]}">`; list = "ol"; }
        html += `<li>${inline(m[2])}</li>`;
      } else if (line === "") {
        closeList();
      } else {
        closeList();
        html += `<p>${inline(line)}</p>`;
      }
    }
    closeList();
    return html;
  }

  // ---------- api ----------
  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (res.status === 401) { showLogin(); throw new Error("unauthenticated"); }
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail; } catch {}
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return res.json();
  }

  // ---------- auth ----------
  function showLogin() {
    $("#login-overlay").classList.remove("hidden");
    $("#app").classList.add("hidden");
  }
  function showApp() {
    $("#login-overlay").classList.add("hidden");
    $("#app").classList.remove("hidden");
  }

  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errEl = $("#login-error");
    errEl.classList.add("hidden");
    try {
      await api("/api/login", {
        method: "POST",
        body: JSON.stringify({
          username: $("#login-user").value,
          password: $("#login-pass").value,
        }),
      });
      await boot();
    } catch (err) {
      errEl.textContent = err.message || "Login failed";
      errEl.classList.remove("hidden");
    }
  });

  $("#logout").addEventListener("click", async () => {
    await fetch("/api/logout", { method: "POST" });
    location.reload();
  });

  // ---------- chats sidebar ----------
  async function refreshChats() {
    const { chats } = await api("/api/chats");
    state.chats = chats;
    const list = $("#chat-list");
    list.innerHTML = "";
    for (const chat of chats) {
      const el = document.createElement("div");
      el.className = "chat-item" + (chat.id === state.chatId ? " active" : "");
      el.innerHTML = `<span class="title"></span>
                      <button class="ghost archive" title="Archive">🗑</button>`;
      el.querySelector(".title").textContent = chat.title;
      el.addEventListener("click", () => openChat(chat.id));
      el.querySelector(".archive").addEventListener("click", async (e) => {
        e.stopPropagation();
        await api(`/api/chats/${chat.id}/archive`, { method: "POST" });
        if (state.chatId === chat.id) { state.chatId = null; clearMessages(); navigate("chat"); }
        refreshChats();
      });
      list.appendChild(el);
    }
  }

  $("#new-chat").addEventListener("click", () => {
    state.chatId = null; clearMessages(); refreshChats(); navigate("chat");
  });

  function clearMessages() {
    $("#messages").innerHTML = "";
    $("#messages").appendChild($("#welcome") || welcomeNode());
    $("#welcome")?.classList.remove("hidden");
  }
  let welcomeHTML = null;
  function welcomeNode() {
    const div = document.createElement("div");
    div.id = "welcome";
    div.innerHTML = welcomeHTML;
    bindSuggestions(div);
    return div;
  }

  // Load a chat's saved messages into the view (no navigation side-effects).
  async function loadChat(chatId) {
    const { history } = await api(`/api/chats/${chatId}/history`);
    if (!history.length) {            // chat is gone (e.g. wiped) or brand new
      state.chatId = null;
      clearMessages();
      refreshChats();
      return;
    }
    state.chatId = chatId;
    $("#messages").innerHTML = "";
    for (const msg of history) addMessage(msg.role, msg.content, msg.sources);
    refreshChats();
  }
  // Sidebar click: load the chat and route to it.
  async function openChat(chatId) {
    await loadChat(chatId);
    navigate("chat", state.chatId);
  }

  // ---------- messages ----------
  function addMessage(role, content, sources) {
    $("#welcome")?.remove();
    const wrap = document.createElement("div");
    wrap.className = `msg ${role}`;
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    if (role === "assistant") bubble.innerHTML = renderMarkdown(content);
    else bubble.textContent = content;
    wrap.appendChild(bubble);
    if (role === "assistant" && sources && sources.length) {
      const btn = document.createElement("button");
      btn.className = "sources-btn";
      btn.textContent = `Sources (${sources.length})`;
      btn.addEventListener("click", () => showSources(sources));
      bubble.appendChild(btn);
    }
    $("#messages").appendChild(wrap);
    $("#messages").scrollTop = $("#messages").scrollHeight;
    return bubble;
  }

  async function sendMessage(text) {
    if (state.sending || !text.trim()) return;
    state.sending = true;
    $("#send").disabled = true;
    try {
      if (!state.chatId) {
        const chat = await api("/api/chats", { method: "POST", body: "{}" });
        state.chatId = chat.id;
        // Sync the URL silently so a reload mid-session reopens this chat
        // (replaceState does not fire hashchange, avoiding a reload race).
        history.replaceState(null, "", `#/chat/${chat.id}`);
      }
      addMessage("user", text);
      const bubble = addMessage("assistant", "");
      bubble.innerHTML = '<span class="typing">Searching the knowledge base…</span>';

      const res = await fetch(`/api/chats/${state.chatId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input: text }),
      });
      if (res.status === 401) { showLogin(); return; }
      if (!res.ok) throw new Error(`Request failed (${res.status})`);

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "", answer = "", sources = [];
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buffer.indexOf("\n\n")) >= 0) {
          const block = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          const event = (block.match(/^event: (.*)$/m) || [])[1];
          const data = (block.match(/^data: (.*)$/m) || [])[1];
          if (event === "sources") sources = JSON.parse(data);
          else if (event === "delta") {
            answer += JSON.parse(data);
            bubble.innerHTML = renderMarkdown(answer);
            $("#messages").scrollTop = $("#messages").scrollHeight;
          }
        }
      }
      bubble.innerHTML = renderMarkdown(answer || "_No response._");
      if (sources.length) {
        const btn = document.createElement("button");
        btn.className = "sources-btn";
        btn.textContent = `Sources (${sources.length})`;
        btn.addEventListener("click", () => showSources(sources));
        bubble.appendChild(btn);
      }
      refreshChats();
    } catch (err) {
      addMessage("assistant", `_Error: ${err.message}_`);
    } finally {
      state.sending = false;
      $("#send").disabled = false;
    }
  }

  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("#input").value;
    $("#input").value = "";
    sendMessage(text);
  });
  $("#input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      $("#composer").requestSubmit();
    }
  });
  function bindSuggestions(root) {
    root.querySelectorAll(".suggestion").forEach((btn) =>
      btn.addEventListener("click", () => sendMessage(btn.textContent)));
  }

  // ---------- sources panel ----------
  function showSources(sources) {
    const list = $("#sources-list");
    list.innerHTML = "";
    for (const src of sources) {
      const item = document.createElement("div");
      item.className = "source-item";
      const badge = src.type === "law" ? "statute"
        : src.type === "form" ? "form" : src.type;
      const recency = src.superseded_by || src.is_current === false
        ? '<span class="badge older">older</span>'
        : (src.type === "article" || src.type === "update"
           ? '<span class="badge current">current</span>' : "");
      item.innerHTML = `<div class="label"></div>
        <span class="badge ${src.type === "form" ? "gold" : ""}">${badge}</span>
        ${recency}
        <div class="small muted"></div><div class="actions"></div>`;
      item.querySelector(".label").textContent = src.label;
      item.querySelector(".small").textContent =
        [src.title !== src.label ? src.title : "", src.date].filter(Boolean).join(" — ");
      const actions = item.querySelector(".actions");
      if (src.form_id && src.downloadable) {
        const a = document.createElement("a");
        a.href = `/api/forms/${encodeURIComponent(src.form_id)}/download`;
        a.textContent = "Download PDF";
        actions.appendChild(a);
      } else if (isSafeUrl(src.url)) {
        const a = document.createElement("a");
        a.href = src.url; a.target = "_blank"; a.rel = "noopener noreferrer";
        a.textContent = "View source";
        actions.appendChild(a);
      }
      list.appendChild(item);
    }
    $("#sources-panel").classList.remove("hidden");
  }
  $("#close-sources").addEventListener("click", () =>
    $("#sources-panel").classList.add("hidden"));

  // ---------- views & routing ----------
  // Pure DOM view toggle (no navigation side-effects).
  function showView(name) {
    document.querySelectorAll(".tab").forEach((t) =>
      t.classList.toggle("active", t.dataset.view === name));
    document.querySelectorAll(".view").forEach((v) =>
      v.classList.toggle("hidden", v.id !== `view-${name}`));
    if (name === "forms") loadForms();
    if (name === "laws") loadLaws("");
  }

  // Hash-based routing so the browser Back/Forward buttons move between views
  // (and reopen the active chat) instead of unloading the single-page app and
  // dropping the current conversation. Routes: #/chat, #/chat/<id>, #/forms,
  // #/laws. The hash is never sent to the server, so static hosting is fine.
  const VIEWS = new Set(["chat", "forms", "laws"]);
  function parseHash() {
    const [view, chatId] = location.hash.replace(/^#\/?/, "").split("/");
    return { view: VIEWS.has(view) ? view : "chat", chatId: chatId || null };
  }
  function navigate(view, chatId = null) {
    const hash = view === "chat" && chatId ? `#/chat/${chatId}` : `#/${view}`;
    if (location.hash === hash) applyRoute();   // same hash: re-apply manually
    else location.hash = hash;                  // else hashchange -> applyRoute
  }
  async function applyRoute() {
    const { view, chatId } = parseHash();
    if (view === "chat" && chatId) {
      // Re-hydrate from the server when this is a different chat or the message
      // DOM was lost (e.g. a reload mid-session) — the conversation is saved
      // server-side, so navigating back always restores it.
      if (chatId !== state.chatId || !$("#messages").querySelector(".msg"))
        await loadChat(chatId);
    }
    showView(view);
  }
  window.addEventListener("hashchange", applyRoute);
  document.querySelectorAll(".tab").forEach((tab) =>
    tab.addEventListener("click", () => navigate(tab.dataset.view, state.chatId)));

  // ---------- forms ----------
  let formsCache = null;
  async function loadForms() {
    if (!formsCache) formsCache = (await api("/api/forms")).forms;
    renderForms($("#forms-filter").value);
  }
  function renderForms(filter) {
    const list = $("#forms-list");
    list.innerHTML = "";
    const q = (filter || "").toLowerCase();
    for (const form of formsCache || []) {
      const hay = `${form.title} ${form.description || ""}`.toLowerCase();
      if (q && !hay.includes(q)) continue;
      const card = document.createElement("div");
      card.className = "card";
      card.innerHTML = `<h4></h4><div class="tagline"></div><div class="actions"></div>`;
      card.querySelector("h4").textContent = form.title;
      card.querySelector(".tagline").textContent = form.description || "";
      const actions = card.querySelector(".actions");
      if (form.downloadable) {
        const a = document.createElement("a");
        a.href = `/api/forms/${encodeURIComponent(form.id)}/download`;
        a.textContent = "Download PDF";
        actions.appendChild(a);
      } else if (isSafeUrl(form.source_url)) {
        const a = document.createElement("a");
        a.href = form.source_url; a.target = "_blank"; a.rel = "noopener noreferrer";
        a.textContent = "View on MHCO.org";
        actions.appendChild(a);
      }
      list.appendChild(card);
    }
    if (!list.children.length)
      list.innerHTML = '<p class="muted">No forms loaded yet. Run the ingest pipeline.</p>';
  }
  $("#forms-filter").addEventListener("input", (e) => renderForms(e.target.value));

  // ---------- laws ----------
  let lawsTimer = null;
  async function loadLaws(q) {
    const { laws } = await api(`/api/laws?q=${encodeURIComponent(q)}`);
    const list = $("#laws-list");
    list.innerHTML = "";
    for (const law of laws) {
      const card = document.createElement("div");
      card.className = "card";
      card.innerHTML = `<h4></h4><div class="tagline"></div>`;
      card.querySelector("h4").textContent = law.section_id || law.title;
      card.querySelector(".tagline").textContent =
        law.text.length > 400 ? law.text.slice(0, 400) + "…" : law.text;
      list.appendChild(card);
    }
    if (!laws.length)
      list.innerHTML = '<p class="muted">No statutes matched.</p>';
  }
  $("#laws-filter").addEventListener("input", (e) => {
    clearTimeout(lawsTimer);
    lawsTimer = setTimeout(() => loadLaws(e.target.value), 300);
  });

  // ---------- boot ----------
  async function boot() {
    try {
      const me = await api("/api/me");
      showApp();
      welcomeHTML = $("#welcome").innerHTML;
      bindSuggestions($("#welcome"));
      await refreshChats();
      const status = await api("/api/status");
      const total = Object.values(status.documents || {}).reduce((a, b) => a + b, 0);
      let note = `${total} documents · ${status.forms} forms`;
      if (status.seed_documents) note += " · seed data (run ingest)";
      if (!me.llm_configured) note += " · AI key missing";
      $("#kb-status").textContent = note;
      await applyRoute();   // restore the view + open chat from the URL hash
    } catch {
      showLogin();
    }
  }
  boot();
})();
