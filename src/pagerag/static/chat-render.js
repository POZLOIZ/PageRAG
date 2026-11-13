/* 问答消息渲染：回答、引用核验摘要、引用来源面板。 */
(function () {
  const P = window.PageRAG;
  const VERDICTS = {
    supported: "支持",
    partial: "部分支持",
    unsupported: "不支持",
    uncited: "未标注引用",
  };

  function validRanks(data) {
    return new Set((data.citations || []).map((item) => Number(item.rank)));
  }

  // 回答里实际出现的角标才展示给用户。后端 citations 是本次进入上下文的
  // 全部证据，直接全列会把没有被回答引用的段落也画成“引用来源”。
  function citedRanks(data) {
    const answer = String(data.answer || "");
    const ranks = new Set();
    for (const match of answer.matchAll(/\[(\d+)\]/g)) {
      ranks.add(Number(match[1]));
    }
    return ranks;
  }

  function groupCitations(citations, ranks) {
    const groups = new Map();
    (citations || []).forEach((citation) => {
      const rank = Number(citation.rank);
      if (ranks && !ranks.has(rank)) return;
      if (!groups.has(rank)) groups.set(rank, []);
      groups.get(rank).push(citation);
    });
    return [...groups.entries()]
      .sort((left, right) => left[0] - right[0])
      .map(([rank, items]) => ({ rank, items }));
  }

  function location(citation) {
    return [
      citation.page != null ? `第 ${citation.page} 页` : "",
      citation.line_start != null ? `第 ${citation.line_start} 至 ${citation.line_end} 行` : "",
    ].filter(Boolean).join("，");
  }

  function evidenceCards(data, options) {
    const settings = options || {};
    const groups = groupCitations(data.citations || [], citedRanks(data));
    if (!groups.length) {
      return `<p class="muted">这条回答没有标注引用。</p>`;
    }
    return groups.map((group) => {
      const first = group.items[0];
      const chunks = group.items.map((citation) => {
        const deleted = citation.available === false;
        const text = deleted
          ? `<div class="source-text">资料已删除，正文不再展示。</div>`
          : `<div class="source-text md ${settings.fullText ? "" : "is-clipped"}">${P.renderMarkdown(citation.text || "", new Set())}</div>`;
        return `<div class="source-chunk" data-chunk-row="${P.escapeHtml(citation.chunk_id)}">
          ${text}
          <div class="source-foot">
            <span>${P.escapeHtml(location(citation))}</span>
            <button class="text-button" type="button" data-action="read-original"
                    data-chunk="${P.escapeHtml(citation.chunk_id)}">查看原文</button>
          </div>
          <div class="original-zone"></div>
        </div>`;
      }).join("");
      return `<section class="source-unit" data-source-rank="${group.rank}"
                id="${settings.idPrefix}-${group.rank}">
        <div class="source-head">
          <span class="rank-badge">[${group.rank}]</span>
          <strong>${P.escapeHtml(first.document_title)}</strong>
        </div>
        ${chunks}
      </section>`;
    }).join("");
  }

  function verificationLine(data) {
    const verification = data.verification;
    if (!verification || verification.status === "skipped") return "";
    if (verification.status === "unavailable") {
      return `<span class="verify-dot warn"></span>引用核验暂时不可用`;
    }
    const issues = verification.issues || {};
    const claims = verification.claims || [];
    if (!claims.length) return "";
    const flagged = Number(issues.partial || 0) + Number(issues.unsupported || 0)
      + Number(issues.uncited || 0);
    if (!flagged) return `<span class="verify-dot ok"></span>引用核验通过`;
    return `<span class="verify-dot warn"></span>${P.formatNumber(flagged)} 条结论待复核`;
  }

  function verificationDetail(data) {
    const verification = data.verification;
    if (!verification) {
      return `<p class="muted">本次回答没有核验记录。</p>`;
    }
    const claims = (verification.claims || []).map((claim) => {
      const verdict = claim.verdict || "uncited";
      const cites = (claim.citations || []).map((number) =>
        `<button type="button" class="cite-ref" data-cite="${number}">[${number}]</button>`
      ).join(" ");
      return `<div class="claim ${P.escapeHtml(verdict)}">
        <div class="claim-head">
          <span>${P.escapeHtml(claim.claim)}</span>
          <span class="verdict">${P.escapeHtml(VERDICTS[verdict] || verdict)}</span>
        </div>
        <div class="claim-meta">${cites || "没有引用"}${claim.reason
          ? `，${P.escapeHtml(claim.reason)}` : ""}</div>
      </div>`;
    }).join("");
    const error = verification.error
      ? `<p class="error-text">${P.escapeHtml(verification.error)}</p>` : "";
    if (!claims) return `${error}<p class="muted">核验没有返回逐条结论。</p>`;
    return `${claims ? `<div class="claim-list">${claims}</div>` : ""}${error}`;
  }

  function assistantHtml(data, options) {
    const settings = options || {};
    if (data.status === "failed") {
      return `<article class="chat-turn assistant" data-query-id="${P.escapeHtml(data.query_id || "")}">
        <div class="assistant-mark">${P.icon("assistant-glyph")}</div>
        <div class="assistant-main">
          <div class="assistant-name">PageRAG</div>
          <div class="answer-error">
            <strong>回答未完成</strong>
            <p>${P.escapeHtml(data.failure_reason || "生成已中断")}</p>
          </div>
        </div>
      </article>`;
    }
    const ranks = validRanks(data);
    const answer = P.renderMarkdown(data.answer || "模型没有返回内容。", ranks);
    const cited = groupCitations(data.citations || [], citedRanks(data));
    const insufficient = data.insufficient
      ? `<p class="notice">资料中没有足够的信息回答这个问题。</p>` : "";
    const verification = verificationLine(data);
    const verifyBlock = verification
      ? `<details class="verify-block">
          <summary>${verification}</summary>
          <div class="verify-body">${verificationDetail(data)}</div>
        </details>` : "";
    return `<article class="chat-turn assistant" data-query-id="${P.escapeHtml(data.query_id || "")}">
      <div class="assistant-mark">${P.icon("assistant-glyph")}</div>
      <div class="assistant-main">
        <div class="assistant-name">PageRAG</div>
        <div class="answer-text md">${insufficient}${answer}</div>
        ${verifyBlock}
        <div class="answer-actions">
          <button class="text-button" type="button" data-action="copy-answer">复制</button>
          ${cited.length ? `<button class="text-button" type="button" data-action="open-sources">查看引用来源</button>` : ""}
          <button class="text-button" type="button" data-action="ask-again">重新提问</button>
        </div>
      </div>
    </article>`;
  }

  function userHtml(question) {
    return `<article class="chat-turn user">
      <div class="user-bubble">${P.escapeHtml(question)}</div>
    </article>`;
  }

  function drawerHtml(data, activeRank) {
    const groups = groupCitations(data.citations || [], citedRanks(data));
    if (!groups.length) return `<p class="muted">这条回答没有标注引用。</p>`;
    return evidenceCards(data, { fullText: true, idPrefix: "drawer-evidence" })
      .replace(/data-source-rank="(\d+)"/g, (match, rank) =>
        Number(rank) === Number(activeRank)
          ? `data-source-rank="${rank}" data-active="true"` : match);
  }

  function loadingHtml() {
    return `<article class="chat-turn assistant is-loading" id="chat-loading">
      <div class="assistant-mark">${P.icon("assistant-glyph")}</div>
      <div class="assistant-main">
        <div class="assistant-name">PageRAG</div>
        <div class="stream-status">
          <span class="spinner"></span>
          <span data-stream-stage>正在检索资料</span>
          <span class="stream-time"><span data-elapsed>0.0</span> 秒</span>
          <button class="text-button" type="button" data-action="cancel-ask">停止</button>
        </div>
        <div class="stream-text" data-stream-text></div>
      </div>
    </article>`;
  }

  function errorHtml(message) {
    return `<article class="chat-turn assistant">
      <div class="assistant-mark">${P.icon("assistant-glyph")}</div>
      <div class="assistant-main">
        <div class="assistant-name">PageRAG</div>
        <div class="answer-error">
          <strong>回答失败</strong>
          <p>${P.escapeHtml(message)}</p>
        </div>
      </div>
    </article>`;
  }

  P.chatRender = {
    assistantHtml,
    userHtml,
    drawerHtml,
    loadingHtml,
    errorHtml,
    groupCitations,
    citedRanks,
    validRanks,
  };
}());
