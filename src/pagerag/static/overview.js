/* 概览页：真实运行数据、最近查询与查询详情入口。 */
(function () {
  const P = window.PageRAG;
  let loading = false;
  let generation = 0;

  const $ = (selector) => document.querySelector(selector);

  const QUERY_STATUS = {
    succeeded: "已回答",
    failed: "失败",
    running: "进行中",
  };
  const VERIFY_STATUS = {
    succeeded: "通过",
    partial: "待复核",
    unavailable: "不可用",
    skipped: "未执行",
  };
  const JOB_STATUS = {
    pending: "等待中",
    queued: "排队中",
    running: "处理中",
    building: "处理中",
    succeeded: "成功",
    failed: "失败",
    cancelled: "已取消",
  };

  function statusPill(status, label) {
    return `<span class="status-pill ${P.escapeHtml(status || "")}">${
      P.escapeHtml(label || status || "未知")}</span>`;
  }

  function queryLabel(item) {
    if (item.status === "failed") return "失败";
    if (item.insufficient) return "资料不足";
    return QUERY_STATUS[item.status] || item.status || "未知";
  }

  function verifyLabel(item) {
    if (!item.verification_status) return "无记录";
    const label = VERIFY_STATUS[item.verification_status] || item.verification_status;
    return item.verification_issue_count
      ? `${label} ${P.formatNumber(item.verification_issue_count)} 条` : label;
  }

  function verifyTone(item) {
    if (!item.verification_status || item.verification_status === "skipped") {
      return "skipped";
    }
    if (item.verification_status === "succeeded"
        && !item.verification_issue_count) return "succeeded";
    if (item.verification_status === "unavailable") return "unavailable";
    return "partial";
  }

  function tile(label, value, note, tone) {
    return `<div class="metric-tile ${tone || ""}">
      <span class="metric-label">${P.escapeHtml(label)}</span>
      <strong class="metric-value">${P.escapeHtml(value)}</strong>
      ${note ? `<span class="metric-note">${P.escapeHtml(note)}</span>` : ""}
    </div>`;
  }

  function rate(value) {
    return P.formatPercent(Number(value || 0));
  }

  function kpis(data) {
    const stats = data.stats || {};
    const totals = data.totals || {};
    const answered = stats.query_count
      ? Number(stats.answered_count) / Number(stats.query_count) : 0;
    return `<div class="metric-grid">
      ${tile("知识库", P.formatNumber(totals.collections), "当前账号可见", "")}
      ${tile("可用文档", P.formatNumber(totals.documents_available),
        `共 ${P.formatNumber(totals.documents)} 篇`, "")}
      ${tile("索引块", P.formatNumber(totals.chunks), "当前活动索引", "")}
      ${tile("累计查询", P.formatNumber(totals.queries), "全部历史", "")}
      ${tile("回答率", rate(answered),
        `最近 ${P.formatNumber(stats.query_count)} 次`, "accent")}
      ${tile("生成 P50", stats.generate_ms_p50 == null ? "—"
        : P.formatMs(stats.generate_ms_p50), "最近样本", "")}
    </div>`;
  }

  function trendHtml(trend) {
    const days = trend || [];
    const max = Math.max(1, ...days.map((day) => day.total));
    const bars = days.map((day) => {
      const height = (day.total / max) * 100;
      const parts = [
        ["answered", day.answered],
        ["insufficient", day.insufficient],
        ["failed", day.failed],
        ["other", day.other],
      ].filter(([, count]) => count > 0).map(([kind, count]) =>
        `<span class="trend-seg ${kind}" style="height:${
          day.total ? (count / day.total) * 100 : 0}%"></span>`).join("");
      const title = `${day.date}：回答 ${day.answered}，资料不足 ${
        day.insufficient}，失败 ${day.failed}，其他 ${day.other}`;
      return `<div class="trend-column" title="${P.escapeHtml(title)}">
        <div class="trend-bar-zone">
          <div class="trend-bar ${day.total ? "" : "is-empty"}"
               style="height:${day.total ? height : 0}%">${parts}</div>
        </div>
        <span class="trend-label">${P.escapeHtml(day.date.slice(5))}</span>
      </div>`;
    }).join("");
    const total = days.reduce((sum, day) => sum + day.total, 0);
    return `<section class="data-card trend-card">
      <div class="card-head">
        <div>
          <h2>最近 14 天查询</h2>
          <p class="card-sub">按回答结果堆叠，共 ${P.formatNumber(total)} 次。</p>
        </div>
        <div class="legend">
          <span class="legend-item"><i class="dot answered"></i>已回答</span>
          <span class="legend-item"><i class="dot insufficient"></i>资料不足</span>
          <span class="legend-item"><i class="dot failed"></i>失败</span>
        </div>
      </div>
      ${total ? `<div class="trend-chart">${bars}</div>`
        : `<div class="empty-state compact"><h2>最近 14 天没有查询</h2>
            <p>去问答页提一个问题，这里就会记录真实的回答结果。</p></div>`}
    </section>`;
  }

  function qualityHtml(data) {
    const stats = data.stats || {};
    const verification = data.verification || {};
    const count = Number(stats.query_count || 0);
    const percent = (value) => count ? Number(value || 0) / count * 100 : 0;
    const rows = [
      ["已回答", stats.answered_count, "answered"],
      ["资料不足", stats.insufficient_count, "insufficient"],
      ["失败", stats.failed_count, "failed"],
    ].map(([label, value, kind]) => `
      <div class="quality-row">
        <span class="quality-label">${P.escapeHtml(label)}</span>
        <div class="quality-track"><span class="quality-fill ${kind}"
          style="width:${percent(value)}%"></span></div>
        <span class="quality-value">${P.formatNumber(value)}</span>
      </div>`).join("");
    return `<section class="data-card quality-card">
      <div class="card-head">
        <div>
          <h2>回答质量</h2>
          <p class="card-sub">统计最近 ${P.formatNumber(count)} 次查询。</p>
        </div>
      </div>
      <div class="quality-list">${count ? rows
        : `<p class="muted">还没有查询记录。</p>`}</div>
      <div class="quality-split">
        <div><span class="metric-label">引用核验</span>
          <strong>${P.formatNumber(verification.checked)}</strong>
          <span class="metric-note">有核验记录</span></div>
        <div><span class="metric-label">通过</span>
          <strong>${P.formatNumber(verification.clean)}</strong>
          <span class="metric-note">无待复核结论</span></div>
        <div><span class="metric-label">待复核结论</span>
          <strong>${P.formatNumber(verification.issue_claims)}</strong>
          <span class="metric-note">部分支持、不支持或未引用</span></div>
      </div>
      <p class="detail-note">生成 P95 ${stats.generate_ms_p95 == null ? "—"
        : P.formatMs(stats.generate_ms_p95)}，检索 P50 ${
        stats.retrieval_ms_p50 == null ? "—" : P.formatMs(stats.retrieval_ms_p50)}。
        平均输入 ${P.formatNumber(stats.avg_input_tokens)} token，平均输出 ${
        P.formatNumber(stats.avg_output_tokens)} token。</p>
    </section>`;
  }

  function tasksHtml(tasks) {
    if (!tasks || !tasks.length) return "";
    return `<section class="data-card task-card">
      <div class="card-head"><div><h2>正在进行的重建</h2>
        <p class="card-sub">后台任务来自当前进程。</p></div></div>
      ${tasks.map((task) => `<div class="task-row">
        <span>${P.escapeHtml(task.collection_name || task.collection_id)}</span>
        ${statusPill(task.status, JOB_STATUS[task.status] || task.status)}
        <span class="detail-row-meta">${P.escapeHtml(task.message || "")}</span>
      </div>`).join("")}
    </section>`;
  }

  function collectionsHtml(collections) {
    if (!collections || !collections.length) {
      return `<section class="data-card"><div class="card-head"><div>
        <h2>知识库</h2></div></div>
        <div class="empty-state compact"><h2>还没有知识库</h2>
        <p>在知识库页新建一个，再导入文档。</p></div></section>`;
    }
    return `<section class="data-card">
      <div class="card-head">
        <div><h2>知识库</h2>
          <p class="card-sub">文档、索引与最近一次构建状态。</p></div>
      </div>
      <div class="table-wrap">
        <table class="data-table">
          <thead><tr><th>名称</th><th>文档</th><th>索引块</th><th>索引版本</th>
            <th>查询</th><th>最近构建</th></tr></thead>
          <tbody>${collections.map((item) => `<tr class="clickable"
            data-collection="${P.escapeHtml(item.collection_id)}" tabindex="0">
            <td><span class="doc-title">${P.escapeHtml(item.name)}</span></td>
            <td>${P.formatNumber(item.documents_available)} / ${
              P.formatNumber(item.documents_total)}</td>
            <td>${P.formatNumber(item.chunks_total)}</td>
            <td>${item.active_index_version == null ? "—"
              : `v${P.formatNumber(item.active_index_version)}`}</td>
            <td>${P.formatNumber(item.query_count)}</td>
            <td>${item.last_job_status
              ? statusPill(item.last_job_status,
                JOB_STATUS[item.last_job_status] || item.last_job_status)
              : `<span class="muted">—</span>`}</td>
          </tr>`).join("")}</tbody>
        </table>
      </div>
    </section>`;
  }

  function queriesHtml(queries) {
    if (!queries || !queries.length) {
      return `<section class="data-card"><div class="card-head"><div>
        <h2>最近查询</h2></div></div>
        <div class="empty-state compact"><h2>还没有查询记录</h2>
        <p>去问答页提问后，这里会显示真实的回答状态和耗时。</p></div></section>`;
    }
    return `<section class="data-card">
      <div class="card-head">
        <div><h2>最近查询</h2>
          <p class="card-sub">点一行查看回答、检索过程和引用原文。</p></div>
      </div>
      <div class="table-wrap">
        <table class="data-table query-table">
          <thead><tr><th>问题</th><th>知识库</th><th>状态</th><th>核验</th>
            <th>引用</th><th>耗时</th><th>时间</th><th></th></tr></thead>
          <tbody>${queries.map((item) => `<tr class="clickable"
            data-query-id="${P.escapeHtml(item.query_id)}" tabindex="0">
            <td class="query-question"><span class="doc-title">${
              P.escapeHtml(item.question)}</span>
              ${item.answer_excerpt ? `<span class="query-excerpt">${
                P.escapeHtml(item.answer_excerpt)}</span>` : ""}</td>
            <td>${P.escapeHtml(item.collection_name || "跨集合")}</td>
            <td>${statusPill(item.insufficient ? "insufficient" : item.status,
              queryLabel(item))}</td>
            <td>${statusPill(verifyTone(item), verifyLabel(item))}</td>
            <td>${P.formatNumber(item.citation_count)}</td>
            <td>${item.duration_ms == null ? "—"
              : P.formatMs(item.duration_ms)}</td>
            <td class="doc-date">${P.formatDate(item.created_at)}</td>
            <td><button class="text-button" type="button"
              data-query="${P.escapeHtml(item.query_id)}">详情</button></td>
          </tr>`).join("")}</tbody>
        </table>
      </div>
    </section>`;
  }

  function render(data) {
    const content = $("#overview-content");
    if (!content) return;
    content.innerHTML = `
      ${tasksHtml(data.active_tasks)}
      ${kpis(data)}
      <div class="overview-split">
        ${trendHtml(data.trend)}
        ${qualityHtml(data)}
      </div>
      ${collectionsHtml(data.collections)}
      ${queriesHtml(data.recent_queries)}
      <p class="detail-note">数据生成于 ${P.escapeHtml(
        P.formatDate(data.generated_at))}。累计查询来自全部历史，
        比率与耗时分位数只统计${P.escapeHtml((data.window || {}).label
          || "最近查询")}。</p>`;
    const stamp = $("#overview-stamp");
    if (stamp) stamp.textContent = `更新于 ${P.formatDate(data.generated_at).slice(11)}`;
    bindRows(content);
  }

  function bindRows(root) {
    root.querySelectorAll("[data-query-id]").forEach((row) => {
      const open = () => P.detail.openQuery(row.dataset.queryId, {
        title: row.querySelector(".doc-title").textContent,
      });
      row.addEventListener("click", (event) => {
        if (event.target.closest("button, a")) return;
        open();
      });
      row.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          open();
        }
      });
    });
    root.querySelectorAll("[data-query]").forEach((button) => {
      button.addEventListener("click", () => {
        const row = button.closest("[data-query-id]");
        P.detail.openQuery(button.dataset.query, {
          title: row ? row.querySelector(".doc-title").textContent : "",
        });
      });
    });
    root.querySelectorAll("[data-collection]").forEach((row) => {
      const open = () => {
        if (P.library && P.library.select) {
          P.library.select(row.dataset.collection);
          window.location.hash = "#library";
        } else {
          window.location.hash = "#library";
        }
      };
      row.addEventListener("click", open);
      row.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          open();
        }
      });
    });
  }

  function errorHtml(message) {
    return `<div class="empty-state"><h2>概览读取失败</h2>
      <p>${P.escapeHtml(message)}</p>
      <button class="ghost" type="button" id="overview-retry">再试一次</button></div>`;
  }

  async function load() {
    if (loading) return;
    loading = true;
    generation += 1;
    const token = generation;
    const content = $("#overview-content");
    if (content) {
      content.innerHTML = `<div class="loading-panel"><span class="spinner"></span>
        正在汇总运行数据……</div>`;
    }
    try {
      const data = await P.api("/api/overview");
      if (token !== generation) return;
      render(data);
    } catch (error) {
      if (token !== generation) return;
      if (content) content.innerHTML = errorHtml(error.message);
      const retry = $("#overview-retry");
      if (retry) retry.addEventListener("click", () => load());
    } finally {
      loading = false;
    }
  }

  function init() {
    const refresh = $("#overview-refresh");
    if (refresh) refresh.addEventListener("click", () => load());
    if ((window.location.hash || "") === "#overview") load();
  }

  P.overview = { init, load };
}());
