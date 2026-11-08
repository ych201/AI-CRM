/* SeaChat 管理端：客户列表 / 详情抽屉 / 知识库管理 */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var KEY_STORE = 'seachat_admin_key';
  var STAGE = { new: '新客', contacted: '已接触', qualified: '有明确意向', won: '已成交', lost: '已流失' };
  var LEVEL = { cold: '冷', warm: '中', hot: '高' };
  var ROUTE = {
    rag_market: '职业咨询', rag_course: '课程咨询', web_search: '实时资讯',
    sql: '数据查询', direct: '直聊', rag_miss: '知识库未命中'
  };
  var PROFILE_LABELS = [
    ['current_role', '身份'], ['years_exp', '经验'], ['goal', '目标'],
    ['budget', '预算'], ['time_window', '时间窗'], ['city', '城市'], ['concerns', '顾虑']
  ];
  var reloadPage = function () {};

  /* ------------------------------------------------------------ 公共 */
  function apiKey() { return localStorage.getItem(KEY_STORE) || ''; }

  function toast(message, kind) {
    var host = $('toastHost');
    if (!host) return;
    var el = document.createElement('div');
    el.className = 'toast' + (kind ? ' is-' + kind : '');
    el.textContent = message;
    host.appendChild(el);
    setTimeout(function () { el.remove(); }, 3200);
  }

  function readResponse(res) {
    return res.json().catch(function () { return {}; }).then(function (data) {
      if (res.status === 401) {
        toast('API Key 无效，请在右上角填写', 'error');
        throw new Error('unauthorized');
      }
      if (!res.ok) {
        var message = (data.error && data.error.message) || '请求失败';
        toast(message, 'error');
        throw new Error(message);
      }
      return data;
    });
  }

  function api(path, options) {
    options = options || {};
    var headers = options.headers || {};
    headers['Content-Type'] = 'application/json';
    if (apiKey()) headers['X-API-Key'] = apiKey();
    var init = { method: options.method || 'GET', headers: headers };
    if (options.body) init.body = JSON.stringify(options.body);
    return fetch(path, init).then(readResponse);
  }

  function apiUpload(path, formData) {
    var headers = {};
    if (apiKey()) headers['X-API-Key'] = apiKey();
    return fetch(path, { method: 'POST', headers: headers, body: formData }).then(readResponse);
  }

  function text(tag, value, className) {
    var el = document.createElement(tag);
    if (value !== undefined && value !== null) el.textContent = value;
    if (className) el.className = className;
    return el;
  }

  function fmtTime(value) {
    if (!value) return '—';
    return String(value).slice(5, 16);
  }

  function stageBadge(stage) {
    var el = text('span', STAGE[stage] || stage || '—', 'badge');
    if (stage === 'qualified' || stage === 'won') el.classList.add('badge-rag_course');
    else if (stage === 'lost') el.classList.add('badge-sql');
    else el.classList.add('badge-direct');
    return el;
  }

  function levelText(level) {
    var el = text('span', LEVEL[level] || level || '—');
    el.style.fontWeight = '500';
    el.style.color = level === 'hot' ? '#b45309' : (level === 'warm' ? '#047857' : '#6b7280');
    return el;
  }

  function initKeyBox() {
    var input = $('apiKey');
    var marker = $('keyState');
    if (!input || !marker) return;
    input.value = apiKey();
    function mark() {
      marker.textContent = apiKey() ? '已保存' : '未设置';
      marker.classList.toggle('is-bad', !apiKey());
    }
    mark();
    input.addEventListener('change', function () {
      localStorage.setItem(KEY_STORE, input.value.trim());
      mark();
      reloadPage();
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { input.blur(); }
    });
  }

  /* ============================================================ 客户页 */
  function initCustomers() {
    var state = { page: 1, pageSize: 20, total: 0, ids: [], q: '', stage: '', intent: '' };
    var current = null;
    var timer = null;

    function params() {
      var qs = new URLSearchParams();
      qs.set('page', state.page);
      qs.set('page_size', state.pageSize);
      if (state.q) qs.set('q', state.q);
      if (state.stage) qs.set('stage', state.stage);
      if (state.intent) qs.set('intent_level', state.intent);
      return qs.toString();
    }

    function load(reset) {
      if (reset) state.page = 1;
      api('/api/customers?' + params()).then(function (data) {
        if (reset) state.ids = [];
        render(data, reset);
      }).catch(function () {});
    }

    function render(data, reset) {
      var stats = data.stats || {};
      $('statTotal').textContent = stats.total === undefined ? '—' : stats.total;
      $('statActive').textContent = stats.active_today === undefined ? '—' : stats.active_today;
      $('statHot').textContent = stats.hot === undefined ? '—' : stats.hot;
      $('statNew').textContent = stats.new_this_week === undefined ? '—' : stats.new_this_week;

      var tbody = $('customerRows');
      if (reset) tbody.innerHTML = '';
      (data.items || []).forEach(function (item) {
        state.ids.push(item.id);
        tbody.appendChild(rowFor(item));
      });
      state.total = data.total || 0;
      $('emptyState').classList.toggle('hidden', state.total !== 0);
      $('loadMoreWrap').classList.toggle('hidden', state.ids.length >= state.total);
      $('listMeta').textContent = state.total ? ('共 ' + state.total + ' 位客户') : '';
    }

    function rowFor(item) {
      var tr = document.createElement('tr');
      tr.className = 'is-clickable';
      tr.appendChild(text('td', item.phone || '—', 'cell-phone'));
      tr.appendChild(text('td', item.name || '—'));
      var stageTd = document.createElement('td');
      stageTd.appendChild(stageBadge(item.stage));
      tr.appendChild(stageTd);
      var intentTd = document.createElement('td');
      intentTd.appendChild(levelText(item.intent_level));
      tr.appendChild(intentTd);
      tr.appendChild(text('td', item.last_message || '—', 'cell-clip'));
      tr.appendChild(text('td', item.message_count === undefined ? '—' : item.message_count));
      tr.appendChild(text('td', fmtTime(item.last_active_at)));
      tr.addEventListener('click', function () { openDrawer(item.id); });
      return tr;
    }

    function openDrawer(id) {
      api('/api/customers/' + id).then(function (customer) {
        current = customer;
        fillDrawer(customer);
        $('drawer').classList.add('is-open');
        $('drawer').setAttribute('aria-hidden', 'false');
        $('drawerMask').classList.add('is-open');
      }).catch(function () {});
    }

    function closeDrawer() {
      $('drawer').classList.remove('is-open');
      $('drawer').setAttribute('aria-hidden', 'true');
      $('drawerMask').classList.remove('is-open');
    }

    function fillDrawer(c) {
      $('dAvatar').textContent = (c.name || '客').slice(0, 1);
      $('dPhone').textContent = c.phone || '—';
      $('dMeta').textContent = (STAGE[c.stage] || c.stage) + ' · ' + (LEVEL[c.intent_level] || c.intent_level);
      $('dStage').value = c.stage || 'new';
      $('dIntent').value = c.intent_level || 'cold';
      $('dNote').value = c.note || '';

      var profile = $('dProfile');
      profile.innerHTML = '';
      PROFILE_LABELS.forEach(function (row) {
        var value = (c.profile || {})[row[0]];
        if (Array.isArray(value)) value = value.join('、');
        profile.appendChild(text('dt', row[1]));
        var dd = text('dd', value || '—');
        if (!value) dd.className = 'muted';
        profile.appendChild(dd);
      });

      renderTags(c.tags || []);

      var host = $('dMessages');
      host.innerHTML = '';
      var messages = (c.recent_messages || []).slice(-20);
      if (!messages.length) {
        host.appendChild(text('p', '还没有对话记录', 'muted'));
      }
      messages.forEach(function (m) {
        var box = document.createElement('div');
        box.className = 'mini-msg';
        var who = m.role === 'user' ? '客户' : '小海';
        if (m.route && m.route !== 'direct') who += ' · ' + (ROUTE[m.route] || m.route);
        box.appendChild(text('b', who + '  ' + (m.created_at || '').slice(5, 16)));
        box.appendChild(text('span', m.content || ''));
        host.appendChild(box);
      });

      $('dOpenChat').onclick = function (e) {
        e.preventDefault();
        api('/api/customers/' + c.id + '/contact').then(function (data) {
          window.open('/?phone=' + encodeURIComponent(data.phone), '_blank');
        }).catch(function () {});
      };
    }

    function renderTags(tags) {
      var host = $('dTags');
      host.innerHTML = '';
      if (!tags.length) {
        host.appendChild(text('span', '暂无标签', 'muted'));
        return;
      }
      tags.forEach(function (tag) {
        var chip = text('span', null, 'chip');
        chip.appendChild(document.createTextNode(tag));
        var remove = text('button', '×');
        remove.type = 'button';
        remove.title = '移除';
        remove.addEventListener('click', function () {
          var next = tags.filter(function (t) { return t !== tag; });
          patch({ tags: next }, function (updated) {
            renderTags(updated.tags || []);
            refreshRow(updated);
          });
        });
        chip.appendChild(remove);
        host.appendChild(chip);
      });
    }

    function patch(payload, done) {
      if (!current) return;
      api('/api/customers/' + current.id, { method: 'PATCH', body: payload })
        .then(function (updated) {
          current = updated;
          toast('已保存', 'ok');
          $('dMeta').textContent = (STAGE[updated.stage] || updated.stage) + ' · ' +
            (LEVEL[updated.intent_level] || updated.intent_level);
          refreshRow(updated);
          if (done) done(updated);
        }).catch(function () {});
    }

    function refreshRow(updated) {
      var rows = $('customerRows').children;
      for (var i = 0; i < rows.length; i += 1) {
        var cell = rows[i].firstChild;
        if (cell && cell.textContent === updated.phone) {
          rows[i].children[1].textContent = updated.name || '—';
          var stageTd = rows[i].children[2];
          stageTd.innerHTML = '';
          stageTd.appendChild(stageBadge(updated.stage));
          var intentTd = rows[i].children[3];
          intentTd.innerHTML = '';
          intentTd.appendChild(levelText(updated.intent_level));
          break;
        }
      }
    }

    function addTag() {
      var input = $('dTagInput');
      var value = input.value.trim();
      if (!value || !current) return;
      if (value.length > 12) { toast('标签不超过 12 字', 'error'); return; }
      var tags = (current.tags || []).slice();
      if (tags.indexOf(value) === -1) tags.push(value);
      input.value = '';
      patch({ tags: tags }, function (updated) { renderTags(updated.tags || []); });
    }

    // ---- 事件
    $('search').addEventListener('input', function (e) {
      state.q = e.target.value.trim();
      clearTimeout(timer);
      timer = setTimeout(function () { load(true); }, 300);
    });
    $('stageFilter').addEventListener('change', function (e) {
      state.stage = e.target.value;
      load(true);
    });
    $('intentFilter').addEventListener('change', function (e) {
      state.intent = e.target.value;
      load(true);
    });
    $('refreshBtn').addEventListener('click', function () { load(true); });
    $('loadMore').addEventListener('click', function () {
      state.page += 1;
      load(false);
    });
    $('drawerClose').addEventListener('click', closeDrawer);
    $('drawerMask').addEventListener('click', closeDrawer);
    $('dStage').addEventListener('change', function (e) { patch({ stage: e.target.value }); });
    $('dIntent').addEventListener('change', function (e) { patch({ intent_level: e.target.value }); });
    $('dTagAdd').addEventListener('click', addTag);
    $('dTagInput').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); addTag(); }
    });
    $('dNote').addEventListener('blur', function (e) {
      if (current && e.target.value !== (current.note || '')) {
        patch({ note: e.target.value });
      }
    });

    reloadPage = function () { load(true); };
    load(true);
  }

  /* ============================================================ 知识库 */
  function initKb() {
    var state = { collection: 'market', items: [] };
    var editing = null;
    var pending = [];
    var uploading = false;
    var UPLOAD_EXTS = ['.pdf', '.docx', '.txt', '.md', '.markdown'];
    var UPLOAD_MAX = 10 * 1024 * 1024;

    function load() {
      var qs = '?collection=' + state.collection + ($('showDisabled').checked ? '&show_disabled=1' : '');
      api('/api/kb/documents' + qs).then(function (data) {
        state.items = data.items || [];
        render(data);
      }).catch(function () {});
    }

    function render(data) {
      var tbody = $('docRows');
      tbody.innerHTML = '';
      state.items.forEach(function (doc) {
        var tr = document.createElement('tr');
        tr.appendChild(text('td', doc.title || '—'));
        tr.appendChild(text('td', doc.doc_id, 'cell-phone'));
        tr.appendChild(text('td', doc.chunk_count));
        tr.appendChild(text('td', doc.source || 'manual'));
        tr.appendChild(text('td', fmtTime(doc.updated_at)));
        var ops = document.createElement('td');
        var edit = text('button', '编辑', 'btn btn-sm');
        edit.type = 'button';
        edit.style.marginRight = '6px';
        edit.addEventListener('click', function () { openModal(doc.doc_id); });
        var del = text('button', '删除', 'btn btn-sm btn-danger');
        del.type = 'button';
        del.addEventListener('click', function () { removeDoc(doc); });
        ops.appendChild(edit);
        ops.appendChild(del);
        tr.appendChild(ops);
        tbody.appendChild(tr);
      });
      $('kbEmpty').classList.toggle('hidden', state.items.length > 0);
      $('kbMeta').textContent = '共 ' + (data.total || 0) + ' 条';
    }

    function openModal(docId) {
      editing = null;
      $('modalError').textContent = '';
      $('docId').disabled = false;
      $('docId').value = '';
      $('docTitle').value = '';
      $('docContent').value = '';
      $('contentCount').textContent = '0 / 20000';
      $('docCollection').value = state.collection;
      $('modalTitle').textContent = '新建知识条目';

      if (!docId) {
        $('docModal').classList.remove('hidden');
        $('docId').focus();
        return;
      }
      api('/api/kb/documents/' + encodeURIComponent(docId)).then(function (doc) {
        editing = doc;
        $('modalTitle').textContent = '编辑知识条目';
        $('docId').value = doc.doc_id;
        $('docId').disabled = true;
        $('docCollection').value = doc.collection;
        $('docTitle').value = doc.title || '';
        $('docContent').value = doc.content || '';
        updateCount();
        $('docModal').classList.remove('hidden');
      }).catch(function () {});
    }

    function closeModal() {
      $('docModal').classList.add('hidden');
      editing = null;
    }

    function updateCount() {
      var len = $('docContent').value.length;
      $('contentCount').textContent = len + ' / 20000';
      $('contentCount').style.color = len > 20000 ? '#dc2626' : '';
    }

    function save() {
      var content = $('docContent').value;
      var title = $('docTitle').value.trim();
      var collection = $('docCollection').value;
      var docId = $('docId').value.trim();
      if (!content.trim()) {
        $('modalError').textContent = '内容不能为空';
        return;
      }
      if (content.length > 20000) {
        $('modalError').textContent = '内容不能超过 20000 字符';
        return;
      }
      if (docId && !/^[A-Za-z0-9_.\-]{1,80}$/.test(docId)) {
        $('modalError').textContent = 'doc_id 只允许字母、数字、下划线、短横线、点';
        return;
      }
      var btn = $('modalSave');
      btn.disabled = true;
      btn.textContent = '索引中…';
      api('/api/kb/documents', {
        method: 'POST',
        body: {
          doc_id: docId || undefined,
          collection: collection,
          title: title || undefined,
          content: content,
          source: 'manual'
        }
      }).then(function (result) {
        toast('已索引 ' + result.chunks + ' 个切片', 'ok');
        state.collection = result.collection;
        syncTabs();
        closeModal();
        load();
      }).catch(function (err) {
        $('modalError').textContent = err.message || '保存失败';
      }).then(function () {
        btn.disabled = false;
        btn.textContent = '保存并索引';
      });
    }

    function removeDoc(doc) {
      if (!window.confirm('删除「' + (doc.title || doc.doc_id) + '」及其向量？此操作不可撤销。')) return;
      api('/api/kb/documents/' + encodeURIComponent(doc.doc_id), { method: 'DELETE' })
        .then(function () {
          toast('已删除', 'ok');
          load();
        }).catch(function () {});
    }

    function rebuild() {
      if (!window.confirm('按「' + state.collection + '」全量重建索引？数据量大时可能需要几十秒。')) return;
      var btn = $('rebuildBtn');
      btn.disabled = true;
      btn.textContent = '重建中…';
      var slow = setTimeout(function () {
        toast('仍在处理，请稍后刷新', 'error');
      }, 30000);
      api('/api/kb/rebuild', { method: 'POST', body: { collection: state.collection } })
        .then(function (result) {
          toast('重建完成：' + result.documents + ' 条文档 / ' + result.chunks + ' 个切片', 'ok');
          load();
        }).catch(function () {})
        .then(function () {
          clearTimeout(slow);
          btn.disabled = false;
          btn.textContent = '重建索引';
        });
    }

    /* ---- 文件上传 */
    function fileExt(name) {
      var i = String(name).lastIndexOf('.');
      return i < 0 ? '' : String(name).slice(i).toLowerCase();
    }

    function fmtSize(bytes) {
      if (bytes < 1024) return bytes + ' B';
      if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + ' KB';
      return (bytes / 1024 / 1024).toFixed(1) + ' MB';
    }

    function openUpload() {
      pending = [];
      uploading = false;
      $('uploadError').textContent = '';
      $('uploadCollection').value = state.collection;
      $('fileInput').value = '';
      $('uploadSave').disabled = false;
      $('uploadSave').textContent = '上传并索引';
      renderUploads();
      $('uploadModal').classList.remove('hidden');
    }

    function closeUpload() {
      if (uploading) return;
      $('uploadModal').classList.add('hidden');
      pending = [];
    }

    function addFiles(files) {
      if (uploading) return;
      var rejected = [];
      Array.prototype.forEach.call(files || [], function (file) {
        var ext = fileExt(file.name);
        if (UPLOAD_EXTS.indexOf(ext) === -1) {
          rejected.push(file.name + '：格式不支持');
          return;
        }
        if (file.size === 0) {
          rejected.push(file.name + '：文件为空');
          return;
        }
        if (file.size > UPLOAD_MAX) {
          rejected.push(file.name + '：超过 10MB');
          return;
        }
        var dup = pending.some(function (item) {
          return item.file.name === file.name && item.file.size === file.size;
        });
        if (!dup) pending.push({ file: file, status: 'ready', message: '' });
      });
      $('uploadError').textContent = rejected.length ? rejected.join('；') : '';
      renderUploads();
    }

    function renderUploads() {
      var host = $('uploadList');
      host.innerHTML = '';
      pending.forEach(function (item, index) {
        var cls = 'upload-item';
        if (item.status === 'done') cls += ' is-done';
        if (item.status === 'error') cls += ' is-error';
        var li = document.createElement('li');
        li.className = cls;
        li.appendChild(text('span', item.file.name, 'name'));
        li.appendChild(text('span', fmtSize(item.file.size), 'muted'));
        var status = item.status === 'uploading' ? '解析中…'
          : item.status === 'done'
            ? ('已索引 ' + item.chunks + ' 切片' + (item.documents > 1 ? '（拆为 ' + item.documents + ' 条）' : ''))
            : item.status === 'error' ? (item.message || '失败')
              : '待上传';
        li.appendChild(text('span', status, 'status'));
        if (!uploading && item.status !== 'done') {
          var remove = text('button', '×', 'remove');
          remove.type = 'button';
          remove.title = '移除';
          remove.addEventListener('click', function () {
            pending.splice(index, 1);
            renderUploads();
          });
          li.appendChild(remove);
        }
        host.appendChild(li);
      });
    }

    function uploadAll() {
      if (uploading) return;
      if (!pending.length) {
        $('uploadError').textContent = '请先选择要上传的文件';
        return;
      }
      var collection = $('uploadCollection').value;
      var btn = $('uploadSave');
      uploading = true;
      $('uploadError').textContent = '';
      btn.disabled = true;
      btn.textContent = '处理中…';

      function finish() {
        uploading = false;
        btn.disabled = false;
        btn.textContent = '上传并索引';
        renderUploads();
        var done = pending.filter(function (item) { return item.status === 'done'; }).length;
        var failed = pending.length - done;
        if (!done) {
          toast('上传失败，请检查日志后重试', 'error');
          return;
        }
        toast('已导入 ' + done + ' 个文件' + (failed ? '，' + failed + ' 个失败' : ''), failed ? 'error' : 'ok');
        state.collection = collection;
        syncTabs();
        load();
        if (!failed) setTimeout(closeUpload, 600);
      }

      function next(i) {
        if (i >= pending.length) { finish(); return; }
        var item = pending[i];
        if (item.status === 'done') { next(i + 1); return; }
        item.status = 'uploading';
        renderUploads();
        var form = new FormData();
        form.append('file', item.file);
        form.append('collection', collection);
        apiUpload('/api/kb/upload', form).then(function (result) {
          item.status = 'done';
          item.chunks = result.chunks;
          item.documents = result.documents || 1;
          item.docId = result.doc_id;
        }).catch(function (err) {
          item.status = 'error';
          item.message = err.message || '失败';
        }).then(function () {
          renderUploads();
          next(i + 1);
        });
      }

      next(0);
    }

    function syncTabs() {
      var tabs = $('tabs').querySelectorAll('.tab');
      for (var i = 0; i < tabs.length; i += 1) {
        tabs[i].classList.toggle('is-active', tabs[i].dataset.collection === state.collection);
      }
    }

    $('tabs').addEventListener('click', function (e) {
      var tab = e.target.closest('.tab');
      if (!tab) return;
      state.collection = tab.dataset.collection;
      syncTabs();
      load();
    });
    $('showDisabled').addEventListener('change', load);
    $('newDocBtn').addEventListener('click', function () { openModal(null); });
    $('uploadBtn').addEventListener('click', openUpload);
    $('rebuildBtn').addEventListener('click', rebuild);
    $('modalClose').addEventListener('click', closeModal);
    $('modalCancel').addEventListener('click', closeModal);
    $('modalSave').addEventListener('click', save);
    $('docContent').addEventListener('input', updateCount);
    $('docModal').addEventListener('click', function (e) {
      if (e.target === $('docModal')) closeModal();
    });

    $('uploadClose').addEventListener('click', closeUpload);
    $('uploadCancel').addEventListener('click', closeUpload);
    $('uploadSave').addEventListener('click', uploadAll);
    $('fileInput').addEventListener('change', function (e) {
      addFiles(e.target.files);
      e.target.value = '';
    });
    $('uploadModal').addEventListener('click', function (e) {
      if (e.target === $('uploadModal')) closeUpload();
    });
    ['dragenter', 'dragover'].forEach(function (name) {
      $('dropZone').addEventListener(name, function (e) {
        e.preventDefault();
        e.stopPropagation();
        $('dropZone').classList.add('is-over');
      });
    });
    ['dragleave', 'drop'].forEach(function (name) {
      $('dropZone').addEventListener(name, function (e) {
        e.preventDefault();
        e.stopPropagation();
        $('dropZone').classList.remove('is-over');
      });
    });
    $('dropZone').addEventListener('drop', function (e) {
      if (e.dataTransfer && e.dataTransfer.files) addFiles(e.dataTransfer.files);
    });

    reloadPage = load;
    load();
  }

  /* ------------------------------------------------------------ 启动 */
  function init() {
    initKeyBox();
    var page = document.querySelector('.admin-wrap');
    if (!page) return;
    if (page.dataset.page === 'customers') initCustomers();
    if (page.dataset.page === 'kb') initKb();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
