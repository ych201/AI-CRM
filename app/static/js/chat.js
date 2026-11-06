/* SeaChat 客户端聊天页：手机号进入 + SSE 流式对话 */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var PHONE_RE = /^1[3-9]\d{9}$/;
  var STAGE = { new: '新客', contacted: '已接触', qualified: '有明确意向', won: '已成交', lost: '已流失' };
  var LEVEL = { cold: '冷', warm: '中', hot: '高' };
  var ROUTE = {
    rag_market: '职业咨询', rag_course: '课程咨询', web_search: '实时资讯',
    sql: '数据查询', direct: '直聊', rag_miss: '知识库未命中'
  };
  var PROFILE_ROWS = [
    ['current_role', '身份'], ['goal', '目标'], ['budget', '预算'],
    ['time_window', '时间窗'], ['concerns', '顾虑']
  ];

  var state = {
    phone: '', conversationId: null, customer: null,
    sending: false, controller: null, lastText: '', stick: true
  };

  /* ------------------------------------------------------------ 基础 */
  function toast(msg, kind) {
    var host = $('toastHost');
    if (!host) return;
    var el = document.createElement('div');
    el.className = 'toast' + (kind ? ' is-' + kind : '');
    el.textContent = msg;
    host.appendChild(el);
    setTimeout(function () { el.remove(); }, 3200);
  }

  function showLogin(message) {
    $('loginError').textContent = message || '';
    $('loginView').classList.remove('hidden');
    $('chatView').classList.add('hidden');
    $('panelToggle').classList.add('hidden');
    var input = $('phoneInput');
    if (input) input.focus();
  }

  function showChat() {
    $('loginView').classList.add('hidden');
    $('chatView').classList.remove('hidden');
    $('panelToggle').classList.remove('hidden');
  }

  /* ------------------------------------------------------------ 客户卡 */
  function renderCustomer(c) {
    if (!c) return;
    state.customer = c;
    $('topbarPhone').textContent = c.phone || '';
    $('cardPhone').textContent = c.phone || '—';
    $('cardMeta').textContent = (STAGE[c.stage] || c.stage || '新客') + ' · ' + (LEVEL[c.intent_level] || '冷');
    $('avatar').textContent = (c.name || '客').slice(0, 1);

    var kv = $('profileKv');
    kv.innerHTML = '';
    PROFILE_ROWS.forEach(function (row) {
      var value = (c.profile || {})[row[0]];
      if (Array.isArray(value)) value = value.join('、');
      var dt = document.createElement('dt');
      dt.textContent = row[1];
      var dd = document.createElement('dd');
      dd.textContent = value || '—';
      if (!value) dd.className = 'muted';
      kv.appendChild(dt);
      kv.appendChild(dd);
    });

    var row = $('tagRow');
    row.innerHTML = '';
    var tags = c.tags || [];
    tags.forEach(function (tag) {
      var chip = document.createElement('span');
      chip.className = 'chip';
      chip.textContent = tag;
      row.appendChild(chip);
    });
    $('noTags').classList.toggle('hidden', tags.length > 0);
  }

  function loadCard(withMessages) {
    return fetch('/api/customer-card?phone=' + encodeURIComponent(state.phone) +
      (withMessages ? '&with_messages=1' : ''))
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (data) {
        if (!data) return;
        renderCustomer(data);
        if (withMessages && data.recent_messages && data.recent_messages.length) {
          renderHistory(data.recent_messages);
        }
      })
      .catch(function () { /* 静默失败，不打扰客户 */ });
  }

  /* ------------------------------------------------------------ 消息渲染 */
  function addMessage(role, text, opts) {
    opts = opts || {};
    var root = document.createElement('div');
    root.className = 'msg msg-' + role;

    if (role === 'assistant') {
      var meta = document.createElement('div');
      meta.className = 'msg-meta';
      var name = document.createElement('span');
      name.className = 'msg-name';
      name.textContent = '小海';
      meta.appendChild(name);
      var badge = document.createElement('span');
      badge.className = 'badge hidden';
      meta.appendChild(badge);
      if (opts.time) {
        var time = document.createElement('span');
        time.textContent = opts.time;
        meta.appendChild(time);
      }
      root.appendChild(meta);
    }

    var bubble = document.createElement('div');
    bubble.className = 'bubble';
    var textEl = document.createElement('span');
    textEl.textContent = text || '';
    bubble.appendChild(textEl);

    var typing = null;
    if (role === 'assistant' && !text && opts.pending) {
      typing = document.createElement('span');
      typing.className = 'typing';
      typing.innerHTML = '<i></i><i></i><i></i>';
      bubble.appendChild(typing);
    }
    root.appendChild(bubble);

    var tools = document.createElement('div');
    tools.className = 'tool-chips';
    root.appendChild(tools);

    var slow = document.createElement('div');
    slow.className = 'slow-hint hidden';
    root.appendChild(slow);

    $('messages').appendChild(root);

    var node = {
      root: root, badge: badge, bubble: bubble, textEl: textEl,
      tools: tools, slow: slow, typing: typing, hasText: false
    };
    if (opts.route) setBadge(node, opts.route);
    scrollToBottom(false);
    return node;
  }

  function setBadge(node, route) {
    if (!node || !node.badge) return;
    node.badge.className = 'badge badge-' + (route || 'direct');
    node.badge.textContent = ROUTE[route] || route || '';
  }

  function addHint(node, text) {
    var hint = document.createElement('div');
    hint.className = 'slow-hint';
    hint.textContent = text;
    node.root.appendChild(hint);
  }

  function addToolChip(node, data) {
    var chip = document.createElement('span');
    chip.className = 'tool-chip';
    var detail = '';
    if (data.args) {
      detail = data.args.purpose || data.args.query || data.args.sql || '';
    }
    chip.textContent = data.name + (detail ? ' · ' + String(detail).slice(0, 26) : '') +
      (typeof data.ms === 'number' ? ' · ' + data.ms + 'ms' : '');
    node.tools.appendChild(chip);
  }

  function renderCitations(node, items) {
    if (!items || !items.length) return;
    var box = node.root.querySelector('.citations');
    if (!box) {
      box = document.createElement('details');
      box.className = 'citations';
      box.innerHTML = '<summary></summary><div class="cite-list"></div>';
      node.root.appendChild(box);
    }
    var summary = box.querySelector('summary');
    summary.textContent = '▸ 引用 ' + items.length + ' 条';
    var list = box.querySelector('.cite-list');
    list.innerHTML = '';
    items.forEach(function (item) {
      var wrap = document.createElement('div');
      wrap.className = 'cite';
      var title = document.createElement('strong');
      title.textContent = item.title || item.doc_id || '来源';
      wrap.appendChild(title);
      if (typeof item.score === 'number') {
        var score = document.createElement('span');
        score.className = 'score';
        score.textContent = '  ' + item.score.toFixed(2);
        wrap.appendChild(score);
      }
      var snippet = document.createElement('p');
      snippet.textContent = item.snippet || '';
      wrap.appendChild(snippet);
      if (item.doc_id && /^https?:/.test(item.doc_id)) {
        var link = document.createElement('a');
        link.href = item.doc_id;
        link.target = '_blank';
        link.rel = 'noreferrer';
        link.textContent = '打开来源';
        link.style.fontSize = '11px';
        wrap.appendChild(link);
      }
      list.appendChild(wrap);
    });
  }

  function renderHistory(messages) {
    var host = $('messages');
    host.innerHTML = '';
    messages.forEach(function (m) {
      var node = addMessage(m.role, m.content, {
        time: (m.created_at || '').slice(11, 16),
        route: m.route
      });
      if (m.role === 'assistant' && m.citations && m.citations.length) {
        renderCitations(node, m.citations);
      }
    });
    scrollToBottom(true);
  }

  function scrollToBottom(force) {
    var host = $('messages');
    if (!host) return;
    if (force || state.stick) host.scrollTop = host.scrollHeight;
  }

  /* ------------------------------------------------------------ 发送 */
  function setSendingUI(sending) {
    var btn = $('sendBtn');
    state.sending = sending;
    if (sending) {
      btn.textContent = '停止';
      btn.classList.remove('btn-primary');
      btn.classList.add('btn-danger');
      btn.disabled = false;
    } else {
      btn.textContent = '发送';
      btn.classList.remove('btn-danger');
      btn.classList.add('btn-primary');
      btn.disabled = !$('input').value.trim();
    }
  }

  function send(text, options) {
    options = options || {};
    text = (text || '').trim();
    if (!text || state.sending) return;
    state.lastText = text;

    if (options.echo !== false) addMessage('user', text);
    var node = addMessage('assistant', '', { pending: true });
    setSendingUI(true);

    var controller = new AbortController();
    state.controller = controller;
    var firstToken = false;
    var slowTimer = setTimeout(function () {
      if (!firstToken) {
        node.slow.classList.remove('hidden');
        node.slow.textContent = '模型响应较慢，稍等';
      }
    }, 5000);

    fetch('/api/chat', {
      method: 'POST',
      signal: controller.signal,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        phone: state.phone,
        conversation_id: state.conversationId,
        message: text
      })
    }).then(function (res) {
      if (!res.ok) {
        return res.json().catch(function () { return {}; }).then(function (data) {
          var err = new Error((data.error && data.error.message) || '请求失败');
          err.status = res.status;
          throw err;
        });
      }
      return consume(res, node, function () {
        firstToken = true;
        node.slow.classList.add('hidden');
      });
    }).catch(function (err) {
      if (err && err.name === 'AbortError') {
        node.bubble.classList.remove('is-pending');
        var stopped = document.createElement('div');
        stopped.className = 'msg-stopped';
        stopped.textContent = '已停止生成';
        node.root.appendChild(stopped);
        return;
      }
      if (err && err.status === 429) {
        toast('说话太快了，休息 10 秒', 'error');
      } else if (err && err.status === 503) {
        toast('后端服务暂时不可用', 'error');
      } else if (!navigator.onLine) {
        $('connBar').classList.remove('hidden');
      }
      showErrorBar(node, (err && err.message) || '网络异常，请重试');
    }).then(function () {
      clearTimeout(slowTimer);
      setSendingUI(false);
      state.controller = null;
      if (node.typing) { node.typing.remove(); node.typing = null; }
      scrollToBottom(false);
    });
  }

  function consume(response, node, onFirstToken) {
    var reader = response.body.getReader();
    var decoder = new TextDecoder('utf-8');
    var buffer = '';

    function dispatch(block) {
      var event = '';
      var data = '';
      block.split('\n').forEach(function (line) {
        line = line.replace(/\r$/, '');
        if (line.indexOf('event:') === 0) event = line.slice(6).trim();
        else if (line.indexOf('data:') === 0) data += line.slice(5).trim();
      });
      if (!event) return;
      var payload = {};
      try { payload = JSON.parse(data || '{}'); } catch (e) { payload = {}; }
      handleEvent(event, payload, node, onFirstToken);
    }

    function pump() {
      return reader.read().then(function (result) {
        if (result.done) {
          if (buffer.trim()) dispatch(buffer);
          return;
        }
        buffer += decoder.decode(result.value, { stream: true });
        var index;
        while ((index = buffer.indexOf('\n\n')) >= 0) {
          var block = buffer.slice(0, index);
          buffer = buffer.slice(index + 2);
          if (block.trim()) dispatch(block);
        }
        return pump();
      });
    }
    return pump();
  }

  function handleEvent(event, data, node, onFirstToken) {
    if (event === 'meta') {
      setBadge(node, data.route);
      if (data.conversation_id) state.conversationId = data.conversation_id;
      if (data.intent_low_confidence) addHint(node, '我不太确定你的意思，可以换个说法');
      return;
    }
    if (event === 'delta') {
      if (node.typing) { node.typing.remove(); node.typing = null; }
      node.bubble.classList.remove('is-pending');
      node.textEl.textContent += (data.t || '');
      node.hasText = true;
      if (onFirstToken) { onFirstToken(); onFirstToken = null; }
      scrollToBottom(false);
      return;
    }
    if (event === 'tool') {
      addToolChip(node, data);
      return;
    }
    if (event === 'citation') {
      renderCitations(node, data.items);
      scrollToBottom(false);
      return;
    }
    if (event === 'done') {
      if (node.typing) { node.typing.remove(); node.typing = null; }
      if (data.customer) renderCustomer(data.customer);
      setTimeout(function () { loadCard(false); }, 1200);
      setTimeout(function () { loadCard(false); }, 3200);
      return;
    }
    if (event === 'error') {
      if (node.typing) { node.typing.remove(); node.typing = null; }
      showErrorBar(node, data.message || '服务异常，请重试');
      return;
    }
  }

  function showErrorBar(node, message) {
    var bar = document.createElement('div');
    bar.className = 'error-bar';
    var text = document.createElement('span');
    text.textContent = message;
    var retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'btn btn-sm';
    retry.textContent = '重试';
    retry.addEventListener('click', function () {
      bar.remove();
      if (state.lastText) send(state.lastText, { echo: false });
    });
    bar.appendChild(text);
    bar.appendChild(retry);
    node.root.appendChild(bar);
    scrollToBottom(false);
  }

  /* ------------------------------------------------------------ 进入 */
  function enter(phone) {
    $('loginError').textContent = '';
    fetch('/api/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone: phone })
    }).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok) {
          var msg = (data.error && data.error.message) || '进入失败';
          throw new Error(msg);
        }
        return data;
      });
    }).then(function (data) {
      state.phone = phone;
      state.conversationId = data.conversation_id;
      localStorage.setItem('seachat_phone', phone);
      if (window.history && window.history.replaceState) {
        window.history.replaceState({}, '', '/');
      }
      $('messages').innerHTML = '';
      showChat();
      renderCustomer(data.customer);
      addMessage('assistant', data.greeting, { route: 'direct' });
      loadCard(true);
      $('input').focus();
    }).catch(function (err) {
      showLogin((err && err.message) || '网络异常，请重试');
    });
  }

  /* ------------------------------------------------------------ 交互 */
  function autoResize() {
    var input = $('input');
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 132) + 'px';
  }

  function bind() {
    $('loginForm').addEventListener('submit', function (e) {
      e.preventDefault();
      var phone = $('phoneInput').value.trim();
      if (!PHONE_RE.test(phone)) {
        $('loginError').textContent = '请输入 11 位手机号';
        return;
      }
      enter(phone);
    });

    $('input').addEventListener('input', function () {
      autoResize();
      if (!state.sending) $('sendBtn').disabled = !$('input').value.trim();
    });

    $('input').addEventListener('keydown', function (e) {
      if (e.isComposing) return;
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        var text = $('input').value;
        if (!text.trim() || state.sending) return;
        $('input').value = '';
        autoResize();
        send(text);
      }
    });

    $('sendBtn').addEventListener('click', function () {
      if (state.sending && state.controller) {
        state.controller.abort();
        return;
      }
      var text = $('input').value;
      if (!text.trim()) return;
      $('input').value = '';
      autoResize();
      send(text);
    });

    $('switchPhone').addEventListener('click', function () {
      localStorage.removeItem('seachat_phone');
      window.location.href = '/';
    });

    $('panelToggle').addEventListener('click', function () {
      $('customerPanel').classList.toggle('is-open');
    });

    $('messages').addEventListener('scroll', function () {
      var host = $('messages');
      state.stick = host.scrollHeight - host.scrollTop - host.clientHeight < 80;
    });

    window.addEventListener('offline', function () {
      $('connBar').classList.remove('hidden');
    });
    window.addEventListener('online', function () {
      $('connBar').classList.add('hidden');
      toast('连接已恢复', 'ok');
    });
  }

  function init() {
    bind();
    var params = new URLSearchParams(window.location.search);
    var phone = params.get('phone') || localStorage.getItem('seachat_phone');
    if (phone && PHONE_RE.test(phone)) {
      enter(phone);
    } else {
      showLogin('');
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
