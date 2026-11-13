/* 登录门禁：服务端开启账号后，先登录再加载页面模块。 */
(function () {
  const P = window.PageRAG;
  const state = { enabled: false, user: null };
  let overlay = null;
  let pending = null;
  let resolveGate = null;

  function showOverlay() {
    if (overlay) {
      overlay.hidden = false;
      const input = overlay.querySelector("input[name=username]");
      if (input) input.focus();
    }
    document.body.classList.add("auth-locked");
  }

  function hideOverlay() {
    if (overlay) overlay.hidden = true;
    document.body.classList.remove("auth-locked");
  }

  function renderAccount() {
    const chip = document.getElementById("account-chip");
    const name = document.getElementById("account-name");
    if (!chip || !name) return;
    if (!state.enabled || !state.user) {
      chip.hidden = true;
      return;
    }
    chip.hidden = false;
    name.textContent = state.user.username;
  }

  function setUser(user) {
    state.user = user || null;
    renderAccount();
  }

  function buildOverlay() {
    overlay = document.createElement("div");
    overlay.className = "auth-overlay";
    overlay.hidden = true;
    overlay.innerHTML = `
      <form class="auth-card" id="auth-form">
        <span class="welcome-mark">${P.icon("auth-glyph")}</span>
        <h2>登录 PageRAG</h2>
        <p class="hint">登录后，自己创建的资料和对话只有自己能看到。</p>
        <div class="auth-tabs" role="tablist">
          <button class="auth-tab is-active" type="button" role="tab"
                  aria-selected="true" data-mode="login">登录</button>
          <button class="auth-tab" type="button" role="tab"
                  aria-selected="false" data-mode="register">注册</button>
        </div>
        <label class="field">
          <span>用户名</span>
          <input name="username" type="text" autocomplete="username"
                 minlength="3" maxlength="40" required>
        </label>
        <label class="field">
          <span>密码</span>
          <input name="password" type="password" autocomplete="current-password"
                 minlength="8" required>
        </label>
        <p class="auth-error" id="auth-error" role="alert" hidden></p>
        <button class="primary" type="submit" id="auth-submit">登录</button>
        <p class="hint auth-note">第一次使用先切到注册，用户名至少 3 个字符，密码至少 8 个字符。</p>
      </form>`;
    document.body.appendChild(overlay);

    let mode = "login";
    const form = overlay.querySelector("#auth-form");
    const errorNode = overlay.querySelector("#auth-error");
    const submit = overlay.querySelector("#auth-submit");
    const password = overlay.querySelector("input[name=password]");

    overlay.querySelectorAll(".auth-tab").forEach((button) => {
      button.addEventListener("click", () => {
        mode = button.dataset.mode;
        overlay.querySelectorAll(".auth-tab").forEach((item) => {
          item.classList.toggle("is-active", item === button);
          item.setAttribute("aria-selected", item === button ? "true" : "false");
        });
        submit.textContent = mode === "login" ? "登录" : "注册并登录";
        password.setAttribute("autocomplete",
          mode === "login" ? "current-password" : "new-password");
        errorNode.hidden = true;
      });
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(form);
      const username = String(data.get("username") || "").trim();
      const secret = String(data.get("password") || "");
      if (!username || !secret) return;
      submit.disabled = true;
      errorNode.hidden = true;
      try {
        const user = await P.api(`/auth/${mode}`, {
          method: "POST",
          body: { username, password: secret },
          timeout: 20000,
        });
        setUser(user);
        hideOverlay();
        form.reset();
        if (resolveGate) {
          const done = resolveGate;
          resolveGate = null;
          done(user);
        }
      } catch (error) {
        errorNode.textContent = error.message || "登录失败，请重试";
        errorNode.hidden = false;
      } finally {
        submit.disabled = false;
      }
    });
  }

  async function gate() {
    const session = await P.api("/auth/session", { timeout: 8000 });
    state.enabled = Boolean(session.enabled);
    if (!state.enabled) {
      hideOverlay();
      return null;
    }
    if (session.user) {
      setUser(session.user);
      hideOverlay();
      return session.user;
    }
    setUser(null);
    buildOverlayIfNeeded();
    showOverlay();
    pending = new Promise((resolve) => { resolveGate = resolve; });
    return pending;
  }

  function buildOverlayIfNeeded() {
    if (!overlay) buildOverlay();
  }

  async function logout() {
    try {
      await P.api("/auth/logout", { method: "POST" });
    } catch (error) {
      // 会话已经失效时也要回到登录页。
    }
    setUser(null);
    window.location.reload();
  }

  document.addEventListener("pagerag:unauthorized", () => {
    if (!state.enabled) return;
    setUser(null);
    buildOverlayIfNeeded();
    showOverlay();
  });

  function init() {
    const button = document.getElementById("logout-button");
    if (button) button.addEventListener("click", logout);
    renderAccount();
  }

  window.PageRAG.auth = {
    state,
    gate,
    init,
    logout,
    get user() { return state.user; },
  };
}());
