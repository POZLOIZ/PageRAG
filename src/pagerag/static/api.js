/* 共用请求、格式化与小工具。所有页面只通过这里访问后端接口。 */
(function () {
  const state = {
    collections: [],
    selectedCollectionId: null,
  };

  async function api(path, options) {
    const config = Object.assign({ method: "GET", timeout: 30000 }, options || {});
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), config.timeout);
    // 页面可以传入自己的取消信号；停止等待时只中断浏览器请求，服务端
    // 已经开始的查询可能继续完成并留下记录。
    const external = config.signal;
    const forwardAbort = () => controller.abort();
    if (external) {
      if (external.aborted) controller.abort();
      else external.addEventListener("abort", forwardAbort, { once: true });
    }
    const init = {
      method: config.method,
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
    };
    if (config.body !== undefined) {
      init.body = JSON.stringify(config.body);
    }
    try {
      const response = await fetch(path, init);
      const text = await response.text();
      let data = {};
      if (text) {
        try {
          data = JSON.parse(text);
        } catch (error) {
          data = { detail: text };
        }
      }
      if (!response.ok) {
        if (response.status === 401 && !path.startsWith("/auth/")) {
          document.dispatchEvent(new CustomEvent("pagerag:unauthorized"));
        }
        const detail = data.detail || data.error || response.statusText;
        throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      }
      return data;
    } catch (error) {
      if (error.name === "AbortError") {
        const stopped = new Error(external && external.aborted
          ? "已停止等待" : "请求超时，请稍后重试");
        stopped.cancelled = Boolean(external && external.aborted);
        throw stopped;
      }
      throw error;
    } finally {
      clearTimeout(timer);
      if (external) external.removeEventListener("abort", forwardAbort);
    }
  }

  async function stream(path, body, options, onEvent) {
    const config = Object.assign({ method: "POST" }, options || {});
    const external = config.signal;
    try {
      const response = await fetch(path, {
        method: config.method,
        headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
        body: JSON.stringify(body),
        signal: external,
      });
      if (!response.ok) {
        if (response.status === 401) {
          document.dispatchEvent(new CustomEvent("pagerag:unauthorized"));
        }
        const text = await response.text();
        let detail = text;
        try {
          const parsed = JSON.parse(text);
          detail = parsed.detail || parsed.error || text;
        } catch (error) {
          detail = text || response.statusText;
        }
        throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      }
      if (!response.body) throw new Error("浏览器不支持流式读取");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const read = await reader.read();
        if (read.done) break;
        buffer += decoder.decode(read.value, { stream: true }).replace(/\r\n/g, "\n");
        let boundary = buffer.indexOf("\n\n");
        while (boundary >= 0) {
          const block = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          if (block.trim()) dispatch(block, onEvent);
          boundary = buffer.indexOf("\n\n");
        }
      }
      if (buffer.trim()) dispatch(buffer, onEvent);
    } catch (error) {
      if (error.name === "AbortError") {
        const stopped = new Error("已停止等待");
        stopped.cancelled = true;
        throw stopped;
      }
      throw error;
    }
  }

  function dispatch(block, onEvent) {
    let event = "message";
    const data = [];
    block.split("\n").forEach((line) => {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    });
    if (!data.length) return;
    let payload = {};
    try {
      payload = JSON.parse(data.join("\n"));
    } catch (error) {
      payload = { text: data.join("\n") };
    }
    if (onEvent) onEvent(event, payload);
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[char]));
  }

  function icon(className) {
    return `<svg class="brand-glyph ${escapeHtml(className || "")}" viewBox="0 0 32 32"
      aria-hidden="true"><use href="#pagerag-mark"></use></svg>`;
  }

  function formatDate(value) {
    if (!value) return "未知时间";
    let text = String(value);
    // 旧接口可能返回没有时区的时间；按 UTC 解释，避免在 UTC+8 显示成早八小时。
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(text)
        && !/(Z|[+-]\d{2}:\d{2})$/.test(text)) {
      text += "Z";
    }
    const date = new Date(text);
    if (Number.isNaN(date.getTime())) return "未知时间";
    const pad = (number) => String(number).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
      + `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function formatNumber(value, digits) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    return Number(value).toLocaleString("zh-CN", {
      maximumFractionDigits: digits === undefined ? 2 : digits,
    });
  }

  function formatPercent(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    return `${(Number(value) * 100).toFixed(1)}%`;
  }

  function formatMs(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    return Number(value) >= 1000
      ? `${(Number(value) / 1000).toFixed(1)} 秒`
      : `${Number(value).toFixed(0)} 毫秒`;
  }

  const drawerState = { id: null, opener: null };

  function openDrawer(id, opener) {
    const node = document.getElementById(id);
    if (!node) return;
    if (drawerState.id && drawerState.id !== id) {
      const previous = document.getElementById(drawerState.id);
      if (previous) previous.hidden = true;
    }
    if (drawerState.id !== id) {
      drawerState.opener = opener || document.activeElement;
    }
    drawerState.id = id;
    node.hidden = false;
    node.scrollTop = 0;
    const body = node.querySelector(".drawer-body");
    if (body) body.scrollTop = 0;
    document.body.classList.add("drawer-open");
  }

  function closeDrawer(restoreFocus) {
    if (!drawerState.id) return;
    const node = document.getElementById(drawerState.id);
    if (node) node.hidden = true;
    const opener = drawerState.opener;
    drawerState.id = null;
    drawerState.opener = null;
    if (!document.querySelector(".source-drawer:not([hidden])")) {
      document.body.classList.remove("drawer-open");
    }
    if (restoreFocus !== false && opener && opener.isConnected) opener.focus();
  }

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !drawerState.id) return;
    const modal = document.getElementById("modal");
    if (modal && !modal.hidden) return;
    closeDrawer();
  });

  let modalResolve = null;
  function showModal(options) {
    const settings = options || {};
    return new Promise((resolve) => {
      const backdrop = document.getElementById("modal");
      const title = document.getElementById("modal-title");
      const message = document.getElementById("modal-message");
      const input = document.getElementById("modal-input");
      const ok = document.getElementById("modal-ok");
      const cancel = document.getElementById("modal-cancel");
      if (!backdrop || !title || !message || !input || !ok || !cancel) {
        resolve(settings.input ? null : false);
        return;
      }
      if (modalResolve) modalResolve(settings.input ? null : false);
      title.textContent = settings.title || "";
      message.textContent = settings.message || "";
      message.hidden = !settings.message;
      input.hidden = !settings.input;
      input.value = settings.value || "";
      ok.textContent = settings.okLabel || "确定";
      ok.classList.toggle("danger", Boolean(settings.danger));
      cancel.textContent = settings.cancelLabel || "取消";
      backdrop.hidden = false;
      const previous = document.activeElement;
      const finish = (result) => {
        backdrop.hidden = true;
        document.removeEventListener("keydown", onKey);
        modalResolve = null;
        if (previous && previous.focus) previous.focus();
        resolve(result);
      };
      const onKey = (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          finish(settings.input ? null : false);
        } else if (event.key === "Tab") {
          const focusable = Array.from(backdrop.querySelectorAll(
            "button:not([disabled]), input:not([hidden]):not([disabled]), [tabindex='0']",
          )).filter((node) => !node.hidden);
          if (!focusable.length) return;
          const first = focusable[0];
          const last = focusable[focusable.length - 1];
          if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
          }
        } else if (event.key === "Enter" && !event.isComposing) {
          event.preventDefault();
          finish(settings.input ? input.value.trim() : true);
        }
      };
      modalResolve = finish;
      ok.onclick = () => finish(settings.input ? input.value.trim() : true);
      cancel.onclick = () => finish(settings.input ? null : false);
      backdrop.onclick = (event) => {
        if (event.target === backdrop) finish(settings.input ? null : false);
      };
      document.addEventListener("keydown", onKey);
      if (settings.input) {
        input.focus();
        input.select();
      } else {
        ok.focus();
      }
    });
  }

  function promptText(title, message, value) {
    return showModal({ title, message, value: value || "", input: true });
  }

  function confirmAction(title, message, okLabel) {
    return showModal({
      title, message, okLabel: okLabel || "确定", danger: true,
    }).then((result) => result === true);
  }

  let toastTimer = null;
  function toast(message, bad) {
    const node = document.getElementById("toast");
    if (!node) return;
    node.textContent = message;
    node.classList.toggle("bad", Boolean(bad));
    node.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.classList.remove("show"), 3600);
  }

  function setCollections(collections) {
    state.collections = collections || [];
    document.dispatchEvent(new CustomEvent("pagerag:collections", {
      detail: { collections: state.collections },
    }));
  }

  function selectCollection(collectionId, source) {
    state.selectedCollectionId = collectionId;
    document.dispatchEvent(new CustomEvent("pagerag:collection-selected", {
      // source 区分用户点选与页面自动选中；问答页只跟随用户点选。
      detail: { collectionId, source: source || "auto" },
    }));
  }

  async function refreshCollections() {
    const data = await api("/api/collections");
    setCollections(data.collections || []);
    return state.collections;
  }

  async function checkHealth() {
    const badge = document.getElementById("health-badge");
    const label = badge ? badge.querySelector(".health-text") : null;
    if (!badge || !label) return;
    try {
      await api("/healthz", { timeout: 5000 });
      label.textContent = "在线";
      badge.classList.add("ok");
      badge.classList.remove("bad");
    } catch (error) {
      label.textContent = "离线";
      badge.classList.add("bad");
      badge.classList.remove("ok");
    }
  }

  window.PageRAG = {
    api,
    stream,
    escapeHtml,
    icon,
    formatDate,
    formatNumber,
    formatPercent,
    formatMs,
    toast,
    promptText,
    confirmAction,
    state,
    setCollections,
    selectCollection,
    refreshCollections,
    checkHealth,
    drawers: { open: openDrawer, close: closeDrawer },
  };
}());
