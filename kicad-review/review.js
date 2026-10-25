// kicad-review: code shared by the schematic page (sch.html) and the board page (board.html), as window.Review.
// The review notes are talkpage items: this file reads them with talkpage's long poll (/_talk/api/state) and writes
// with /_talk/api/user and /_talk/api/seen; the pages do not load talkpage.js.
'use strict';
(function () {
  // ------------------------------------------------------------ helpers
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (_) {} },
    del(k) { try { localStorage.removeItem(k); } catch (_) {} },
  };
  async function api(path, body, method) {
    const opt = body === undefined ? { method: method || 'GET', cache: 'no-store' } :
      { method: method || 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
    const r = await fetch(path, opt);
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    return d;
  }
  function notice(el, text) {            // a short confirmation right next to the control that was used
    const r = el.getBoundingClientRect();
    const n = document.createElement('div');
    n.className = 'notice';
    n.textContent = text;
    n.style.left = (r.right + 6) + 'px';
    n.style.top = (r.top + r.height / 2 - 10) + 'px';
    document.body.appendChild(n);
    if (r.right + 6 + n.offsetWidth > innerWidth) n.style.left = Math.max(4, r.left - n.offsetWidth - 6) + 'px';
    setTimeout(() => { n.style.opacity = 0; }, 1300);
    setTimeout(() => n.remove(), 1700);
  }

  // ------------------------------------------------------------ statuses, severity, owner
  // stored values: '' 未確認, このまま (OK), 修正を依頼 (直して), 修正済み; 質問 and 自分で修正 come from older reviews (no button)
  const STATUS_BTNS = ['修正済み', '修正を依頼', 'このまま'];
  const BTN_LABEL = { '修正済み': '✔ 修正済み', '修正を依頼': '⚑ 直して', 'このまま': '✓ OK' };
  const BTN_TITLE = { '修正済み': '直した・直っている。', '修正を依頼': 'エージェントに直してもらう（コメントは任意）。',
    'このまま': '確認した・このままでよい（直さない）。' };
  const ST_NAME = { '': '未確認', 'このまま': 'OK（このまま）', '修正を依頼': '直して（修正を依頼）', '修正済み': '修正済み',
    '質問': '質問', '自分で修正': '自分で修正' };
  const ST_MARK = { '': '○', 'このまま': '✓', '修正を依頼': '⚑', '修正済み': '✔', '質問': '？', '自分で修正': '✎' };
  const ST_LINE = { '': '未確認に戻した', 'このまま': '✓ OK', '修正を依頼': '⚑ 直して', '修正済み': '✔ 修正済み',
    '質問': '？ 質問', '自分で修正': '✎ 自分で修正' };
  const DONE = new Set(['このまま', '修正済み']);
  const SEVERITIES = ['必須', '推奨', '任意', '情報'];
  const SEV_MEANING = { '必須': '直さないと動かない・壊れる・製造で不良', '推奨': 'できれば直してほしい（性能・信頼性・製造の余裕）',
    '任意': '直さなくても動作・製造に影響しない（見た目など）', '情報': 'そのままでよい・変更の報告' };
  const OWNER_MEANING = { '人': 'KiCad で手で直す方が早い', 'エージェント': 'エージェントが直す', '未定': '担当はまだ決めていない' };
  const stClass = st => 's-' + (st || '未確認');     // CSS class of a status (.s-未確認 for '')

  const isWaiting = it => !!it && (it.user_rev || 0) > (it.agent_rev || 0);
  const isUnread = it => !!it && (it.thread || []).some(m => m.who === 'agent' && m.text && (m.rev || 0) > (it.seen_rev || 0));

  // ------------------------------------------------------------ talkpage long poll
  const PRESENCE = {
    listening: ['エージェント 受付中', 'チェックとコメントは、すぐエージェントに届きます'],
    working: ['エージェント 対応中', 'エージェントが届いた書き込みを処理しています。この間の書き込みも保存され、次に受け付けたときに届きます'],
    absent: ['エージェント 待ち受けなし', '書き込みは保存されます。エージェントが待ち受けを始めたときに届きます'],
    down: ['サーバー未接続', 'サーバーにつながりません。この間の書き込みは保存されません'],
  };
  function showPresence(p, why) {
    const el = $('presence');
    if (!el) return;
    const t = PRESENCE[p] || PRESENCE.absent;
    el.textContent = t[0];
    el.title = t[1] + (p === 'down' && why ? '（' + why + '）' : '');
    el.dataset.p = p;
  }

  // onState(d) gets {page, file, rev, presence, items, groups?} on every change; setPage(key) follows another review
  function connect(page, onState) {
    const h = { page, rev: -1, presence: 'down', gen: 0, ctl: null };
    function loop(first) {
      const gen = h.gen;
      const q = '/_talk/api/state?page=' + encodeURIComponent(h.page) +
        (first ? '' : '&rev=' + h.rev + '&presence=' + encodeURIComponent(h.presence) + '&wait=25');
      h.ctl = new AbortController();
      fetch(q, { cache: 'no-store', signal: h.ctl.signal })
        .then(r => r.json().then(d => { if (!r.ok) throw new Error(d.error || r.statusText); return d; }))
        .then(d => {
          if (gen !== h.gen) return;
          h.rev = d.rev;
          h.presence = d.presence || 'absent';
          showPresence(h.presence);
          try { onState(d); } catch (e) { console.error(e); }
          loop(false);
        })
        .catch(e => {
          if (gen !== h.gen) return;
          h.presence = 'down';
          showPresence('down', e && e.message);
          setTimeout(() => { if (gen === h.gen) loop(true); }, 3000);
        });
    }
    h.setPage = key => {
      if (key === h.page) return;
      h.page = key;
      h.rev = -1;
      h.gen++;
      if (h.ctl) h.ctl.abort();
      loop(true);
    };
    showPresence('down');
    loop(true);
    return h;
  }

  // the user read the agent's reply: only while the detail is on screen and the page is visible
  const seenBusy = new Set();
  function markSeen(page, id, it, onItem) {
    if (!id || !isUnread(it) || document.visibilityState !== 'visible' || seenBusy.has(page + '\n' + id)) return;
    seenBusy.add(page + '\n' + id);
    api('/_talk/api/seen', { page, id }).then(onItem, () => {}).finally(() => seenBusy.delete(page + '\n' + id));
  }

  // ------------------------------------------------------------ header: tabs, presence, newer page
  let info = null, pageStamp;
  const infoListeners = [];
  function initHeader(view) {
    window.name = 'kireview-' + view;     // the cross-links and the tabs of the other page come back to this tab
    const tabs = $('tabs');
    tabs.innerHTML = '<a href="/sch" target="kireview-sch" data-v="sch">回路図</a><a href="/board" target="kireview-board" data-v="board">基板</a>';
    tabs.querySelector('[data-v="' + view + '"]').classList.add('cur');
    $('pageNew').addEventListener('click', () => location.reload());
    const poll = async () => {
      try { info = await api('/api/info'); } catch (_) { return; }
      if (pageStamp === undefined) pageStamp = info.page_stamp;
      $('pageNew').classList.toggle('off', !(info.page_stamp && info.page_stamp !== pageStamp));
      tabs.querySelectorAll('a').forEach(a => {
        const loaded = !!info[a.dataset.v];
        a.classList.toggle('off', !loaded);
        a.title = loaded ? (a.dataset.v === 'sch' ? info.sch.title : info.board.stem) : '読み込まれていない（開くと選ぶ画面）';
      });
      for (const fn of infoListeners) { try { fn(info); } catch (e) { console.error(e); } }
    };
    poll();
    setInterval(poll, 5000);
  }
  const onInfo = fn => { infoListeners.push(fn); if (info) fn(info); };

  // #ref=<REF> in the URL: the reference, and the hash cleared so that the same link works again
  function takeRef() {
    const m = /(?:^#|&)ref=([^&]*)/.exec(location.hash);
    if (!m) return null;
    history.replaceState(null, '', location.pathname + location.search);
    return decodeURIComponent(m[1]).trim() || null;
  }

  // ------------------------------------------------------------ list filters (未読も表示 / 完了も表示)
  // finished items are hidden unless 完了も表示; with 未読も表示 a finished item with an unread reply or waiting for one is
  // listed and stays listed (read or not) until a filter button is pressed, so rows do not vanish on a click
  function filters(prefix, onChange) {
    const f = {
      showDone: store.get(prefix + '.showDone') === '1',
      showUnread: store.get(prefix + '.showUnread') !== '0',
      stick: new Set(),
      keep(id, it, sel) {
        if (f.showDone || !DONE.has((it && it.status) || '') || id === sel) return true;
        if (f.showUnread && (f.stick.has(id) || isWaiting(it) || isUnread(it))) { f.stick.add(id); return true; }
        return false;
      },
      paint() { $('showDone').classList.toggle('on', f.showDone); $('showUnread').classList.toggle('on', f.showUnread); },
    };
    $('showDone').title = '確認済み（OK・修正済み）の項目も一覧に表示';
    $('showUnread').title = '確認済みでも、未読の返信・返信待ちがある項目は一覧に表示';
    $('showDone').addEventListener('click', () => { f.showDone = !f.showDone; store.set(prefix + '.showDone', f.showDone ? '1' : '0'); f.stick.clear(); onChange(); });
    $('showUnread').addEventListener('click', () => { f.showUnread = !f.showUnread; store.set(prefix + '.showUnread', f.showUnread ? '1' : '0'); f.stick.clear(); onChange(); });
    return f;
  }

  // ------------------------------------------------------------ list rows
  function sevChip(sev) {
    const c = getComputedStyle(document.documentElement).getPropertyValue('--sev-' + sev).trim() || '#8a8a85';
    return '<span class="chip" style="background:' + c + '">' + esc(sev || '-') + '</span>';
  }
  const ownerChip = owner => '<span class="chip owner">' + esc(owner || '未定') + '</span>';

  // o: {id, it (talk item or null), label (shown id, optional), title, titleClass, detail, sel, wrap, extra (HTML)}
  function rowHtml(o) {
    const it = o.it || {}, st = it.status || '';
    const chips = (it.severity ? sevChip(it.severity) : '') + (it.owner ? ownerChip(it.owner) : '');
    const tag = isUnread(it) ? '<span class="new">未読</span>' : isWaiting(it) ? '<span class="new wait">返信待ち</span>' : '';
    return '<div class="item' + (o.sel ? ' sel' : '') + (o.wrap ? ' wrap' : '') + (DONE.has(st) ? ' done' : '') +
      (st === '修正済み' ? ' fixed' : '') + '" data-id="' + esc(o.id) + '">' +
      '<span class="st ' + stClass(st) + '" title="' + esc(ST_NAME[st] || st) + '">' + esc(ST_MARK[st] || '○') + '</span>' +
      '<div class="main"><div class="line">' + (o.label ? '<span class="id">' + esc(o.label) + '</span>' : '') +
      (chips ? '<span class="chips">' + chips + '</span>' : '') +
      '<span class="t ' + esc(o.titleClass || '') + '">' + esc(o.title) + '</span></div>' +
      (o.detail ? '<div class="d">' + esc(o.detail) + '</div>' : '') + '</div>' + tag + (o.extra || '') + '</div>';
  }
  const groupHtml = (label, count, cls, attr) => '<div class="grp-h ' + (cls || '') + '"' + (attr || '') + '><span>' + esc(label) +
    '</span><small>' + count + ' 件</small></div>';

  // the row d rows away from id in the list as shown (id null: the first row, or the last when going up)
  function neighbor(id, d) {
    const ids = [...document.querySelectorAll('#list .item')].map(el => el.dataset.id);
    const i = id != null ? ids.indexOf(id) : -1;
    if (i < 0) return d < 0 ? ids[ids.length - 1] : ids[0];
    return ids[i + d];
  }

  // ------------------------------------------------------------ detail sections and thread
  // 依頼, figures, 重要度 / 担当 (when present). figBase: '/figures/<review name>/'
  function askHtml(it, figBase) {
    let h = it.ask ? '<div class="req">' + esc(it.ask) + '</div>' : '';
    if (it.figures && it.figures.length) h += '<div class="figs">' + it.figures.map(n => '<img src="' + esc(figBase + encodeURIComponent(n)) +
      '" alt="' + esc(n) + '" title="押すと全画面">').join('') + '</div>';
    if (it.severity) h += '<div class="row"><span class="k">重要度</span>' + sevChip(it.severity) + '<span class="meaning">' + esc(SEV_MEANING[it.severity] || '') + '</span></div>';
    if (it.owner) h += '<div class="row"><span class="k">担当</span>' + ownerChip(it.owner) + '<span class="meaning">' + esc(OWNER_MEANING[it.owner] || '') + '</span></div>';
    return h;
  }
  // 信号・電流・注意, 理想, 経緯, 内容, 直し方, and 意図 when rationale is given (a string, '' = not written yet)
  function notesHtml(it, rationale) {
    let h = '';
    if (it.signal) h += '<h4>信号・電流・注意</h4><div class="text">' + esc(it.signal) + '</div>';
    if (it.ideal) h += '<h4>理想</h4><div class="text">' + esc(it.ideal) + '</div>';
    if (it.background) h += '<h4>経緯</h4><div class="text bg">' + esc(it.background) + '</div>';
    if (it.text) h += '<h4>内容</h4><div class="text">' + esc(it.text) + '</div>';
    if (it.action) h += '<h4>直し方</h4><div class="action">' + esc(it.action) + '</div>';
    if (rationale !== undefined) h += '<h4>意図</h4><div class="why' + (rationale ? '' : ' none') + '">' +
      esc(rationale || '（未記入。エージェントが書き込むか、コメントで質問）') + '</div>';
    return h;
  }
  const shortTime = t => { const m = /^\d{4}-(\d\d)-(\d\d)[ T](\d\d:\d\d)/.exec(t || ''); return m ? m[1] + '/' + m[2] + ' ' + m[3] : (t || ''); };
  // messages, and status changes as small grey lines
  function msgsHtml(thread) {
    return (thread || []).map(m => {
      const who = m.who === 'agent' ? 'エージェント' : 'あなた', cls = m.who === 'agent' ? 'agent' : 'user';
      if (m.text == null) return '<div class="stline ' + cls + '">' + esc(shortTime(m.time) + ' ' + who + ': ' + (ST_LINE[m.status || ''] || m.status)) + '</div>';
      return '<div class="msg ' + cls + '"><small>' + who + '　' + esc(shortTime(m.time)) + '</small>' + esc(m.text) + '</div>';
    }).join('');
  }
  const threadHtml = thread => '<h4>やりとり</h4>' + ((thread || []).length ? msgsHtml(thread) : '<div class="none">まだありません</div>');

  // ------------------------------------------------------------ figure full screen; a click anywhere or Esc closes it
  function openFigure(src) { const lb = $('lb'); lb.innerHTML = '<img src="' + esc(src) + '">'; lb.hidden = false; }
  function closeFigure() { const lb = $('lb'); lb.hidden = true; lb.innerHTML = ''; }
  const figureOpen = () => !$('lb').hidden;
  function bindFigures(box) { box.querySelectorAll('.figs img').forEach(im => im.addEventListener('click', () => openFigure(im.src))); }

  // ------------------------------------------------------------ footer: status buttons and the comment box
  // o: {page() → review page key, current() → [id, item | null] or null, title(id) → title to send (schematic) or
  //     undefined (board), next() → the row to go to after a status press, update(id, item, next) → store the item and
  //     show it (select next when given)}
  function footer(o) {
    const ta = $('comment');
    let cur = null;                    // page + id whose draft the box holds
    const draftKey = (page, id) => 'kireview.draft.' + page + '.' + id;
    const fit = () => {               // the comment box grows with its text, up to its max-height
      ta.style.height = '';
      if (ta.value) ta.style.height = Math.min(ta.scrollHeight + 2, 120) + 'px';
    };
    const saveDraft = () => {
      if (!cur) return;
      if (ta.value.trim()) store.set(cur, ta.value); else store.del(cur);
    };
    function show() {
      const c = o.current();
      $('post').hidden = !c;
      const key = c ? draftKey(o.page(), c[0]) : null;
      if (key !== cur) { saveDraft(); cur = key; ta.value = key ? store.get(key) || '' : ''; fit(); }
      if (!c) return;
      const st = (c[1] && c[1].status) || '';
      $('stBtns').innerHTML = STATUS_BTNS.map(b => '<button data-st="' + b + '" class="' + (st === b ? 'on' : '') + '" title="' +
        esc(BTN_TITLE[b] + 'もう一度押すと未確認に戻る') + '">' + BTN_LABEL[b] + '</button>').join('');
    }
    async function post(btn, body, next) {
      const c = o.current();
      if (!c) return;
      const id = c[0], page = o.page(), title = o.title(id);
      const req = Object.assign({ page, id }, title ? { title } : {}, body);
      try {
        const r = await api('/_talk/api/user', req);
        if (body.comment) { ta.value = ''; store.del(draftKey(page, id)); fit(); }
        o.update(id, r, next);
        const b = body.status !== undefined && document.querySelector('#stBtns button[data-st="' + btn.dataset.st + '"]');
        notice(b || btn, '送信しました');
      } catch (e) { notice(btn, 'エラー: ' + e.message); }
    }
    // a status press moves on to the next row; text in the box goes with it (a note on OK, the request for 直して)
    $('stBtns').addEventListener('click', e => {
      const btn = e.target.closest('button');
      const c = o.current();
      if (!btn || !c) return;
      const status = ((c[1] && c[1].status) || '') === btn.dataset.st ? '' : btn.dataset.st;
      const text = ta.value.trim();
      const body = status && text ? { status, comment: text } : { status };
      post(btn, body, status ? o.next() : null);
    });
    const send = () => { const text = ta.value.trim(); if (text) post($('send'), { comment: text }, null); };   // stays on the row
    $('send').addEventListener('click', send);
    ta.addEventListener('input', () => { fit(); saveDraft(); });
    ta.addEventListener('keydown', e => { if (e.key === 'Enter' && e.ctrlKey) { e.preventDefault(); send(); } });
    return { show };
  }

  // ------------------------------------------------------------ dockable panels
  // #work: #upper (#dockL, #center = the drawing, #dockR) over #dockB. The panels [data-panel] (list = 一覧, detail = 詳細)
  // move between the docks by their title bar; a dock without panels takes no space. Arrangement and sizes are kept
  // under <prefix>.layout; onChange() after a change (the drawing refits / redraws).
  const DOCKS = ['L', 'R', 'B'];
  const DOCK_NAME = { L: '左', R: '右', B: '下' };
  const PANEL_NAME = { list: '一覧', detail: '詳細' };
  function docks(prefix, onChange) {
    const key = prefix + '.layout', work = $('work'), upper = $('upper');
    const panels = {};
    document.querySelectorAll('[data-panel]').forEach(p => { panels[p.dataset.panel] = p; });
    const DEF = { docks: { L: ['list', 'detail'], R: [], B: [] }, size: { L: 400, R: 380, B: 260 }, ratio: { L: 0.5, R: 0.5, B: 0.5 } };
    let lay = JSON.parse(JSON.stringify(DEF));
    try {
      const s = JSON.parse(store.get(key) || 'null');
      const all = s && [].concat(...DOCKS.map(d => s.docks[d] || []));
      if (all && all.length === 2 && all.includes('list') && all.includes('detail')) {
        lay = { docks: { L: s.docks.L || [], R: s.docks.R || [], B: s.docks.B || [] }, size: Object.assign({}, DEF.size, s.size),
                ratio: Object.assign({}, DEF.ratio, s.ratio) };
      }
    } catch (_) {}
    const save = () => store.set(key, JSON.stringify(lay));
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    const done = () => { save(); onChange(); };

    // a bar dragged with the pointer: move(ev) while dragging, then end()
    const bar = (el, move, end) => el.addEventListener('pointerdown', e => {
      if (e.button !== 0) return;
      e.preventDefault();
      el.classList.add('drag');
      el.setPointerCapture(e.pointerId);
      const mv = ev => move(ev);
      const up = () => { el.classList.remove('drag'); el.removeEventListener('pointermove', mv); el.removeEventListener('pointerup', up); end(); };
      el.addEventListener('pointermove', mv);
      el.addEventListener('pointerup', up);
    });

    function sizes() {
      for (const d of DOCKS) {
        work.style.setProperty('--dock' + d, lay.size[d] + 'px');
        lay.docks[d].forEach((n, i) => {
          panels[n].style.flex = lay.docks[d].length < 2 ? '1 1 0' : i === 0 ? '0 0 calc(' + (lay.ratio[d] * 100).toFixed(2) + '% - 3px)' : '1 1 0';
        });
      }
    }
    function apply() {
      for (const d of DOCKS) {
        const el = $('dock' + d), names = lay.docks[d], kids = [];
        names.forEach((n, i) => {
          if (i) {                                   // between two panels of one dock
            const s = document.createElement('div');
            s.className = 'psplit';
            s.title = 'ドラッグで大きさを変更';
            bar(s, ev => {
              const r = el.getBoundingClientRect();
              lay.ratio[d] = clamp(d === 'B' ? (ev.clientX - r.left) / r.width : (ev.clientY - r.top) / r.height, 0.1, 0.9);
              sizes();
            }, done);
            kids.push(s);
          }
          kids.push(panels[n]);
        });
        el.replaceChildren(...kids);
        el.hidden = !names.length;
        work.querySelector('.dsplit[data-dock="' + d + '"]').hidden = !names.length;
      }
      sizes();
    }
    // splitters between a dock and the drawing
    work.querySelectorAll('.dsplit').forEach(s => {
      const d = s.dataset.dock;
      s.title = 'ドラッグで大きさを変更';
      bar(s, ev => {
        const r = work.getBoundingClientRect();
        if (d === 'L') lay.size.L = clamp(ev.clientX - r.left, 180, r.width - 240);
        else if (d === 'R') lay.size.R = clamp(r.right - ev.clientX, 180, r.width - 240);
        else lay.size.B = clamp(r.bottom - ev.clientY, 80, r.height - 120);
        sizes();
      }, done);
    });

    // moving a panel: the drop zones 左 / 右 / 下 show over the window; in a dock holding the other panel it goes before
    // or after that panel by the pointer's position. Esc cancels.
    function zoneOf(name, ev) {
      const r = work.getBoundingClientRect(), u = upper.getBoundingClientRect();
      const x = ev.clientX - r.left, y = ev.clientY - r.top;
      const d = x < r.width * 0.22 ? 'L' : x > r.width * 0.78 ? 'R' : y > r.height * 0.7 ? 'B' : null;
      if (!d) return null;
      const other = lay.docks[d].some(n => n !== name);
      const before = other && (d === 'B' ? x < r.width / 2 : ev.clientY - u.top < u.height / 2);
      return { d, other, before };
    }
    function zoneRect(z, r, u) {                  // [left, top, width, height] in #work
      const W = r.width, H = r.height, top = Math.min(u.height, H * 0.7);
      let b = z.d === 'L' ? [0, 0, W * 0.3, u.height] : z.d === 'R' ? [W * 0.7, 0, W * 0.3, u.height] : [0, top, W, H - top];
      if (z.other) b = z.d === 'B' ? (z.before ? [b[0], b[1], b[2] / 2, b[3]] : [b[0] + b[2] / 2, b[1], b[2] / 2, b[3]])
        : (z.before ? [b[0], b[1], b[2], b[3] / 2] : [b[0], b[1] + b[3] / 2, b[2], b[3] / 2]);
      return b;
    }
    const place = (el, b) => { el.style.left = b[0] + 'px'; el.style.top = b[1] + 'px'; el.style.width = b[2] + 'px'; el.style.height = b[3] + 'px'; };
    function startMove(name, e) {
      if (e.button !== 0) return;
      e.preventDefault();
      const layer = document.createElement('div');
      layer.className = 'dropzones';
      for (const d of DOCKS) {
        const z = document.createElement('div');
        z.className = 'dz';
        z.textContent = DOCK_NAME[d];
        place(z, zoneRect({ d, other: false }, work.getBoundingClientRect(), upper.getBoundingClientRect()));
        layer.appendChild(z);
      }
      const hot = document.createElement('div');
      hot.className = 'dz hot';
      hot.hidden = true;
      layer.appendChild(hot);
      work.appendChild(layer);
      document.body.classList.add('moving');
      let target = null;
      const mv = ev => {
        target = zoneOf(name, ev);
        hot.hidden = !target;
        if (!target) return;
        place(hot, zoneRect(target, work.getBoundingClientRect(), upper.getBoundingClientRect()));
        const side = !target.other ? '' : target.d === 'B' ? (target.before ? '（左側）' : '（右側）') : (target.before ? '（上側）' : '（下側）');
        hot.textContent = PANEL_NAME[name] + ' → ' + DOCK_NAME[target.d] + side;
      };
      const finish = commit => {
        removeEventListener('pointermove', mv);
        removeEventListener('pointerup', up);
        removeEventListener('keydown', esc, true);
        layer.remove();
        document.body.classList.remove('moving');
        if (!commit || !target) return;
        for (const d of DOCKS) lay.docks[d] = lay.docks[d].filter(n => n !== name);
        if (target.before) lay.docks[target.d].unshift(name); else lay.docks[target.d].push(name);
        apply();
        done();
      };
      const up = () => finish(true);
      const esc = ev => { if (ev.key === 'Escape') { ev.preventDefault(); ev.stopImmediatePropagation(); finish(false); } };
      addEventListener('pointermove', mv);
      addEventListener('pointerup', up);
      addEventListener('keydown', esc, true);
    }
    for (const [name, p] of Object.entries(panels)) {
      const t = p.querySelector('.ptitle');
      t.title = 'ドラッグで移動（左・右・下）';
      t.addEventListener('pointerdown', e => startMove(name, e));
    }
    apply();
  }

  // ↑ / ↓: the previous / next row of the list as shown; not while typing or while the figure or a panel is open
  function arrowKeys(blocked, go) {
    document.addEventListener('keydown', e => {
      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp' || e.altKey || e.ctrlKey || e.metaKey) return;
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName) || figureOpen() || blocked()) return;
      e.preventDefault();
      go(e.key === 'ArrowDown' ? 1 : -1);
    });
  }

  function init(view) {
    initHeader(view);
    $('lb').addEventListener('click', closeFigure);
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && figureOpen()) { e.stopImmediatePropagation(); closeFigure(); } }, true);
  }

  window.Review = {
    $, esc, store, api, notice, init, connect, markSeen, onInfo, info: () => info, takeRef,
    STATUS_BTNS, BTN_LABEL, ST_NAME, ST_MARK, DONE, SEVERITIES, SEV_MEANING, OWNER_MEANING, stClass, isWaiting, isUnread,
    filters, sevChip, ownerChip, rowHtml, groupHtml, neighbor, askHtml, notesHtml, msgsHtml, threadHtml,
    openFigure, closeFigure, figureOpen, bindFigures, footer, docks, arrowKeys,
  };
})();
