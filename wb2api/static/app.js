// WB2Api Application Script - Compatible with Liber2Api UX Style
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = {
    key: localStorage.getItem('wb2api_admin_key') || '',
    status: null,
    pendingGoogle: null,
    tokens: []
  };

  // --- Theme Management ---
  window.setTheme = function(theme) {
    localStorage.setItem("wb2api_theme", theme);
    const isDark = theme === "dark" || (theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    if (isDark) {
      document.documentElement.classList.add("dark");
    } else {
      document.documentElement.classList.remove("dark");
    }
    updateThemeIcon();
    const menu = $('theme-menu');
    if (menu) menu.classList.add('hidden');
  };

  window.toggleThemeMenu = function() {
    const menu = $('theme-menu');
    if (menu) menu.classList.toggle('hidden');
  };

  function updateThemeIcon() {
    const theme = localStorage.getItem("wb2api_theme") || "dark";
    for (const t of ['dark', 'light', 'system']) {
      const el = $(`theme-icon-${t}`);
      if (el) el.classList.toggle('hidden', t !== theme);
    }
  }

  // --- Toast Notifications ---
  window.toast = function(message, isError = false) {
    const container = $('toast-container');
    if (!container) return;
    const toast = document.createElement('div');
    toast.className = `flex items-center space-x-2 px-4 py-2.5 rounded-xl shadow-lg border text-xs font-medium transition-all duration-300 pointer-events-auto ${
      isError
        ? 'bg-rose-50 dark:bg-rose-950/90 text-rose-700 dark:text-rose-200 border-rose-200 dark:border-rose-800'
        : 'bg-white dark:bg-slate-900 text-slate-800 dark:text-slate-100 border-slate-200 dark:border-slate-800'
    }`;
    toast.innerHTML = `<i data-lucide="${isError ? 'alert-circle' : 'check-circle'}" class="w-4 h-4 ${isError ? 'text-rose-500' : 'text-emerald-400'}"></i><span>${message}</span>`;
    container.appendChild(toast);
    if (window.lucide) lucide.createIcons({ root: toast });
    setTimeout(() => {
      toast.classList.add('opacity-0', 'translate-y-2');
      setTimeout(() => toast.remove(), 300);
    }, 3000);
  };

  window.copyText = async function(text, label = '已复制到剪贴板') {
    try {
      await navigator.clipboard.writeText(text);
      toast(label);
    } catch {
      toast('复制失败，请手动选取复制', true);
    }
  };

  // --- HTTP Helper ---
  async function api(path, { method = 'GET', body, auth = true } = {}) {
    const headers = { 'Accept': 'application/json' };
    if (auth) {
      if (!state.key) throw new Error('请先解锁控制台');
      headers['Authorization'] = `Bearer ${state.key}`;
    }
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
    }
    const res = await fetch(path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined
    });
    if (res.status === 401 && auth) {
      lock('管理员密钥无效或已过期，请重新解锁');
      throw new Error('未授权，请重新解锁');
    }
    const text = await res.text();
    let data = null;
    if (text) {
      try { data = JSON.parse(text); }
      catch { throw new Error(`服务端异常 (HTTP ${res.status})`); }
    }
    if (!res.ok) {
      const msg = data?.detail || data?.error?.message || data?.message || `请求失败 (HTTP ${res.status})`;
      throw new Error(msg);
    }
    return data;
  }

  // --- Lock / Unlock ---
  window.lock = function(reason = '') {
    state.key = '';
    localStorage.removeItem('wb2api_admin_key');
    $('lock-screen').classList.remove('hidden');
    $('app-shell').classList.add('hidden');
    $('nav-actions').classList.add('hidden');
    $('btn-logout').classList.add('hidden');
    const errBox = $('unlock-error');
    if (reason) {
      errBox.textContent = reason;
      errBox.classList.remove('hidden');
    } else {
      errBox.classList.add('hidden');
    }
    if (window.lucide) lucide.createIcons();
  };

  window.handleUnlock = async function(e) {
    if (e) e.preventDefault();
    const key = $('admin-key').value.trim();
    if (!key) return;
    const btn = $('btn-unlock');
    btn.disabled = true;
    btn.innerHTML = `<i data-lucide="loader" class="w-4 h-4 animate-spin"></i><span>解锁中…</span>`;
    if (window.lucide) lucide.createIcons({ root: btn });
    $('unlock-error').classList.add('hidden');
    try {
      state.key = key;
      const status = await api('/admin/api/status');
      state.status = status;
      localStorage.setItem('wb2api_admin_key', key);
      $('lock-screen').classList.add('hidden');
      $('app-shell').classList.remove('hidden');
      $('nav-actions').classList.remove('hidden');
      $('btn-logout').classList.remove('hidden');
      renderDashboard(status);
      await loadAccounts();
      await loadTokens();
      renderCurl();
      toast('控制台已解锁');
    } catch (err) {
      state.key = '';
      $('unlock-error').textContent = err.message;
      $('unlock-error').classList.remove('hidden');
    } finally {
      btn.disabled = false;
      btn.innerHTML = `<i data-lucide="key-round" class="w-4 h-4"></i><span>解锁控制台</span>`;
      if (window.lucide) lucide.createIcons();
    }
  };

  // --- Dashboard Status & Health ---
  window.syncStatus = async function() {
    try {
      const status = await api('/admin/api/status');
      state.status = status;
      renderDashboard(status);
      await loadAccounts();
      await loadTokens();
      toast('状态已刷新');
    } catch (err) {
      toast(err.message, true);
    }
  };

  function renderDashboard(status) {
    const accs = status.accounts || [];
    const total = accs.length;
    const ready = accs.filter(a => a.enabled && a.ready).length;
    $('stat-total').textContent = total;
    $('stat-ready').textContent = ready;
    $('stat-protocol').textContent = status.protocol_configured ? '已配置' : '未配置';
    $('stat-protocol').className = `text-2xl font-bold ${status.protocol_configured ? 'text-indigo-400' : 'text-slate-400'}`;
  }

  // --- Login Tabs ---
  window.switchLoginTab = function(tab) {
    const isPhone = tab === 'phone';
    $('pane-phone').classList.toggle('hidden', !isPhone);
    $('pane-token').classList.toggle('hidden', isPhone);
    $('tab-phone').className = `px-2.5 py-1 rounded-md text-xs transition ${isPhone ? 'bg-white dark:bg-slate-700 font-medium shadow-sm' : 'text-slate-500 hover:text-slate-800 dark:hover:text-slate-200'}`;
    $('tab-token').className = `px-2.5 py-1 rounded-md text-xs transition ${!isPhone ? 'bg-white dark:bg-slate-700 font-medium shadow-sm' : 'text-slate-500 hover:text-slate-800 dark:hover:text-slate-200'}`;
    $('google-challenge-box').classList.add('hidden');
    hideMsg();
  };

  function showMsg(text, isError = false) {
    const box = $('form-msg');
    box.textContent = text;
    box.className = `text-xs text-center rounded-lg p-2.5 mt-4 ${isError ? 'bg-rose-50 dark:bg-rose-950/50 text-rose-600 border border-rose-200 dark:border-rose-800' : 'bg-emerald-50 dark:bg-emerald-950/50 text-emerald-600 border border-emerald-200 dark:border-emerald-800'}`;
    box.classList.remove('hidden');
  }

  function hideMsg() {
    $('form-msg').classList.add('hidden');
  }

  // --- Phone & Google Verification ---
  window.handleSendSms = async function() {
    const phone = $('sms-phone').value.trim();
    if (!phone) { showMsg('请输入手机号 (含国际区号，如 +86138...)', true); return; }
    const btn = $('btn-send-sms');
    btn.disabled = true;
    btn.textContent = '发送中…';
    hideMsg();
    try {
      await api('/admin/api/auth/send-code', { method: 'POST', body: { phone_number: phone } });
      showMsg('验证码已发送至手机，请查收');
      toast('验证码短信已发送');
    } catch (err) {
      showMsg(err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = '重新发送';
    }
  };

  window.handleVerifySms = async function() {
    const phone = $('sms-phone').value.trim();
    const code = $('sms-code').value.trim();
    const label = $('sms-label').value.trim() || '手机账号';
    if (!phone || !code) { showMsg('请填写手机号和短信验证码', true); return; }
    const btn = $('btn-verify-sms');
    btn.disabled = true;
    btn.innerHTML = `<i data-lucide="loader" class="w-4 h-4 animate-spin"></i><span>验证中…</span>`;
    if (window.lucide) lucide.createIcons({ root: btn });
    hideMsg();
    $('google-challenge-box').classList.add('hidden');
    try {
      const res = await api('/admin/api/auth/check-code', {
        method: 'POST',
        body: { phone_number: phone, code, label, account_id: state.reauthAccountId || '' }
      });
      if (res.requires_google) {
        state.pendingGoogle = { phone, state: res.google_state, label, accountId: state.reauthAccountId || '' };
        $('btn-google-auth-link').href = res.google_url;
        $('google-challenge-box').classList.remove('hidden');
        if (window.lucide) lucide.createIcons();
        showMsg('该账号触发了 Wabi Google 二次验证，请按下方 3 步完成授权。');
        toast('请完成 Google 二次验证');
        return;
      }
      toast(state.reauthAccountId ? '账号凭据已原地更新成功！' : '短信验证成功，账号已入库！');
      state.reauthAccountId = null;
      $('sms-code').value = '';
      await syncStatus();
    } catch (err) {
      showMsg(err.message, true);
    } finally {
      btn.disabled = false;
      btn.innerHTML = `<i data-lucide="check" class="w-4 h-4"></i><span>验证并导入</span>`;
      if (window.lucide) lucide.createIcons();
    }
  };

  window.handleCompleteGoogleVerify = async function() {
    const rawUrl = $('google-code-input').value.trim();
    if (!rawUrl) { showMsg('请粘贴 Google 授权跳转后的完整网址或 code', true); return; }
    const info = state.pendingGoogle || {};
    const btn = $('btn-submit-google');
    btn.disabled = true;
    btn.textContent = '提交验证中…';
    hideMsg();
    try {
      await api('/admin/api/auth/google-verify', {
        method: 'POST',
        body: {
          phone_number: info.phone || $('sms-phone').value.trim(),
          state: info.state || '',
          code: rawUrl,
          label: info.label || 'Google Phone Account',
          account_id: info.accountId || state.reauthAccountId || ''
        }
      });
      toast('Google 验证成功，账号凭据已更新！');
      $('google-challenge-box').classList.add('hidden');
      $('sms-code').value = '';
      $('google-code-input').value = '';
      state.pendingGoogle = null;
      state.reauthAccountId = null;
      await syncStatus();
    } catch (err) {
      showMsg(err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = '第 3 步：完成验证并激活入库';
    }
  };

  // --- Manual Token Import ---
  window.handleManualImport = async function() {
    const label = $('import-label').value.trim() || '手动导入';
    const rt = $('import-rt').value.trim();
    const idToken = $('import-id-token').value.trim();
    if (!rt && !idToken) { showMsg('请至少填入 Refresh Token 或 ID Token', true); return; }
    const btn = $('btn-manual-import');
    btn.disabled = true;
    btn.textContent = '导入中…';
    hideMsg();
    try {
      await api('/admin/api/accounts', {
        method: 'POST',
        body: { label, refresh_token: rt, id_token: idToken }
      });
      toast('账号凭证导入成功！');
      $('import-rt').value = '';
      $('import-id-token').value = '';
      await syncStatus();
    } catch (err) {
      showMsg(err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = '导入账号凭证';
    }
  };

  window.reauthAccount = function(id, label, phone) {
    state.reauthAccountId = id;
    switchLoginTab('phone');
    if (phone) $('sms-phone').value = phone;
    $('sms-label').value = label;
    showMsg(`正在为账号【${label}】重新验证并更新凭据。验证通过后将原地更新该账号。`);
    $('pane-phone').scrollIntoView({ block: 'start', behavior: 'smooth' });
    $('sms-phone').focus();
    toast(`已进入账号【${label}】重新验证模式`);
  };

  // --- Account Pool Table ---
  window.loadAccounts = async function() {
    const tbody = $('accounts-table-body');
    if (!tbody) return;
    try {
      const data = await api('/admin/api/status');
      const accounts = data.accounts || [];
      if (!accounts.length) {
        tbody.innerHTML = `<tr><td colspan="6" class="py-8 text-center text-slate-400">号池中暂无可用账号，请通过上方添加账号</td></tr>`;
        return;
      }
      tbody.innerHTML = accounts.map(acc => {
        const idShort = acc.id.slice(0, 8) + '…';
        const readyBadge = acc.enabled
          ? (acc.ready
              ? `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20"><i data-lucide="check" class="w-3 h-3"></i>凭据就绪</span>`
              : `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium bg-amber-500/10 text-amber-400 border border-amber-500/20"><i data-lucide="clock" class="w-3 h-3"></i>待刷新</span>`)
          : `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium bg-slate-500/10 text-slate-400 border border-slate-500/20">已停用</span>`;
        const exp = acc.expires_at ? new Date(acc.expires_at * 1000).toLocaleString('zh-CN', { hour12: false }) : '永久/自动续期';
        return `
          <tr class="hover:bg-slate-50/50 dark:hover:bg-slate-900/50 transition">
            <td class="py-3 px-4 font-mono text-slate-500"><span title="${acc.id}">${idShort}</span></td>
            <td class="py-3 px-4 font-medium text-slate-900 dark:text-slate-100">${acc.label || '默认账号'}</td>
            <td class="py-3 px-4 font-mono text-[11px] text-slate-500">${acc.has_refresh_token ? 'Refresh ✓' : ''} ${acc.has_id_token ? 'ID ✓' : ''}</td>
            <td class="py-3 px-4 font-mono text-[11px] text-slate-400">${exp}</td>
            <td class="py-3 px-4">${readyBadge}</td>
            <td class="py-3 px-4 text-right space-x-1.5">
              <button onclick="reauthAccount('${acc.id}', '${acc.label}', '${acc.phone_number || ''}')" class="px-2.5 py-1 text-[11px] rounded-lg bg-sky-50 dark:bg-sky-950/50 hover:bg-sky-100 dark:hover:bg-sky-900/50 text-sky-600 dark:text-sky-400 border border-sky-200 dark:border-sky-800/40 transition">重新验证</button>
              <button onclick="checkAccount('${acc.id}')" class="px-2.5 py-1 text-[11px] rounded-lg border border-slate-200 dark:border-slate-800 hover:bg-slate-100 dark:hover:bg-slate-800 transition" title="只读探测 Wabi 上游状态">探测</button>
              <button onclick="toggleAccount('${acc.id}', ${!acc.enabled})" class="px-2.5 py-1 text-[11px] rounded-lg border border-slate-200 dark:border-slate-800 hover:bg-slate-100 dark:hover:bg-slate-800 transition">${acc.enabled ? '停用' : '启用'}</button>
              <button onclick="deleteAccount('${acc.id}')" class="px-2.5 py-1 text-[11px] rounded-lg border border-rose-200 dark:border-rose-900/40 text-rose-500 hover:bg-rose-50 dark:hover:bg-rose-950/40 transition">删除</button>
            </td>
          </tr>
        `;
      }).join('');
      if (window.lucide) lucide.createIcons({ root: tbody });
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="6" class="py-6 text-center text-rose-500">${err.message}</td></tr>`;
    }
  };

  window.checkAccount = async function(id) {
    toast('正在向 Wabi 上游执行只读探测…');
    try {
      const res = await api(`/admin/api/accounts/${id}/check`, { method: 'POST' });
      if (res.ok) {
        toast('Wabi 上游认证有效，账号健康！');
      } else {
        toast('Wabi 上游返回异常', true);
      }
    } catch (err) {
      toast(err.message, true);
    }
  };

  window.toggleAccount = async function(id, enabled) {
    try {
      await api(`/admin/api/accounts/${id}`, { method: 'PATCH', body: { enabled } });
      toast(`账号已${enabled ? '启用' : '停用'}`);
      await syncStatus();
    } catch (err) {
      toast(err.message, true);
    }
  };

  window.deleteAccount = async function(id) {
    if (!confirm('确定彻底从号池中删除此账号吗？')) return;
    try {
      await api(`/admin/api/accounts/${id}`, { method: 'DELETE' });
      toast('账号已从号池删除');
      await syncStatus();
    } catch (err) {
      toast(err.message, true);
    }
  };

  // --- API Tokens Modal & Management ---
  window.openTokenModal = function() {
    $('token-modal').classList.remove('hidden');
    loadTokens();
    if (window.lucide) lucide.createIcons();
  };

  window.closeTokenModal = function() {
    $('token-modal').classList.add('hidden');
  };

  window.loadTokens = async function() {
    const tbody = $('token-table-body');
    if (!tbody) return;
    try {
      const tokens = await api('/admin/api/keys');
      state.tokens = tokens;
      $('stat-keys').textContent = tokens.length;
      if (!tokens.length) {
        tbody.innerHTML = `<tr><td colspan="5" class="py-6 text-center text-slate-400">暂无自定义 API Key，请在上方添加</td></tr>`;
        return;
      }
      tbody.innerHTML = tokens.map(t => {
        const date = t.created_at ? new Date(t.created_at * 1000).toLocaleDateString('zh-CN') : '-';
        return `
          <tr class="hover:bg-slate-50/50 dark:hover:bg-slate-900/50 transition">
            <td class="py-2.5 px-3 font-medium text-slate-800 dark:text-slate-200">${t.name}</td>
            <td class="py-2.5 px-3 font-mono text-[11px] text-slate-600 dark:text-slate-300">
              <span class="inline-flex items-center gap-1.5">
                <span>${t.key}</span>
                <button onclick="copyText('${t.key}', '已复制 API Key')" class="text-sky-500 hover:text-sky-400"><i data-lucide="copy" class="w-3 h-3"></i></button>
              </span>
            </td>
            <td class="py-2.5 px-3">${t.enabled ? '<span class="text-emerald-500 font-medium">启用</span>' : '<span class="text-slate-400">停用</span>'}</td>
            <td class="py-2.5 px-3 text-slate-400 text-[11px]">${date}</td>
            <td class="py-2.5 px-3 text-right space-x-1">
              <button onclick="toggleToken('${t.id}', ${!t.enabled})" class="text-[11px] text-slate-500 hover:text-slate-800 dark:hover:text-slate-200">${t.enabled ? '停用' : '启用'}</button>
              <span class="text-slate-300 dark:text-slate-700">·</span>
              <button onclick="deleteToken('${t.id}', '${t.name}')" class="text-[11px] text-rose-500 hover:text-rose-400">删除</button>
            </td>
          </tr>
        `;
      }).join('');
      if (window.lucide) lucide.createIcons({ root: tbody });
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="5" class="py-4 text-center text-rose-500">${err.message}</td></tr>`;
    }
  };

  window.handleCreateToken = async function() {
    const name = $('new-token-name').value.trim();
    const customKey = $('new-token-val').value.trim();
    if (!name) { toast('请填写密钥名称', true); return; }
    try {
      const res = await api('/admin/api/keys', {
        method: 'POST',
        body: { name, key: customKey }
      });
      toast(`新密钥创建成功：${res.key}`);
      $('new-token-name').value = '';
      $('new-token-val').value = '';
      await loadTokens();
      renderCurl();
    } catch (err) {
      toast(err.message, true);
    }
  };

  window.toggleToken = async function(id, enabled) {
    try {
      await api(`/admin/api/keys/${id}`, { method: 'PATCH', body: { enabled } });
      toast('密钥状态已切换');
      await loadTokens();
    } catch (err) {
      toast(err.message, true);
    }
  };

  window.deleteToken = async function(id, name) {
    if (!confirm(`确定删除密钥【${name}】吗？`)) return;
    try {
      await api(`/admin/api/keys/${id}`, { method: 'DELETE' });
      toast('密钥已删除');
      await loadTokens();
    } catch (err) {
      toast(err.message, true);
    }
  };

  // --- Protocol Modal ---
  window.openProtocolModal = async function() {
    $('protocol-modal').classList.remove('hidden');
    try {
      const res = await api('/admin/api/status');
      // If protocol configured, load from server or template
      $('protocol-json').value = JSON.stringify({
        event_type_path: "type",
        turn_id_path: "turn_id",
        thread_id_path: "thread_id",
        text_path: "delta.text",
        delta_events: ["turn.delta", "delta"],
        done_events: ["turn.done", "done"],
        error_events: ["turn.error", "error"],
        mode: "delta"
      }, null, 2);
    } catch {
      loadProtocolTemplate();
    }
    if (window.lucide) lucide.createIcons();
  };

  window.closeProtocolModal = function() {
    $('protocol-modal').classList.add('hidden');
  };

  window.loadProtocolTemplate = function() {
    $('protocol-json').value = JSON.stringify({
      event_type_path: "type",
      turn_id_path: "turn_id",
      thread_id_path: "thread_id",
      text_path: "delta.text",
      delta_events: ["turn.delta", "message_delta"],
      done_events: ["turn.done", "message_done"],
      error_events: ["turn.error", "error"],
      mode: "delta"
    }, null, 2);
  };

  window.saveProtocolSettings = async function() {
    let mapping;
    try {
      mapping = JSON.parse($('protocol-json').value);
    } catch {
      toast('JSON 语法错误，请检查', true);
      return;
    }
    const btn = $('btn-save-protocol');
    btn.disabled = true;
    try {
      await api('/admin/api/protocol', { method: 'PUT', body: mapping });
      toast('协议映射配置已保存！');
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
    const key = (state.tokens && state.tokens[0] ? state.tokens[0].key : 'sk-wb-your-key-here');
    const cmd = `curl ${origin}/v1/chat/completions \\\\
  -H "Authorization: Bearer ${key}" \\\\
  -H "Content-Type: application/json" \\\\
  -d '{
    "model": "wabi",
    "messages": [{"role": "user", "content": "你好！"}],
    "stream": true
  }'`;
    $('curl-box').textContent = cmd;
    if ($('label-base-url')) $('label-base-url').textContent = `${origin}/v1`;
  }

  window.copyBaseUrl = function() {
    copyText(`${window.location.origin}/v1`, '已复制 Base URL');
  };

  window.copyCurlExample = function() {
    copyText($('curl-box').textContent, '已复制完整 cURL 示例');
  };

  // --- Init ---
  document.addEventListener('DOMContentLoaded', () => {
    updateThemeIcon();
    if (window.lucide) lucide.createIcons();
    // Auto-login if key exists
    if (state.key) {
      $('admin-key').value = state.key;
      handleUnlock();
    }
  });

})();
