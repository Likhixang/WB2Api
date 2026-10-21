(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = { key: '', epoch: 0, status: null, controllers: new Set(), syncing: false, protocolLoading: false, protocolLoaded: false };
  const views = { overview: '状态概览', accounts: '账户管理', protocol: '协议设置', integration: '接入说明' };
  const pathFields = ['event_type_path', 'turn_id_path', 'thread_id_path', 'text_path'];
  const eventFields = ['delta_events', 'done_events', 'error_events'];
  const emptyProtocol = { event_type_path: '', turn_id_path: '', thread_id_path: '', text_path: '', delta_events: [], done_events: [], error_events: [], mode: 'delta' };

  class RequestError extends Error {
    constructor(message, options = {}) { super(message); Object.assign(this, options); }
  }

  function setText(id, value) { $(id).textContent = value; }
  function message(id, text, error = false) {
    const el = $(id);
    el.textContent = text;
    el.classList.toggle('error', error);
  }
  function badge(id, text, accent = false) {
    setText(id, text);
    $(id).classList.toggle('accent', accent);
  }
  function safeMessage(value, secrets = []) {
    let text = typeof value === 'string' && value.trim() ? value : '请求失败，请检查服务日志（请勿公开凭据）。';
    // Redact known submitted secrets and common bearer / JWT formats even in server errors.
    for (const secret of [state.key, ...secrets].filter((v) => typeof v === 'string' && v.length > 0).sort((a, b) => b.length - a.length)) {
      text = text.split(secret).join('[已隐藏]');
    }
    return text.replace(/Bearer\s+[^\s,;"']+/gi, 'Bearer [已隐藏]')
      .replace(/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g, '[令牌已隐藏]')
      .slice(0, 450);
  }
  function toast(text, error = false) {
    const el = document.createElement('div');
    el.className = error ? 'toast error' : 'toast';
    const content = document.createElement('span');
    content.textContent = text;
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'button quiet';
    close.setAttribute('aria-label', '关闭通知');
    close.textContent = '×';
    close.addEventListener('click', () => el.remove());
    el.append(content, close);
    $('toast-region').append(el);
    // Errors remain dismissible instead of disappearing before they can be read.
    if (!error) window.setTimeout(() => { if (!el.contains(document.activeElement)) el.remove(); }, 7000);
  }
  function report(error, target) {
    if (error.cancelled || error.unauthorized) return;
    const text = error.message || '操作失败，请重试。';
    if (target) message(target, text, true);
    toast(text, true);
  }
  async function busy(button, action, loading = '处理中…') {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = loading;
    button.setAttribute('aria-busy', 'true');
    try { return await action(); }
    finally { button.disabled = false; button.textContent = original; button.removeAttribute('aria-busy'); }
  }

  async function request(path, { method = 'GET', body, auth = true, timeout = 45000 } = {}) {
    const epoch = state.epoch;
    if (auth && !state.key) throw new RequestError('请先解锁控制台。', { cancelled: true });
    const controller = new AbortController();
    state.controllers.add(controller);
    let timedOut = false;
    const timer = window.setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
    const secrets = body && typeof body === 'object'
      ? Object.entries(body).filter(([key]) => /token|key|code/i.test(key)).map(([, value]) => value) : [];
    try {
      const headers = { Accept: 'application/json' };
      if (auth) headers.Authorization = `Bearer ${state.key}`;
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      const response = await fetch(path, {
        method, headers, body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal, cache: 'no-store', credentials: 'omit', redirect: 'error',
      });
      if (epoch !== state.epoch) throw new RequestError('请求已取消。', { cancelled: true });
      if (response.status === 401 && auth) {
        lock('管理员密钥无效或已过期，请重新解锁。');
        throw new RequestError('请重新解锁。', { unauthorized: true });
      }
      const raw = await response.text();
      if (epoch !== state.epoch) throw new RequestError('请求已取消。', { cancelled: true });
      let data = null;
      if (raw.trim()) {
        try { data = JSON.parse(raw); }
        catch { throw new RequestError(`服务返回了非 JSON 响应（HTTP ${response.status}）。`); }
      }
      if (!response.ok) {
        throw new RequestError(safeMessage(data?.error?.message || `请求失败（HTTP ${response.status}）。`, secrets));
      }
      if (data?.error) throw new RequestError(safeMessage(data.error.message, secrets));
      return data;
    } catch (error) {
      if (epoch !== state.epoch) throw new RequestError('请求已取消。', { cancelled: true });
      if (error instanceof RequestError) throw error;
      if (timedOut) throw new RequestError('请求超时。写入或短信请求可能已被服务端处理，请先检查结果，避免重复提交。');
      if (error.name === 'AbortError') throw new RequestError('请求已取消。', { cancelled: true });
      throw new RequestError('无法连接管理服务，请检查网络、同源部署及服务状态。');
    } finally { window.clearTimeout(timer); state.controllers.delete(controller); }
  }

  function resetSensitiveForms() {
    $('unlock-form').reset();
    $('import-form').reset();
    $('sms-form').reset();
    $('challenge-notice').hidden = true;
    $('challenge-notice').textContent = '';
    $('google-flow').hidden = true;
    $('google-redirect-url').value = '';
    for (const id of ['import-message', 'send-code-message', 'sms-message', 'google-verify-message', 'protocol-message']) message(id, '');
  }
  function lock(reason = '') {
    state.epoch += 1;
    state.key = '';
    state.status = null;
    state.syncing = false;
    state.protocolLoaded = false;
    state.protocolLoading = false;
    for (const controller of state.controllers) controller.abort();
    state.controllers.clear();
    resetSensitiveForms();
    $('protocol-editor').value = '';
    $('accounts-list').replaceChildren();
    $('toast-region').replaceChildren();
    $('app-shell').hidden = true;
    $('lock-screen').hidden = false;
    message('unlock-error', reason, Boolean(reason));
    $('admin-key').focus();
  }

  function route(moveFocus = false) {
    const name = location.hash.slice(1);
    const view = Object.hasOwn(views, name) ? name : 'overview';
    for (const key of Object.keys(views)) $('view-' + key).hidden = key !== view;
    for (const link of document.querySelectorAll('[data-view]')) {
      if (link.dataset.view === view) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    setText('breadcrumb-title', views[view]);
    document.title = `${views[view]} · WB2Api`;
    if (moveFocus && state.key && !$('app-shell').hidden) $(view + '-title').focus({ preventScroll: true });
    if (view === 'protocol' && state.key && !state.protocolLoaded && !state.protocolLoading) loadProtocol();
  }

  function formatExpiry(value) {
    if (value === null || value === undefined || value === '') return '未提供到期时间';
    const date = typeof value === 'number' ? new Date(value < 1e12 ? value * 1000 : value) : new Date(value);
    return Number.isNaN(date.getTime()) ? '到期时间格式未知' : date.toLocaleString('zh-CN', { hour12: false });
  }
  function node(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  }
  function renderAccounts(accounts) {
    const list = $('accounts-list');
    list.replaceChildren();
    setText('account-count', `${accounts.length} 个账户`);
    if (!accounts.length) {
      const empty = node('div', 'empty-state');
      const symbol = node('span', 'empty-symbol', '◎');
      symbol.setAttribute('aria-hidden', 'true');
      empty.append(symbol, node('h3', '', '还没有账户'), node('p', '', '导入已有令牌，或展开手机号登录。所有验证操作都由你主动发起。'));
      list.append(empty);
      return;
    }
    for (const account of accounts) {
      const card = node('article', 'account-card');
      const top = node('div', 'account-card-top');
      const heading = node('div');
      const label = typeof account.label === 'string' && account.label ? account.label : '未命名账户';
      heading.append(node('h3', '', label), node('p', 'account-id', `ID · ${String(account.id ?? '未提供')}`));
      top.append(heading, node('span', 'badge', account.enabled ? (account.ready ? '凭据就绪 · 未联调' : '启用 · 待补充凭据') : '已停用'));
      const meta = node('div', 'account-meta');
      meta.append(node('span', '', `Refresh token：${account.has_refresh_token ? '已保存' : '未提供'}`), node('span', '', `ID token：${account.has_id_token ? '已保存' : '未提供'}`), node('span', '', `到期：${formatExpiry(account.expires_at)}`));
      const actions = node('div', 'account-actions');
      const toggle = node('button', 'button secondary', account.enabled ? '停用账户' : '启用账户');
      toggle.type = 'button';
      toggle.setAttribute('aria-label', `${account.enabled ? '停用' : '启用'}账户 ${label}`);
      const check = node('button', 'button quiet', '只读验证');
      check.type = 'button';
      check.setAttribute('aria-label', `只读验证账户 ${label}（可能访问上游网络）`);
      const result = node('p', 'account-check-result');
      result.setAttribute('role', 'status');
      const idValid = typeof account.id === 'string' || typeof account.id === 'number';
      toggle.disabled = !idValid;
      check.disabled = !idValid;
      toggle.addEventListener('click', () => busy(toggle, async () => {
        check.disabled = true;
        try {
          await request(`/admin/api/accounts/${encodeURIComponent(String(account.id))}`, { method: 'PATCH', body: { enabled: !account.enabled } });
          toast('启停请求已提交，正在重新读取账户状态。');
          await syncStatus();
        } catch (error) { report(error); }
        finally { check.disabled = false; }
      }));
      check.addEventListener('click', () => busy(check, async () => {
        toggle.disabled = true;
        result.textContent = '正在执行只读验证，可能访问上游网络…';
        try {
          const data = await request(`/admin/api/accounts/${encodeURIComponent(String(account.id))}/check`, { method: 'POST', body: {} });
          if (typeof data?.ok !== 'boolean') throw new RequestError('账户验证响应缺少 ok 标记。');
          // Profile is intentionally not rendered: upstream profiles may include tokens.
          result.textContent = data.ok ? '账户只读验证通过；尚未验证聊天协议或对话。' : '账户验证未通过，请检查凭据和上游验证要求。';
        } catch (error) { if (!error.cancelled && !error.unauthorized) result.textContent = error.message; report(error); }
        finally { toggle.disabled = false; }
      }, '验证中…'));
      actions.append(toggle, check);
      card.append(top, meta, actions, result);
      list.append(card);
    }
  }
  function renderStatus(data) {
    const accounts = data.accounts;
    const enabled = accounts.filter((a) => a.enabled).length;
    const ready = accounts.filter((a) => a.enabled && a.ready).length;
    setText('stat-total', String(accounts.length));
    setText('stat-enabled', `${enabled} 个启用 · ${accounts.length - enabled} 个停用`);
    setText('stat-ready', String(ready));
    setText('stat-protocol', data.protocol_configured ? '已配置' : '未配置');
    badge('protocol-badge', data.protocol_configured ? '已配置 · 待联调' : '未配置');
    badge('overview-badge', data.protocol_configured && ready > 0 ? '配置就绪 · 待联调' : '待完成配置');
    setText('check-account', ready > 0 ? `${ready} 个启用账户标记为凭据就绪；不代表上游已验证。` : '没有启用且凭据就绪的账户，请导入或检查凭据。');
    setText('check-protocol', data.protocol_configured ? '服务端标记协议已配置；仍需与真实上游事件核对。' : '尚未配置真实事件映射，配置后才可聊天。');
    const version = typeof data.version === 'string' ? data.version : '未提供';
    setText('service-version', version);
    setText('sidebar-version', `版本 ${version}`);
    setText('service-base-url', typeof data.base_url === 'string' ? data.base_url : '未提供');
    setText('service-timeout', data.request_timeout == null ? '未提供' : `${String(data.request_timeout)} 秒`);
    const synced = new Date().toLocaleString('zh-CN', { hour12: false });
    setText('service-sync', synced);
    setText('sync-label', `已同步 ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`);
    $('stale-notice').hidden = true;
    renderAccounts(accounts);
  }
  async function health() {
    badge('health-badge', '检查中');
    try {
      const data = await request('/healthz', { auth: false, timeout: 12000 });
      // HTTP success from the documented health endpoint means process liveness only.
      if (data?.ok === false || data?.status === 'error' || data?.status === 'unhealthy') {
        badge('health-badge', '健康接口报告异常');
      } else badge('health-badge', '进程存活 · 非聊天验证', true);
    } catch (error) { if (!error.cancelled) badge('health-badge', '健康检查失败'); }
  }
  async function syncStatus() {
    if (state.syncing || !state.key) return;
    state.syncing = true;
    const epoch = state.epoch;
    await busy($('refresh-button'), async () => {
      try {
        const data = await request('/admin/api/status');
        if (!data || !Array.isArray(data.accounts) || data.accounts.some((a) => !a || typeof a !== 'object') || typeof data.protocol_configured !== 'boolean') {
          throw new RequestError('管理状态响应格式不符合契约，无法展示。');
        }
        state.status = data;
        renderStatus(data);
        await health();
      } catch (error) {
        if (!error.cancelled && !error.unauthorized) {
          $('stale-notice').hidden = false;
          setText('sync-label', '同步失败');
          badge('overview-badge', '状态未确认');
          badge('health-badge', '未检查最新状态');
          report(error);
        }
      } finally { if (epoch === state.epoch) state.syncing = false; }
    }, '同步中…');
  }

  function validateProtocol() {
    let data;
    try { data = JSON.parse($('protocol-editor').value); }
    catch { throw new RequestError('JSON 无法解析。请检查双引号、逗号与括号；未提交任何配置。'); }
    if (!data || typeof data !== 'object' || Array.isArray(data)) throw new RequestError('配置必须是 JSON 对象。');
    const allowed = new Set([...pathFields, ...eventFields, 'mode']);
    for (const key of Object.keys(data)) if (!allowed.has(key)) throw new RequestError(`未定义的配置字段：${key}`);
    for (const key of pathFields) if (typeof data[key] !== 'string') throw new RequestError(`${key} 必须是字符串字段路径。`);
    for (const key of ['turn_id_path', 'text_path']) if (!data[key].trim()) throw new RequestError(`${key} 不能留空；空结构不能直接保存。`);
    for (const key of eventFields) {
      if (!Array.isArray(data[key]) || data[key].some((v) => typeof v !== 'string' || !v.trim())) throw new RequestError(`${key} 必须是字符串数组，不得包含空事件名。`);
    }

    if (!['delta', 'snapshot'].includes(data.mode)) throw new RequestError('mode 必须是 delta 或 snapshot。');
    return data;
  }
  async function loadProtocol() {
    if (state.protocolLoading || !state.key) return;
    state.protocolLoading = true;
    const epoch = state.epoch;
    await busy($('reload-protocol'), async () => {
      try {
        const data = await request('/admin/api/protocol');
        if (!data || typeof data !== 'object' || Array.isArray(data)) throw new RequestError('协议接口未返回映射 JSON 对象。');
        $('protocol-editor').value = JSON.stringify(data, null, 2);
        state.protocolLoaded = true;
        message('protocol-message', '已读取服务器配置；保存前请核对真实事件样本。');
      } catch (error) { report(error, 'protocol-message'); }
      finally { if (epoch === state.epoch) state.protocolLoading = false; }
    }, '读取中…');
  }

  $('unlock-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const key = $('admin-key').value;
    if (!key.trim()) { message('unlock-error', '请输入管理员密钥。', true); return; }
    busy($('unlock-button'), async () => {
      state.key = key;
      $('admin-key').value = '';
      message('unlock-error', '正在验证管理员权限…');
      try {
        const data = await request('/admin/api/status');
        if (!data || !Array.isArray(data.accounts) || data.accounts.some((a) => !a || typeof a !== 'object') || typeof data.protocol_configured !== 'boolean') throw new RequestError('管理状态响应格式不符合契约，无法解锁。');
        state.status = data;
        renderStatus(data);
        $('lock-screen').hidden = true;
        $('app-shell').hidden = false;
        message('unlock-error', '');
        route(true);
        await health();
      } catch (error) {
        if (!error.unauthorized && !error.cancelled) { state.key = ''; message('unlock-error', error.message, true); $('admin-key').focus(); }
      }
    }, '验证中…');
  });
  $('lock-button').addEventListener('click', () => lock());
  $('refresh-button').addEventListener('click', syncStatus);
  window.addEventListener('hashchange', () => route(true));
  window.addEventListener('pagehide', () => lock());
  $('open-import').addEventListener('click', () => {
    $('import-details').open = true;
    $('import-details').scrollIntoView({ block: 'start', behavior: 'auto' });
    $('import-label').focus({ preventScroll: true });
  });

  $('import-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = new FormData(form);
    const body = Object.fromEntries(['label', 'firebase_api_key', 'refresh_token', 'id_token', 'user_id'].map((key) => [key, String(values.get(key) || '').trim()]));
    if (!body.refresh_token && !body.id_token) { message('import-message', 'Refresh token 与 ID token 至少填写一项。', true); $('refresh-token').focus(); return; }
    busy(form.querySelector('[type="submit"]'), async () => {
      message('import-message', '正在导入…');
      try {
        await request('/admin/api/accounts', { method: 'POST', body });
        form.reset();
        message('import-message', '导入请求已提交，正在重新读取账户列表。');
        toast('账户导入请求成功；请检查列表中的实际状态。');
        await syncStatus();
      } catch (error) { report(error, 'import-message'); }
      finally { for (const key of ['firebase_api_key', 'refresh_token', 'id_token']) body[key] = ''; }
    }, '导入中…');
  });

  $('send-code-button').addEventListener('click', () => {
    const phone = $('phone-number');
    if (!phone.reportValidity()) return;
    const phone_number = phone.value.trim();
    if (!phone_number) return;
    busy($('send-code-button'), async () => {
      message('send-code-message', '正在请求发送短信，请勿重复提交…');
      try {
        const data = await request('/admin/api/auth/send-code', { method: 'POST', body: { phone_number } });
        if (data?.ok !== true) throw new RequestError('服务未确认短信发送成功，请检查手机号及上游要求。');
        message('send-code-message', '服务已确认发送请求，请查收短信；不会自动重发。');
      } catch (error) { report(error, 'send-code-message'); }
    }, '发送中…');
  });
  $('sms-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = new FormData(form);
    const body = Object.fromEntries(['phone_number', 'code', 'firebase_api_key', 'label'].map((key) => [key, String(values.get(key) || '').trim()]));
    if (!body.phone_number || !body.code) { message('sms-message', '请填写手机号和短信验证码。', true); return; }
    busy(form.querySelector('[type="submit"]'), async () => {
      $('challenge-notice').hidden = true;
      $('challenge-notice').textContent = '';
      message('sms-message', '正在验证…');
      try {
        const data = await request('/admin/api/auth/check-code', { method: 'POST', body });
        $('sms-code').value = '';
        if (data?.requires_google === true) {
          state.pendingGoogle = {
            phoneNumber: data.phone_number,
            state: data.google_state,
            label: data.label,
            firebaseApiKey: body.firebase_api_key
          };
          $('google-url-link').href = data.google_url;
          $('google-flow').hidden = false;
          message('sms-message', '已触发 Google 二次验证，请按下方指引完成授权。');
          return;
        }
        if (data?.ok === false && data.requires_verification === true) {
          const challenges = [];
          if (data.details?.mailbox_code_required === true) challenges.push('邮箱验证码');
          // Deliberately allowlist boolean flags only; never print raw details or account tokens.
          $('challenge-notice').textContent = `上游要求进一步验证${challenges.length ? `：${challenges.join('、')}` : '（未提供可展示的验证类型）'}。当前页面不支持完成此验证，请通过上游官方流程处理后再导入凭据。未确认账户导入成功。`;
          $('challenge-notice').hidden = false;
          message('sms-message', '需要额外验证；未展示任何上游敏感详情。');
          return;
        }
        if (data?.ok !== true) throw new RequestError('短信验证未通过，服务未确认导入成功。');
        form.reset();
        $('google-flow').hidden = true;
        message('sms-message', '短信验证成功，正在重新读取账户状态。');
        toast('验证并导入请求成功，账户信息以列表为准。');
        await syncStatus();
      } catch (error) { report(error, 'sms-message'); }
      finally { body.code = ''; body.firebase_api_key = ''; }
    }, '验证中…');
  });

  $('google-verify-button').addEventListener('click', () => {
    const rawCode = ($('google-redirect-url').value || '').trim();
    if (!rawCode) {
      message('google-verify-message', '请粘贴跳转后的网址或授权 Code。', true);
      return;
    }
    const info = state.pendingGoogle || {};
    busy($('google-verify-button'), async () => {
      message('google-verify-message', '正在提交 Google 验证…');
      try {
        const body = {
          phone_number: info.phoneNumber || '',
          state: info.state || '',
          code: rawCode,
          label: info.label || '',
          firebase_api_key: info.firebaseApiKey || ''
        };
        const data = await request('/admin/api/auth/google-verify', { method: 'POST', body });
        if (data?.ok !== true) throw new RequestError('Google 验证未通过，请检查 Code 是否有效。');
        $('sms-form').reset();
        $('google-flow').hidden = true;
        $('google-redirect-url').value = '';
        state.pendingGoogle = null;
        message('sms-message', 'Google 验证通过，已成功导入账户！');
        toast('账户登录成功并已存入列表！');
        await syncStatus();
      } catch (error) {
        report(error, 'google-verify-message');
      }
    }, '验证中…');
  });

  $('reload-protocol').addEventListener('click', () => {
    if ($('protocol-editor').value && !window.confirm('重新读取会覆盖尚未保存的编辑，是否继续？')) return;
    loadProtocol();
  });
  $('protocol-template').addEventListener('click', () => {
    if ($('protocol-editor').value && !window.confirm('载入空结构会替换当前编辑内容，不会保存到服务器。是否继续？')) return;
    $('protocol-editor').value = JSON.stringify(emptyProtocol, null, 2);
    state.protocolLoaded = true;
    message('protocol-message', '已载入空结构，尚未保存。请按真实样本填写所有路径与事件列表。');
    $('protocol-editor').focus();
  });
  $('validate-protocol').addEventListener('click', () => {
    try { validateProtocol(); message('protocol-message', 'JSON 结构检查通过；不代表真实事件匹配。未提交配置。'); }
    catch (error) { message('protocol-message', error.message, true); }
  });
  $('protocol-form').addEventListener('submit', (event) => {
    event.preventDefault();
    let mapping;
    try { mapping = validateProtocol(); }
    catch (error) { message('protocol-message', error.message, true); return; }
    busy($('save-protocol'), async () => {
      $('reload-protocol').disabled = true;
      $('protocol-template').disabled = true;
      const source = $('protocol-editor').value;
      message('protocol-message', '正在保存…');
      try {
        await request('/admin/api/protocol', { method: 'PUT', body: mapping });
        // Read back the exact target before claiming the new configuration was saved.
        const saved = await request('/admin/api/protocol');
        if (!saved || typeof saved !== 'object' || Array.isArray(saved)) throw new RequestError('保存请求已提交，但读取确认失败；请重新读取配置，不要重复提交。');
        const mismatched = [...pathFields, ...eventFields, 'mode'].some((key) => JSON.stringify(saved[key]) !== JSON.stringify(mapping[key]));
        if (mismatched) throw new RequestError('保存请求已提交，但服务器读回值与提交值不同。请重新读取配置核对。');
        if ($('protocol-editor').value === source) $('protocol-editor').value = JSON.stringify(saved, null, 2);
        message('protocol-message', '配置已保存并读回确认；仍需真实对话联调。');
        toast('协议配置已保存并确认，尚未验证聊天。');
        await syncStatus();
      } catch (error) { report(error, 'protocol-message'); }
      finally { $('reload-protocol').disabled = false; $('protocol-template').disabled = false; }
    }, '保存中…');
  });

  const clientBase = location.origin + '/v1';
  setText('client-base-url', clientBase);
  // Quote the origin defensively for the shell; never interpolate user-provided credentials.
  const quote = (text) => "'" + text.replace(/'/g, "'\\''") + "'";
  const curl = [
    `curl ${quote(clientBase + '/chat/completions')} \\`,
    "  -H 'Authorization: Bearer YOUR_API_KEY' \\",
    "  -H 'Content-Type: application/json' \\",
    "  --no-buffer \\",
    `  -d '${JSON.stringify({ model: 'wabi', messages: [{ role: 'user', content: '你好' }], stream: true }, null, 2)}'`,
  ].join('\n');
  setText('curl-example', curl);
  $('copy-curl').addEventListener('click', async () => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(curl);
      toast('已复制接入示例；不含真实密钥。');
    } catch {
      const range = document.createRange();
      range.selectNodeContents($('curl-example'));
      const selection = window.getSelection();
      if (selection) { selection.removeAllRanges(); selection.addRange(range); }
      toast('浏览器不允许写入剪贴板，已选中示例，请手动复制。', true);
    }
  });
  route();
  $('admin-key').disabled = false;
  $('unlock-button').disabled = false;
  $('admin-key').focus();
})();
