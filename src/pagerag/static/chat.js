/* 问答页：服务端对话、流式回答与引用来源。 */
(function () {
  const P = window.PageRAG;
  const R = P.chatRender;
  const LAST_COLLECTION = "pagerag.lastCollection";
  const ALL_COLLECTIONS = "__all__";
  const LAST_CONVERSATION = "pagerag.lastConversation.";
  let collections = [];
  let activeCollectionId = null;
  let conversations = [];
  let activeConversationId = null;
  let activeConversationTitle = "";
  let turns = [];
  let suggestions = [];
  let loading = false;
  let initialized = false;
  let hasDraft = false;
  let controller = null;
  let scopeController = null;
  let epoch = 0;
  let elapsedTimer = null;
  let startedAt = 0;
  let pendingQuestion = "";
  let searchQuery = "";
  let searchTimer = null;

  function $(selector, root) {
    return (root || document).querySelector(selector);
  }

  function lastConversationKey(collectionId) {
    return `${LAST_CONVERSATION}${collectionId}`;
  }

  function currentCollection() {
    return collections.find((item) => item.collection_id === activeCollectionId);
  }

  function allScope() {
    return activeCollectionId === ALL_COLLECTIONS;
  }

  function readyToAsk() {
    if (allScope()) {
      return collections.some((item) => item.active_index_version != null);
    }
    const collection = currentCollection();
    return Boolean(collection && collection.active_index_version != null);
  }

  function isStale(token, collectionId) {
    return token !== epoch || activeCollectionId !== collectionId;
  }

  function updateComposerState() {
    const hint = $("#composer-hint");
    if (hint) {
      if (!collections.length) hint.textContent = "先在知识库中导入文档";
      else if (!readyToAsk()) hint.textContent = "等待知识库完成处理";
      else hint.textContent = "Enter 发送，Shift + Enter 换行";
    }
    const button = $("#ask-button");
    if (button) button.disabled = loading || !readyToAsk();
    const select = $("#chat-collection");
    if (select) select.disabled = loading;
  }

  function updateConversationHead() {
    const title = $("#conversation-title");
    const meta = $("#conversation-meta");
    if (title) {
      title.textContent = allScope()
        ? "全部知识库"
        : (activeConversationId ? activeConversationTitle || "未命名对话" : "新对话");
    }
    if (meta) {
      const collection = currentCollection();
      if (allScope()) {
        meta.textContent = "跨知识库检索";
      } else if (turns.length) {
        meta.textContent = `${P.formatNumber(turns.length)} 轮问答`;
      } else if (collection) {
        meta.textContent = `基于 ${collection.name} 回答`;
      } else {
        meta.textContent = "选择知识库后开始提问";
      }
    }
    const rename = $("#rename-chat");
    const remove = $("#delete-chat");
    if (rename) rename.hidden = allScope() || !activeConversationId;
    if (remove) remove.hidden = allScope() || !activeConversationId;
  }

  function updateWelcome() {
    const welcome = $("#chat-welcome");
    if (welcome) welcome.hidden = turns.length > 0 || loading;
  }

  function renderSuggestions() {
    const zone = $("#chat-suggestions");
    if (!zone) return;
    if (!collections.length) {
      zone.innerHTML = `<button type="button" class="suggestion" data-open-library>
        创建知识库</button>`;
      return;
    }
    zone.innerHTML = suggestions.map((question) =>
      `<button type="button" class="suggestion" data-question="${P.escapeHtml(question)}">
        ${P.escapeHtml(question)}</button>`).join("");
  }

  function renderConversationList() {
    const zone = $("#conversation-list");
    if (!zone) return;
    if (allScope()) {
      zone.innerHTML = `<p class="muted">在具体知识库中查看对话</p>`;
      return;
    }
    if (!conversations.length) {
      zone.innerHTML = `<p class="muted">${searchQuery
        ? "没有匹配的对话" : "还没有对话"}</p>`;
      return;
    }
    zone.innerHTML = conversations.map((item) =>
      `<button type="button" class="conversation-item ${
        item.conversation_id === activeConversationId ? "is-active" : ""}"
        data-conversation="${P.escapeHtml(item.conversation_id)}">
        <span class="conversation-title">${P.escapeHtml(item.title)}</span>
        <span class="conversation-meta">${P.formatDate(item.updated_at)}</span>
      </button>`).join("");
  }

  function scrollToBottom() {
    const scroll = $("#chat-scroll");
    if (scroll) scroll.scrollTop = scroll.scrollHeight;
  }

  function isNearBottom() {
    const scroll = $("#chat-scroll");
    return !scroll || scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 160;
  }

  function renderTranscript(forceBottom) {
    const zone = $("#transcript");
    if (!zone) return;
    const stick = forceBottom || isNearBottom();
    zone.innerHTML = turns.map((turn) => `<div class="turn">
      ${R.userHtml(turn.question)}
      ${R.assistantHtml(turn.data, {})}
    </div>`).join("");
    if (loading) {
      // 回答还在生成时也要先画出用户刚问的这句话，否则输入框清空后
      // 界面上只剩一个转圈，看不出正在回答哪个问题。
      if (pendingQuestion) {
        zone.insertAdjacentHTML("beforeend",
          `<div class="turn">${R.userHtml(pendingQuestion)}</div>`);
      }
      zone.insertAdjacentHTML("beforeend",
        `<div class="turn">${R.loadingHtml()}</div>`);
    }
    updateWelcome();
    updateConversationHead();
    if (stick) scrollToBottom();
  }

  async function refreshCollections() {
    try {
      const data = await P.api("/api/collections");
      collections = data.collections || [];
      P.setCollections(collections);
      const previous = activeCollectionId;
      fillCollections();
      if (initialized && activeCollectionId !== previous) {
        await switchCollection(activeCollectionId);
      }
    } catch (error) {
      P.toast(`读取知识库失败：${error.message}`, true);
    }
  }

  function fillCollections() {
    const select = $("#chat-collection");
    if (!select) return;
    if (!collections.length) {
      select.innerHTML = `<option value="">没有知识库</option>`;
      activeCollectionId = null;
      updateComposerState();
      return;
    }
    const saved = localStorage.getItem(LAST_COLLECTION);
    let keep;
    if (activeCollectionId === ALL_COLLECTIONS
        || collections.some((item) => item.collection_id === activeCollectionId)) {
      keep = activeCollectionId;
    } else if (saved === ALL_COLLECTIONS) {
      keep = ALL_COLLECTIONS;
    } else if (collections.some((item) => item.collection_id === saved)) {
      keep = saved;
    } else {
      const ready = collections.find((item) => item.active_index_version != null);
      keep = (ready || collections[0]).collection_id;
    }
    activeCollectionId = keep;
    const options = collections.map((item) => {
      const suffix = item.active_index_version == null ? "（未就绪）" : "";
      return `<option value="${P.escapeHtml(item.collection_id)}"
        ${item.collection_id === keep ? "selected" : ""}>
        ${P.escapeHtml(item.name)}${suffix}
      </option>`;
    }).join("");
    select.innerHTML = `<option value="${ALL_COLLECTIONS}"
      ${keep === ALL_COLLECTIONS ? "selected" : ""}>全部知识库</option>${options}`;
    select.value = keep;
    updateComposerState();
  }

  async function switchCollection(collectionId) {
    epoch += 1;
    const token = epoch;
    if (scopeController) scopeController.abort();
    scopeController = new AbortController();
    P.drawers.close(false);
    activeCollectionId = collectionId;
    localStorage.setItem(LAST_COLLECTION, collectionId);
    activeConversationId = null;
    activeConversationTitle = "";
    turns = [];
    hasDraft = false;
    searchQuery = "";
    const search = $("#conversation-search");
    if (search) search.value = "";
    updateComposerState();
    renderTranscript(true);
    renderConversationList();
    if (allScope()) {
      await loadSuggestions(token);
      if (isStale(token, collectionId)) return;
      renderTranscript(true);
      return;
    }
    await Promise.all([loadConversations(token), loadSuggestions(token)]);
    if (isStale(token, collectionId)) return;
    await restoreLastConversation(token);
  }

  async function restoreLastConversation(token) {
    const collectionId = activeCollectionId;
    if (!collectionId || allScope()) {
      startNewConversation();
      return;
    }
    const saved = localStorage.getItem(lastConversationKey(collectionId));
    if (saved) {
      try {
        await loadConversation(saved, token);
        return;
      } catch (error) {
        if (isStale(token, collectionId)) return;
        localStorage.removeItem(lastConversationKey(collectionId));
      }
    }
    // 没有本地指针时打开最近一次对话，换浏览器或重新登录后不会看到空白页。
    const latest = conversations[0];
    if (latest) {
      try {
        await loadConversation(latest.conversation_id, token);
        return;
      } catch (error) {
        if (isStale(token, collectionId)) return;
      }
    }
    startNewConversation();
  }

  async function loadConversations(token) {
    const collectionId = activeCollectionId;
    if (!collectionId || allScope()) {
      conversations = [];
      renderConversationList();
      return;
    }
    try {
      const query = searchQuery ? `&q=${encodeURIComponent(searchQuery)}` : "";
      const data = await P.api(
        `/conversations?collection_id=${encodeURIComponent(collectionId)}${query}`,
        { signal: scopeController.signal });
      if (isStale(token, collectionId)) return;
      conversations = data.conversations || [];
    } catch (error) {
      if (isStale(token, collectionId) || error.cancelled) return;
      conversations = [];
      P.toast(`读取对话失败：${error.message}`, true);
    }
    renderConversationList();
  }

  async function loadConversation(conversationId, token) {
    const collectionId = activeCollectionId;
    const detail = await P.api(`/conversations/${encodeURIComponent(conversationId)}`,
      { signal: scopeController.signal });
    if (isStale(token, collectionId)) return;
    if (detail.collection_id !== collectionId) {
      throw new Error("这个对话不属于当前知识库");
    }
    if (activeConversationId !== detail.conversation_id) P.drawers.close(false);
    activeConversationId = detail.conversation_id;
    activeConversationTitle = detail.title || "未命名对话";
    hasDraft = false;
    turns = (detail.turns || []).map((row) => ({
      question: (row.query && row.query.question) || "历史提问",
      data: row.query || {},
    }));
    localStorage.setItem(lastConversationKey(collectionId), activeConversationId);
    renderTranscript(true);
    renderConversationList();
  }

  function startNewConversation() {
    P.drawers.close(false);
    activeConversationId = null;
    activeConversationTitle = "";
    turns = [];
    hasDraft = true;
    if (activeCollectionId && !allScope()) {
      localStorage.removeItem(lastConversationKey(activeCollectionId));
    }
    renderTranscript(true);
    renderConversationList();
  }

  async function ensureConversation(question, token) {
    const collectionId = activeCollectionId;
    if (activeConversationId) return activeConversationId;
    const data = await P.api("/conversations", {
      method: "POST",
      body: { collection_id: collectionId, question },
    });
    if (isStale(token, collectionId)) return null;
    activeConversationId = data.conversation_id;
    activeConversationTitle = data.title || question.slice(0, 60);
    hasDraft = false;
    localStorage.setItem(lastConversationKey(collectionId), activeConversationId);
    // 新对话立即出现在侧栏，不等回答结束后的列表刷新。
    if (!conversations.some((item) => item.conversation_id === activeConversationId)) {
      conversations.unshift({
        conversation_id: activeConversationId,
        title: activeConversationTitle,
        turn_count: 0,
        updated_at: new Date().toISOString(),
      });
      renderConversationList();
    }
    updateConversationHead();
    return activeConversationId;
  }

  async function loadSuggestions(token) {
    suggestions = [];
    const collection = allScope()
      ? collections.find((item) => item.active_index_version != null)
      : currentCollection();
    if (!collection) {
      renderSuggestions();
      return;
    }
    try {
      const data = await P.api(
        `/api/collections/${encodeURIComponent(collection.collection_id)}/documents`,
        { signal: scopeController.signal });
      if (isStale(token, activeCollectionId)) return;
      const documents = (data.documents || []).filter((doc) => doc.available);
      const first = documents[0];
      const second = documents[1];
      if (first) {
        suggestions.push(`介绍一下《${first.title}》`);
        suggestions.push(`《${first.title}》有哪些要点？`);
      }
      if (second) suggestions.push(`介绍一下《${second.title}》`);
    } catch (error) {
      if (isStale(token, activeCollectionId) || error.cancelled) return;
      suggestions = [];
    }
    renderSuggestions();
  }

  function setBusy(value) {
    loading = value;
    updateComposerState();
    if (!value) {
      stopElapsed();
      pendingQuestion = "";
    }
  }

  function startElapsed() {
    stopElapsed();
    startedAt = Date.now();
    elapsedTimer = setInterval(() => {
      const node = $("[data-elapsed]");
      if (node) node.textContent = ((Date.now() - startedAt) / 1000).toFixed(1);
    }, 200);
  }

  function stopElapsed() {
    if (elapsedTimer) clearInterval(elapsedTimer);
    elapsedTimer = null;
  }

  function setStreamStage(stage, insufficient) {
    const labels = {
      retrieving: "正在检索资料",
      generating: "正在生成回答",
      verifying: "正在核对引用",
    };
    const node = $("[data-stream-stage]");
    if (node) node.textContent = insufficient ? "资料不足" : (labels[stage] || "处理中");
  }

  function appendStream(text) {
    const node = $("[data-stream-text]");
    if (!node || !text) return;
    const stick = isNearBottom();
    node.textContent += text;
    if (stick) scrollToBottom();
  }

  async function ask(question) {
    const text = String(question || "").trim();
    if (!text) {
      P.toast("请输入问题", true);
      return;
    }
    if (loading) return;
    if (!activeCollectionId) {
      P.toast("先在知识库中导入文档", true);
      return;
    }
    if (!readyToAsk()) {
      P.toast("知识库还没有完成处理", true);
      return;
    }
    pendingQuestion = text;
    const scopeAll = allScope();
    const collectionId = activeCollectionId;
    const token = epoch;
    setBusy(true);
    let streamController = null;
    try {
      const conversationId = scopeAll ? null : await ensureConversation(text, token);
      if (isStale(token, collectionId)) return;
      renderTranscript(true);
      startElapsed();
      streamController = new AbortController();
      controller = streamController;
      let finalData = null;
      let streamError = "";
      await P.stream(
        scopeAll
          ? "/queries/stream"
          : `/collections/${encodeURIComponent(collectionId)}/queries/stream`,
        scopeAll ? { question: text }
          : { question: text, conversation_id: conversationId },
        { signal: streamController.signal },
        (event, data) => {
          if (isStale(token, collectionId)) return;
          if (event === "status") setStreamStage(data.stage, data.insufficient);
          else if (event === "delta") appendStream(data.text);
          else if (event === "done") finalData = data;
          else if (event === "error") streamError = data.message || "查询失败";
        });
      if (isStale(token, collectionId)) return;
      if (streamError) throw new Error(streamError);
      if (!finalData) throw new Error("没有收到完整回答");
      if (!scopeAll) {
        activeConversationId = finalData.conversation_id || conversationId;
        activeConversationTitle = activeConversationTitle || text.slice(0, 60);
      }
      turns.push({ question: text, data: finalData });
      setBusy(false);
      renderTranscript(true);
      if (!scopeAll) await loadConversations(token);
    } catch (error) {
      if (isStale(token, collectionId)) return;
      const asked = pendingQuestion;
      pendingQuestion = "";
      setBusy(false);
      if (error.cancelled) {
        P.toast("已停止生成");
        if (!scopeAll) {
          if (activeConversationId) {
            try {
              await loadConversation(activeConversationId, token);
            } catch (ignored) {
              renderTranscript(false);
            }
          }
          await loadConversations(token);
        }
        return;
      }
      const zone = $("#transcript");
      if (zone) {
        // 失败时也保留用户的问题，页面不会像什么都没发生过。
        if (asked) {
          zone.insertAdjacentHTML("beforeend",
            `<div class="turn">${R.userHtml(asked)}</div>`);
        }
        zone.insertAdjacentHTML("beforeend", R.errorHtml(error.message));
      }
      const welcome = $("#chat-welcome");
      if (welcome) welcome.hidden = true;
      scrollToBottom();
      P.toast("回答失败", true);
      if (!scopeAll) await loadConversations(token);
    } finally {
      if (controller === streamController) controller = null;
    }
  }

  function cancelAsk() {
    if (controller) controller.abort();
  }

  function dataForTurn(element) {
    const article = element.closest("[data-query-id]");
    if (!article) return null;
    return turns.find((turn) => turn.data.query_id === article.dataset.queryId) || null;
  }

  function openDrawer(data, rank, opener) {
    if (!data) return;
    $("#drawer-body").innerHTML = R.drawerHtml(data, rank);
    P.drawers.open("source-drawer", opener);
    $("#drawer-close").focus();
  }

  function closeDrawer() {
    P.drawers.close();
  }

  async function readOriginal(button) {
    const zone = button.closest(".source-chunk").querySelector(".original-zone");
    if (zone.dataset.loaded === "true") {
      zone.innerHTML = "";
      zone.dataset.loaded = "false";
      button.textContent = "查看原文";
      return;
    }
    button.disabled = true;
    try {
      const data = await P.api(`/evidence/${encodeURIComponent(button.dataset.chunk)}`);
      zone.innerHTML = data.text
        ? `<div class="source-text md">${P.renderMarkdown(data.text, new Set())}</div>`
        : `<p class="muted">资料已删除，正文不再展示。</p>`;
      zone.dataset.loaded = "true";
      button.textContent = "收起";
    } catch (error) {
      zone.innerHTML = `<p class="error-text">读取失败：${P.escapeHtml(error.message)}</p>`;
    } finally {
      button.disabled = false;
    }
  }

  async function copyAnswer(article) {
    const turn = dataForTurn(article);
    if (!turn) return;
    const cited = R.groupCitations(
      turn.data.citations || [], R.citedRanks(turn.data));
    const sources = cited.map((group) => {
      const first = group.items[0];
      return `[${group.rank}] ${first.document_title}`;
    });
    const text = `${turn.data.answer || ""}${sources.length
      ? `\n\n引用来源：\n${sources.join("\n")}` : ""}`;
    try {
      await navigator.clipboard.writeText(text);
      P.toast("已复制");
    } catch (error) {
      P.toast("复制失败", true);
    }
  }

  async function renameActiveConversation() {
    if (!activeConversationId) return;
    const title = await P.promptText("重命名对话", "输入一个便于查找的名称。",
      activeConversationTitle);
    if (title == null || !title.trim()) return;
    try {
      const data = await P.api(
        `/conversations/${encodeURIComponent(activeConversationId)}`, {
          method: "PATCH", body: { title: title.trim() },
        });
      activeConversationTitle = data.title;
      updateConversationHead();
      await loadConversations(epoch);
    } catch (error) {
      P.toast(`改名失败：${error.message}`, true);
    }
  }

  async function deleteActiveConversation() {
    if (!activeConversationId) return;
    const confirmed = await P.confirmAction("删除对话", "删除后无法恢复。", "删除");
    if (!confirmed) return;
    try {
      await P.api(`/conversations/${encodeURIComponent(activeConversationId)}`, {
        method: "DELETE",
      });
      startNewConversation();
      await loadConversations(epoch);
      P.toast("对话已删除");
    } catch (error) {
      P.toast(`删除失败：${error.message}`, true);
    }
  }

  function bind() {
    $("#ask-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const input = $("#question-input");
      const question = input.value.trim();
      if (!question || loading) return;
      input.value = "";
      ask(question);
    });
    $("#question-input").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        $("#ask-form").requestSubmit();
      }
    });
    $("#chat-collection").addEventListener("change", (event) => {
      if (loading) return;
      switchCollection(event.target.value || null);
      P.selectCollection(activeCollectionId);
    });
    $("#new-chat").addEventListener("click", () => {
      if (loading) cancelAsk();
      startNewConversation();
      $("#question-input").focus();
    });
    $("#rename-chat").addEventListener("click", renameActiveConversation);
    $("#delete-chat").addEventListener("click", deleteActiveConversation);
    $("#conversation-search").addEventListener("input", (event) => {
      searchQuery = event.target.value.trim();
      clearTimeout(searchTimer);
      const token = epoch;
      searchTimer = setTimeout(() => loadConversations(token), 250);
    });
    $("#conversation-list").addEventListener("click", async (event) => {
      const button = event.target.closest("[data-conversation]");
      if (!button || loading) return;
      const token = epoch;
      try {
        await loadConversation(button.dataset.conversation, token);
      } catch (error) {
        P.toast(`打开对话失败：${error.message}`, true);
      }
    });
    $("#chat-suggestions").addEventListener("click", (event) => {
      if (event.target.closest("[data-open-library]")) {
        window.location.hash = "#library";
        return;
      }
      const button = event.target.closest("[data-question]");
      if (!button) return;
      const input = $("#question-input");
      input.value = button.dataset.question;
      input.focus();
    });
    $("#transcript").addEventListener("click", (event) => {
      const cite = event.target.closest("[data-cite]");
      if (cite) {
        const turn = dataForTurn(cite);
        if (turn) openDrawer(turn.data, Number(cite.dataset.cite), cite);
        return;
      }
      const action = event.target.closest("[data-action]");
      if (!action) return;
      const turn = dataForTurn(action);
      if (action.dataset.action === "cancel-ask") cancelAsk();
      if (action.dataset.action === "copy-answer") copyAnswer(action);
      if (action.dataset.action === "open-sources" && turn) {
        const groups = R.groupCitations(
          turn.data.citations || [], R.citedRanks(turn.data));
        openDrawer(turn.data, groups.length ? groups[0].rank : null, action);
      }
      if (action.dataset.action === "ask-again" && turn) {
        $("#question-input").value = turn.question;
        $("#question-input").focus();
      }
    });
    $("#drawer-close").addEventListener("click", closeDrawer);
    $("#source-drawer").addEventListener("click", (event) => {
      const action = event.target.closest("[data-action='read-original']");
      if (action) readOriginal(action);
    });
    document.addEventListener("pagerag:collection-selected", (event) => {
      if (event.detail.source !== "library") return;
      if (loading || !event.detail.collectionId) return;
      if (event.detail.collectionId === activeCollectionId) return;
      const select = $("#chat-collection");
      if (select) select.value = event.detail.collectionId;
      switchCollection(event.detail.collectionId);
    });
  }

  async function refresh() {
    await refreshCollections();
    if (!initialized || loading) return;
    const token = epoch;
    if (!allScope() && activeConversationId) {
      try {
        await loadConversation(activeConversationId, token);
      } catch (error) {
        if (!error.cancelled) startNewConversation();
      }
    } else {
      await loadConversations(token);
      if (!allScope() && !hasDraft && !activeConversationId && conversations[0]) {
        try {
          await loadConversation(conversations[0].conversation_id, token);
        } catch (error) {
          if (!error.cancelled) startNewConversation();
        }
      }
    }
    await loadSuggestions(token);
  }

  async function init() {
    bind();
    await refreshCollections();
    initialized = true;
    await switchCollection(activeCollectionId);
  }

  window.PageRAG.chat = { init, refresh };
}());
