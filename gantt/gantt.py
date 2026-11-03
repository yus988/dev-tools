#!/usr/bin/env python3
"""gantt: a small Gantt chart. An agent writes a JSON file, a person adjusts it in a desktop window (drag bars, edit
fields), and the same renderer writes a static SVG / PNG / PDF to share or embed in an HTML report.

  python gantt.py app [FILE] [--day N]                  desktop window (pywebview)
  python gantt.py serve FILE [--port 8195] [--host H]   the same UI in a browser
  python gantt.py svg FILE [-o OUT] [--day N] [--no-legend]
  python gantt.py png FILE [-o OUT] [--day N] [--scale 2]    needs Edge / Chrome (env GANTT_BROWSER)
  python gantt.py pdf FILE [-o OUT] [--day N]                one page sized to the chart
  python gantt.py share FILE                            write the files listed in the data's "share"
  python gantt.py embed FILE PAGE [--day N]             SVG between <!-- gantt:begin --> and <!-- gantt:end -->
  python gantt.py changes FILE [--peek] [--all]         what the person changed since the agent last looked
  python gantt.py check FILE                            validate
  python gantt.py selftest FILE                         open the app hidden, check that the page loads

FILE is a *.gantt.json (README.md). Standard library only; app / selftest import pywebview.
"""
import argparse
import copy
import datetime as dt
import html
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.join(os.path.expanduser('~'), '.gantt')      # outside AppData on purpose (packaged shells redirect it)
STORE = os.path.join(HOME, 'webview')
ICON = os.path.join(HERE, 'gantt.ico')
KINDS = ('plan', 'tbd', 'due', 'ext', 'wait', 'off', 'done')
LABELS = {'plan': '作業', 'tbd': '要相談', 'due': '締め切り・発注', 'ext': '外の期日', 'wait': '待ち', 'off': '休み',
          'done': '済み'}
ITEM_FIELDS = ('from', 'to', 'on', 'label', 'kind', 'note')
ROW_FIELDS = {'title': '項目', 'owner': '担当', 'link': 'リンク', 'note': 'メモ', 'group': '区切り名'}
META_FIELDS = {'title': '題名', 'from': '開始日', 'to': '終了日', 'link_base': 'リンクの基準'}
COMPACT = ('items', 'off', 'lines', 'log')                 # elements of these lists go on one line each
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
LOG_KEEP = 500
BEGIN, END = '<!-- gantt:begin -->', '<!-- gantt:end -->'


class Conflict(Exception):
    """The file changed under the UI (an item moved or is gone)."""


# ---------------------------------------------------------------- data file
def pd(s):
    """'YYYY-MM-DD' -> date, or None."""
    if isinstance(s, str) and DATE_RE.match(s):
        try:
            return dt.date.fromisoformat(s)
        except ValueError:
            pass
    return None


def md(d):
    return '%d/%d' % (d.month, d.day)


def read_text(path):
    with open(path, 'rb') as f:
        return f.read().decode('utf-8-sig')


def new_id(rows):
    used = {r.get('id') for r in rows if isinstance(r, dict)}
    n = 1
    while 'r%d' % n in used:
        n += 1
    return 'r%d' % n


def normalize(data):
    """Shape check; rows without an id get r1, r2, ... (unique)."""
    if not isinstance(data, dict):
        raise ValueError('the top level must be an object')
    rows = data.setdefault('rows', [])
    if not isinstance(rows, list):
        raise ValueError('rows must be a list')
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise ValueError('rows[%d] must be an object' % i)
        if not isinstance(r.get('items', []), list) or any(not isinstance(x, dict) for x in r.get('items', [])):
            raise ValueError('rows[%d].items must be a list of objects' % i)
    for r in rows:
        if not r.get('id'):
            r['id'] = new_id(rows)
    for k in ('off', 'lines', 'log'):
        if not isinstance(data.get(k, []), list):
            raise ValueError('%s must be a list' % k)
    return data


def load(path):
    return normalize(json.loads(read_text(path)))


def _one(v):
    return json.dumps(v, ensure_ascii=False, separators=(', ', ': '))


def _flat(row):
    """A row without items (a group header) stays on one line too."""
    return isinstance(row, dict) and not any(isinstance(y, (list, dict)) for y in row.values())


def _enc(v, lvl, key=None):
    pad = ' ' * lvl
    if isinstance(v, dict) and v:
        parts = ['%s %s: %s' % (pad, json.dumps(k, ensure_ascii=False), _enc(x, lvl + 1, k)) for k, x in v.items()]
        return '{\n' + ',\n'.join(parts) + '\n' + pad + '}'
    if isinstance(v, list) and v:
        parts = ['%s %s' % (pad, _one(x) if key in COMPACT or key == 'rows' and _flat(x) else _enc(x, lvl + 1)) for x in v]
        return '[\n' + ',\n'.join(parts) + '\n' + pad + ']'
    return json.dumps(v, ensure_ascii=False)


def dumps(data):
    """1-space indent, readable in git diffs: every element of items / off / lines / log on one line."""
    return _enc(data, 0) + '\n'


def write_bytes(path, body):
    """Write beside, then replace; Windows: a sync client or an editor may hold the file for a moment."""
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(body)
    for i in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 19:
                raise
            time.sleep(0.1)


def save(path, data):
    write_bytes(path, dumps(data).encode('utf-8'))


def copy_over(src, dst):
    """Copy to the final name in one step (the same name every time: people keep the link)."""
    for i in range(20):
        try:
            shutil.copyfile(src, dst)
            return
        except PermissionError:
            if i == 19:
                raise
            time.sleep(0.1)


def legend_map(data):
    over = data.get('legend') if isinstance(data.get('legend'), dict) else {}
    return {k: str(over.get(k) or LABELS[k]) for k in KINDS}


def kind_of(it):
    return it.get('kind') if it.get('kind') in KINDS else 'plan'


def is_group(r):
    return 'group' in r


# ---------------------------------------------------------------- ops
def row_of(data, rid):
    for r in data['rows']:
        if r.get('id') == rid:
            return r
    raise ValueError('unknown row: %s' % rid)


def name_of(r):
    return str(r.get('title') or r.get('group') or r.get('id'))


def item_name(it):
    return str(it.get('label') or '無題')


def span(it):
    def one(x):
        d = pd(x)
        return md(d) if d else str(x)
    if it.get('on') is not None:
        return one(it['on'])
    return '%s〜%s' % (one(it.get('from')), one(it.get('to')))


def shown(v):
    v = '' if v is None else str(v)
    return (v if len(v) <= 40 else v[:39] + '…') if v else '（なし）'


def next_time(log, floor=None):
    """Local time for a new log entry; strictly after the last one (and after `floor`), so that `seen` never hides it."""
    now = dt.datetime.now().replace(microsecond=0)
    for t in ([log[-1].get('time')] if log else []) + [floor]:
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
            try:
                last = dt.datetime.strptime(str(t), fmt)
            except ValueError:
                continue
            if now <= last:
                now = last + dt.timedelta(seconds=1)
            break
    return now.strftime('%Y-%m-%d %H:%M:%S')


def clean_item(it):
    """Make `it` a valid bar (from, to) or milestone (on) in place; reversed dates are swapped."""
    for k in [k for k, v in it.items() if v is None]:
        del it[k]
    for k in ('label', 'note', 'kind'):
        if k in it and not isinstance(it[k], str):
            raise ValueError('%s must be a string' % k)
    if 'on' in it:
        if 'from' in it or 'to' in it:
            raise ValueError('an item is a bar (from, to) or a milestone (on), not both')
        if not pd(it['on']):
            raise ValueError('on: YYYY-MM-DD')
    else:
        a, b = pd(it.get('from')), pd(it.get('to'))
        if not a or not b:
            raise ValueError('a bar needs from and to (YYYY-MM-DD)')
        if a > b:
            it['from'], it['to'] = it['to'], it['from']
    if 'kind' in it and it['kind'] not in KINDS:
        it['kind'] = 'plan'
    return it


def ordered(it):
    first = [k for k in ('on', 'from', 'to', 'label', 'kind', 'note') if k in it]
    return dict([(k, it[k]) for k in first] + [(k, v) for k, v in it.items() if k not in first])


def _check_expect(it, expect):
    for k, v in (expect or {}).items():
        cur = it.get(k)
        if k == 'kind':
            cur, v = cur or 'plan', v or 'plan'
        if cur != v:
            raise Conflict('%s changed: %s -> %s' % (k, v, cur))


def _item_at(row, o):
    items = row.get('items') or []
    i = o.get('i')
    if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(items):
        raise Conflict('item %s is gone' % i)
    _check_expect(items[i], o.get('expect'))
    return items, i


def _set_fields(src, set_, allowed):
    out = dict(src)
    for k, v in (set_ or {}).items():
        if k not in allowed:
            raise ValueError('cannot set %s' % k)
        if v is None:
            out.pop(k, None)
        else:
            out[k] = v
    return out


def op_item(d, o):
    row = row_of(d, o.get('row'))
    items, i = _item_at(row, o)
    old, new = items[i], _set_fields(items[i], o.get('set'), ITEM_FIELDS)
    clean_item(new)
    parts = []
    if span(old) != span(new) or ('on' in old) != ('on' in new):
        parts.append('%s → %s' % (span(old), span(new)))
    lg = legend_map(d)
    for k, nm in (('label', '名前'), ('kind', '種類'), ('note', 'メモ')):
        a, b = old.get(k), new.get(k)
        if k == 'kind':
            a, b = lg[kind_of(old)], lg[kind_of(new)]
        if a != b:
            parts.append('%s: %s → %s' % (nm, shown(a), shown(b)))
    old_name = item_name(old)
    old.clear()
    old.update(new)
    return '「%s」の「%s」: %s' % (name_of(row), old_name, '、'.join(parts)) if parts else None


def op_add_item(d, o):
    row = row_of(d, o.get('row'))
    if is_group(row):
        raise ValueError('a group row has no items')
    src = o.get('item')
    if not isinstance(src, dict) or set(src) - set(ITEM_FIELDS):
        raise ValueError('item: an object of %s' % ', '.join(ITEM_FIELDS))
    it = ordered(clean_item(dict(src)))
    row.setdefault('items', []).append(it)
    return '「%s」に「%s」（%s）を追加' % (name_of(row), item_name(it), span(it))


def op_del_item(d, o):
    row = row_of(d, o.get('row'))
    items, i = _item_at(row, o)
    it = items.pop(i)
    return '「%s」の「%s」（%s）を削除' % (name_of(row), item_name(it), span(it))


def op_row(d, o):
    row = row_of(d, o.get('row'))
    new = _set_fields(row, o.get('set'), ROW_FIELDS)
    for k in ROW_FIELDS:
        if k in new and not isinstance(new[k], str):
            raise ValueError('%s must be a string' % k)
    if 'group' in new and 'group' not in row and row.get('items'):
        raise ValueError('a row with items cannot become a group')
    parts = ['%s: %s → %s' % (nm, shown(row.get(k)), shown(new.get(k))) for k, nm in ROW_FIELDS.items()
             if row.get(k) != new.get(k)]
    old_name = name_of(row)
    row.clear()
    row.update(new)
    return '「%s」の%s' % (old_name, '、'.join(parts)) if parts else None


def op_add_row(d, o):
    rows = d['rows']
    src = o.get('row')
    if not isinstance(src, dict):
        raise ValueError('row: an object')
    row = {k: v for k, v in src.items() if v is not None}
    if is_group(row) and row.get('items'):
        raise ValueError('a group row has no items')
    if not isinstance(row.get('items', []), list):
        raise ValueError('items must be a list')
    if 'items' in row:
        row['items'] = [ordered(clean_item(dict(x))) for x in row['items']]
    if row.get('id') in [r.get('id') for r in rows]:
        raise ValueError('duplicate row id: %s' % row['id'])
    row = dict([('id', row.get('id') or new_id(rows))] + [(k, v) for k, v in row.items() if k != 'id'])
    after = o.get('after')
    rows.insert(len(rows) if after is None else rows.index(row_of(d, after)) + 1, row)
    return '行「%s」を追加' % name_of(row)


def op_del_row(d, o):
    row = row_of(d, o.get('row'))
    d['rows'].remove(row)
    return '行「%s」を削除' % name_of(row)


def op_move_row(d, o):
    row = row_of(d, o.get('row'))
    step = o.get('dir')
    if step not in (-1, 1):
        raise ValueError('dir: -1 or 1')
    rows = d['rows']
    i = rows.index(row)
    if not 0 <= i + step < len(rows):
        return None
    rows[i], rows[i + step] = rows[i + step], rows[i]
    return '「%s」を%s移動' % (name_of(row), '上へ' if step < 0 else '下へ')


def op_meta(d, o):
    new = _set_fields(d, o.get('set'), META_FIELDS)
    for k in META_FIELDS:
        if k in new and not isinstance(new[k], str):
            raise ValueError('%s must be a string' % k)
    for k in ('from', 'to'):
        if k in new and not pd(new[k]):
            raise ValueError('%s: YYYY-MM-DD' % k)
    if pd(new.get('from')) and pd(new.get('to')) and pd(new['from']) > pd(new['to']):
        raise ValueError('from must not be after to')

    def fmt(k, v):
        d_ = pd(v)
        return md(d_) if k in ('from', 'to') and d_ else shown(v)
    parts = ['%s: %s → %s' % (nm, fmt(k, d.get(k)), fmt(k, new.get(k))) for k, nm in META_FIELDS.items()
             if d.get(k) != new.get(k)]
    d.clear()
    d.update(new)
    return '全体の' + '、'.join(parts) if parts else None


OPS = {'item': op_item, 'add_item': op_add_item, 'del_item': op_del_item, 'row': op_row, 'add_row': op_add_row,
       'del_row': op_del_row, 'move_row': op_move_row, 'meta': op_meta}


def apply(data, ops, by='user'):
    """Apply a batch of ops to `data` (all or nothing); append a log entry per change; return the log texts.
    Raises Conflict (the UI saw something else) or ValueError (bad op)."""
    if not isinstance(ops, list):
        raise ValueError('ops must be a list')
    work = copy.deepcopy(data)
    texts = []
    for o in ops:
        fn = OPS.get(o.get('op')) if isinstance(o, dict) else None
        if not fn:
            raise ValueError('unknown op: %s' % (o.get('op') if isinstance(o, dict) else o))
        t = fn(work, o)
        if t:
            texts.append(t)
    log = work.setdefault('log', [])
    for t in texts:
        log.append({'time': next_time(log), 'by': by, 'text': t})
    del log[:-LOG_KEEP]
    data.clear()
    data.update(work)
    return texts


# ---------------------------------------------------------------- check
def check(data):
    """(problems, notes): problems make `check` fail; notes are only printed."""
    bad, note = [], []
    d0, d1 = pd(data.get('from')), pd(data.get('to'))
    if not d0 or not d1:
        bad.append('from / to: YYYY-MM-DD each (the range shown)')
    elif d0 > d1:
        bad.append('from is after to')
    ids = set()

    def dates(where, o, out_ok):
        if o.get('on') is not None:
            if 'from' in o or 'to' in o:
                bad.append('%s: both on and from/to' % where)
            a = pd(o['on'])
            if not a:
                bad.append('%s: on is not YYYY-MM-DD' % where)
            elif d0 and d1 and not d0 <= a <= d1 and out_ok:
                note.append('%s: outside the range, not drawn' % where)
        else:
            a, b = pd(o.get('from')), pd(o.get('to'))
            if not a or not b:
                bad.append('%s: from / to missing or not YYYY-MM-DD' % where)
            elif a > b:
                bad.append('%s: from is after to' % where)
            elif d0 and d1 and (b < d0 or a > d1) and out_ok:
                note.append('%s: outside the range, not drawn' % where)

    for i, r in enumerate(data.get('rows') or []):
        rid = r.get('id')
        where = 'row %s' % (rid or i)
        if not rid:
            bad.append('%s: no id' % where)
        elif rid in ids:
            bad.append('%s: duplicate id' % where)
        ids.add(rid)
        if is_group(r) and r.get('items'):
            bad.append('%s: a group row has items' % where)
        for j, it in enumerate(r.get('items') or []):
            w = '%s item %d' % (where, j)
            dates(w, it, True)
            if 'kind' in it and it['kind'] not in KINDS:
                bad.append('%s: unknown kind %r (%s)' % (w, it['kind'], ', '.join(KINDS)))
    for k in ('off', 'lines'):
        for j, o in enumerate(data.get(k) or []):
            dates('%s[%d]' % (k, j), o, False)
    return bad, note


# ---------------------------------------------------------------- render
def f(x):
    s = ('%.2f' % x).rstrip('0').rstrip('.')
    return '0' if s in ('-0', '') else s


_BAD = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]')


def esc(s):
    return html.escape(_BAD.sub('', str(s)), quote=True)


def tw(s, size):
    """Width estimate: CJK / full-width = 1 em, anything else 0.58 em."""
    return size * sum(1.0 if ord(c) >= 0x2E80 else 0.58 for c in s)


def fit(s, avail, size):
    """(text that fits in `avail`, truncated?). The text may be empty when not even the ellipsis fits."""
    if tw(s, size) <= avail:
        return s, False
    for n in range(len(s) - 1, 0, -1):
        if tw(s[:n] + '…', size) <= avail:
            return s[:n] + '…', True
    return '', True


def resolve_link(link, base, folder):
    """Absolute URL as is; relative against link_base, else against the data file's folder. Only http / https / file."""
    if not isinstance(link, str) or not link.strip():
        return None
    link = link.strip().replace('\\', '/')
    m = re.match(r'^([A-Za-z][A-Za-z0-9+.-]*):', link)
    if m and len(m.group(1)) == 1:                           # C:/path
        url = pathlib.Path(link).as_uri()
    elif m:
        url = link
    elif base:
        url = urllib.parse.urljoin(base, link)
    elif folder:
        url = urllib.parse.urljoin(pathlib.Path(folder).resolve().as_uri() + '/', link)
    else:
        return link
    return url if urllib.parse.urlsplit(url).scheme.lower() in ('http', 'https', 'file') else None


KIND_CSS = {
    'plan': 'fill:var(--rec,#e8f0fb);stroke:var(--recs,#3d6fb6)',
    'tbd': 'fill:url(#gt-h);stroke:var(--recs,#3d6fb6);stroke-dasharray:3 2',
    'due': 'fill:var(--warn,#fff3e0);stroke:var(--warns,#d08a2a)',
    'ext': 'fill:var(--ng,#fbe9e7);stroke:var(--ngs,#c0504d)',
    'wait': 'fill:url(#gt-w);stroke:var(--line,#d7dbe3)',
    'off': 'fill:var(--line,#d7dbe3);stroke:none',
    'done': 'fill:var(--ok,#e6f4ea);stroke:var(--oks,#4b9a5f)',
}
CSS = (
    '.gantt{font-family:var(--gantt-font,"Yu Gothic UI","Meiryo","Hiragino Sans",sans-serif);font-size:11.5px}'
    '.gantt text{fill:var(--ink,#1d2330)}.gantt .mu{fill:var(--muted,#5b6475)}.gantt .bd{font-weight:700}'
    '.gantt .g{font-weight:700;fill:var(--gantt-group,var(--ink,#1d2330))}'
    '.gantt .sl,.gantt .hw{font-size:10px}.gantt .hm{font-size:11px;font-weight:700}'
    '.gantt .lw{fill:var(--warns,#d08a2a)}.gantt .se{text-anchor:end}'
    '.gantt a.lnk:hover text{text-decoration:underline}.gantt .go{fill:var(--accent,#a3385f)}'
    '.gantt .bg{fill:var(--card,#fff)}.gantt .hd,.gantt .grp{fill:var(--head,#eef1f6)}'
    '.gantt .we{fill:var(--line,#d7dbe3);fill-opacity:.25}.gantt .off{fill:var(--line,#d7dbe3);fill-opacity:.4}'
    '.gantt .gw,.gantt .gd,.gantt .sep{fill:none;stroke:var(--line,#d7dbe3);stroke-width:1}.gantt .gd{stroke-opacity:.45}'
    '.gantt .ln{fill:none;stroke:var(--warns,#d08a2a);stroke-width:1.2;stroke-dasharray:4 3}'
    '.gantt .td{fill:none;stroke:var(--accent,#a3385f);stroke-width:1.5;pointer-events:none}'
    '.gantt .p1{fill:var(--card,#fff)}.gantt .p2{fill:var(--rec,#e8f0fb)}.gantt .p3{fill:var(--line,#d7dbe3)}'
    '.gantt .b,.gantt .m{stroke-width:1}'
    + ''.join('.gantt .b.k-%s,.gantt .m.k-%s{%s}' % (k, k, v) for k, v in KIND_CSS.items()))
CSS_UI = '.gantt .hov,.gantt .hit,.gantt .tc{fill:transparent}.gantt .hov{pointer-events:none}'
DEFS = ('<defs><pattern id="gt-h" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
        '<rect class="p1" width="6" height="6"/><rect class="p2" width="3" height="6"/></pattern>'
        '<pattern id="gt-w" width="5" height="6" patternUnits="userSpaceOnUse"><rect class="p3" width="2" height="6"/>'
        '</pattern></defs>')
HEAD_H, ROW_H, GROUP_H, STRIP_H, FS = 36, 24, 22, 12, 11.5


def date_range(data):
    d0, d1 = pd(data.get('from')), pd(data.get('to'))
    if not d0 or not d1:                                    # derive it from what is there
        ds = []
        for r in data.get('rows') or []:
            for it in r.get('items') or []:
                ds += [pd(it.get(k)) for k in ('from', 'to', 'on')]
        for k in ('off', 'lines'):
            for o in data.get(k) or []:
                ds += [pd(o.get(x)) for x in ('from', 'to', 'on')]
        ds = [x for x in ds if x]
        a = min(ds) if ds else dt.date.today()
        b = max(ds) if ds else a + dt.timedelta(days=27)
        d0, d1 = a - dt.timedelta(days=a.weekday()), b + dt.timedelta(days=6 - b.weekday())
    if d1 < d0:
        d1 = d0
    return d0, min(d1, d0 + dt.timedelta(days=1499))


def _draw(data, day=14, interactive=False, legend=False, today=None, base_dir=None, zoom=False):
    day = max(6, min(40, int(day)))
    d0, d1 = date_range(data)
    nd = (d1 - d0).days + 1
    rows = [r for r in data.get('rows') or [] if isinstance(r, dict)]
    tcol = int(data.get('label_width') or 240)
    ocol = 80 if any(r.get('owner') for r in rows) else 0
    x0 = tcol + ocol
    W = x0 + nd * day
    day1 = dt.timedelta(days=1)

    def X(d):
        return x0 + (d - d0).days * day

    def clip(a, b):
        a, b = max(a, d0), min(b, d1)
        return (a, b) if a <= b else None

    offs = [(clip(pd(o.get('from')), pd(o.get('to'))), o.get('label')) for o in data.get('off') or []
            if isinstance(o, dict) and pd(o.get('from')) and pd(o.get('to'))]
    offs = [(c, lb) for c, lb in offs if c]
    lines = [(pd(o.get('on')), o.get('label')) for o in data.get('lines') or [] if isinstance(o, dict)]
    lines = [(d, lb) for d, lb in lines if d and d0 <= d <= d1]
    strip = STRIP_H if any(lb for _, lb in offs + lines) else 0
    by = HEAD_H
    y = by + strip
    ys = []
    for r in rows:
        h = GROUP_H if is_group(r) else ROW_H
        ys.append((y, h))
        y += h
    bend = y
    bh = bend - by
    used = []
    for r in rows:
        for it in r.get('items') or []:
            k = kind_of(it)
            if k not in used and (pd(it.get('on')) or (pd(it.get('from')) and pd(it.get('to')))):
                used.append(k)
    used = [k for k in KINDS if k in used]
    lg = legend_map(data)
    leg, lx, ly = [], 8.0, bend + 8
    if legend and used:
        for k in used:
            w = 19 + tw(lg[k], 11) + 16
            if lx > 8 and lx + w > W - 8:
                lx, ly = 8.0, ly + 18
            leg.append('<rect class="b k-%s" x="%s" y="%s" width="14" height="10" rx="2"/>'
                       '<text class="mu" x="%s" y="%s">%s</text>' % (k, f(lx), f(ly), f(lx + 19), f(ly + 9), esc(lg[k])))
            lx += w
        H = ly + 18
    else:
        H = bend + 1

    o = []
    P = o.append
    # --- backgrounds, shades, grid
    P('<rect class="bg" x="0" y="%d" width="%s" height="%s"/><rect class="hd" x="0" y="0" width="%s" height="%d"/>'
      % (by, f(W), f(bh), f(W), HEAD_H))
    g = ''.join('M0 %s h%s v%d h-%s z' % (f(yy), f(W), h, f(W)) for r, (yy, h) in zip(rows, ys) if is_group(r))
    if g:
        P('<path class="grp" d="%s"/>' % g)
    we, d = [], d0
    while d <= d1:
        if d.weekday() >= 5:
            n = 2 if d.weekday() == 5 and d + day1 <= d1 else 1
            we.append('M%s %d h%s v%s h-%s z' % (f(X(d)), by, f(n * day), f(bh), f(n * day)))
            d += dt.timedelta(days=n)
        else:
            d += day1
    if we:
        P('<path class="we" d="%s"/>' % ''.join(we))
    if offs:
        P('<path class="off" d="%s"/>' % ''.join('M%s %d h%s v%s h-%s z' % (f(X(a)), by, f((b - a).days * day + day), f(bh),
                                                                              f((b - a).days * day + day)) for (a, b), _ in offs))
    mon = d0 + dt.timedelta(days=(7 - d0.weekday()) % 7)
    gw = ['M%s 18 V%s' % (f(X(m)), f(bend)) for m in (mon + dt.timedelta(days=7 * i) for i in range(nd // 7 + 1))
          if m <= d1 and X(m) > x0]
    gw.append('M%s 0 V%s' % (f(x0), f(bend)))
    months = []
    d = d0
    while d <= d1:
        nxt = (d.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        months.append((d, min(nxt - day1, d1)))
        d = nxt
    gw += ['M%s 0 V18' % f(X(a)) for a, _ in months[1:]]
    P('<path class="gw" d="%s"/>' % ''.join(gw))
    if day >= 12:
        P('<path class="gd" d="%s"/>' % ''.join('M%s %d V%s' % (f(X(d0 + dt.timedelta(days=i))), by, f(bend))
                                                  for i in range(1, nd) if (d0 + dt.timedelta(days=i)).weekday() != 0))
    P('<path class="sep" d="M0 18 H%s M0 %d H%s%s"/>' % (f(W), HEAD_H, f(W),
                                                         ''.join(' M0 %s H%s' % (f(yy + h), f(W)) for yy, h in ys)))
    if lines:
        P('<path class="ln" d="%s"/>' % ''.join('M%s %d V%s' % (f(X(d) + day / 2), by, f(bend)) for d, _ in lines))

    # --- rows
    for r, (y, h) in zip(rows, ys):
        rid, grp, cy = str(r.get('id', '')), is_group(r), y + h / 2 + 4
        if interactive:
            P('<g class="row" data-r="%s" data-y="%s" data-h="%d"%s><rect class="hov" x="0" y="%s" width="%s" height="%d"/>'
              % (esc(rid), f(y), h, ' data-g="1"' if grp else '', f(y), f(W), h))
            if not grp:
                P('<rect class="hit" x="%s" y="%s" width="%s" height="%d"/>' % (f(x0), f(y), f(W - x0), h))
        name = str((r.get('group') if grp else r.get('title')) or '')
        url = None if grp else resolve_link(r.get('link'), data.get('link_base'), base_dir)
        s, cut = fit(name, tcol - 14 - (14 if url and interactive else 0), FS)
        t = '<text class="%s" x="8" y="%s">%s%s</text>' % ('g' if grp else 't', f(cy), esc(s),
                                                           '<title>%s</title>' % esc(name) if cut else '')
        if interactive:
            P('<g class="ttl" data-r="%s"><rect class="tc" x="0" y="%s" width="%d" height="%d"/>%s%s</g>'
              % (esc(rid), f(y), tcol, h, t, '<text class="go" data-href="%s" x="%d" y="%s">↗</text>'
                 % (esc(url), tcol - 14, f(cy)) if url else ''))
        else:
            P('<a class="lnk" href="%s" target="_blank">%s</a>' % (esc(url), t) if url else t)
        if ocol and r.get('owner'):
            s, cut = fit(str(r['owner']), ocol - 10, FS)
            P('<text class="mu" x="%d" y="%s">%s%s</text>' % (tcol + 6, f(cy), esc(s),
                                                              '<title>%s</title>' % esc(r['owner']) if cut else ''))
        # items: geometry first (the free space to the right of a label ends at the next item)
        geo = []
        for idx, it in enumerate(r.get('items') or [] if not grp else []):
            k = kind_of(it)
            if it.get('on') is not None:
                a = pd(it['on'])
                if a and d0 <= a <= d1:
                    cx = X(a) + day / 2
                    geo.append((idx, it, k, 'm', cx - 6, cx + 6))
            else:
                a, b = pd(it.get('from')), pd(it.get('to'))
                c = clip(a, b) if a and b and a <= b else None
                if c:
                    geo.append((idx, it, k, 'b', X(c[0]), X(c[1]) + day))
        lefts = sorted(gg[4] for gg in geo)
        for idx, it, k, shape, xl, xr in geo:
            nxt = next((v for v in lefts if v >= xr - 0.01), W)
            label = str(it.get('label') or '')
            cls = 'bt' + (' bd' if k in ('due', 'ext') else ' mu' if k in ('wait', 'off') else '')
            text, tip = '', esc(it['note']) if it.get('note') else ''
            if shape == 'b':
                inside = xr - xl - 8
                if not label:
                    pass
                elif tw(label, FS) <= inside:
                    text = '<text class="%s" x="%s" y="%s">%s</text>' % (cls, f(xl + 4), f(y + 16), esc(label))
                elif tw(label, FS) <= nxt - xr - 6:         # not in the bar: to its right, muted
                    text = '<text class="bt mu" x="%s" y="%s">%s</text>' % (f(xr + 4), f(y + 16), esc(label))
                else:
                    s, _ = fit(label, inside, FS)
                    if s:
                        text = '<text class="%s" x="%s" y="%s">%s<title>%s</title></text>' % (cls, f(xl + 4), f(y + 16), esc(s), esc(label))
                    else:
                        tip = esc(label)                    # not even the ellipsis fits: the full text as a tooltip
            elif label:
                room = nxt - xr - 5
                s, cut = fit(label, room, FS)
                if s:
                    text = '<text class="%s" x="%s" y="%s">%s%s</text>' % (cls, f(xr + 3), f(y + 16), esc(s),
                                                                         '<title>%s</title>' % esc(label) if cut else '')
                else:
                    tip = esc(label)
            tip = '<title>%s</title>' % tip if tip else ''
            attrs = ' data-r="%s" data-i="%d"' % (esc(rid), idx)
            attrs += ' data-on="%s"' % esc(it['on']) if shape == 'm' else ' data-from="%s" data-to="%s"' % (esc(it['from']), esc(it['to']))
            if interactive:
                P('<g class="it k-%s"%s>' % (k, attrs))
            if shape == 'b':
                P('<rect class="b k-%s" x="%s" y="%s" width="%s" height="16" rx="3">%s</rect>' % (k, f(xl), f(y + 4), f(xr - xl), tip))
            else:
                P('<path class="m k-%s" d="M%s %sL%s %sL%s %sL%s %sz">%s</path>'
                  % (k, f(xl), f(y + 12), f(xl + 6), f(y + 6), f(xr), f(y + 12), f(xl + 6), f(y + 18), tip))
            if text:
                P(text)
            if interactive:
                P('</g>')
        if interactive:
            P('</g>')

    # --- today, header text, strip labels, legend
    t0 = today if isinstance(today, dt.date) else pd(today) or dt.date.today()
    if d0 <= t0 <= d1:
        P('<path class="td" d="M%s %d V%s"/>' % (f(X(t0) + day / 2), by, f(bend)))
    for a, b in months:
        label = '%d月' % a.month
        if tw(label, 11) <= (b - a).days * day + day - 6:
            P('<text class="hm" x="%s" y="13">%s</text>' % (f(X(a) + 4), label))
    for i in range(nd // 7 + 1):
        m = mon + dt.timedelta(days=7 * i)
        if m <= d1 and tw(md(m), 10) <= 7 * day - 4:
            P('<text class="hw mu" x="%s" y="31">%s</text>' % (f(X(m) + 3), md(m)))
    if strip:
        for (a, b), lb in offs:
            s, _ = fit(str(lb or ''), (b - a).days * day + day - 6, 10) if lb else ('', False)
            if s:
                P('<text class="sl mu" x="%s" y="%s">%s</text>' % (f(X(a) + 3), by + 9, esc(s)))
        for d, lb in lines:                    # left of the line, so it stays clear of a holiday label right after it
            xl, lb = X(d) + day / 2, str(lb or '')
            if lb and tw(lb, 10) <= xl - 3 - x0:
                P('<text class="sl lw se" x="%s" y="%s">%s</text>' % (f(xl - 3), by + 9, esc(lb)))
                continue
            s, _ = fit(lb, W - xl - 8, 10) if lb else ('', False)
            if s:
                P('<text class="sl lw" x="%s" y="%s">%s</text>' % (f(xl + 3), by + 9, esc(s)))
    o += leg
    svg = ('<svg class="gantt%s" xmlns="http://www.w3.org/2000/svg" width="%s" height="%s" viewBox="0 0 %s %s"%s>'
           % (' zoom' if zoom else '', f(W), f(H), f(W), f(H),
              ' data-x0="%d" data-dw="%d" data-d0="%s" data-d1="%s"' % (x0, day, d0, d1) if interactive else '')
           + '<style>%s%s</style>' % (CSS, CSS_UI if interactive else '') + DEFS + ''.join(o) + '</svg>')
    return svg, W, H


def render(data, day=14, interactive=False, legend=False, today=None, base_dir=None, zoom=False):
    """The chart as one SVG string. base_dir: folder against which relative row links are resolved when the data has
    no link_base. interactive adds the data-* hooks and hit areas app.html uses; zoom adds class `zoom` to the root."""
    return _draw(data, day, interactive, legend, today, base_dir, zoom)[0]


# ---------------------------------------------------------------- png / pdf / share
BROWSERS = [r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
            r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
            r'C:\Program Files\Google\Chrome\Application\chrome.exe',
            r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
            '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome']


class ExportError(Exception):
    pass


def find_browser():
    """$GANTT_BROWSER, else Edge, else Chrome; None when there is none."""
    for p in [os.environ.get('GANTT_BROWSER')] + BROWSERS:
        if p and os.path.isfile(p):
            return p
    for name in ('msedge', 'chrome', 'google-chrome', 'chromium'):
        p = shutil.which(name)
        if p:
            return p
    return None


def page_html(data, svg, w, h, stamp):
    """The chart on a white page sized to it: a title line, then the SVG (light colours = the SVG's fallbacks).
    Returns (html, page width, page height) in CSS px."""
    pad, head = 12, 30
    pw, ph = int(w + 2 * pad + 0.999), int(h + head + 2 * pad + 0.999)
    title = esc(data.get('title') or '')
    body = ('<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>%s</title><style>'
            '@page{size:%dpx %dpx;margin:0}html,body{margin:0;background:#fff;color:#1d2330;color-scheme:light}'
            'body{width:%dpx;height:%dpx;overflow:hidden;padding:%dpx %dpx 0;box-sizing:border-box;'
            'font-family:"Yu Gothic UI","Meiryo","Hiragino Sans",sans-serif}'
            '.h{height:%dpx;font-size:14px;line-height:%dpx}.h b{margin-right:12px}.h span{color:#5b6475;font-size:12px}'
            'svg{display:block}</style></head><body><div class="h"><b>%s</b><span>更新 %s</span></div>%s</body></html>'
            % (title, pw, ph, pw, ph - 2, pad, pad, head, head, title, esc(stamp), svg))
    return body, pw, ph


def export(data, out, fmt, day=14, scale=2.0, base_dir=None, work=None):
    """Render `data` to out (.png / .pdf) with headless Edge / Chrome. The page and the browser profile are written under
    `work` (default: beside out) and removed afterwards; raises ExportError."""
    exe = find_browser()
    if not exe:
        raise ExportError('no Edge / Chrome found (set GANTT_BROWSER to the browser executable)')
    svg, w, h = _draw(data, day, False, True, None, base_dir, False)
    text, pw, ph = page_html(data, svg, w, h, time.strftime('%Y-%m-%d %H:%M'))
    work = work or os.path.dirname(os.path.abspath(out))
    tag = '.gantt-%d-%d' % (os.getpid(), threading.get_ident() % 100000)
    page, prof = os.path.join(work, tag + '.html'), os.path.join(work, tag + '.profile')
    if os.path.exists(out):
        os.remove(out)
    try:
        with open(page, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text)
        args = [exe, '--headless=new', '--disable-gpu', '--no-first-run', '--user-data-dir=' + prof,
                '--virtual-time-budget=3000']
        if fmt == 'pdf':
            args += ['--no-pdf-header-footer', '--print-to-pdf=' + os.path.abspath(out)]
        else:
            args += ['--screenshot=' + os.path.abspath(out), '--window-size=%d,%d' % (pw, ph), '--hide-scrollbars',
                     '--force-device-scale-factor=%s' % f(scale)]
        try:
            subprocess.run(args + [pathlib.Path(page).resolve().as_uri()], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            raise ExportError('%s failed: %s' % (os.path.basename(exe), e))
        if not os.path.isfile(out) or os.path.getsize(out) == 0:
            raise ExportError('%s wrote no %s' % (os.path.basename(exe), fmt))
    finally:
        for p in (page,):
            if os.path.isfile(p):
                os.remove(p)
        shutil.rmtree(prof, ignore_errors=True)         # a fresh profile made by the browser: no links inside


def write_share(path, data):
    """Write each file of data["share"] (format from the extension): render beside the data file, then copy over the
    target, so that a sync client only ever sees the finished file. Returns the paths."""
    targets = data.get('share')
    if not targets or not isinstance(targets, list) or any(not isinstance(t, str) for t in targets):
        raise ExportError('share: the data needs "share": a list of file paths (.pdf / .png / .svg)')
    folder = os.path.dirname(os.path.abspath(path))
    day = clamp_day(data['day']) if isinstance(data.get('day'), int) and not isinstance(data.get('day'), bool) else 14
    done = []
    for t in targets:
        dst = os.path.abspath(os.path.expanduser(t))
        ext = os.path.splitext(dst)[1].lower()
        if ext not in ('.pdf', '.png', '.svg'):
            raise ExportError('share: %s is not .pdf / .png / .svg' % t)
        if not os.path.isdir(os.path.dirname(dst)):
            raise ExportError('share: no folder %s' % os.path.dirname(dst))
        tmp = os.path.join(folder, '.gantt-share-%d%s' % (os.getpid(), ext))
        try:
            if ext == '.svg':
                svg, _, _ = _draw(data, day, False, True, None, folder, False)
                with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
                    fh.write('<?xml version="1.0" encoding="UTF-8"?>\n' + svg + '\n')
            else:
                export(data, tmp, ext[1:], day, base_dir=folder, work=folder)
            copy_over(tmp, dst)
        finally:
            if os.path.isfile(tmp):
                os.remove(tmp)
        done.append(dst)
    return done


# ---------------------------------------------------------------- the page's API (pywebview js_api / POST /api/<name>)
def clamp_day(day):
    return max(6, min(40, int(day)))


def day_of(a, data):
    """--day, else the data's own "day" (its usual width for pictures), else 14."""
    d = data.get('day')
    return clamp_day(a.day or (d if isinstance(d, int) and not isinstance(d, bool) else 0) or 14)


def sans_seen(data):
    return {k: v for k, v in data.items() if k != 'seen'}


class Api:
    """What app.html calls. Public names are the API; state is private (pywebview walks public attributes)."""

    def __init__(self, path, day=14):
        self._path = os.path.abspath(path)
        self._dir = os.path.dirname(self._path)
        self._day = clamp_day(day)
        self._lock = threading.Lock()                       # read - apply - write is one step
        self._undo = []                                     # (text before, text after) per batch this process wrote
        self._window = None
        self._remember = None
        self._title = None

    def _mtime(self):
        st = os.stat(self._path)
        return '%d-%d' % (st.st_mtime_ns // 1000, st.st_size)      # a string: JS numbers would round nanoseconds

    def _read(self):
        text = read_text(self._path)
        return text, normalize(json.loads(text))

    def _view(self, data, day):
        day = clamp_day(day if day is not None else self._day)
        if day != self._day:
            self._day = day
            if self._remember:
                self._remember(day)
        stem = os.path.basename(self._path)
        title = '%s — %s' % (data.get('title') or stem, stem)
        if self._window is not None and title != self._title:
            try:
                self._window.set_title(title)
            except Exception:
                pass
        self._title = title
        return {'svg': render(data, day, interactive=True, base_dir=self._dir),
                'data': {k: v for k, v in data.items() if k != 'log'}, 'mtime': self._mtime(), 'file': stem,
                'day': day, 'legend': legend_map(data), 'kinds': list(KINDS), 'share': bool(data.get('share'))}

    def get(self, day=None):
        with self._lock:
            return self._view(self._read()[1], day)

    def apply(self, ops, day=None):
        with self._lock:
            text, data = self._read()
            try:
                texts = apply(data, ops)                    # the module's apply, not this method
            except Conflict as e:
                return {'ok': False, 'conflict': True, 'error': str(e)}
            except ValueError as e:
                return {'ok': False, 'conflict': False, 'error': str(e)}
            if texts:
                new = dumps(data)
                write_bytes(self._path, new.encode('utf-8'))
                self._undo = (self._undo + [(text, new)])[-100:]
            return dict(self._view(data, day), ok=True)

    def poll(self, mtime=None, day=None):
        with self._lock:
            if self._mtime() == mtime:
                return None
            try:
                return self._view(self._read()[1], day)
            except (ValueError, OSError) as e:
                return {'error': str(e)}

    def undo(self, day=None):
        with self._lock:
            if not self._undo:
                return {'ok': False, 'error': '戻せる操作がありません'}
            before, after = self._undo[-1]
            _, cur = self._read()
            if sans_seen(cur) != sans_seen(normalize(json.loads(after))):
                self._undo = []
                return {'ok': False, 'error': 'ほかで書き換わったので戻せません'}
            old = normalize(json.loads(before))
            old.pop('seen', None)
            if 'seen' in cur:
                old['seen'] = cur['seen']                   # what the agent has read stays read
            gone = [e for e in cur.get('log', []) if e not in old.get('log', [])]
            if cur.get('seen') and any(str(e.get('time')) <= cur['seen'] for e in gone):
                log = old.setdefault('log', [])             # the agent already saw the change: tell it about the undo
                log.append({'time': next_time(log, cur['seen']), 'by': 'user',
                            'text': '元に戻す: ' + ' / '.join(str(e.get('text')) for e in gone)})
            write_bytes(self._path, dumps(old).encode('utf-8'))
            self._undo.pop()
            return dict(self._view(old, day), ok=True)

    def open_link(self, href=None):
        if urllib.parse.urlsplit(str(href or '')).scheme.lower() not in ('http', 'https', 'file'):
            return False
        return webbrowser.open(href)

    def share(self):
        try:
            with self._lock:
                data = self._read()[1]
            return {'ok': True, 'paths': write_share(self._path, data)}
        except (ExportError, OSError, ValueError) as e:
            return {'ok': False, 'error': str(e)}


# ---------------------------------------------------------------- HTTP (serve)
class Handler(BaseHTTPRequestHandler):
    server_version = 'gantt/1'
    api = None
    names = ('127.0.0.1', 'localhost')                      # host names this server answers to
    METHODS = ('get', 'apply', 'poll', 'undo', 'open_link', 'share')

    def log_message(self, *_):
        pass

    def _send(self, code, body, ctype='application/json'):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype + ('; charset=utf-8' if ctype.startswith('text/') or 'json' in ctype else ''))
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'SAMEORIGIN')    # another site cannot frame the page to steer clicks
        self.end_headers()
        self.wfile.write(body)

    def _local(self):
        """Only this server's own page and the agent's clients. Host: another name is DNS rebinding. Origin (every
        POST, other sites' fetches): ours or nothing. Sec-Fetch-Site (also <img>, <script>, forms): another site may
        open the page by a link and nothing else. A POST needs our Origin, or a request of our page (same-origin, or
        our Referer in a browser without Sec-Fetch-Site); clients without a browser send Origin."""
        port = self.server.server_address[1]
        own = tuple('http://%s:%d' % (n, port) for n in self.names)
        h = self.headers
        if (h.get('Host') or '').lower() not in [o[len('http://'):] for o in own]:
            return False
        origin = (h.get('Origin') or '').lower()
        if origin:
            return origin in own                                   # also refuses "null" (sandboxed frames, file:)
        site = (h.get('Sec-Fetch-Site') or '').lower()
        if site in ('cross-site', 'same-site'):
            return (self.command == 'GET' and not self.path.startswith('/api/')
                    and h.get('Sec-Fetch-Mode') == 'navigate' and h.get('Sec-Fetch-Dest') == 'document')
        ref = urllib.parse.urlsplit(h.get('Referer') or '')
        ref = ('%s://%s' % (ref.scheme, ref.netloc)).lower() if ref.netloc else ''
        if ref and ref not in own:
            return False
        return self.command == 'GET' or site == 'same-origin' or bool(ref)

    def _refuse(self):
        port = self.server.server_address[1]
        self._send(403, {'error': 'only the page of http://127.0.0.1:%d; a client without a browser sends the header '
                                  'Origin: http://127.0.0.1:%d' % (port, port)})

    def do_GET(self):
        if not self._local():
            return self._refuse()
        path = self.path.partition('?')[0]
        if path in ('/', '/index.html'):
            with open(os.path.join(HERE, 'app.html'), 'rb') as fh:
                return self._send(200, fh.read(), 'text/html')
        if path == '/favicon.ico':
            with open(ICON, 'rb') as fh:
                return self._send(200, fh.read(), 'image/x-icon')
        self._send(404, {'error': 'not found'})

    def do_POST(self):
        if not self._local():
            return self._refuse()
        name = self.path.partition('?')[0][len('/api/'):] if self.path.startswith('/api/') else ''
        if name not in self.METHODS:
            return self._send(404, {'error': 'not found'})
        if not (self.headers.get('Content-Type') or '').lower().startswith('application/json'):
            return self._send(415, {'error': 'Content-Type: application/json'})
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if n > 5000000:
                raise ValueError('body too large')
            args = json.loads(self.rfile.read(n).decode('utf-8')) if n else []
            if not isinstance(args, list):
                raise ValueError('the body is a JSON array of arguments')
            self._send(200, getattr(self.api, name)(*args))
        except (ValueError, TypeError) as e:
            self._send(400, {'error': str(e)})
        except OSError as e:
            self._send(500, {'error': str(e)})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != 'nt'                   # on Windows SO_REUSEADDR would let two servers share a port


# ---------------------------------------------------------------- command line
def data_stem(path):
    base = os.path.basename(path)
    for suf in ('.gantt.json', '.json'):
        if base.endswith(suf):
            return base[:-len(suf)]
    return base


def need_file(path):
    if not os.path.isfile(path):
        raise SystemExit('gantt: no such file: %s' % path)
    return os.path.abspath(path)


def load_or_exit(path):
    try:
        return load(need_file(path))
    except ValueError as e:
        raise SystemExit('gantt: %s: %s' % (path, e))


def cmd_svg(a):
    path = need_file(a.file)
    data = load_or_exit(path)
    out = a.output or os.path.join(os.path.dirname(path), data_stem(path) + '.svg')
    svg = render(data, day_of(a, data), legend=not a.no_legend, base_dir=os.path.dirname(path))
    write_bytes(out, ('<?xml version="1.0" encoding="UTF-8"?>\n' + svg + '\n').encode('utf-8'))
    print('gantt: wrote %s (%d bytes)' % (out, os.path.getsize(out)))


def cmd_image(a):
    path = need_file(a.file)
    data = load_or_exit(path)
    fmt = a.cmd
    out = a.output or os.path.join(os.path.dirname(path), data_stem(path) + '.' + fmt)
    try:
        export(data, out, fmt, day_of(a, data), getattr(a, 'scale', 2.0), os.path.dirname(path))
    except ExportError as e:
        raise SystemExit('gantt: %s' % e)
    print('gantt: wrote %s (%d bytes)' % (out, os.path.getsize(out)))


def cmd_share(a):
    path = need_file(a.file)
    data = load_or_exit(path)
    try:
        for p in write_share(path, data):
            print('gantt: wrote %s (%d bytes)' % (p, os.path.getsize(p)))
    except ExportError as e:
        raise SystemExit('gantt: %s' % e)


def cmd_embed(a):
    path = need_file(a.file)
    data = load_or_exit(path)
    with open(a.page, 'rb') as fh:
        raw = fh.read()
    b = raw.find(BEGIN.encode())
    e = raw.find(END.encode(), b) if b >= 0 else -1
    if b < 0 or e < 0:
        raise SystemExit('gantt: %s has no %s ... %s markers' % (a.page, BEGIN, END))
    svg = render(data, day_of(a, data), legend=True, base_dir=os.path.dirname(path), zoom=True).encode('utf-8')
    write_bytes(a.page, raw[:b + len(BEGIN)] + b'\n' + svg + b'\n' + raw[e:])
    print('gantt: embedded %d bytes in %s' % (len(svg), a.page))


def cmd_changes(a):
    path = need_file(a.file)
    data = load_or_exit(path)
    seen = str(data.get('seen') or '')
    new = [e for e in data.get('log') or [] if e.get('by') == 'user' and (a.all or str(e.get('time')) > seen)]
    for e in new:
        print('%s  %s' % (e.get('time'), e.get('text')))
    if not new:
        print('(no changes)')
    elif not a.peek:
        data['seen'] = max(str(e.get('time')) for e in new)
        save(path, data)


def cmd_check(a):
    path = need_file(a.file)
    try:
        data = load(path)
    except ValueError as e:
        raise SystemExit('gantt: %s: %s' % (path, e))
    bad, note = check(data)
    for s in note:
        print('note: ' + s)
    for s in bad:
        print('problem: ' + s)
    if bad:
        sys.exit(1)
    print('ok: %d rows, %d items' % (len(data['rows']), sum(len(r.get('items') or []) for r in data['rows'])))


def read_last():
    """What the app window remembers (~/.gantt/last.json): the file and the zoom (the window has no localStorage)."""
    try:
        with open(os.path.join(HOME, 'last.json'), encoding='utf-8') as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_last(**kw):
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(os.path.join(HOME, 'last.json'), 'w', encoding='utf-8') as fh:
            json.dump(dict(read_last(), **kw), fh)
    except OSError:
        pass


def last_file():
    p = read_last().get('file')
    return p if isinstance(p, str) and os.path.isfile(p) else None


def pick_file():
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        p = filedialog.askopenfilename(title='gantt', filetypes=[('Gantt data', '*.gantt.json'), ('JSON', '*.json')])
        root.destroy()
        return p or None
    except Exception:
        return None


def start_gui(api, title, hidden, run=None, store=STORE):
    """Create the window and run the GUI loop (blocks). Returns the window."""
    try:
        import webview
    except ImportError:
        raise SystemExit('gantt: app needs pywebview (pip install pywebview; Windows: pythonnet comes with it). '
                         'Without it use: python gantt.py serve FILE')
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('devtools.gantt')   # own taskbar icon
    except Exception:
        pass
    with open(os.path.join(HERE, 'app.html'), encoding='utf-8') as fh:
        page = fh.read()
    win = webview.create_window(title, html=page, js_api=api, width=1400, height=900, min_size=(800, 500),
                                hidden=hidden, text_select=True)
    api._window = win
    webview.start(run, (win,) if run else None, private_mode=True, storage_path=store,
                  icon=ICON if os.path.isfile(ICON) else None)
    return win


def cmd_app(a):
    path = a.file or last_file() or pick_file()
    if not path:
        raise SystemExit('gantt: no file (python gantt.py app FILE)')
    path = need_file(path)
    data = load_or_exit(path)
    last = read_last()
    write_last(file=path)
    api = Api(path, a.day or last.get('day') or day_of(a, data))
    api._remember = lambda day: write_last(day=day)             # the window has no localStorage: keep the zoom here
    start_gui(api, '%s — %s' % (data.get('title') or data_stem(path), os.path.basename(path)), False)


def cmd_serve(a):
    path = need_file(a.file)
    Handler.api = Api(path, day_of(a, load_or_exit(path)))
    if a.host.lower() not in Handler.names:
        if a.host in ('0.0.0.0', ''):
            print('gantt: listening on all interfaces, but only http://127.0.0.1 / localhost is answered; '
                  'to open it from another device give that address with --host', file=sys.stderr)
        else:
            Handler.names += (a.host.lower(),)
            print('gantt: http://%s:%d/ can be reached from other computers' % (a.host, a.port), file=sys.stderr)
    try:
        srv = Server((a.host, a.port), Handler)
    except OSError as e:
        raise SystemExit('gantt: cannot listen on %s:%d (%s)' % (a.host, a.port, e))
    print('gantt: %s at http://%s:%d/  (Ctrl-C to stop)' % (path, a.host, a.port), flush=True)
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


SELFTEST_JS = ('JSON.stringify({svg: !!document.querySelector("#chart svg.gantt"), '
               'rows: document.querySelectorAll("#chart g.row").length, items: document.querySelectorAll("#chart g.it").length, '
               'errors: window.__errors || [], status: (document.getElementById("status")||{}).textContent, '
               'storage: (function(){try{localStorage.setItem("gantt.t","1");localStorage.removeItem("gantt.t");return "ok"}'
               'catch(e){return "blocked"}})()})')


def cmd_selftest(a):
    path = need_file(a.file)
    api = Api(path, day_of(a, load_or_exit(path)))
    res = {}

    def run(win):
        try:
            end = time.time() + 25
            while time.time() < end:
                try:
                    if win.evaluate_js('!!document.querySelector("#chart svg.gantt")'):
                        break
                except Exception:
                    pass
                time.sleep(0.3)
            res['page'] = json.loads(win.evaluate_js(SELFTEST_JS))
            try:                                           # is the window icon ours? (blue square, not the default)
                from webview.platforms.winforms import BrowserView
                bmp = BrowserView.instances[win.uid].Icon.ToBitmap()
                c = bmp.GetPixel(3, bmp.Height // 2)
                res['icon'] = 'ours' if c.B > c.R + 50 else 'default (rgb %d,%d,%d)' % (c.R, c.G, c.B)
            except Exception as e:
                res['icon'] = 'unknown (%s)' % e
        except Exception as e:
            res['error'] = repr(e)
        finally:
            win.destroy()

    start_gui(api, 'gantt selftest', True, run, STORE + '-selftest')
    p = res.get('page') or {}
    ok = bool(p.get('svg')) and not p.get('errors') and 'error' not in res
    print('selftest: %s  rows=%s items=%s errors=%s localStorage=%s icon=%s%s'
          % ('ok' if ok else 'FAILED', p.get('rows'), p.get('items'), p.get('errors'), p.get('storage'), res.get('icon'),
             '  ' + res['error'] if 'error' in res else ''))
    sys.exit(0 if ok else 1)


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)

    def day(s, default=None):
        s.add_argument('--day', type=int, default=default, help='pixels per day, 6-40 (default: "day" in the data, else 14)')
    s = sub.add_parser('app', help='desktop window')
    s.add_argument('file', nargs='?')
    day(s, None)
    s = sub.add_parser('serve', help='the same UI in a browser')
    s.add_argument('file')
    s.add_argument('--port', type=int, default=8195)
    s.add_argument('--host', default='127.0.0.1')
    day(s)
    s = sub.add_parser('svg', help='static SVG')
    s.add_argument('file')
    s.add_argument('-o', '--output')
    s.add_argument('--no-legend', action='store_true')
    day(s)
    for name in ('png', 'pdf'):
        s = sub.add_parser(name, help='static %s via headless Edge / Chrome' % name.upper())
        s.add_argument('file')
        s.add_argument('-o', '--output')
        if name == 'png':
            s.add_argument('--scale', type=float, default=2.0, help='device pixel ratio (default 2)')
        day(s)
    s = sub.add_parser('share', help='write the files listed in the data\'s "share"')
    s.add_argument('file')
    s = sub.add_parser('embed', help='put the SVG between the gantt:begin / gantt:end markers of an HTML page')
    s.add_argument('file')
    s.add_argument('page')
    day(s)
    s = sub.add_parser('changes', help='what the person changed since the agent last looked')
    s.add_argument('file')
    s.add_argument('--peek', action='store_true', help='do not move "seen"')
    s.add_argument('--all', action='store_true', help='every user entry, not only the new ones')
    s = sub.add_parser('check', help='validate')
    s.add_argument('file')
    s = sub.add_parser('selftest', help='open the app hidden and check that the page loads')
    s.add_argument('file')
    day(s)
    a = p.parse_args()
    {'app': cmd_app, 'serve': cmd_serve, 'svg': cmd_svg, 'png': cmd_image, 'pdf': cmd_image, 'share': cmd_share,
     'embed': cmd_embed, 'changes': cmd_changes, 'check': cmd_check, 'selftest': cmd_selftest}[a.cmd](a)


if __name__ == '__main__':
    main()
