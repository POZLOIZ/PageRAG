/* 知识库页：知识库列表、文档导入与处理进度。 */
(function () {
  const P = window.PageRAG;
  const ACCEPT = ".md,.markdown,.txt,.pdf,text/markdown,text/plain,application/pdf";
  let selectedId = null;
  let pollTimer = null;
  let pollGeneration = 0;
  let detailGeneration = 0;
  let busy = false;
  let replaceDocumentId = null;

  function $(selector, root) {
    return (root || document).querySelector(selector);
  }

  function documentState(doc, collection) {
    if (doc.revision_status === "failed") {
      return { label: "失败", kind: "bad", note: doc.revision_error || "" };
    }
    if (collection.active_index_version == null) {
      return { label: "待处理", kind: "warn", note: "" };
    }
    if (doc.revision_status === "indexed") {
      return { label: "就绪", kind: "ok", note: "" };
    }
    return { label: "处理中", kind: "warn", note: doc.revision_error || "" };
  }

  async function loadCollections(keepSelection) {
    const list = $("#collection-list");
    try {
      const collections = await P.refreshCollections();
      if (!collections.length) {
        list.innerHTML = `<p class="muted">还没有知识库</p>`;
        selectedId = null;
        renderEmpty();
        return;
      }
      const preferred = keepSelection && collections.some(
        (item) => item.collection_id === selectedId) ? selectedId : null;
      selectedId = preferred || collections[0].collection_id;
      renderCollectionList(collections);
      P.selectCollection(selectedId);
      await loadDetail(selectedId, { scroll: false });
    } catch (error) {
      list.innerHTML = `<p class="error-text">读取知识库失败：${P.escapeHtml(error.message)}</p>`;
    }
  }

  function renderCollectionList(collections) {
    const list = $("#collection-list");
    list.innerHTML = collections.map((item) => `
      <button type="button" class="collection-item ${item.collection_id === selectedId ? "is-active" : ""}"
              data-collection="${P.escapeHtml(item.collection_id)}">
        <span class="name">${P.escapeHtml(item.name)}</span>
        <span class="meta">${P.formatNumber(item.documents_available)} 篇文档</span>
      </button>
    `).join("");
    list.querySelectorAll("[data-collection]").forEach((button) => {
      button.addEventListener("click", () => {
        selectedId = button.dataset.collection;
        renderCollectionList(collections);
        P.selectCollection(selectedId, "library");
        stopPolling();
        loadDetail(selectedId, { scroll: true });
      });
    });
  }

  function renderEmpty() {
    $("#collection-main").innerHTML = `
      <div class="empty-state">
        <h2>创建第一个知识库</h2>
        <p>在左侧输入名称即可开始。</p>
      </div>`;
  }

  async function loadDetail(collectionId, options) {
    const settings = options || {};
    detailGeneration += 1;
    const generation = detailGeneration;
    const main = $("#collection-main");
    try {
      const [collection, docs, progress] = await Promise.all([
        P.api(`/api/collections/${encodeURIComponent(collectionId)}`),
        P.api(`/api/collections/${encodeURIComponent(collectionId)}/documents`),
        P.api(`/api/collections/${encodeURIComponent(collectionId)}/progress`),
      ]);
      // 快速切换知识库时，晚到的旧响应不能覆盖当前详情。
      if (generation !== detailGeneration || selectedId !== collectionId) return;
      main.innerHTML = renderDetail(collection, docs.documents || []);
      updateProgress(progress);
      bindDetailEvents(collectionId);
      resumeProgress(collectionId, progress);
      if (settings.scroll && window.matchMedia("(max-width: 900px)").matches) {
        main.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    } catch (error) {
      if (generation !== detailGeneration || selectedId !== collectionId) return;
      main.innerHTML = `<div class="empty-state"><p class="error-text">读取知识库失败：${P.escapeHtml(error.message)}</p></div>`;
    }
  }

  function renderDetail(collection, documents) {
    // 软删除的文档不再出现在列表里；历史引用仍由后端保留。
    const visible = documents.filter((doc) => doc.available);
    const rows = visible.length ? visible.map((doc) => {
      const state = documentState(doc, collection);
      return `<tr>
        <td>
          <span class="doc-title">${P.escapeHtml(doc.title)}</span>
          ${state.note ? `<span class="doc-error" title="${P.escapeHtml(state.note)}">${P.escapeHtml(state.note)}</span>` : ""}
        </td>
        <td><span class="status ${state.kind}">${P.escapeHtml(state.label)}</span></td>
        <td class="doc-date">${P.formatDate(doc.created_at)}</td>
        <td class="doc-actions">
          <button class="text-button" type="button" data-action="document-detail"
                  data-document="${P.escapeHtml(doc.document_id)}">详情</button>
          <button class="text-button" type="button" data-action="replace-doc"
                  data-document="${P.escapeHtml(doc.document_id)}">更新</button>
          <button class="text-button danger" type="button" data-action="delete-doc"
                  data-document="${P.escapeHtml(doc.document_id)}">删除</button>
        </td>
      </tr>`;
    }).join("") : `<tr><td colspan="4" class="empty-row">还没有文档</td></tr>`;

    return `
      <div class="kb-view">
        <div class="kb-head">
          <div>
            <h2>${P.escapeHtml(collection.name)}</h2>
            <p>${P.formatNumber(visible.length)} 篇文档</p>
          </div>
          <div class="kb-actions">
            <button class="ghost" type="button" data-action="rebuild">重新处理</button>
            <button class="primary" type="button" data-action="pick-upload">导入文档</button>
            <input id="upload-files" class="hidden-input" type="file" multiple accept="${ACCEPT}">
            <input id="replace-file" class="hidden-input" type="file" accept="${ACCEPT}">
          </div>
        </div>

        <div class="progress-zone" id="progress-zone"></div>

        ${visible.length ? `
        <div class="doc-table">
          <table>
            <thead><tr><th>文档</th><th>状态</th><th>添加时间</th><th></th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>` : `
        <div class="empty-state upload-empty">
          <h2>导入第一份文档</h2>
          <p>支持 Markdown、TXT 和带文本层的 PDF。</p>
          <button class="primary" type="button" data-action="pick-upload">选择文件</button>
        </div>`}
      </div>`;
  }

  function bindDetailEvents(collectionId) {
    const main = $("#collection-main");
    const uploadInput = $("#upload-files", main);
    const replaceInput = $("#replace-file", main);
    if (uploadInput) {
      uploadInput.addEventListener("change", () => {
        const files = Array.from(uploadInput.files || []);
        uploadInput.value = "";
        if (files.length) handleUploads(collectionId, files);
      });
    }
    if (replaceInput) {
      replaceInput.addEventListener("change", () => {
        const file = replaceInput.files && replaceInput.files[0];
        const documentId = replaceDocumentId;
        replaceInput.value = "";
        replaceDocumentId = null;
        if (file && documentId) replaceDocument(collectionId, documentId, file);
      });
    }
    main.onclick = (event) => {
      const button = event.target.closest("[data-action]");
      if (!button) return;
      if (button.dataset.action === "pick-upload" && uploadInput) uploadInput.click();
      if (button.dataset.action === "rebuild") startRebuild(collectionId);
      if (button.dataset.action === "delete-doc") {
        deleteDocument(button.dataset.document, collectionId);
      }
      if (button.dataset.action === "document-detail") {
        P.detail.openDocument(button.dataset.document, { opener: button });
      }
      if (button.dataset.action === "replace-doc" && replaceInput) {
        replaceDocumentId = button.dataset.document;
        replaceInput.click();
      }
    };
  }

  function toBase64(buffer) {
    const bytes = new Uint8Array(buffer);
    const chunkSize = 0x8000;
    let binary = "";
    for (let index = 0; index < bytes.length; index += chunkSize) {
      binary += String.fromCharCode.apply(null, bytes.subarray(index, index + chunkSize));
    }
    return btoa(binary);
  }

  function fileTitle(file) {
    return file.name.replace(/\.[^.]+$/, "") || file.name;
  }

  function sourceKind(file) {
    const name = file.name.toLowerCase();
    if (name.endsWith(".pdf")) return "pdf";
    if (name.endsWith(".txt")) return "text";
    return "markdown";
  }

  function checkFiles(files) {
    const allowed = [".md", ".markdown", ".txt", ".pdf"];
    const bad = files.filter((file) => !allowed.some(
      (suffix) => file.name.toLowerCase().endsWith(suffix)));
    if (bad.length) {
      P.toast(`不支持的文件：${bad.map((file) => file.name).join("、")}`, true);
      return false;
    }
    return true;
  }

  async function uploadFile(collectionId, file, title, revisionId) {
    const buffer = await file.arrayBuffer();
    await P.api(`/collections/${encodeURIComponent(collectionId)}/documents`, {
      method: "POST",
      timeout: 120000,
      body: {
        title: title || fileTitle(file),
        filename: file.name,
        source_kind: sourceKind(file),
        revision_id: revisionId,
        content_b64: toBase64(buffer),
      },
    });
  }

  async function handleUploads(collectionId, files) {
    if (busy) return;
    if (!checkFiles(files)) return;
    busy = true;
    let imported = 0;
    const failures = [];
    try {
      for (const [index, file] of files.entries()) {
        try {
          await uploadFile(collectionId, file, fileTitle(file),
            `r${Date.now()}-${index}`);
          imported += 1;
        } catch (error) {
          failures.push(`${file.name}：${error.message}`);
        }
      }
      if (!imported) {
        P.toast(failures[0] || "导入失败", true);
        return;
      }
      P.toast(`已导入 ${imported} 份文档，开始处理`);
      if (failures.length) {
        P.toast(`${failures.length} 份文档未导入`, true);
      }
      await loadDetail(collectionId);
      await startRebuild(collectionId);
    } finally {
      busy = false;
    }
  }

  async function replaceDocument(collectionId, documentId, file) {
    if (busy) return;
    if (!checkFiles([file])) return;
    busy = true;
    try {
      const buffer = await file.arrayBuffer();
      await P.api(`/documents/${encodeURIComponent(documentId)}/revisions`, {
        method: "POST",
        timeout: 120000,
        body: {
          filename: file.name,
          source_kind: sourceKind(file),
          revision_id: `r${Date.now()}`,
          content_b64: toBase64(buffer),
        },
      });
      P.toast("文档已更新，开始处理");
      await loadDetail(collectionId);
      await startRebuild(collectionId);
    } catch (error) {
      P.toast(`更新失败：${error.message}`, true);
    } finally {
      busy = false;
    }
  }

  async function deleteDocument(documentId, collectionId) {
    const confirmed = await P.confirmAction(
      "删除文档", "删除后这篇文档不再参与问答，历史引用记录会保留。", "删除");
    if (!confirmed) return;
    try {
      await P.api(`/api/documents/${encodeURIComponent(documentId)}`, { method: "DELETE" });
      P.toast("文档已删除");
      await loadDetail(collectionId);
      await loadCollections(true);
    } catch (error) {
      P.toast(`删除失败：${error.message}`, true);
    }
  }

  function stopPolling() {
    pollGeneration += 1;
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
  }

  function updateProgress(progress) {
    const zone = $("#progress-zone");
    if (!zone) return;
    const state = progress ? progress.state : "empty";
    const running = state === "building";
    if (!running && state !== "failed") {
      zone.innerHTML = "";
      return;
    }
    const percent = progress.percent == null ? 0 : Number(progress.percent);
    const label = state === "failed" ? "上次处理未完成" : "正在处理文档";
    zone.innerHTML = `
      <div class="progress-card ${state === "failed" ? "is-failed" : ""}">
        <div class="progress-head">
          <span>${label}</span>
          <span>${Math.max(0, Math.min(100, percent)).toFixed(0)}%</span>
        </div>
        <div class="progress-bar"><span style="width:${Math.max(0, Math.min(100, percent))}%"></span></div>
        <div class="progress-note">
          已完成 ${P.formatNumber(progress.documents_done)} / ${P.formatNumber(progress.documents_total)} 篇
          ${progress.error ? `<span class="progress-error">${P.escapeHtml(progress.error)}</span>` : ""}
          ${state === "failed" && progress.resume_available
            ? `<span>可以点重新处理继续，已完成的部分不会重做。</span>` : ""}
        </div>
      </div>`;
  }

  function resumeProgress(collectionId, progress) {
    const running = progress && progress.state === "building";
    if (!running || pollTimer) return;
    const generation = pollGeneration;
    let ticks = 0;
    const tick = async () => {
      if (generation !== pollGeneration || selectedId !== collectionId) return;
      ticks += 1;
      try {
        const current = await P.api(
          `/api/collections/${encodeURIComponent(collectionId)}/progress`);
        if (generation !== pollGeneration || selectedId !== collectionId) return;
        updateProgress(current);
        if (current.state === "building" && ticks < 400) {
          pollTimer = setTimeout(tick, 1800);
          return;
        }
        pollTimer = null;
        if (current.state === "active") {
          P.toast("文档处理完成");
          await loadDetail(collectionId);
          await loadCollections(true);
        } else if (current.state === "failed") {
          P.toast("文档处理失败", true);
        }
      } catch (error) {
        P.toast(`读取进度失败：${error.message}`, true);
      }
    };
    pollTimer = setTimeout(tick, 1200);
  }

  async function startRebuild(collectionId) {
    stopPolling();
    const generation = pollGeneration;
    try {
      const task = await P.api(`/api/collections/${encodeURIComponent(collectionId)}/rebuild`, {
        method: "POST", timeout: 15000,
      });
      await pollTask(task.task_id, collectionId, generation);
    } catch (error) {
      P.toast(`无法开始处理：${error.message}`, true);
    }
  }

  async function pollTask(taskId, collectionId, generation) {
    const tick = async () => {
      if (generation !== pollGeneration || selectedId !== collectionId) return;
      try {
        const [task, progress] = await Promise.all([
          P.api(`/api/tasks/${encodeURIComponent(taskId)}`),
          P.api(`/api/collections/${encodeURIComponent(collectionId)}/progress`),
        ]);
        if (generation !== pollGeneration || selectedId !== collectionId) return;
        updateProgress(progress);
        if (task.status === "succeeded") {
          pollTimer = null;
          P.toast("文档处理完成");
          await loadDetail(collectionId);
          await loadCollections(true);
          return;
        }
        if (task.status === "failed") {
          pollTimer = null;
          P.toast("文档处理失败", true);
          await loadDetail(collectionId);
          return;
        }
        pollTimer = setTimeout(tick, 1400);
      } catch (error) {
        P.toast(`读取进度失败：${error.message}`, true);
      }
    };
    await tick();
  }

  async function select(collectionId) {
    if (!collectionId) return;
    selectedId = collectionId;
    P.selectCollection(collectionId, "library");
    await loadCollections(true);
  }

  function init() {
    $("#create-collection-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = $("#collection-name");
      const name = input.value.trim();
      if (!name) return;
      try {
        const data = await P.api("/collections", { method: "POST", body: { name } });
        input.value = "";
        P.toast("知识库已创建");
        selectedId = data.collection_id;
        await loadCollections(true);
      } catch (error) {
        P.toast(`创建失败：${error.message}`, true);
      }
    });
    loadCollections(false);
  }

  window.PageRAG.library = { init, loadCollections, select };
}());
