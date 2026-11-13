/* 详情抽屉：查询记录与文档块。所有内容来自已有查询快照或数据库。 */
(function () {
  const P = window.PageRAG;
  const $ = (selector) => document.querySelector(selector);
  let documentState = null;

  const REVISION_STATUS = {
    pending: "待处理",
    chunked: "已切块",
    indexed: "已入库",
    failed: "失败",
  };
  const QUERY_STATUS = {
    succeeded: "已回答",
    failed: "失败",
    running: "进行中",
  };
  const TIMING_LABELS = {
    resolve: "追问改写",
    decompose: "问题拆解",
    embed_query: "查询嵌入",
    milvus_search: "向量检索",
    sparse_search: "关键词检索",
    load_evidence: "读取证据",
    rerank: "重排",
    expand_neighbors: "邻块扩展",
    generate: "回答生成",
    verify_verify: "引用核验",
    verify_repair: "引用修复",
    verify_reverify: "复核改写",
  };

  // chunk.context 存的是 {"sections": ["# 依赖项", "## 子依赖项"]}。
  // 直接拼进 HTML 会得到 [object Object]，这里还原成章节路径。
  function contextLabel(context) {
    if (!context) return "未标注章节";
    if (typeof context === "string") return context;
    const sections = Array.isArray(context.sections)
      ? context.sections : [context.sections];
    const path = sections
      .filter((item) => typeof item === "string" && item.trim())
      .map((item) => item.replace(/^#{1,6}\s*/, "").trim())
      .filter(Boolean);
    return path.length ? path.join(" / ") : "未标注章节";
  }

  function statusPill(status, label) {
    return `<span class="status-pill ${P.escapeHtml(status || "")}">${
      P.escapeHtml(label || status || "未知")}</span>`;
  }

  function queryLabel(data) {
    if (data.status === "failed") return "失败";
    if (data.insufficient) return "资料不足";
    return QUERY_STATUS[data.status] || data.status || "未知";
  }

  function verificationLabel(data) {
    const verification = data.verification;
    if (!verification) return "无记录";
    if (verification.status === "succeeded") return "通过";
    if (verification.status === "partial") return "有待复核";
    if (verification.status === "unavailable") return "核验不可用";
    if (verification.status === "skipped") return "未执行";
    return verification.status;
  }

  function verificationTone(data) {
    const status = data.verification && data.verification.status;
    if (status === "succeeded") return "succeeded";
    if (status === "partial") return "partial";
    if (status === "unavailable") return "unavailable";
    return "skipped";
  }

  function open(options) {
    const settings = options || {};
    const drawer = $("#detail-drawer");
    if (!drawer) return;
    $("#detail-drawer-kicker").textContent = settings.kicker || "详情";
    $("#detail-drawer-title").textContent = settings.title || "详情";
    $("#detail-drawer-sub").textContent = settings.sub || "";
    $("#detail-drawer-body").innerHTML = settings.html || "";
    P.drawers.open("detail-drawer", settings.opener || document.activeElement);
    drawer.scrollTop = 0;
    const close = $("#detail-drawer-close");
    if (close && !settings.keepFocus) close.focus();
  }

  function close() {
    P.drawers.close();
  }

  function loading(title, sub) {
    open({
      kicker: "详情", title, sub,
      html: `<div class="loading-panel"><span class="spinner"></span>正在读取详情……</div>`,
    });
  }

  function errorHtml(message) {
    return `<div class="empty-state compact"><h2>读取失败</h2>
      <p>${P.escapeHtml(message)}</p></div>`;
  }

  function tile(label, value, note, tone) {
    return `<div class="metric-tile ${tone || ""}">
      <span class="metric-label">${P.escapeHtml(label)}</span>
      <strong class="metric-value">${P.escapeHtml(value)}</strong>
      ${note ? `<span class="metric-note">${P.escapeHtml(note)}</span>` : ""}
    </div>`;
  }

  function timingsHtml(timings) {
    const rows = Object.entries(timings || {})
      .filter(([key, value]) => key !== "llm_attempts"
        && key !== "llm_endpoint"
        && key !== "verify_total"
        && typeof value === "number")
      .map(([key, value]) => `
        <div class="timing-row">
          <span>${P.escapeHtml(TIMING_LABELS[key]
            || (key.startsWith("verify_") ? "核验" : key))}</span>
          <span class="timing-value">${P.formatMs(Number(value) * 1000)}</span>
        </div>`)
      .join("");
    if (!rows) return `<p class="muted">这次查询没有耗时记录。</p>`;
    return `<div class="timing-list">${rows}</div>`;
  }

  function funnelHtml(evidence) {
    const pool = evidence.pool || [];
    const candidates = evidence.candidates || [];
    const items = evidence.items || [];
    const strategy = evidence.retrieval_strategy || "未记录";
    const context = evidence.context || "";
    return `
      <div class="funnel-grid">
        ${tile("候选池", P.formatNumber(pool.length), "融合前两路去重", "")}
        ${tile("重排候选", P.formatNumber(candidates.length), "按分数排序", "")}
        ${tile("进入上下文", P.formatNumber(items.length), "最终证据单元", "")}
        ${tile("上下文字符", P.formatNumber(context.length), "送入生成模型", "")}
      </div>
      <p class="detail-note">检索策略：<code>${P.escapeHtml(strategy)}</code>。
        候选池为空表示这次没有进入融合选路，或该策略只返回重排候选。</p>
      ${candidates.length ? `<details class="detail-fold">
        <summary>查看前 10 条候选</summary>
        <div class="detail-table">
          ${candidates.slice(0, 10).map((item) => `
            <div class="detail-row">
              <span class="rank-badge">#${P.escapeHtml(item.rank)}</span>
              <span class="detail-row-main">${P.escapeHtml(
                item.document_title || "未命名文档")}</span>
              <span class="detail-row-meta">块 ${P.escapeHtml(item.chunk_id)}</span>
              <span class="detail-row-meta">${P.formatNumber(item.score, 4)}</span>
            </div>`).join("")}
        </div>
      </details>` : ""}
      ${items.length ? `<details class="detail-fold">
        <summary>查看送入上下文的分段</summary>
        <div class="context-list">
          ${items.map((item) => `<article class="context-item">
            <div class="context-head">
              <span class="rank-badge">[${P.escapeHtml(item.rank)}]</span>
              <strong>${P.escapeHtml(item.header || "未标注来源")}</strong>
            </div>
            <div class="source-text">${P.escapeHtml(item.text || "")}</div>
          </article>`).join("")}
        </div>
      </details>` : ""}`;
  }

  function citationsHtml(citations) {
    if (!citations || !citations.length) {
      return `<p class="muted">这次回答没有保存引用记录。</p>`;
    }
    return citations.map((citation) => {
      const location = [
        citation.page != null ? `第 ${citation.page} 页` : "",
        citation.line_start != null
          ? `第 ${citation.line_start} 至 ${citation.line_end} 行` : "",
      ].filter(Boolean).join("，");
      const text = citation.available === false
        ? `<p class="muted">资料已删除，正文不再展示。</p>`
        : `<div class="source-text md">${P.renderMarkdown(
          citation.text || "", new Set())}</div>`;
      return `<details class="detail-chunk">
        <summary>
          <span class="rank-badge">[${P.escapeHtml(citation.rank)}]</span>
          <span class="detail-row-main">${P.escapeHtml(citation.document_title)}</span>
          <span class="detail-row-meta">${P.escapeHtml(location)}</span>
          ${citation.score != null ? `<span class="detail-row-meta">${
            P.formatNumber(citation.score, 4)}</span>` : ""}
        </summary>
        <div class="detail-chunk-body">${text}
          <p class="detail-note">修订 ${P.escapeHtml(citation.revision_id)}。</p>
        </div>
      </details>`;
    }).join("");
  }

  function queryHtml(data) {
    const ranks = new Set((data.citations || []).map((item) => Number(item.rank)));
    const answer = data.answer
      ? P.renderMarkdown(data.answer, ranks)
      : `<p class="muted">${data.status === "failed"
        ? "这次查询失败，没有回答。" : "没有保存回答正文。"}</p>`;
    const duration = data.duration_ms;
    const resolved = data.resolved_question && data.resolved_question !== data.question
      ? `<p class="detail-note">实际检索问题：${P.escapeHtml(data.resolved_question)}</p>`
      : "";
    return `
      ${resolved}
      <div class="detail-badges">
        ${statusPill(data.insufficient ? "insufficient" : data.status,
                     queryLabel(data))}
        ${statusPill(verificationTone(data), `核验：${verificationLabel(data)}`)}
        ${statusPill("", data.scope && data.scope.mode === "federated"
          ? "跨集合" : "单集合")}
      </div>
      <div class="funnel-grid compact">
        ${tile("总耗时", duration ? P.formatMs(duration) : "—", "含检索与生成")}
        ${tile("引用", P.formatNumber((data.citations || []).length), "编号来源")}
        ${tile("证据", P.formatNumber(data.evidence.items.length), "进入上下文")}
        ${tile("索引版本", `v${P.formatNumber(data.index_version)}`, "回答固定版本")}
      </div>
      <section class="detail-section">
        <h3>回答</h3>
        <div class="detail-answer md">${answer}</div>
      </section>
      <section class="detail-section">
        <h3>检索过程</h3>
        ${funnelHtml(data.evidence)}
      </section>
      <section class="detail-section">
        <h3>阶段耗时</h3>
        ${timingsHtml(data.timings)}
      </section>
      <section class="detail-section">
        <h3>引用原文</h3>
        ${citationsHtml(data.citations)}
      </section>
      <p class="detail-note">查询编号 ${P.escapeHtml(data.query_id)}，
        ${P.escapeHtml(P.formatDate(data.created_at))}，
        集合 ${P.escapeHtml((data.scope && data.scope.indexes
          && data.scope.indexes[0] && data.scope.indexes[0].collection_name)
          || "未记录")}。</p>`;
  }

  async function openQuery(queryId, options) {
    loading("查询详情", queryId);
    try {
      const data = await P.api(`/queries/${encodeURIComponent(queryId)}`);
      open({
        kicker: "查询详情",
        title: options && options.title ? options.title : "这次回答怎么来的",
        sub: `${P.formatDate(data.created_at)} · ${queryLabel(data)}`,
        html: queryHtml(data),
        keepOpener: true,
      });
    } catch (error) {
      open({
        kicker: "查询详情", title: "读取失败", sub: queryId,
        html: errorHtml(error.message), keepOpener: true,
      });
    }
  }

  function chunksHtml(data) {
    if (!data.chunks.length) {
      return `<p class="muted">所选修订在这个索引版本里没有块。</p>`;
    }
    const start = data.offset + 1;
    const end = data.offset + data.chunks.length;
    return `
      <div class="chunk-meta-line">显示第 ${start} 至 ${end} 块，共
        ${P.formatNumber(data.chunks_total)} 块</div>
      <div class="chunk-list">
        ${data.chunks.map((chunk) => {
          const location = [
            chunk.page != null ? `第 ${chunk.page} 页` : "",
            chunk.line_start != null
              ? `第 ${chunk.line_start} 至 ${chunk.line_end} 行` : "",
          ].filter(Boolean).join("，");
          const text = chunk.text == null
            ? `<p class="muted">资料已删除，正文不再展示。</p>`
            : `<div class="source-text md">${P.renderMarkdown(chunk.text, new Set())}</div>`;
          return `<details class="detail-chunk">
            <summary>
              <span class="rank-badge">#${P.formatNumber(chunk.chunk_index + 1)}</span>
              <span class="detail-row-main">${P.escapeHtml(
                contextLabel(chunk.context))}</span>
              <span class="detail-row-meta">${P.escapeHtml(location)}</span>
              <span class="detail-row-meta">${P.formatNumber(chunk.char_count)} 字符</span>
            </summary>
            <div class="detail-chunk-body">${text}
              ${chunk.start_cut || chunk.end_cut
                ? `<p class="detail-note">这一块在边界处有截断。</p>` : ""}
            </div>
          </details>`;
        }).join("")}
      </div>
      <div class="pager">
        <button class="ghost" type="button" data-doc-page="prev"
          ${data.offset <= 0 ? "disabled" : ""}>上一页</button>
        <span class="pager-status">第 ${Math.floor(data.offset / data.limit) + 1} 页</span>
        <button class="ghost" type="button" data-doc-page="next"
          ${end >= data.chunks_total ? "disabled" : ""}>下一页</button>
      </div>`;
  }

  function documentHtml(data) {
    const revisionOptions = data.revisions.map((revision) =>
      `<option value="${P.escapeHtml(revision.revision_id)}"
        ${revision.revision_id === data.selected_revision_id ? "selected" : ""}>
        ${P.escapeHtml(revision.revision_id)}
        ${revision.is_current ? "，当前修订" : ""}
        ${revision.status === "indexed" ? "" : `，${REVISION_STATUS[revision.status]
          || revision.status}`}
      </option>`).join("");
    const revisions = data.revisions.map((revision) => `
      <tr class="${revision.revision_id === data.selected_revision_id
        ? "is-selected" : ""}">
        <td>${P.escapeHtml(revision.revision_id)}</td>
        <td>${statusPill(revision.status,
          REVISION_STATUS[revision.status] || revision.status)}</td>
        <td>${P.formatNumber(revision.chunk_count)}</td>
        <td>${P.escapeHtml(P.formatDate(revision.created_at))}</td>
      </tr>`).join("");
    return `
      <div class="detail-badges">
        ${statusPill(data.available ? "succeeded" : "failed",
          data.available ? "可用" : "已删除")}
        ${statusPill("", data.source_kind ? data.source_kind.toUpperCase() : "未知格式")}
        ${statusPill("", data.active_index_version
          ? `当前索引 v${data.active_index_version}` : "没有可用索引")}
      </div>
      <div class="funnel-grid compact">
        ${tile("当前修订", data.current_revision_id || "—", "文档最新版本")}
        ${tile("查看版本", `v${P.formatNumber(data.index_version)}`,
          "这一版回答可引用")}
        ${tile("块总数", P.formatNumber(data.chunks_total), "所选版本")}
        ${tile("集合", data.collection_name, "所属知识库")}
      </div>
      <p class="detail-note">添加于 ${P.escapeHtml(P.formatDate(data.created_at))}。
        ${data.available ? "" : "文档已软删除，历史引用仍保留记录。"}</p>
      <section class="detail-section">
        <h3>修订记录</h3>
        <label class="field-label" for="detail-revision">查看修订</label>
        <select id="detail-revision" class="detail-select">${revisionOptions}</select>
        <table class="data-table compact">
          <thead><tr><th>修订</th><th>状态</th><th>块数</th>
            <th>创建时间</th></tr></thead>
          <tbody>${revisions || `<tr><td colspan="4" class="muted">没有修订记录。</td></tr>`}</tbody>
        </table>
      </section>
      <section class="detail-section">
        <h3>文档块</h3>
        ${chunksHtml(data)}
      </section>`;
  }

  async function loadDocument(offset) {
    if (!documentState) return;
    const { documentId, revisionId, limit } = documentState;
    const query = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    if (revisionId) query.set("revision_id", revisionId);
    const data = await P.api(`/api/documents/${encodeURIComponent(documentId)}?${query}`);
    documentState = {
      documentId, revisionId: data.selected_revision_id, limit, data,
    };
    open({
      kicker: "资料详情",
      title: data.title,
      sub: `${data.collection_name} · ${data.source_kind.toUpperCase()} · ${
        data.available ? "可用" : "已删除"}`,
      html: documentHtml(data),
      keepOpener: true,
      keepFocus: offset > 0,
    });
  }

  async function openDocument(documentId, options) {
    documentState = { documentId, revisionId: "", limit: 50, data: null };
    loading("资料详情", options && options.title ? options.title : documentId);
    try {
      await loadDocument(0);
    } catch (error) {
      documentState = null;
      open({
        kicker: "资料详情", title: "读取失败", sub: documentId,
        html: errorHtml(error.message), keepOpener: true,
      });
    }
  }

  function bind() {
    const drawer = $("#detail-drawer");
    if (!drawer) return;
    $("#detail-drawer-close").addEventListener("click", close);
    drawer.addEventListener("change", (event) => {
      if (event.target.id !== "detail-revision" || !documentState) return;
      documentState.revisionId = event.target.value;
      loadDocument(0).catch((error) => P.toast(`读取修订失败：${error.message}`, true));
    });
    drawer.addEventListener("click", (event) => {
      const page = event.target.closest("[data-doc-page]");
      if (!page || !documentState || !documentState.data) return;
      const data = documentState.data;
      const offset = page.dataset.docPage === "next"
        ? data.offset + data.limit : Math.max(0, data.offset - data.limit);
      loadDocument(offset).catch((error) => P.toast(`翻页失败：${error.message}`, true));
    });
  }

  P.detail = { open, close, openQuery, openDocument, bind };
}());
