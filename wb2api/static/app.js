// WB2Api console — account-first workspace.
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const storage = {
    get(k) {
      try {
        return localStorage.getItem(k);
      } catch {
        return null;
      }
    },
    set(k, v) {
      try {
        localStorage.setItem(k, v);
      } catch {}
    },
    remove(k) {
      try {
        localStorage.removeItem(k);
      } catch {}
    },
  };
  const icons = () => {
    if (window.lucide) lucide.createIcons();
  };
  const state = {
    key: storage.get("wb2api_admin_key") || "",
    status: null,
    pendingGoogle: null,
    tokens: [],
  };

  // System is the default; an explicit preference survives reloads.
  const scheme = window.matchMedia("(prefers-color-scheme: dark)");
  function themePreference() {
    const value = storage.get("wb2api_theme");
    return ["light", "dark", "system"].includes(value) ? value : "system";
  }
  function updateThemeIcon() {
    const theme = themePreference();
    document.documentElement.classList.toggle(
      "dark",
      theme === "dark" || (theme === "system" && scheme.matches),
    );
    for (const t of ["dark", "light", "system"])
      $(`theme-icon-${t}`)?.classList.toggle("hidden", t !== theme);
    $("theme-label").textContent = {
      system: "跟随系统",
      light: "浅色模式",
      dark: "暗色模式",
    }[theme];
    document
      .querySelectorAll("[data-theme]")
      .forEach((el) =>
        el.setAttribute("aria-pressed", String(el.dataset.theme === theme)),
      );
  }
  function closeThemeMenu() {
    $("theme-menu").classList.add("hidden");
    $("theme-btn").setAttribute("aria-expanded", "false");
  }
  window.setTheme = function (theme) {
    if (!["light", "dark", "system"].includes(theme)) return;
    storage.set("wb2api_theme", theme);
    updateThemeIcon();
    closeThemeMenu();
    $("theme-btn").focus();
  };
  window.toggleThemeMenu = function () {
    const open = $("theme-menu").classList.toggle("hidden") === false;
    $("theme-btn").setAttribute("aria-expanded", String(open));
    if (open) $("theme-menu").querySelector('[aria-pressed="true"]').focus();
  };
  if (scheme.addEventListener)
    scheme.addEventListener("change", updateThemeIcon);
  else scheme.addListener(updateThemeIcon);
  window.addEventListener("storage", (e) => {
    if (e.key === "wb2api_theme") updateThemeIcon();
  });

  // --- Toast Notifications ---
  window.toast = function (message, isError = false) {
    const container = $("toast-container");
    if (!container) return;
    const toast = document.createElement("div");
    toast.className = `toast${isError ? " error" : ""}`;
    toast.innerHTML = `<i data-lucide="${isError ? "alert-circle" : "check-circle"}"></i>`;
    const messageNode = document.createElement("span");
    messageNode.textContent = String(message);
    toast.appendChild(messageNode);
    while (container.children.length >= 2) container.firstElementChild.remove();
    container.appendChild(toast);
    if (window.lucide) lucide.createIcons({ root: toast });
    setTimeout(() => {
      toast.classList.add("leaving");
      setTimeout(() => toast.remove(), 300);
    }, 3000);
  };

  window.copyText = async function (text, label = "已复制到剪贴板") {
    try {
      await navigator.clipboard.writeText(text);
      toast(label);
    } catch {
      toast("复制失败，请手动选取复制", true);
    }
  };

  // --- HTTP Helper ---
  async function api(path, { method = "GET", body, auth = true } = {}) {
    const headers = { Accept: "application/json" };
    if (auth) {
      if (!state.key) throw new Error("请先解锁控制台");
      headers["Authorization"] = `Bearer ${state.key}`;
    }
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    const res = await fetch(path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(30000),
    });
    const text = await res.text();
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        throw new Error(`服务端异常 (HTTP ${res.status})`);
      }
    }
    if (res.status === 401 && auth) {
      const isActualAdmin401 =
        data?.detail === "Invalid admin key" || path === "/admin/api/status";
      if (isActualAdmin401) {
        lock("管理员密钥无效或已过期，请重新解锁");
        throw new Error("未授权，请重新解锁");
      }
    }
    if (!res.ok) {
      const msg =
        data?.detail ||
        data?.error?.message ||
        data?.message ||
        `请求失败 (HTTP ${res.status})`;
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return data;
  }

  // --- Lock / Unlock ---
  window.lock = function (reason = "") {
    closeModal();
    cancelReauth();
    state.key = "";
    state.tokens = [];
    state.status = null;
    $("admin-key").value = "";
    $("token-table-body").replaceChildren();
    $("api-key-text").textContent = "未解锁";
    for (const id of ["import-rt", "new-token-val"]) $(id).value = "";
    $("toast-container").replaceChildren();
    storage.remove("wb2api_admin_key");
    $("lock-screen").classList.remove("hidden");
    $("app-shell").classList.add("hidden");
    $("nav-actions").classList.add("hidden");
    $("btn-logout").classList.add("hidden");
    const errBox = $("unlock-error");
    if (reason) {
      errBox.textContent = reason;
      errBox.classList.remove("hidden");
    } else {
      errBox.classList.add("hidden");
    }
    $("admin-key").focus();
    if (window.lucide) lucide.createIcons();
  };

  window.handleUnlock = async function (e) {
    if (e) e.preventDefault();
    const key = $("admin-key").value.trim();
    if (!key) return;
    const btn = $("btn-unlock");
    btn.disabled = true;
    btn.innerHTML = `<i data-lucide="loader" class="w-4 h-4 animate-spin"></i><span>解锁中…</span>`;
    if (window.lucide) lucide.createIcons({ root: btn });
    $("unlock-error").classList.add("hidden");
    try {
      state.key = key;
      const status = await api("/admin/api/status");
      state.status = status;
      storage.set("wb2api_admin_key", key);
      $("lock-screen").classList.add("hidden");
      $("app-shell").classList.remove("hidden");
      $("nav-actions").classList.remove("hidden");
      $("btn-logout").classList.remove("hidden");
      renderDashboard(status);
      renderAccounts(status.accounts || []);
      await loadTokens();
      if (!state.key) return;
      renderCurl();
      $("admin-key").value = "";
      $("app-shell").focus();
      toast("控制台已解锁");
    } catch (err) {
      lock();
      $("unlock-error").textContent = err.message;
      $("unlock-error").classList.remove("hidden");
    } finally {
      btn.disabled = false;
      btn.innerHTML = `<i data-lucide="key-round" class="w-4 h-4"></i><span>解锁控制台</span>`;
      if (window.lucide) lucide.createIcons();
    }
  };

  // --- Dashboard Status & Health ---
  window.syncStatus = async function () {
    const btn = $("btn-refresh");
    if (btn.disabled) return;
    btn.disabled = true;
    $("accounts-table-body").setAttribute("aria-busy", "true");
    try {
      const status = await api("/admin/api/status");
      if (!state.key) return;
      state.status = status;
      renderDashboard(status);
      renderAccounts(status.accounts || []);
      $("dashboard-error").classList.add("hidden");
      await loadTokens();
      toast("状态已刷新");
    } catch (err) {
      $("stat-health").textContent = "同步失败";
      $("dashboard-error").textContent =
        `无法刷新：${err.message}。以下可能是上次同步的数据，请重试。`;
      $("dashboard-error").classList.remove("hidden");
      toast(err.message, true);
    } finally {
      btn.disabled = false;
      $("accounts-table-body").removeAttribute("aria-busy");
    }
  };

  function renderDashboard(status) {
    const accs = status.accounts || [];
    $("stat-total").textContent = accs.length;
    $("account-count").textContent = accs.length;
    $("stat-ready").textContent = accs.filter(
      (a) =>
        a.enabled &&
        a.ready &&
        !["pending", "invalid"].includes(a.activation_status),
    ).length;
    $("stat-protocol").textContent = status.protocol_configured
      ? "已配置"
      : "未配置";
    $("stat-health").textContent = "已连接";
    $("last-synced").textContent =
      `同步于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
  }

  function showMsg(text, isError = false) {
    const box = $("form-msg");
    box.textContent = text;
    box.className = `notice${isError ? " error" : ""}`;
    box.classList.remove("hidden");
  }

  function hideMsg() {
    $("form-msg").classList.add("hidden");
  }

  // --- Phone & Google Verification ---
  window.handleSendSms = async function () {
    const phone = $("sms-phone").value.trim();
    if (!phone) {
      showMsg("请输入手机号 (含国际区号，如 +86138...)", true);
      return;
    }
    const btn = $("btn-send-sms");
    btn.disabled = true;
    btn.textContent = "发送中…";
    hideMsg();
    try {
      await api("/admin/api/auth/send-code", {
        method: "POST",
        body: { phone_number: phone },
      });
      showMsg("验证码已发送至手机，请查收");
      toast("验证码短信已发送");
    } catch (err) {
      showMsg(err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "重新发送";
    }
  };

  window.handleVerifySms = async function () {
    const phone = $("sms-phone").value.trim();
    const code = $("sms-code").value.trim();
    const label = $("sms-label").value.trim() || "手机账号";
    if (!phone || !code) {
      showMsg("请填写手机号和短信验证码", true);
      return;
    }
    const btn = $("btn-verify-sms");
    btn.disabled = true;
    btn.innerHTML = `<i data-lucide="loader" class="w-4 h-4 animate-spin"></i><span>验证中…</span>`;
    if (window.lucide) lucide.createIcons({ root: btn });
    hideMsg();
    if ($("mailbox-challenge-box"))
      $("mailbox-challenge-box").classList.add("hidden");
    if ($("google-challenge-box"))
      $("google-challenge-box").classList.add("hidden");
    try {
      const res = await api("/admin/api/auth/check-code", {
        method: "POST",
        body: {
          phone_number: phone,
          code,
          label,
          account_id: state.reauthAccountId || "",
        },
      });
      if (res.requires_mailbox) {
        state.pendingMailbox = {
          phone,
          state: res.mailbox_state,
          label,
          accountId: state.reauthAccountId || "",
        };
        $("mailbox-challenge-box").classList.remove("hidden");
        if (res.mailbox_hint) {
          $("mailbox-hint-text").textContent =
            `验证码已发送至绑定邮箱（${res.mailbox_hint}），请输入 6 位数字验证码：`;
        }
        startMailboxTimer(res.retry_after_seconds || 60);
        if (window.lucide) lucide.createIcons();
        showMsg(
          "手机短信校验成功！已向你的绑定邮箱发送二次验证码，请在下方填入。",
        );
        toast("请查收并填入邮箱验证码");
        $("mailbox-code-input").focus();
        return;
      }
      if (res.requires_google) {
        state.pendingGoogle = {
          phone,
          state: res.google_state,
          label,
          accountId: state.reauthAccountId || "",
        };
        const googleUrl = new URL(res.google_url);
        if (googleUrl.protocol !== "https:")
          throw new Error("授权网址无效：必须使用 HTTPS");
        $("btn-google-auth-link").href = googleUrl.href;
        $("google-challenge-box").classList.remove("hidden");
        if (window.lucide) lucide.createIcons();
        showMsg("该账号触发了 Wabi Google 二次验证，请按提示完成授权。");
        toast("请完成 Google 二次验证");
        return;
      }
      toast(
        state.reauthAccountId
          ? "账号凭据已原地更新成功！"
          : "短信验证成功，账号已入库！",
      );
      cancelReauth();
      $("sms-code").value = "";
      await syncStatus();
    } catch (err) {
      showMsg(err.message, true);
    } finally {
      btn.disabled = false;
      btn.innerHTML = `<i data-lucide="check" class="w-4 h-4"></i><span>验证并存入号池</span>`;
      if (window.lucide) lucide.createIcons();
    }
  };

  let mailboxTimer = null;
  function startMailboxTimer(seconds) {
    let remain = seconds;
    const btn = $("btn-resend-mailbox");
    const timerSpan = $("mailbox-timer");
    if (!btn) return;
    if (mailboxTimer) clearInterval(mailboxTimer);
    btn.disabled = true;
    if (timerSpan) timerSpan.textContent = `(${remain}s)`;
    mailboxTimer = setInterval(() => {
      remain--;
      if (remain <= 0) {
        clearInterval(mailboxTimer);
        mailboxTimer = null;
        btn.disabled = false;
        if (timerSpan) timerSpan.textContent = "";
      } else {
        if (timerSpan) timerSpan.textContent = `(${remain}s)`;
      }
    }, 1000);
  }

  window.handleVerifyMailbox = async function () {
    const code = $("mailbox-code-input").value.trim();
    if (!code) {
      showMsg("请输入 6 位邮箱验证码", true);
      return;
    }
    const info = state.pendingMailbox || {};
    const btn = $("btn-submit-mailbox");
    btn.disabled = true;
    btn.textContent = "验证激活中…";
    hideMsg();
    try {
      await api("/admin/api/auth/mailbox-verify", {
        method: "POST",
        body: {
          phone_number: info.phone || $("sms-phone").value.trim(),
          state: info.state || "",
          code,
          label: info.label || "手机账号",
          account_id: info.accountId || state.reauthAccountId || "",
        },
      });
      toast(
        state.reauthAccountId
          ? "邮箱验证成功，账号凭据已原地更新！"
          : "邮箱验证成功，凭据已保存，请查看账号就绪状态。",
      );
      $("mailbox-challenge-box").classList.add("hidden");
      $("sms-code").value = "";
      $("mailbox-code-input").value = "";
      state.pendingMailbox = null;
      cancelReauth();
      await syncStatus();
    } catch (err) {
      showMsg(err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "完成邮箱验证";
    }
  };

  window.handleResendMailbox = async function () {
    const info = state.pendingMailbox || {};
    const btn = $("btn-resend-mailbox");
    btn.disabled = true;
    hideMsg();
    try {
      await api("/admin/api/auth/mailbox-resend", {
        method: "POST",
        body: {
          phone_number: info.phone || $("sms-phone").value.trim(),
          state: info.state || "",
        },
      });
      toast("邮箱验证码已重新发送，请查收");
      startMailboxTimer(60);
    } catch (err) {
      showMsg(err.message, true);
      btn.disabled = false;
    }
  };

  window.handleCompleteGoogleVerify = async function () {
    const rawUrl = $("google-code-input").value.trim();
    if (!rawUrl) {
      showMsg("请粘贴 Google 授权跳转后的完整网址或 code", true);
      return;
    }
    const info = state.pendingGoogle || {};
    const btn = $("btn-submit-google");
    btn.disabled = true;
    btn.textContent = "提交验证中…";
    hideMsg();
    try {
      await api("/admin/api/auth/google-verify", {
        method: "POST",
        body: {
          phone_number: info.phone || $("sms-phone").value.trim(),
          state: info.state || "",
          code: rawUrl,
          label: info.label || "Google Phone Account",
          account_id: info.accountId || state.reauthAccountId || "",
        },
      });
      toast("Google 验证成功，账号凭据已更新！");
      $("google-challenge-box").classList.add("hidden");
      $("sms-code").value = "";
      $("google-code-input").value = "";
      state.pendingGoogle = null;
      cancelReauth();
      await syncStatus();
    } catch (err) {
      showMsg(err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "完成 Google 验证入库";
    }
  };

  // --- Manual Token Import & Collapse ---
  window.toggleManualImport = function () {
    const pane = $("pane-token");
    const icon = $("manual-import-icon");
    if (!pane) return;
    $("manual-import-toggle").setAttribute(
      "aria-expanded",
      String(pane.classList.contains("hidden")),
    );
    if (pane.classList.contains("hidden")) {
      pane.classList.remove("hidden");
      if (icon) icon.style.transform = "rotate(90deg)";
    } else {
      pane.classList.add("hidden");
      if (icon) icon.style.transform = "";
    }
  };

  window.handleManualImport = async function () {
    const label = $("import-label").value.trim() || "手动导入";
    const rt = $("import-rt").value.trim();
    if (!rt) {
      showMsg("请填入 Refresh Token", true);
      return;
    }
    const btn = $("btn-manual-import");
    btn.disabled = true;
    btn.textContent = "导入中…";
    hideMsg();
    try {
      await api("/admin/api/accounts", {
        method: "POST",
        body: { label, refresh_token: rt, id_token: "" },
      });
      toast("账号凭证导入成功！");
      $("import-rt").value = "";
      $("import-label").value = "";
      await syncStatus();
    } catch (err) {
      showMsg(err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "导入该凭证";
    }
  };

  window.copyCurrentApiKey = function () {
    const key = state.tokens.find((t) => t.enabled)?.key || "";
    if (key) {
      copyText(key, "已复制可用 API Key");
    } else {
      openTokenModal();
    }
  };

  window.reauthAccount = function (id, label, phone) {
    cancelReauth();
    state.reauthAccountId = id;
    $("sms-phone").value = phone || "";
    $("reauth-label").textContent = `正在重验：${label}`;
    $("reauth-notice").classList.remove("hidden");
    $("sms-label").value = label;
    showMsg(
      `正在为账号【${label}】重新验证并更新凭据。验证通过后将原地更新该账号。`,
    );
    $("sms-phone").scrollIntoView({
      block: "center",
      behavior: matchMedia("(prefers-reduced-motion: reduce)").matches
        ? "instant"
        : "smooth",
    });
    $("sms-phone").focus();
    toast(`已进入账号【${label}】重新验证模式`);
  };

  window.cancelReauth = function () {
    state.reauthAccountId = null;
    state.pendingMailbox = null;
    state.pendingGoogle = null;
    if (mailboxTimer) {
      clearInterval(mailboxTimer);
      mailboxTimer = null;
    }
    for (const id of [
      "mailbox-challenge-box",
      "google-challenge-box",
      "reauth-notice",
    ])
      $(id).classList.add("hidden");
    for (const id of ["sms-code", "mailbox-code-input", "google-code-input"])
      $(id).value = "";
    $("mailbox-timer").textContent = "";
    $("btn-resend-mailbox").disabled = false;
    hideMsg();
  };

  // --- Account Pool Table ---
  window.exportAccountsJson = async function () {
    if (
      !confirm(
        "导出文件包含可登录账号的敏感凭据（Refresh Token 等），未经加密。请仅保存在可信设备，不要分享。确认下载？",
      )
    )
      return;
    toast("正在导出号池凭据…");
    try {
      const accounts = await api("/admin/api/accounts/export");
      if (!accounts || !accounts.length) {
        toast("号池为空，暂无凭据可导出", true);
        return;
      }
      const blob = new Blob([JSON.stringify(accounts, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      const date = new Date().toISOString().slice(0, 10).replace(/-/g, "");
      a.href = url;
      a.download = `wb2api-accounts-${date}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      toast(`已成功导出 ${accounts.length} 个账号凭据！`);
    } catch (err) {
      toast("导出失败: " + err.message, true);
    }
  };

  function cell(row, label, value, className = "") {
    const td = document.createElement("td");
    td.dataset.label = label;
    td.className = className;
    td.textContent = value;
    row.appendChild(td);
    return td;
  }
  function action(container, label, handler, className = "") {
    const btn = document.createElement("button");
    btn.textContent = label;
    btn.className = className;
    btn.addEventListener("click", async () => {
      if (btn.disabled) return;
      btn.disabled = true;
      try {
        await handler();
      } finally {
        btn.disabled = false;
      }
    });
    container.appendChild(btn);
    return btn;
  }
  function tableMessage(tbody, columns, title, detail = "", retry) {
    tbody.replaceChildren();
    const row = document.createElement("tr");
    const td = cell(row, "", "", "empty-state");
    td.colSpan = columns;
    const heading = document.createElement("strong");
    heading.textContent = title;
    td.appendChild(heading);
    const text = document.createElement("span");
    text.textContent = detail;
    td.appendChild(text);
    if (retry) {
      td.appendChild(document.createElement("br"));
      action(
        td,
        columns === 6 && !detail.includes("失败") && title === "还没有账号"
          ? "添加第一个账号"
          : "重试",
        retry,
      );
    }
    tbody.appendChild(row);
  }
  function renderAccounts(accounts) {
    const tbody = $("accounts-table-body");
    tbody.replaceChildren();
    $("uninitialized-alert-banner").classList.toggle(
      "hidden",
      !accounts.some((a) => a.activation_status === "pending"),
    );
    if (!accounts.length) {
      tableMessage(
        tbody,
        6,
        "还没有账号",
        "在「添加账号」中使用短信登录，或手动导入凭据。",
        () => $("sms-phone").focus(),
      );
      return;
    }
    for (const acc of accounts) {
      const row = document.createElement("tr");
      const id = String(acc.id || "");
      cell(
        row,
        "ID",
        id.length > 12 ? `${id.slice(0, 5)}…${id.slice(-4)}` : id,
        "account-id",
      ).title = id;
      cell(row, "账号", acc.label || "默认账号", "account-name");
      cell(
        row,
        "凭据",
        [
          acc.has_refresh_token ? "Refresh ✓" : "",
          acc.has_id_token ? "ID ✓" : "",
        ]
          .filter(Boolean)
          .join(" / ") || "无凭据",
        "mono",
      );
      cell(
        row,
        "到期时间",
        acc.expires_at
          ? new Date(acc.expires_at * 1000).toLocaleString("zh-CN", {
              hour12: false,
            })
          : "未提供 / 按需刷新",
        "mono",
      );
      const status = cell(row, "状态", "", "account-state");
      let text, tone;
      if (acc.activation_status === "pending") {
        text = "待在 App 初始化";
        tone = "warning";
      } else if (acc.activation_status === "invalid") {
        text = "凭据失效";
        tone = "error";
      } else if (!acc.enabled) {
        text = "已停用";
        tone = "off";
      } else if (acc.ready) {
        text = "凭据就绪";
        tone = "good";
      } else {
        text = "待刷新 / 未就绪";
        tone = "warning";
      }
      const badge = document.createElement("span");
      badge.className = `badge ${tone}`;
      badge.textContent = text;
      status.appendChild(badge);
      if (!acc.enabled && acc.activation_status !== "active") {
        const off = document.createElement("small");
        off.textContent = " · 已停用";
        status.appendChild(off);
      }
      const actions = cell(row, "操作", "", "actions-cell");
      const group = document.createElement("div");
      group.className = "row-actions";
      actions.appendChild(group);
      action(group, "重验", () =>
        reauthAccount(acc.id, acc.label || "默认账号", acc.phone_number || ""),
      );
      action(group, "探测", () => checkAccount(acc.id));
      action(group, acc.enabled ? "停用" : "启用", () =>
        toggleAccount(acc.id, !acc.enabled),
      );
      action(group, "删除", () => deleteAccount(acc.id), "danger");
      tbody.appendChild(row);
    }
  }
  window.loadAccounts = async function () {
    try {
      const data = await api("/admin/api/status");
      if (!state.key) return;
      state.status = data;
      renderDashboard(data);
      renderAccounts(data.accounts || []);
    } catch (err) {
      tableMessage(
        $("accounts-table-body"),
        6,
        "账号加载失败",
        err.message,
        loadAccounts,
      );
    }
  };

  window.checkAccount = async function (id) {
    toast("正在向 Wabi 上游执行只读探测…");
    try {
      const res = await api(
        `/admin/api/accounts/${encodeURIComponent(id)}/check`,
        { method: "POST" },
      );
      if (res.ok) {
        toast("Wabi 上游认证有效，账号健康！");
      } else {
        toast(
          `Wabi 上游未通过: ${res.error || res.code || "HTTP " + res.status}`,
          true,
        );
      }
      await loadAccounts();
    } catch (err) {
      toast(err.message, true);
    }
  };

  window.toggleAccount = async function (id, enabled) {
    try {
      await api(`/admin/api/accounts/${encodeURIComponent(id)}`, {
        method: "PATCH",
        body: { enabled },
      });
      toast(`账号已${enabled ? "启用" : "停用"}`);
      await syncStatus();
    } catch (err) {
      toast(err.message, true);
    }
  };

  window.deleteAccount = async function (id) {
    if (!confirm("确定彻底从号池中删除此账号吗？")) return;
    try {
      await api(`/admin/api/accounts/${encodeURIComponent(id)}`, {
        method: "DELETE",
      });
      toast("账号已从号池删除");
      await syncStatus();
    } catch (err) {
      toast(err.message, true);
    }
  };

  // --- API Tokens Modal & Management ---
  window.openTokenModal = function () {
    openModal("token-modal");
    loadTokens();
    if (window.lucide) lucide.createIcons();
  };

  window.closeTokenModal = function () {
    closeModal();
  };

  window.loadTokens = async function () {
    const tbody = $("token-table-body");
    tbody.setAttribute("aria-busy", "true");
    try {
      const tokens = await api("/admin/api/keys");
      if (!state.key) return;
      if (!Array.isArray(tokens)) throw new Error("密钥列表格式异常");
      state.tokens = tokens;
      $("stat-keys").textContent = tokens.length;
      const available = tokens.find((t) => t.enabled);
      $("api-key-text").textContent = available
        ? `${String(available.key).slice(0, 7)}••••${String(available.key).slice(-4)}`
        : "暂无启用的密钥";
      renderCurl();
      if (!tokens.length) {
        tableMessage(
          tbody,
          5,
          "还没有 API Key",
          "创建一个独立密钥，用于客户端接入。",
        );
        return;
      }
      tbody.replaceChildren();
      for (const t of tokens) {
        const row = document.createElement("tr");
        cell(row, "名称", t.name || "未命名密钥", "account-name");
        const val = cell(row, "密钥值", "");
        const wrap = document.createElement("div");
        wrap.className = "token-value";
        const code = document.createElement("code");
        code.textContent = `${String(t.key).slice(0, 7)}••••${String(t.key).slice(-4)}`;
        wrap.appendChild(code);
        const reveal = action(wrap, "显示", () => {
          const visible = reveal.getAttribute("aria-pressed") !== "true";
          reveal.setAttribute("aria-pressed", String(visible));
          reveal.textContent = visible ? "隐藏" : "显示";
          code.textContent = visible
            ? t.key
            : `${String(t.key).slice(0, 7)}••••${String(t.key).slice(-4)}`;
        });
        reveal.setAttribute("aria-pressed", "false");
        action(wrap, "复制", () => copyText(t.key, "已复制 API Key"));
        val.appendChild(wrap);
        cell(row, "状态", t.enabled ? "已启用" : "已停用");
        cell(
          row,
          "创建时间",
          t.created_at
            ? new Date(t.created_at * 1000).toLocaleDateString("zh-CN")
            : "—",
          "mono",
        );
        const td = cell(row, "操作", "");
        const group = document.createElement("div");
        group.className = "row-actions";
        td.appendChild(group);
        action(group, t.enabled ? "停用" : "启用", () =>
          toggleToken(t.id, !t.enabled),
        );
        action(group, "删除", () => deleteToken(t.id, t.name), "danger");
        tbody.appendChild(row);
      }
    } catch (err) {
      state.tokens = [];
      $("stat-keys").textContent = "—";
      $("api-key-text").textContent = "加载失败，点击管理重试";
      tableMessage(tbody, 5, "密钥加载失败", err.message, loadTokens);
    } finally {
      tbody.removeAttribute("aria-busy");
    }
  };

  window.handleCreateToken = async function () {
    const name = $("new-token-name").value.trim();
    const customKey = $("new-token-val").value.trim();
    if (!name) {
      toast("请填写密钥名称", true);
      return;
    }
    const btn = $("btn-create-token");
    if (btn.disabled) return;
    btn.disabled = true;
    try {
      const res = await api("/admin/api/keys", {
        method: "POST",
        body: { name, key: customKey },
      });
      toast("新密钥已创建，可在列表中复制");
      $("new-token-name").value = "";
      $("new-token-val").value = "";
      await loadTokens();
      renderCurl();
    } catch (err) {
      toast(err.message, true);
    } finally {
      btn.disabled = false;
    }
  };

  window.toggleToken = async function (id, enabled) {
    try {
      await api(`/admin/api/keys/${encodeURIComponent(id)}`, {
        method: "PATCH",
        body: { enabled },
      });
      toast("密钥状态已切换");
      await loadTokens();
    } catch (err) {
      toast(err.message, true);
    }
  };

  window.deleteToken = async function (id, name) {
    if (!confirm(`确定删除密钥【${name}】吗？`)) return;
    try {
      await api(`/admin/api/keys/${encodeURIComponent(id)}`, {
        method: "DELETE",
      });
      toast("密钥已删除");
      await loadTokens();
    } catch (err) {
      toast(err.message, true);
    }
  };

  // --- Protocol Modal ---
  window.openProtocolModal = function () {
    openModal("protocol-modal");
    loadProtocolTemplate();
  };

  window.closeProtocolModal = function () {
    closeModal();
  };

  window.loadProtocolTemplate = function () {
    $("protocol-json").value = JSON.stringify(
      {
        event_type_path: "type",
        turn_id_path: "turn_id",
        thread_id_path: "thread_id",
        text_path: "delta.text",
        delta_events: ["turn.delta", "message_delta"],
        done_events: ["turn.done", "message_done"],
        error_events: ["turn.error", "error"],
        mode: "delta",
      },
      null,
      2,
    );
  };

  window.saveProtocolSettings = async function () {
    let mapping;
    try {
      mapping = JSON.parse($("protocol-json").value);
    } catch {
      toast("JSON 语法错误，请检查", true);
      return;
    }
    if (!confirm("将使用编辑框中的 JSON 覆盖现有协议映射，确认保存？")) return;
    const btn = $("btn-save-protocol");
    btn.disabled = true;
    try {
      await api("/admin/api/protocol", { method: "PUT", body: mapping });
      toast("协议映射配置已保存！");
      closeProtocolModal();
      await syncStatus();
    } catch (err) {
      toast(err.message, true);
    } finally {
      btn.disabled = false;
    }
  };

  // --- cURL Example Rendering ---
  function renderCurl() {
    const origin = window.location.origin;
    const cmd = [
      `curl '${origin}/v1/chat/completions' \\`,
      '  -H "Authorization: Bearer YOUR_API_KEY" \\',
      '  -H "Content-Type: application/json" \\',
      `  -d '${JSON.stringify({ model: "wabi", messages: [{ role: "user", content: "你好！" }], stream: true })}'`,
    ].join("\n");
    $("curl-box").textContent = cmd;
    if ($("label-base-url")) $("label-base-url").textContent = `${origin}/v1`;
  }

  window.copyBaseUrl = function () {
    copyText(`${window.location.origin}/v1`, "已复制 Base URL");
  };

  window.copyCurlExample = function () {
    copyText($("curl-box").textContent, "已复制完整 cURL 示例");
  };

  // Modal focus is contained; background content cannot be reached while open.
  let activeModal = null;
  let returnFocus = null;
  function openModal(id) {
    if (activeModal) closeModal();
    returnFocus = document.activeElement;
    activeModal = $(id);
    activeModal.classList.remove("hidden");
    for (const el of document.querySelectorAll(
      ".topbar, .workspace, .lock-screen",
    ))
      el.inert = true;
    document.body.style.overflow = "hidden";
    activeModal.querySelector("input,textarea,button")?.focus();
  }
  function closeModal() {
    if (!activeModal) return;
    activeModal.classList.add("hidden");
    activeModal = null;
    for (const el of document.querySelectorAll(
      ".topbar, .workspace, .lock-screen",
    ))
      el.inert = false;
    document.body.style.overflow = "";
    if (returnFocus?.isConnected) returnFocus.focus();
    returnFocus = null;
  }
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (activeModal) closeModal();
      else {
        closeThemeMenu();
        $("theme-btn").focus();
      }
    }
    if (e.key === "Tab" && activeModal) {
      const els = [
        ...activeModal.querySelectorAll(
          "button:not(:disabled),input:not(:disabled),textarea:not(:disabled),a[href]",
        ),
      ].filter((el) => el.getClientRects().length);
      const first = els[0],
        last = els[els.length - 1];
      if (
        e.shiftKey &&
        (document.activeElement === first ||
          !activeModal.contains(document.activeElement))
      ) {
        e.preventDefault();
        last?.focus();
      } else if (
        !e.shiftKey &&
        (document.activeElement === last ||
          !activeModal.contains(document.activeElement))
      ) {
        e.preventDefault();
        first?.focus();
      }
    }
    if (
      !activeModal &&
      !$("theme-menu").classList.contains("hidden") &&
      ["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)
    ) {
      e.preventDefault();
      const items = [...$("theme-menu").querySelectorAll("button")];
      let i = items.indexOf(document.activeElement);
      i =
        e.key === "Home"
          ? 0
          : e.key === "End"
            ? items.length - 1
            : (i + (e.key === "ArrowUp" ? -1 : 1) + items.length) %
              items.length;
      items[i].focus();
    }
  });
  document.addEventListener("click", (e) => {
    if (!$("theme-menu-container").contains(e.target)) closeThemeMenu();
    if (activeModal && e.target === activeModal) closeModal();
  });
  document.addEventListener("DOMContentLoaded", () => {
    updateThemeIcon();
    icons();
    renderCurl();
    if (state.key) {
      $("admin-key").value = state.key;
      handleUnlock();
    }
  });
})();
