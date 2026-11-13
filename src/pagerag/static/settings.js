/* 设置页：当前进程实际加载的只读配置。密钥与内部地址不显示。 */
(function () {
  const P = window.PageRAG;
  let loading = false;
  let generation = 0;

  const $ = (selector) => document.querySelector(selector);
  const STRATEGY_LABELS = {
    dense: "纯向量检索，分数为余弦相似度",
    "hybrid-rrf": "向量加关键词，按名次融合分数",
    "hybrid-rerank": "向量加关键词，候选交给重排模型",
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

  function flag(value, good, bad) {
    return `<span class="status-pill ${value ? "succeeded" : "skipped"}">${
      P.escapeHtml(value ? good : bad)}</span>`;
  }

  function text(value) {
    if (value === null || value === undefined || value === "") {
      return `<span class="muted">未配置</span>`;
    }
    return P.escapeHtml(value);
  }

  function number(value) {
    return value === null || value === undefined
      ? `<span class="muted">—</span>` : P.formatNumber(value);
  }

  function seconds(value) {
    return value === null || value === undefined
      ? `<span class="muted">—</span>` : `${P.formatNumber(value)} 秒`;
  }

  function card(title, sub, rows) {
    return `<section class="data-card setting-card">
      <div class="card-head"><div><h2>${P.escapeHtml(title)}</h2>
        ${sub ? `<p class="card-sub">${P.escapeHtml(sub)}</p>` : ""}</div></div>
      <dl class="setting-list">
        ${rows.map(([label, value, note]) => `<div class="setting-row">
          <dt>${P.escapeHtml(label)}</dt>
          <dd>${value}${note ? `<span class="setting-note">${
            P.escapeHtml(note)}</span>` : ""}</dd>
        </div>`).join("")}
      </dl>
    </section>`;
  }

  function serviceCard(data) {
    return card("服务", "进程身份与账号开关。", [
      ["名称", text(data.service.name), ""],
      ["版本", text(data.service.version), ""],
      ["进程状态", flag(data.service.status === "running", "运行中", "未知"), ""],
      ["账号隔离", flag(data.service.auth_enabled, "已开启", "未开启"),
        data.service.auth_enabled ? "集合和对话按创建者隔离" : "所有访客共用资料"],
    ]);
  }

  function modelsCard(data) {
    const models = data.models || {};
    const item = (label, model, note) => [
      label,
      `${text(model.model)} ${flag(model.configured, "已配置", "未配置")}`,
      note,
    ];
    return card("模型", "模型名来自当前环境变量，不显示接口地址与密钥。", [
      item("生成主通道", models.generation || {}, "回答生成"),
      item("生成备用通道", models.generation_fallback || {}, "主通道失败时切换"),
      item("嵌入", models.embedding || {}, `维度 ${P.formatNumber(
        (models.embedding || {}).dimensions)}`),
      item("重排", models.rerank || {}, "候选重排序"),
    ]);
  }

  function configuredCard(data) {
    const configured = data.configured || {};
    return card("依赖配置", "只表示环境变量是否齐全，不代表连接健康。", [
      ["MySQL", flag(configured.database, "已配置", "未配置"), ""],
      ["Milvus", flag(configured.milvus, "已配置", "未配置"), ""],
      ["生成接口", flag(configured.generation, "已配置", "未配置"), ""],
      ["嵌入接口", flag(configured.embedding, "已配置", "未配置"), ""],
      ["重排接口", flag(configured.rerank, "已配置", "未配置"), ""],
    ]);
  }

  function retrievalCard(data) {
    const retrieval = data.retrieval || {};
    return card("检索", "召回、融合与重排参数。", [
      ["策略", `<code>${P.escapeHtml(retrieval.strategy || "未配置")}</code>`,
        STRATEGY_LABELS[retrieval.strategy] || "自定义或旧策略"],
      ["单路召回上限", number(retrieval.top_k), "每路最多候选"],
      ["RRF 常数", number(retrieval.rrf_k), "名次融合平滑项"],
      ["关键词候选数", number(retrieval.bm25_top_n), "BM25 路召回上限"],
      ["重排池上限", number(retrieval.rerank_pool_size), "融合后送重排的候选"],
      ["关键词权重", number(retrieval.bm25_weight), "混合打分时使用"],
    ]);
  }

  function chunkingCard(data) {
    const chunking = data.chunking || {};
    return card("分块", "改这些值必须重建索引。", [
      ["解析器版本", `<code>${P.escapeHtml(chunking.parser_version)}</code>`, ""],
      ["切块器版本", `<code>${P.escapeHtml(chunking.chunker_version)}</code>`, ""],
      ["软目标", `${P.formatNumber(chunking.chunk_chars)} 字符`, "装满完整段落后封块"],
      ["硬上限", `${P.formatNumber(chunking.chunk_max_chars)} 字符`,
        "单段过长时才在句末切"],
    ]);
  }

  function evidenceCard(data) {
    const evidence = data.evidence || {};
    return card("证据上下文", "命中块如何组装成送给模型的上下文。", [
      ["单元上限", number(evidence.unit_limit), "最终证据单元数"],
      ["Token 预算", number(evidence.context_token_budget), "含渲染头"],
      ["邻块扩展", number(evidence.neighbor_expansion), "前后各补几块"],
      ["单篇文档配额", number(evidence.document_limit), "第一轮每篇上限"],
      ["上下文模式", `<code>${P.escapeHtml(evidence.context_mode || "未配置")}</code>`, ""],
      ["子查询分组上限", number(evidence.subquery_group_limit), "问题拆解时使用"],
    ]);
  }

  function queryCard(data) {
    const query = data.query || {};
    return card("查询", "问题拆解与生成通道。", [
      ["问题拆解", flag(query.decomposition_enabled, "已开启", "未开启"), ""],
      ["最多子问题", number(query.max_subqueries), ""],
      ["通道重试次数", number(query.channel_max_attempts), ""],
      ["通道退避", seconds(query.channel_backoff_seconds), ""],
      ["模型超时", seconds(query.llm_timeout_seconds), ""],
      ["模型重试", number(query.llm_max_attempts), ""],
    ]);
  }

  function verificationCard(data) {
    const verification = data.verification || {};
    return card("引用核验", "生成后逐条检查结论与引用。", [
      ["核验开关", flag(verification.enabled, "已开启", "未开启"), ""],
      ["自动修订", flag(verification.repair_enabled, "已开启", "未开启"),
        "只做一次局部修订"],
      ["超时", seconds(verification.timeout_seconds), ""],
      ["最多结论数", number(verification.max_claims), ""],
    ]);
  }

  function runtimeCard(data) {
    const runtime = data.runtime || {};
    return card("运行时", "上传与服务边界。", [
      ["最大上传", `${P.formatNumber(runtime.max_upload_mb)} MB`,
        `${P.formatNumber(runtime.max_upload_bytes)} 字节`],
      ["账号隔离", flag(runtime.auth_enabled, "已开启", "未开启"), ""],
    ]);
  }

  function buildConfig(value) {
    if (!value || !Object.keys(value).length) {
      return `<span class="muted">没有记录</span>`;
    }
    const parts = Object.entries(value).map(([key, item]) =>
      `${key}=${typeof item === "object" ? JSON.stringify(item) : item}`);
    return `<code class="wrap-code">${P.escapeHtml(parts.join("，"))}</code>`;
  }

  function indexesCard(data) {
    const indexes = data.active_indexes || [];
    return `<section class="data-card">
      <div class="card-head"><div><h2>活动索引</h2>
        <p class="card-sub">每个知识库当前可查询的索引版本。</p></div></div>
      ${indexes.length ? `<div class="table-wrap"><table class="data-table">
        <thead><tr><th>知识库</th><th>版本</th><th>状态</th><th>构建配置</th>
          <th>激活时间</th></tr></thead>
        <tbody>${indexes.map((item) => `<tr>
          <td><span class="doc-title">${P.escapeHtml(item.collection_name)}</span></td>
          <td>v${P.formatNumber(item.version)}</td>
          <td>${flag(item.status === "active", "活动", item.status)}</td>
          <td>${buildConfig(item.build_config)}</td>
          <td class="doc-date">${P.formatDate(item.activated_at)}</td>
        </tr>`).join("")}</tbody></table></div>`
        : `<div class="empty-state compact"><h2>还没有活动索引</h2>
          <p>导入文档并完成重建后，这里会显示真实索引。</p></div>`}
    </section>`;
  }

  function tasksCard(data) {
    const tasks = data.active_tasks || [];
    if (!tasks.length) return "";
    return `<section class="data-card">
      <div class="card-head"><div><h2>后台任务</h2>
        <p class="card-sub">任务句柄只在当前进程内，重启后不会保留。</p></div></div>
      ${tasks.map((task) => `<div class="task-row">
        <span class="doc-title">${P.escapeHtml(
          task.collection_name || task.collection_id)}</span>
        ${flag(task.status === "running", JOB_STATUS[task.status] || task.status,
          JOB_STATUS[task.status] || task.status)}
        <span class="detail-row-meta">${P.escapeHtml(task.message || "")}</span>
      </div>`).join("")}
    </section>`;
  }

  function render(data) {
    const content = $("#settings-content");
    if (!content) return;
    content.innerHTML = `
      <p class="settings-note">这里是只读快照。修改配置需要编辑 .env 并重启服务，
        本页不会写入任何值，也不会显示密钥、内部地址或文件路径。</p>
      ${serviceCard(data)}
      ${modelsCard(data)}
      ${configuredCard(data)}
      <div class="settings-grid">
        ${retrievalCard(data)}
        ${chunkingCard(data)}
        ${evidenceCard(data)}
        ${queryCard(data)}
        ${verificationCard(data)}
        ${runtimeCard(data)}
      </div>
      ${indexesCard(data)}
      ${tasksCard(data)}`;
  }

  function errorHtml(message) {
    return `<div class="empty-state"><h2>设置读取失败</h2>
      <p>${P.escapeHtml(message)}</p>
      <button class="ghost" type="button" id="settings-retry">再试一次</button></div>`;
  }

  async function load() {
    if (loading) return;
    loading = true;
    generation += 1;
    const token = generation;
    const content = $("#settings-content");
    if (content) {
      content.innerHTML = `<div class="loading-panel"><span class="spinner"></span>
        正在读取配置……</div>`;
    }
    try {
      const data = await P.api("/api/system");
      if (token !== generation) return;
      render(data);
    } catch (error) {
      if (token !== generation) return;
      if (content) content.innerHTML = errorHtml(error.message);
      const retry = $("#settings-retry");
      if (retry) retry.addEventListener("click", () => load());
    } finally {
      loading = false;
    }
  }

  function init() {
    const refresh = $("#settings-refresh");
    if (refresh) refresh.addEventListener("click", () => load());
    if ((window.location.hash || "") === "#settings") load();
  }

  P.settings = { init, load };
}());
