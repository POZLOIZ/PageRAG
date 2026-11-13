/* 页面入口：默认进入问答，标签切换、深链接与服务状态检查。 */
(function () {
  const P = window.PageRAG;
  const VALID_TABS = ["chat", "library", "overview", "settings"];
  let currentTab = null;

  function activatePanel(name) {
    document.querySelectorAll(".tab").forEach((button) => {
      const active = button.dataset.tab === name;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    document.querySelectorAll("[data-panel]").forEach((panel) => {
      const active = panel.dataset.panel === name;
      panel.classList.toggle("is-active", active);
      panel.hidden = !active;
    });
  }

  function refreshTab(name) {
    if (name === "chat" && P.chat) P.chat.refresh();
    if (name === "library" && P.library) P.library.loadCollections(true);
    if (name === "overview" && P.overview) P.overview.load();
    if (name === "settings" && P.settings) P.settings.load();
  }

  function setTab(name, options) {
    const settings = options || {};
    const target = VALID_TABS.includes(name) ? name : "chat";
    const changed = currentTab !== target;
    if (changed && P.drawers) P.drawers.close(false);
    currentTab = target;
    activatePanel(target);
    if (settings.skipRefresh) {
      const url = `#${target}`;
      if (window.location.hash !== url) {
        window.history.replaceState({ tab: target }, "", url);
      }
      return;
    }
    if (changed) refreshTab(target);
    if (settings.fromHistory) return;
    const url = `#${target}`;
    if (settings.replace) {
      window.history.replaceState({ tab: target }, "", url);
    } else if (window.location.hash !== url) {
      window.history.pushState({ tab: target }, "", url);
    }
  }

  async function boot() {
    P.auth.init();
    await P.auth.gate();
    document.querySelectorAll(".tab").forEach((button) => {
      button.addEventListener("click", () => setTab(button.dataset.tab));
    });
    P.library.init();
    P.detail.bind();
    P.overview.init();
    P.settings.init();
    P.chat.init();
    P.checkHealth();
    setInterval(P.checkHealth, 30000);
    const initial = (window.location.hash || "#chat").slice(1);
    // 两个模块在 init 时已经各自读取首屏数据，这里只切显示，避免重复请求。
    setTab(initial, { replace: true, skipRefresh: true });
    window.addEventListener("hashchange", () => {
      setTab((window.location.hash || "#chat").slice(1), { fromHistory: true });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
}());
