#!/usr/bin/env python3
"""gantt (Agent Gantt): a small Gantt chart. An agent writes a JSON file, a person adjusts it in a desktop window (drag
bars, edit fields), and the same renderer writes a static SVG / PNG / PDF to share or embed in an HTML report.

  python gantt.py app [FILE] [--day N]                  desktop window (pywebview); no FILE: the last one, else a dialog
  python gantt.py serve FILE [--port 8195] [--host H]   the same UI in a browser
  python gantt.py svg FILE [-o OUT] [--day N] [--no-legend]
  python gantt.py png FILE [-o OUT] [--day N] [--scale 2]    needs Edge / Chrome (env GANTT_BROWSER)
  python gantt.py pdf FILE [-o OUT] [--day N]                one page sized to the chart
  python gantt.py share FILE                            write the files listed in the data's "share"
  python gantt.py embed FILE PAGE [--day N]             SVG between <!-- gantt:begin --> and <!-- gantt:end -->
  python gantt.py changes FILE [--peek] [--all]         what the person changed since the agent last looked
  python gantt.py check FILE                            validate
  python gantt.py selftest FILE [--write]               open the app hidden and drive it (--write: rename too)

FILE is a *.gantt.json (README.md). Standard library only; app / selftest import pywebview.
"""
import argparse
import copy
import datetime as dt
import html
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
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


def op_rename_person(d, o):
    """Call the person `from` `to` in every row's owner (the other people and the text in brackets stay)."""
    old, new = o.get('from'), o.get('to')
    if not isinstance(old, str) or not isinstance(new, str) or not old.strip() or not new.strip():
        raise ValueError('from / to: the old and the new name')
    old, new = old.strip(), new.strip()
    changed = []
    for r in d['rows']:
        if not is_group(r) and isinstance(r.get('owner'), str):
            v = rename_owner(r['owner'], old, new)
            if v != r['owner']:
                r['owner'] = v
                changed.append('「%s」' % name_of(r))
    if not changed:
        return None
    return '担当の名前「%s」→「%s」: %s（%d 行）' % (old, new, '、'.join(changed), len(changed))


OPS = {'item': op_item, 'add_item': op_add_item, 'del_item': op_del_item, 'row': op_row, 'add_row': op_add_row,
       'del_row': op_del_row, 'move_row': op_move_row, 'meta': op_meta, 'rename_person': op_rename_person}


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
HEAD_H, ROW_H, GROUP_H, STRIP_H, FS = 36, 24, 22, 12, 11.5
TEXT_MIN, TEXT_MAX = 0.8, 2.0       # text scale: font, the title and owner columns, the date header (1 = as before)
ROWS_MIN, ROWS_MAX = 0.7, 3.0       # row scale: row, group and strip heights; never below what the text needs
LABEL_MIN, LABEL_MAX = 120, 1200    # a title column width set in the window (px at text scale 1)


def css(u=1.0):
    """The chart's style sheet; u scales the font sizes."""
    return (
        '.gantt{font-family:var(--gantt-font,"Yu Gothic UI","Meiryo","Hiragino Sans",sans-serif);font-size:%spx}' % f(FS * u)
        + '.gantt text{fill:var(--ink,#1d2330)}.gantt .mu{fill:var(--muted,#5b6475)}.gantt .bd{font-weight:700}'
        '.gantt .g{font-weight:700;fill:var(--gantt-group,var(--ink,#1d2330))}'
        + '.gantt .sl,.gantt .hw{font-size:%spx}.gantt .hm{font-size:%spx;font-weight:700}' % (f(10 * u), f(11 * u))
        + '.gantt .lw{fill:var(--warns,#d08a2a)}.gantt .se{text-anchor:end}'
        '.gantt a.lnk:hover text{text-decoration:underline}.gantt .go{fill:var(--accent,#a3385f)}'
        '.gantt .bg{fill:var(--card,#fff)}.gantt .hd,.gantt .grp{fill:var(--head,#eef1f6)}'
        '.gantt .we{fill:var(--line,#d7dbe3);fill-opacity:.25}.gantt .off{fill:var(--line,#d7dbe3);fill-opacity:.4}'
        '.gantt .gw,.gantt .gd,.gantt .sep{fill:none;stroke:var(--line,#d7dbe3);stroke-width:1}.gantt .gd{stroke-opacity:.45}'
        '.gantt .ln{fill:none;stroke:var(--warns,#d08a2a);stroke-width:1.2;stroke-dasharray:4 3}'
        '.gantt .td{fill:none;stroke:var(--accent,#a3385f);stroke-width:1.5;pointer-events:none}'
        '.gantt .p1{fill:var(--card,#fff)}.gantt .p2{fill:var(--rec,#e8f0fb)}.gantt .p3{fill:var(--line,#d7dbe3)}'
        '.gantt .b,.gantt .m{stroke-width:1}'
        + ''.join('.gantt .b.k-%s,.gantt .m.k-%s{%s}' % (k, k, v) for k, v in KIND_CSS.items()))


CSS = css()
CSS_UI = '.gantt .hov,.gantt .hit,.gantt .tc{fill:transparent}.gantt .hov{pointer-events:none}'
CSS_FIND = '.gantt .qh{fill:var(--gantt-find,#ffd54a);pointer-events:none}'     # behind the items a search found
DEFS = ('<defs><pattern id="gt-h" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
        '<rect class="p1" width="6" height="6"/><rect class="p2" width="3" height="6"/></pattern>'
        '<pattern id="gt-w" width="5" height="6" patternUnits="userSpaceOnUse"><rect class="p3" width="2" height="6"/>'
        '</pattern></defs>')


def num(v):
    """A finite int / float (not a bool)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _tidy(v):
    return int(v) if v == int(v) else v


def clamp_day(day):
    """Pixels per day: 6-40, to 0.01 px (the window's fit mode chooses any width)."""
    return _tidy(max(6.0, min(40.0, round(float(day), 2))))


def clamp_text(scale):
    """The text scale: 0.8-2.0, to 0.01."""
    return _tidy(max(TEXT_MIN, min(TEXT_MAX, round(float(scale), 2))))


def clamp_rows(scale):
    """The row scale: 0.7-3.0, to 0.01."""
    return _tidy(max(ROWS_MIN, min(ROWS_MAX, round(float(scale), 2))))


def clamp_label(width):
    """A title column width from the window: 120-1200 px (at text scale 1), whole px."""
    return int(round(max(LABEL_MIN, min(LABEL_MAX, float(width)))))


# ---------------------------------------------------------------- people, search, the rows shown
UNSET = '未定'                                              # the person of a row without an owner
_SEP = re.compile(r'[・、,，/／\s]')
_OPEN, _CLOSE = '（(', '）)'


def owner_parts(owner):
    """An owner string in pieces, in order: [kind, text] with kind 'name', 'sep' (・ 、 , / or spaces) or 'paren' (a
    balanced （…） / (…), nested too; an unbalanced bracket is part of a name). Joined, they give the string back."""
    s = owner if isinstance(owner, str) else ''
    parts, i = [], 0
    while i < len(s):
        kind, j = None, i + 1
        if s[i] in _OPEN:
            depth, k = 0, i
            while k < len(s):
                depth += (s[k] in _OPEN) - (s[k] in _CLOSE)
                if depth == 0:
                    break
                k += 1
            if k < len(s):
                kind, j = 'paren', k + 1
        if kind is None:
            kind = 'sep' if _SEP.match(s[i]) else 'name'
        if kind != 'paren' and parts and parts[-1][0] == kind:
            parts[-1][1] += s[i:j]
        else:
            parts.append([kind, s[i:j]])
        i = j
    return parts


def _person(name):
    p = name.strip()
    return p[:-2].strip() if p.endswith('さん') else p


def people(owner):
    """The people in an owner string: the text in （） / () does not count, ・ 、 , / and spaces separate people, a
    trailing さん is dropped. '田中さん・佐藤（要相談）' -> ['田中', '佐藤']."""
    out = []
    for kind, text in owner_parts(owner):
        p = _person(text) if kind == 'name' else ''
        if p and p not in out:
            out.append(p)
    return out


def rename_owner(owner, old, new):
    """`owner` with the person `old` called `new` (a trailing さん stays); the other people, the separators and the text
    in brackets stay as they are."""
    out = []
    for kind, text in owner_parts(owner):
        if kind == 'name' and _person(text) == old:
            text = new + ('さん' if text.endswith('さん') and not new.endswith('さん') else '')
        out.append(text)
    return ''.join(out)


def people_of(row):
    return people(row.get('owner')) or [UNSET]


def owners(data):
    """[(person, rows)] over the rows that are not groups: most rows first, then in the order met; 未定 (rows without
    an owner) last."""
    count, first = {}, {}
    for r in data.get('rows') or []:
        if isinstance(r, dict) and not is_group(r):
            for p in people_of(r):
                count[p] = count.get(p, 0) + 1
                first.setdefault(p, len(first))
    return sorted(count.items(), key=lambda pc: (pc[0] == UNSET, -pc[1], first[pc[0]]))


def members(data):
    """{person: [row ids]} in data order."""
    out = {}
    for r in data.get('rows') or []:
        if isinstance(r, dict) and not is_group(r):
            for p in people_of(r):
                out.setdefault(p, []).append(r.get('id'))
    return out


class Person(dict):
    """A person's header row in the by-person view (not in the data)."""


def _norm(s):
    return unicodedata.normalize('NFKC', s).casefold()


def _has(v, q):
    return isinstance(v, str) and q in _norm(v)


def view_rows(data, query=None, owner=None, by_owner=False):
    """The rows the chart shows, in order: [(row, indices of the items the search found)]. The rows are the data's own
    dicts (their ids and item indices stay the real ones); a person header of the by-person view is a Person.
    query: rows whose title, owner or note, or an item's label or note, contain it (ignoring case and full / half
    width); a group stays when a row under it stays, and a group whose name contains it keeps all its rows.
    owner: rows of that person (UNSET: rows without an owner). by_owner: the rows under a header per person, in their
    order in the data; a row of several people is under each of them."""
    rows = [r for r in data.get('rows') or [] if isinstance(r, dict)]
    q = _norm(query).strip() if isinstance(query, str) else ''
    if not q and not owner and not by_owner:
        return [(r, ()) for r in rows]
    out, group, gmatch = [], None, False
    for r in rows:
        if is_group(r):
            group, gmatch = r, bool(q) and _has(r.get('group'), q)
            if gmatch and not owner:
                out.append((r, ()))
                group = None
            continue
        if owner and owner not in people_of(r):
            continue
        hits = ()
        if q:
            hits = tuple(i for i, it in enumerate(r.get('items') or []) if isinstance(it, dict)
                         and (_has(it.get('label'), q) or _has(it.get('note'), q)))
            if not (hits or gmatch or any(_has(r.get(k), q) for k in ('title', 'owner', 'note'))):
                continue
        if group is not None:
            out.append((group, ()))
            group = None
        out.append((r, hits))
    if not by_owner:
        return out
    flat = [(r, h) for r, h in out if not is_group(r)]
    res = []
    for p, _ in owners(data):
        mine = [(r, h) for r, h in flat if p in people_of(r)] if not owner or p == owner else []
        if mine:
            res.append((Person(group='%s（%d 件）' % (p, len(mine)), person=p), ()))
            res += mine
    return res


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


def _marks(data, d0, d1):
    """The off ranges [((from, to) clipped, label)] and the lines [(date, label)] inside d0..d1."""
    def clip(a, b):
        a, b = max(a, d0), min(b, d1)
        return (a, b) if a <= b else None
    offs = [(clip(pd(o.get('from')), pd(o.get('to'))), o.get('label')) for o in data.get('off') or []
            if isinstance(o, dict) and pd(o.get('from')) and pd(o.get('to'))]
    offs = [(c, lb) for c, lb in offs if c]
    lines = [(pd(o.get('on')), o.get('label')) for o in data.get('lines') or [] if isinstance(o, dict)]
    lines = [(d, lb) for d, lb in lines if d and d0 <= d <= d1]
    return offs, lines


def _columns(data, label_width=None):
    """Widths of the title and owner columns at text scale 1; label_width (the window's) wins over the data's."""
    rows = [r for r in data.get('rows') or [] if isinstance(r, dict)]
    tcol = clamp_label(label_width) if num(label_width) else int(data.get('label_width') or 240)
    return tcol, 80 if any(r.get('owner') for r in rows) else 0


def fit_view(data, width, height, by_owner=False, text_scale=1.0, label_width=None):
    """(day, row scale) that make the chart fill width x height px at this text scale: the row scale makes all rows (no
    search, no person filter) fill the height, within 0.7-3.0 (rows never get lower than the text needs; then the
    chart scrolls); the day width makes the whole date range fill the width, within 6-40."""
    u = clamp_text(text_scale)
    d0, d1 = date_range(data)
    nd = (d1 - d0).days + 1
    offs, lines = _marks(data, d0, d1)
    rows = [r for r, _ in view_rows(data, by_owner=by_owner)]
    tall = (STRIP_H if any(lb for _, lb in offs + lines) else 0) + sum(GROUP_H if is_group(r) else ROW_H for r in rows)
    r = clamp_rows(math.floor((float(height) - 1.5 - HEAD_H * u) / tall * 100) / 100) if tall else 1
    tcol, ocol = _columns(data, label_width)
    day = clamp_day(math.floor((float(width) - 0.5 - (tcol + ocol) * u) / nd * 100) / 100)
    return day, r


def _draw(data, day=14, interactive=False, legend=False, today=None, base_dir=None, zoom=False, text_scale=1.0,
          row_scale=1.0, label_width=None, query=None, owner=None, by_owner=False):
    day = clamp_day(day)
    u = clamp_text(text_scale)                              # text, columns, header, paddings: at scale 1 times u
    v = max(clamp_rows(row_scale), u)                       # row heights: at scale 1 times v (v >= u: the text fits)
    tt = (lambda s: '') if interactive else (lambda s: '<title>%s</title>' % s)     # the window has its own tooltip
    d0, d1 = date_range(data)
    nd = (d1 - d0).days + 1
    shown = view_rows(data, query, owner, by_owner)
    rows = [r for r, _ in shown]
    tcol, ocol = _columns(data, label_width)
    tcol, ocol = tcol * u, ocol * u
    fs, head = FS * u, HEAD_H * u
    x0 = tcol + ocol
    W = x0 + nd * day
    day1 = dt.timedelta(days=1)

    def X(d):
        return x0 + (d - d0).days * day

    def clip(a, b):
        a, b = max(a, d0), min(b, d1)
        return (a, b) if a <= b else None

    offs, lines = _marks(data, d0, d1)
    strip = STRIP_H * v if any(lb for _, lb in offs + lines) else 0
    by = head
    y = by + strip
    ys = []
    for r in rows:
        h = (GROUP_H if is_group(r) else ROW_H) * v
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
    leg, lx, ly = [], 8.0 * u, bend + 8 * u
    if legend and used:
        for k in used:
            w = 19 * u + tw(lg[k], 11 * u) + 16 * u
            if lx > 8 * u and lx + w > W - 8 * u:
                lx, ly = 8.0 * u, ly + 18 * u
            leg.append('<rect class="b k-%s" x="%s" y="%s" width="%s" height="%s" rx="%s"/>'
                       '<text class="mu" x="%s" y="%s">%s</text>' % (k, f(lx), f(ly), f(14 * u), f(10 * u), f(2 * u),
                                                                    f(lx + 19 * u), f(ly + 9 * u), esc(lg[k])))
            lx += w
        H = ly + 18 * u
    else:
        H = bend + 1

    o = []
    P = o.append
    # --- backgrounds, shades, grid
    P('<rect class="bg" x="0" y="%s" width="%s" height="%s"/><rect class="hd" x="0" y="0" width="%s" height="%s"/>'
      % (f(by), f(W), f(bh), f(W), f(head)))
    g = ''.join('M0 %s h%s v%s h-%s z' % (f(yy), f(W), f(h), f(W)) for r, (yy, h) in zip(rows, ys) if is_group(r))
    if g:
        P('<path class="grp" d="%s"/>' % g)
    we, d = [], d0
    while d <= d1:
        if d.weekday() >= 5:
            n = 2 if d.weekday() == 5 and d + day1 <= d1 else 1
            we.append('M%s %s h%s v%s h-%s z' % (f(X(d)), f(by), f(n * day), f(bh), f(n * day)))
            d += dt.timedelta(days=n)
        else:
            d += day1
    if we:
        P('<path class="we" d="%s"/>' % ''.join(we))
    if offs:
        P('<path class="off" d="%s"/>' % ''.join('M%s %s h%s v%s h-%s z' % (f(X(a)), f(by), f((b - a).days * day + day), f(bh),
                                                                              f((b - a).days * day + day)) for (a, b), _ in offs))
    mon = d0 + dt.timedelta(days=(7 - d0.weekday()) % 7)
    gw = ['M%s %s V%s' % (f(X(m)), f(18 * u), f(bend)) for m in (mon + dt.timedelta(days=7 * i) for i in range(nd // 7 + 1))
          if m <= d1 and X(m) > x0]
    gw.append('M%s 0 V%s' % (f(x0), f(bend)))
    months = []
    d = d0
    while d <= d1:
        nxt = (d.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        months.append((d, min(nxt - day1, d1)))
        d = nxt
    gw += ['M%s 0 V%s' % (f(X(a)), f(18 * u)) for a, _ in months[1:]]
    P('<path class="gw" d="%s"/>' % ''.join(gw))
    if day >= 12:
        P('<path class="gd" d="%s"/>' % ''.join('M%s %s V%s' % (f(X(d0 + dt.timedelta(days=i))), f(by), f(bend))
                                                  for i in range(1, nd) if (d0 + dt.timedelta(days=i)).weekday() != 0))
    P('<path class="sep" d="M0 %s H%s M0 %s H%s%s"/>' % (f(18 * u), f(W), f(head), f(W),
                                                         ''.join(' M0 %s H%s' % (f(yy + h), f(W)) for yy, h in ys)))
    if lines:
        P('<path class="ln" d="%s"/>' % ''.join('M%s %s V%s' % (f(X(d) + day / 2), f(by), f(bend)) for d, _ in lines))

    # --- rows
    for (r, hits), (y, h) in zip(shown, ys):
        rid, grp = str(r.get('id', '')), is_group(r)
        mid = y + h / 2                                     # the text and the bars sit on the row's middle
        cy = mid + 4 * u
        person = isinstance(r, Person)
        if interactive:
            P('<g class="row" %s data-y="%s" data-h="%s"%s><rect class="hov" x="0" y="%s" width="%s" height="%s"/>'
              % ('data-p="%s"' % esc(r['person']) if person else 'data-r="%s"' % esc(rid), f(y), f(h),
                 ' data-g="1"' if grp else '', f(y), f(W), f(h)))
            if not grp:
                P('<rect class="hit" x="%s" y="%s" width="%s" height="%s"/>' % (f(x0), f(y), f(W - x0), f(h)))
        name = str((r.get('group') if grp else r.get('title')) or '')
        url = None if grp else resolve_link(r.get('link'), data.get('link_base'), base_dir)
        s, cut = fit(name, tcol - 14 * u - (14 * u if url and interactive else 0), fs)
        t = '<text class="%s" x="%s" y="%s">%s%s</text>' % ('g' if grp else 't', f(8 * u), f(cy), esc(s),
                                                            tt(esc(name)) if cut else '')
        if interactive and not person:
            P('<g class="ttl" data-r="%s"><rect class="tc" x="0" y="%s" width="%s" height="%s"/>%s%s</g>'
              % (esc(rid), f(y), f(tcol), f(h), t, '<text class="go" data-href="%s" x="%s" y="%s">↗</text>'
                 % (esc(url), f(tcol - 14 * u), f(cy)) if url else ''))
        else:
            P('<a class="lnk" href="%s" target="_blank">%s</a>' % (esc(url), t) if url else t)
        if ocol and r.get('owner'):
            s, cut = fit(str(r['owner']), ocol - 10 * u, fs)
            P('<text class="mu" x="%s" y="%s">%s%s</text>' % (f(tcol + 6 * u), f(cy), esc(s),
                                                              tt(esc(r['owner'])) if cut else ''))
        # items: geometry first (the free space to the right of a label ends at the next item)
        geo = []
        for idx, it in enumerate(r.get('items') or [] if not grp else []):
            k = kind_of(it)
            if it.get('on') is not None:
                a = pd(it['on'])
                if a and d0 <= a <= d1:
                    cx = X(a) + day / 2
                    geo.append((idx, it, k, 'm', cx - 6 * u, cx + 6 * u))
            else:
                a, b = pd(it.get('from')), pd(it.get('to'))
                c = clip(a, b) if a and b and a <= b else None
                if c:
                    geo.append((idx, it, k, 'b', X(c[0]), X(c[1]) + day))
        lefts = sorted(gg[4] for gg in geo)
        for idx, it, k, shape, xl, xr in geo:
            nxt = next((v_ for v_ in lefts if v_ >= xr - 0.01), W)
            label = str(it.get('label') or '')
            cls = 'bt' + (' bd' if k in ('due', 'ext') else ' mu' if k in ('wait', 'off') else '')
            text, tip = '', esc(it['note']) if it.get('note') else ''
            if shape == 'b':
                inside = xr - xl - 8 * u
                if not label:
                    pass
                elif tw(label, fs) <= inside:
                    text = '<text class="%s" x="%s" y="%s">%s</text>' % (cls, f(xl + 4 * u), f(cy), esc(label))
                elif tw(label, fs) <= nxt - xr - 6 * u:     # not in the bar: to its right, muted
                    text = '<text class="bt mu" x="%s" y="%s">%s</text>' % (f(xr + 4 * u), f(cy), esc(label))
                else:
                    s, _ = fit(label, inside, fs)
                    if s:
                        text = '<text class="%s" x="%s" y="%s">%s%s</text>' % (cls, f(xl + 4 * u), f(cy), esc(s),
                                                                             tt(esc(label)))
                    else:
                        tip = esc(label)                    # not even the ellipsis fits: the full text as a tooltip
            elif label:
                room = nxt - xr - 5 * u
                s, cut = fit(label, room, fs)
                if s:
                    text = '<text class="%s" x="%s" y="%s">%s%s</text>' % (cls, f(xr + 3 * u), f(cy), esc(s),
                                                                         tt(esc(label)) if cut else '')
                else:
                    tip = esc(label)
            tip = tt(tip) if tip else ''
            attrs = ' data-r="%s" data-i="%d"' % (esc(rid), idx)
            attrs += ' data-on="%s"' % esc(it['on']) if shape == 'm' else ' data-from="%s" data-to="%s"' % (esc(it['from']), esc(it['to']))
            if interactive:
                P('<g class="it k-%s"%s>' % (k, attrs))
            if idx in hits:                                 # found by the search: a marker behind it
                hy, hh = (y + 1.5 * v, 21 * v) if shape == 'b' else (mid - 10.5 * u, 21 * u)
                P('<rect class="qh" x="%s" y="%s" width="%s" height="%s" rx="%s"/>'
                  % (f(xl - 3 * u), f(hy), f(xr - xl + 6 * u), f(hh), f(4 * u)))
            if shape == 'b':
                P('<rect class="b k-%s" x="%s" y="%s" width="%s" height="%s" rx="%s">%s</rect>'
                  % (k, f(xl), f(y + 4 * v), f(xr - xl), f(16 * v), f(3 * u), tip))
            else:
                P('<path class="m k-%s" d="M%s %sL%s %sL%s %sL%s %sz">%s</path>'
                  % (k, f(xl), f(mid), f(xl + 6 * u), f(mid - 6 * u), f(xr), f(mid), f(xl + 6 * u), f(mid + 6 * u), tip))
            if text:
                P(text)
            if interactive:
                P('</g>')
        if interactive:
            P('</g>')

    # --- today, header text, strip labels, legend
    t0 = today if isinstance(today, dt.date) else pd(today) or dt.date.today()
    if d0 <= t0 <= d1:
        P('<path class="td" d="M%s %s V%s"/>' % (f(X(t0) + day / 2), f(by), f(bend)))
    for a, b in months:
        label = '%d月' % a.month
        if tw(label, 11 * u) <= (b - a).days * day + day - 6 * u:
            P('<text class="hm" x="%s" y="%s">%s</text>' % (f(X(a) + 4 * u), f(13 * u), label))
    for i in range(nd // 7 + 1):
        m = mon + dt.timedelta(days=7 * i)
        if m <= d1 and tw(md(m), 10 * u) <= 7 * day - 4 * u:
            P('<text class="hw mu" x="%s" y="%s">%s</text>' % (f(X(m) + 3 * u), f(31 * u), md(m)))
    if strip:
        sy = by + strip / 2 + 3 * u
        for (a, b), lb in offs:
            s, _ = fit(str(lb or ''), (b - a).days * day + day - 6 * u, 10 * u) if lb else ('', False)
            if s:
                P('<text class="sl mu" x="%s" y="%s">%s</text>' % (f(X(a) + 3 * u), f(sy), esc(s)))
        for d, lb in lines:                    # left of the line, so it stays clear of a holiday label right after it
            xl, lb = X(d) + day / 2, str(lb or '')
            if lb and tw(lb, 10 * u) <= xl - 3 * u - x0:
                P('<text class="sl lw se" x="%s" y="%s">%s</text>' % (f(xl - 3 * u), f(sy), esc(lb)))
                continue
            s, _ = fit(lb, W - xl - 8 * u, 10 * u) if lb else ('', False)
            if s:
                P('<text class="sl lw" x="%s" y="%s">%s</text>' % (f(xl + 3 * u), f(sy), esc(s)))
    o += leg
    found = any(hits for _, hits in shown)
    svg = ('<svg class="gantt%s" xmlns="http://www.w3.org/2000/svg" width="%s" height="%s" viewBox="0 0 %s %s"%s>'
           % (' zoom' if zoom else '', f(W), f(H), f(W), f(H),
              ' data-x0="%s" data-dw="%s" data-d0="%s" data-d1="%s" data-hh="%s" data-tc="%s"'
              % (f(x0), f(day), d0, d1, f(by + strip), f(tcol)) if interactive else '')
           + '<style>%s%s%s</style>' % (CSS if u == 1 else css(u), CSS_UI if interactive else '', CSS_FIND if found else '')
           + DEFS + ''.join(o) + '</svg>')
    return svg, W, H


def render(data, day=14, interactive=False, legend=False, today=None, base_dir=None, zoom=False, text_scale=1.0,
           row_scale=1.0, label_width=None, query=None, owner=None, by_owner=False):
    """The chart as one SVG string. base_dir: folder against which relative row links are resolved when the data has
    no link_base. interactive adds the data-* hooks and hit areas app.html uses (and leaves out the <title> tooltips:
    the window has its own); zoom adds class `zoom` to the root.
    day: pixels per day (6-40, any 0.01). text_scale: font, the title and owner columns and the date header (0.8-2.0).
    row_scale: row heights (0.7-3.0; never below what the text needs). label_width: the title column's width at text
    scale 1, over the data's label_width. query / owner / by_owner: which rows, in what order (view_rows); data-r /
    data-i stay the data's own."""
    return _draw(data, day, interactive, legend, today, base_dir, zoom, text_scale, row_scale, label_width, query, owner,
                 by_owner)[0]


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


def export(data, out, fmt, day=14, scale=2.0, base_dir=None, work=None, view=None):
    """Render `data` to out (.png / .pdf) with headless Edge / Chrome. scale: device pixels per px (PNG). view: more
    arguments of render (text_scale, row_scale, label_width, query, owner, by_owner). The page and the browser profile are written under `work`
    (default: beside out) and removed afterwards; raises ExportError."""
    exe = find_browser()
    if not exe:
        raise ExportError('no Edge / Chrome found (set GANTT_BROWSER to the browser executable)')
    svg, w, h = _draw(data, day, False, True, None, base_dir, False, **(view or {}))
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
    day = clamp_day(data['day']) if num(data.get('day')) else 14
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
APP_NAME = 'Agent Gantt'
RECENT_KEEP = 8
PREFS = {'day': 14, 'text': 1.3, 'rows': 1, 'fit': True, 'by_owner': False, 'label_width': None}   # the window's
# defaults: the text a size bigger than in files; label_width None = the data's


def day_of(a, data):
    """--day, else the data's own "day" (its usual width for pictures), else 14."""
    d = data.get('day')
    return clamp_day(a.day or (d if num(d) else 0) or 14)


def clean_prefs(p):
    """The valid window settings in p: day, text, rows (numbers), fit, by_owner (true / false), label_width (a number,
    or None for the data's). The first version's "scale" is read as text."""
    p = p if isinstance(p, dict) else {}
    out = {}
    if num(p.get('day')):
        out['day'] = clamp_day(p['day'])
    if num(p.get('text')) or num(p.get('scale')):
        out['text'] = clamp_text(p['text'] if num(p.get('text')) else p['scale'])
    if num(p.get('rows')):
        out['rows'] = clamp_rows(p['rows'])
    for k in ('fit', 'by_owner'):
        if isinstance(p.get(k), bool):
            out[k] = p[k]
    if 'label_width' in p and (p['label_width'] is None or num(p['label_width'])):
        out['label_width'] = None if p['label_width'] is None else clamp_label(p['label_width'])
    return out


def window_title(data, path):
    return '%s — %s' % (APP_NAME, (data or {}).get('title') or data_stem(path)) if path else APP_NAME


def sans_seen(data):
    return {k: v for k, v in data.items() if k != 'seen'}


class Api:
    """What app.html calls. Public names are the API; state is private (pywebview walks public attributes).
    The view calls take `day` and `view` = {text, rows, label_width, query, owner, by_owner, fit, w, h}; with fit, the
    day width and the row scale are chosen to fill w x h px. path None: no file yet (the window asks for one)."""

    def __init__(self, path=None, day=14, prefs=None):
        self._path = os.path.abspath(path) if path else None
        self._dir = os.path.dirname(self._path) if path else None
        self._prefs = dict(PREFS, day=clamp_day(day))
        self._prefs.update(clean_prefs(prefs))
        self._lock = threading.Lock()                       # read - apply - write is one step
        self._undo = []                                     # (text before, text after) per batch this process wrote
        self._window = None
        self._remember = None                               # app: keeps changed prefs (~/.gantt/last.json)
        self._opened = None                                 # app: told the path of every file opened (recent list)
        self._title = None

    def _mtime(self):
        st = os.stat(self._path)
        return '%d-%d' % (st.st_mtime_ns // 1000, st.st_size)      # a string: JS numbers would round nanoseconds

    def _read(self):
        text = read_text(self._path)
        return text, normalize(json.loads(text))

    def _set_title(self, title):
        if self._window is not None and title != self._title:
            try:
                self._window.set_title(title)
            except Exception:
                pass
        self._title = title

    def _view(self, data, day=None, view=None):
        v = view if isinstance(view, dict) else {}
        query = v.get('query') if isinstance(v.get('query'), str) else None
        owner = v.get('owner') if isinstance(v.get('owner'), str) and v.get('owner') else None
        by_owner = v.get('by_owner') is True
        text = clamp_text(v['text'] if num(v.get('text')) else self._prefs['text'])
        label = v.get('label_width', self._prefs['label_width'])
        label = clamp_label(label) if num(label) else None
        fitted = None                                       # the size the chart was fitted to
        if v.get('fit') is True and num(v.get('w')) and num(v.get('h')):
            day, rows = fit_view(data, v['w'], v['h'], by_owner, text, label)
            fitted = [v['w'], v['h']]
        else:
            day = clamp_day(day if num(day) else self._prefs['day'])
            rows = clamp_rows(v['rows'] if num(v.get('rows')) else self._prefs['rows'])
        title = window_title(data, self._path)
        self._set_title(title)
        shown = {r.get('id') for r, _ in view_rows(data, query, owner, by_owner) if not is_group(r)}
        return {'svg': render(data, day, interactive=True, base_dir=self._dir, text_scale=text, row_scale=rows,
                              label_width=label, query=query, owner=owner, by_owner=by_owner),
                'data': {k: v for k, v in data.items() if k != 'log'}, 'mtime': self._mtime(),
                'file': os.path.basename(self._path), 'path': self._path, 'title': title, 'day': day, 'text': text,
                'rows': rows, 'label_width': label, 'fitted': fitted, 'legend': legend_map(data), 'kinds': list(KINDS),
                'share': bool(data.get('share')), 'owners': [list(pc) for pc in owners(data)], 'members': members(data),
                'unset': UNSET, 'shown': len(shown), 'total': sum(1 for r in data['rows'] if not is_group(r))}

    def get(self, day=None, view=None):
        with self._lock:
            if not self._path:
                self._set_title(APP_NAME)
                return {'nofile': True}
            return self._view(self._read()[1], day, view)

    def apply(self, ops, day=None, view=None):
        with self._lock:
            if not self._path:
                return {'ok': False, 'conflict': False, 'error': 'ファイルが開かれていません'}
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
            return dict(self._view(data, day, view), ok=True)

    def poll(self, mtime=None, day=None, view=None):
        with self._lock:
            if not self._path or self._mtime() == mtime:
                return None
            try:
                return self._view(self._read()[1], day, view)
            except (ValueError, OSError) as e:
                return {'error': str(e)}

    def undo(self, day=None, view=None):
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
            return dict(self._view(old, day, view), ok=True)

    def prefs(self, change=None):
        """The window's settings (day, text, rows, fit, by_owner, label_width); `change` sets some of them (the app keeps
        them)."""
        new = clean_prefs(change)
        self._prefs.update(new)
        if new and self._remember:
            self._remember(**new)
        return dict(self._prefs)

    def open_link(self, href=None):
        if urllib.parse.urlsplit(str(href or '')).scheme.lower() not in ('http', 'https', 'file'):
            return False
        return webbrowser.open(href)

    def share(self):
        try:
            if not self._path:
                raise ValueError('ファイルが開かれていません')
            with self._lock:
                data = self._read()[1]
            return {'ok': True, 'paths': write_share(self._path, data)}
        except (ExportError, OSError, ValueError) as e:
            return {'ok': False, 'error': str(e)}

    # --- the desktop window only (not in Handler.METHODS: serve has no file dialog and shows one file)
    def recent(self):
        """The recent files, newest first: [{path, file, title, current}] (only files that are there)."""
        out = []
        for p in recent_files():
            try:
                t = load(p).get('title')
            except (OSError, ValueError):
                t = None
            out.append({'path': p, 'file': os.path.basename(p), 'title': str(t or data_stem(p)),
                        'current': bool(self._path) and same_file(p, self._path)})
        return out

    def open_file(self, path=None, day=None, view=None):
        """Show another data file in this window."""
        p = os.path.abspath(os.path.expanduser(str(path or '')))
        if not path or not os.path.isfile(p):
            return {'ok': False, 'error': 'ファイルがありません: %s' % (path or '')}
        try:
            load(p)
        except (OSError, ValueError) as e:
            return {'ok': False, 'error': '%s: %s' % (os.path.basename(p), e)}
        with self._lock:
            self._path, self._dir, self._undo = p, os.path.dirname(p), []
            if self._opened:
                self._opened(p)
            try:
                return dict(self._view(self._read()[1], day, view), ok=True)
            except (OSError, ValueError) as e:
                return {'ok': False, 'error': str(e)}

    def pick(self, day=None, view=None):
        """The open-file dialog, then open_file. {'ok': False, 'cancel': True} when nothing was chosen."""
        if self._window is None:
            return {'ok': False, 'error': 'ファイルを選ぶ窓が使えません'}
        try:
            import webview
            kind = webview.FileDialog.OPEN if hasattr(webview, 'FileDialog') else webview.OPEN_DIALOG
            got = self._window.create_file_dialog(kind, directory=self._dir or os.path.expanduser('~'),
                                                  file_types=('Gantt data (*.gantt.json)', 'JSON (*.json)'))
        except Exception as e:
            return {'ok': False, 'error': str(e)}
        if not got:
            return {'ok': False, 'cancel': True}
        return self.open_file(got if isinstance(got, str) else got[0], day, view)


# ---------------------------------------------------------------- HTTP (serve)
class Handler(BaseHTTPRequestHandler):
    server_version = 'gantt/1'
    api = None
    names = ('127.0.0.1', 'localhost')                      # host names this server answers to
    METHODS = ('get', 'apply', 'poll', 'undo', 'prefs', 'open_link', 'share')
    APP_ONLY = ('recent', 'open_file', 'pick')             # the desktop window's js_api only

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
    """What the app window remembers (~/.gantt/last.json; the window has no localStorage): file (the last one opened),
    recent (up to 8, newest first), day, text, rows, fit, by_owner, label_width."""
    try:
        with open(os.path.join(HOME, 'last.json'), encoding='utf-8') as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_last(**kw):
    try:
        os.makedirs(HOME, exist_ok=True)
        write_bytes(os.path.join(HOME, 'last.json'),
                    json.dumps(dict(read_last(), **kw), ensure_ascii=False, indent=1).encode('utf-8'))
    except OSError:
        pass


def same_file(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def recent_files():
    """The remembered files that are there, newest first (the last one opened first), at most RECENT_KEEP."""
    last = read_last()
    out = []
    for p in [last.get('file')] + list(last.get('recent') or [] if isinstance(last.get('recent'), list) else []):
        if isinstance(p, str) and os.path.isfile(p) and not any(same_file(p, q) for q in out):
            out.append(os.path.abspath(p))
    return out[:RECENT_KEEP]


def last_file():
    r = recent_files()
    return r[0] if r else None


def push_recent(path):
    """`path` was opened: first in the recent list (files no longer there drop out)."""
    p = os.path.abspath(path)
    write_last(file=p, recent=([p] + [q for q in recent_files() if not same_file(q, p)])[:RECENT_KEEP])


def start_gui(api, title, hidden, run=None, store=STORE, size=(1400, 900)):
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
    win = webview.create_window(title, html=page, js_api=api, width=size[0], height=size[1], min_size=(800, 500),
                                hidden=hidden, text_select=True)
    api._window = win
    webview.start(run, (win,) if run else None, private_mode=True, storage_path=store,
                  icon=ICON if os.path.isfile(ICON) else None)
    return win


def cmd_app(a):
    """FILE, else the last file opened, else none: the window then shows the open-file dialog."""
    path = a.file or last_file()
    path = need_file(path) if path else None
    data = load_or_exit(path) if path else None
    prefs = clean_prefs(read_last())
    kept = prefs.pop('day', None)                               # --day, else the width chosen last, else the data's
    day = a.day or kept or (day_of(a, data) if data else 14)
    if path:
        push_recent(path)
    api = Api(path, day, prefs)
    api._remember = write_last                                  # the window has no localStorage: keep settings here
    api._opened = push_recent
    start_gui(api, window_title(data, path), False)


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


# selftest: helpers put into the page. drag() replaces the page's commit, so the drag is checked and nothing is written.
SELFTEST_JS = r'''window.__st = {
 body: () => document.querySelector('#chart .pn.body svg'),
 state() {
  const svg = this.body(), sc = document.getElementById('sc'), t = document.querySelector('#chart .pn.body text.t');
  const row = document.querySelector('#chart .pn.body g.row[data-r]:not([data-g])');
  return {svg: !!svg, nrows: document.querySelectorAll('#chart .pn.body g.row[data-r]:not([data-g])').length,
   items: document.querySelectorAll('#chart .pn.body g.it').length, marks: document.querySelectorAll('#chart .pn.body rect.qh').length,
   heads: [...document.querySelectorAll('#chart .pn.body g.row[data-p]')].map(g => g.dataset.p), found: document.getElementById('qn').textContent,
   day: S.day, text: S.text, rows: S.rows, labelW: S.labelW, fit: S.fit, by: S.byOwner, person: S.person,
   sw: svg ? +svg.getAttribute('width') : 0, sh: svg ? +svg.getAttribute('height') : 0, tc: svg ? +svg.dataset.tc : 0,
   rowh: row ? +row.dataset.h : 0, cw: sc.clientWidth, ch: sc.clientHeight, font: t ? getComputedStyle(t).fontSize : '',
   pressed: [document.getElementById('bOrder').getAttribute('aria-pressed'), document.getElementById('bBy').getAttribute('aria-pressed')],
   members: S.members, errors: window.__errors.slice(),
   storage: (function () { try { localStorage.setItem('gantt.t', '1'); localStorage.removeItem('gantt.t'); return 'ok'; } catch (e) { return 'blocked'; } })()};
 },
 bar() {
  const b = document.getElementById('bar'), r = b.getBoundingClientRect(), over = [];
  for (const c of b.children) {
   if (c.hidden || getComputedStyle(c).display === 'none') continue;
   const q = c.getBoundingClientRect();
   if (q.width > 0 && (q.right > r.right + 0.5 || q.left < r.left - 0.5 || q.top < r.top - 0.5 || q.bottom > r.bottom + 0.5)) over.push(c.id || c.className);
  }
  return {w: Math.round(r.width), h: Math.round(r.height), over: over, file: !document.getElementById('bFile').hidden,
   title: Math.round(document.getElementById('ttl').getBoundingClientRect().width), folded: document.getElementById('legend').classList.contains('folded')};
 },
 word() {                                   // a word to search for: the label of an item in the middle of the data
  const its = S.data.rows.flatMap(r => (r.items || []).filter(it => it.label));
  return its.length ? its[Math.floor(its.length / 2)].label : '';
 },
 find(text) { const q = document.getElementById('q'); q.value = text; q.dispatchEvent(new Event('input')); },
 person() {                                 // the first person of the 担当 list (not 未定): [value, its text]
  const o = [...document.getElementById('who').options].find(o => !o.disabled && o.value && o.value !== S.unset);
  return o ? [o.value, o.textContent] : null;
 },
 who(name) { const s = document.getElementById('who'); s.value = name; s.dispatchEvent(new Event('change')); return s.value; },
 ids() {                                    // every data-r / data-i of the chart is a row / item of the data
  const rows = new Map(S.data.rows.map(r => [r.id, r]));
  for (const g of document.querySelectorAll('#chart g.it')) {
   const r = rows.get(g.dataset.r), it = r && (r.items || [])[+g.dataset.i];
   if (!it) return 'no item ' + g.dataset.r + ' ' + g.dataset.i;
   if ((g.dataset.on || null) !== (it.on || null) || (g.dataset.from || null) !== (it.from || null)) return 'other item ' + g.dataset.r + ' ' + g.dataset.i;
  }
  return document.querySelectorAll('#chart [id="gt-h"]').length === 1 ? 'ok' : 'duplicate pattern ids';
 },
 frozen() {                                 // scrolled down and right: where the four copies are, against the scroller
  const sc = document.getElementById('sc');
  sc.scrollTop = 300; sc.scrollLeft = 200;
  const r = sc.getBoundingClientRect(), q = c => document.querySelector('#chart .pn.' + c).getBoundingClientRect();
  const out = {st: sc.scrollTop, sl: sc.scrollLeft, head: [q('head').top - r.top, q('head').left - r.left + sc.scrollLeft],
   left: [q('left').left - r.left, q('left').top - r.top + sc.scrollTop], corner: [q('corner').top - r.top, q('corner').left - r.left],
   body: [q('body').top - r.top + sc.scrollTop, q('body').left - r.left + sc.scrollLeft]};
  return out;
 },
 hover() {                                  // the mouse over a bar (one with a memo if there is one)
  const its = [...document.querySelectorAll('#chart .pn.body g.it')];
  const with_ = its.find(g => { const r = S.data.rows.find(x => x.id === g.dataset.r); return r && (r.items[+g.dataset.i] || {}).note; });
  const g = with_ || its[0], r = S.data.rows.find(x => x.id === g.dataset.r), it = r.items[+g.dataset.i];
  const box = g.querySelector('rect.b,path.m').getBoundingClientRect();
  g.querySelector('rect.b,path.m').dispatchEvent(new MouseEvent('mousemove', {bubbles: true, clientX: box.left + box.width / 2, clientY: box.top + box.height / 2}));
  return {note: it.note || '', label: it.label || ''};
 },
 tipText() { const h = document.getElementById('hover'); return h.hidden ? null : h.textContent; },
 wheel(ctrl) {                              // Ctrl / Alt + wheel over the timeline
  const sc = document.getElementById('sc'), r = sc.getBoundingClientRect(), G = geom();
  this.body().dispatchEvent(new WheelEvent('wheel', {deltaY: -100, ctrlKey: ctrl, altKey: !ctrl, bubbles: true, cancelable: true,
   clientX: r.left + G.x0 + 80, clientY: r.top + G.hh + 40}));
 },
 col(dx) {                                  // drag the column handle by dx px
  const rz = document.querySelector('#chart .pn.left .rz'), r = rz.getBoundingClientRect(), x = r.left + 3, y = r.top + 100;
  rz.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0, clientX: x, clientY: y}));
  window.dispatchEvent(new MouseEvent('mousemove', {clientX: x + dx, clientY: y}));
  window.dispatchEvent(new MouseEvent('mouseup', {clientX: x + dx, clientY: y}));
 },
 colReset() { document.querySelector('#chart .pn.left .rz').dispatchEvent(new MouseEvent('dblclick', {bubbles: true})); },
 head(name) {                               // click a person's header of the by-person view
  const g = [...document.querySelectorAll('#chart .pn.body g.row[data-p]')].find(g => g.dataset.p === name);
  g.querySelector('rect.hov').dispatchEvent(new MouseEvent('click', {bubbles: true}));
  return S.person;
 },
 rename(to) {
  const inp = document.querySelector('#drawer [data-pn]');
  inp.value = to;
  document.querySelector('#drawer [data-act=rename]').click();
 },
 drag() {                                   // drag the last bar drawn (with room) by 3 days; the op is caught, not written
  window.__ops = null;
  window.commit = async ops => { window.__ops = ops; return null; };
  const G = geom(), bars = [...document.querySelectorAll('#chart .pn.body g.it')].filter(g => g.querySelector('rect.b')
   && pd(g.dataset.from) >= G.d0 && pd(g.dataset.to) + 3 <= G.d1);
  if (!bars.length) return null;
  const g = bars[bars.length - 1], r = g.querySelector('rect.b').getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2, dx = 3 * G.dw * G.k;
  g.querySelector('rect.b').dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0, clientX: x, clientY: y}));
  window.dispatchEvent(new MouseEvent('mousemove', {clientX: x + dx, clientY: y}));
  window.dispatchEvent(new MouseEvent('mouseup', {clientX: x + dx, clientY: y}));
  return {row: g.dataset.r, i: +g.dataset.i, from: g.dataset.from, to: g.dataset.to};
 }
};
true'''


def _until(win, cond, secs=10.0):
    end = time.time() + secs
    while time.time() < end:
        try:
            if win.evaluate_js(cond):
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def _fills(s):
    """The fitted chart fills the visible width (unless the day width is at its limit)."""
    return not (s['sw'] > s['cw'] + 0.5 or (s['sw'] < s['cw'] - 2 and s['day'] < 40))


def _selftest_steps(win, res, write=False):
    """Load, then: the toolbar, the fit, the text size, search, the person filter, the view switch with the by-person view
    (rename with write; a dry drag), Ctrl / Alt + wheel, the frozen header and columns, the hover details, the column
    handle, and the fit after a resize. Appends report lines and problems to res."""
    js = win.evaluate_js
    out, bad = res['lines'], res['bad']

    def state():
        return json.loads(js('JSON.stringify(__st.state())'))

    def step(code, what):                              # run code, wait for the render it causes
        gen = js('S.gen')
        js(code)
        if not _until(win, 'S.gen > %d && !S.inflight && !S.again' % gen):
            raise RuntimeError('no new chart after %s' % what)
        _until(win, '!S.inflight && !S.again', 2)
        return state()

    if not _until(win, 'typeof S === "object" && S.gen > 0 && !S.inflight && !!document.querySelector("#chart .pn.body svg")', 25):
        raise RuntimeError('the chart did not appear')
    _until(win, '!S.inflight && !S.again', 3)
    js(SELFTEST_JS)
    s = base = state()
    out.append('page: rows=%s items=%s localStorage=%s' % (s['nrows'], s['items'], s['storage']))
    b = json.loads(js('JSON.stringify(__st.bar())'))
    out.append('toolbar: %dx%d overflow=%s title=%dpx legend=%s file-button=%s'
               % (b['w'], b['h'], ','.join(b['over']) or 'none', b['title'], 'folded' if b['folded'] else 'open', b['file']))
    if b['over'] or b['h'] != 46:
        bad.append('toolbar does not fit')
    if s['fit'] and s['cw'] > 200:
        out.append('fit: day=%s rows=%s text=%s chart %sx%s in %sx%s' % (s['day'], s['rows'], s['text'], s['sw'], s['sh'],
                                                                         s['cw'], s['ch']))
        if not _fills(s):
            bad.append('fit: the chart does not fill the width')

    up = base['text'] < TEXT_MAX                       # 文字 ＋, or − when it is already the biggest
    s = step('document.getElementById("%s").click()' % ('bTextIn' if up else 'bTextOut'), 'text size')
    fonts = float(s['font'][:-2] or 0), float(base['font'][:-2] or 0)
    out.append('text: %s -> %s, font %s -> %s, fit %s (day %s, rows %s)' % (
        base['text'], s['text'], base['font'], s['font'], 'on' if s['fit'] else 'off', s['day'], s['rows']))
    if (s['text'] > base['text'] and fonts[0] > fonts[1]) != up or s['text'] == base['text'] or s['fit'] != base['fit']:
        bad.append('text size')

    word = js('__st.word()')
    s = step('__st.find(%s)' % json.dumps(word), 'search')
    out.append('search %r: rows %s of %s, marked %s, box %r' % (word, s['nrows'], base['nrows'], s['marks'], s['found']))
    if not (0 < s['nrows'] <= base['nrows'] and s['marks'] > 0):
        bad.append('search')
    s = step('__st.find("")', 'clear search')
    if s['nrows'] != base['nrows'] or s['marks']:
        bad.append('search not cleared')
    who = json.loads(js('JSON.stringify(__st.person())'))
    if who:
        s = step('__st.who(%s)' % json.dumps(who[0]), 'person filter')
        out.append('person %r (%s): rows %s' % (who[0], who[1], s['nrows']))
        if not 0 < s['nrows'] <= base['nrows'] or ('（%d 件）' % s['nrows']) not in who[1]:
            bad.append('person filter')
        step('__st.who("")', 'all people')

    s = step('document.getElementById("bBy").click()', 'by person')
    ids = js('__st.ids()')
    out.append('view 担当ごと: buttons %s, %d headers (%s), ids %s' % (
        s['pressed'], len(s['heads']), '、'.join(s['heads'][:4]) + ('…' if len(s['heads']) > 4 else ''), ids))
    if s['pressed'] != ['false', 'true'] or not s['heads'] or ids != 'ok':
        bad.append('by-person view')
    if write and who:
        before = s['members'].get(who[0], [])
        js('__st.head(%s)' % json.dumps(who[0]))
        _until(win, 'S.person !== null && !!document.querySelector("#drawer [data-pn]")', 3)
        s = step('__st.rename("ZZテスト")', 'rename')
        renamed = s['members'].get('ZZテスト') == before and who[0] not in s['members']
        s = step('document.getElementById("bUndo").click()', 'undo the rename')
        back = s['members'].get(who[0]) == before and 'ZZテスト' not in s['members']
        out.append('rename %r -> "ZZテスト": %d rows %s, 取り消し %s' % (who[0], len(before), 'ok' if renamed else 'FAILED',
                                                                           'ok' if back else 'FAILED'))
        if not (renamed and back):
            bad.append('rename')
        js('closeDrawer()')
    drag = json.loads(js('JSON.stringify(__st.drag())'))
    _until(win, 'window.__ops !== null', 3)
    ops = json.loads(js('JSON.stringify(window.__ops)'))
    if drag and ops:
        o = ops[0]
        want = {k: (dt.date.fromisoformat(drag[k]) + dt.timedelta(days=3)).isoformat() for k in ('from', 'to')}
        out.append('drag in 担当ごと: %s item %s %s..%s -> %s..%s (not written)' % (
            o.get('row'), o.get('i'), drag['from'], drag['to'], o['set'].get('from'), o['set'].get('to')))
        if (o.get('op'), o.get('row'), o.get('i'), o.get('set')) != ('item', drag['row'], drag['i'], want):
            bad.append('drag in the by-person view')
    else:
        bad.append('drag in the by-person view: no op')
    js('window.commit = undefined')                    # nothing more may write
    s = step('document.getElementById("bOrder").click()', 'item order')
    if s['pressed'] != ['true', 'false'] or s['heads']:
        bad.append('item-order view')

    a = state()
    s = step('__st.wheel(true)', 'Ctrl+wheel')
    b = step('__st.wheel(false)', 'Alt+wheel')
    out.append('Ctrl+wheel: day %s -> %s (rows %s, text %s); Alt+wheel: rows %s -> %s, row height %s -> %s (day %s, text %s)'
               % (a['day'], s['day'], s['rows'], s['text'], s['rows'], b['rows'], s['rowh'], b['rowh'], b['day'], b['text']))
    if not (s['day'] > a['day'] and s['rows'] == a['rows'] and s['text'] == a['text'] and not s['fit']):
        bad.append('Ctrl+wheel')
    if not (b['rowh'] > s['rowh'] and b['day'] == s['day'] and b['text'] == s['text']):
        bad.append('Alt+wheel')

    f = json.loads(js('JSON.stringify(__st.frozen())'))
    out.append('frozen at scroll %s,%s: header %s, columns %s, corner %s, body %s'
               % (f['sl'], f['st'], [round(x, 1) for x in f['head']], [round(x, 1) for x in f['left']],
                  [round(x, 1) for x in f['corner']], [round(x, 1) for x in f['body']]))
    if f['st'] <= 0 or f['sl'] <= 0 or any(abs(x) > 0.5 for x in f['head'] + f['left'] + f['corner'] + f['body']):
        bad.append('frozen panes')

    want = json.loads(js('JSON.stringify(__st.hover())'))
    _until(win, '__st.tipText() !== null', 2)
    text = js('__st.tipText()') or ''
    out.append('hover: %r' % (text[:90] + ('…' if len(text) > 90 else '')))
    if (want['note'] or want['label']) not in text:
        bad.append('hover details')
    js('hideTip()')

    tc = state()['tc']
    s = step('__st.col(60)', 'column handle')
    out.append('column handle +60px: item column %s -> %s px (label_width %s)' % (tc, s['tc'], s['labelW']))
    if abs(s['tc'] - (tc + 60)) > s['text'] / 2 + 0.05:
        bad.append('column handle')
    s = step('__st.colReset()', 'column reset')
    if s['labelW'] is not None or abs(s['tc'] - tc) > 0.05:
        bad.append('column reset')

    s = step('document.getElementById("bFit").click()', 'fit')
    out.append('fit again: day=%s rows=%s text=%s chart %sx%s in %sx%s' % (s['day'], s['rows'], s['text'], s['sw'], s['sh'],
                                                                           s['cw'], s['ch']))
    if not s['fit'] or not _fills(s):
        bad.append('fit')
    gen, cw = js('S.gen'), s['cw']
    win.resize(1500, 1000)                             # still hidden: the chart follows the new size
    if _until(win, 'S.gen > %d && !S.inflight && !S.again && document.getElementById("sc").clientWidth > %d' % (gen, cw + 100), 6):
        _until(win, '!S.inflight && !S.again', 2)
        s = state()
        out.append('resized: day=%s rows=%s chart %sx%s in %sx%s' % (s['day'], s['rows'], s['sw'], s['sh'], s['cw'], s['ch']))
        if not _fills(s):
            bad.append('fit after a resize')
    else:
        bad.append('no new fit after a resize')
    res['errors'] = state()['errors']


def cmd_selftest(a):
    """Open the window hidden (1280 x 900) on FILE and drive the page. Nothing is written to FILE or to last.json; with
    --write the rename of a person is done too (and undone), and FILE gets its old bytes back afterwards."""
    path = need_file(a.file)
    api = Api(path, day_of(a, load_or_exit(path)))
    with open(path, 'rb') as fh:
        original = fh.read()
    res = {'lines': [], 'bad': [], 'errors': []}

    def run(win):
        try:
            _selftest_steps(win, res, a.write)
            try:                                           # is the window icon ours? (blue square, not the default)
                from webview.platforms.winforms import BrowserView
                bmp = BrowserView.instances[win.uid].Icon.ToBitmap()
                c = bmp.GetPixel(3, bmp.Height // 2)
                res['icon'] = 'ours' if c.B > c.R + 50 else 'default (rgb %d,%d,%d)' % (c.R, c.G, c.B)
            except Exception as e:
                res['icon'] = 'unknown (%s)' % e
        except Exception as e:
            res['error'] = repr(e)
            try:
                res['errors'] = json.loads(win.evaluate_js('JSON.stringify(window.__errors || [])'))
            except Exception:
                pass
        finally:
            win.destroy()

    try:
        start_gui(api, 'gantt selftest', True, run, STORE + '-selftest', size=(1280, 900))
    finally:
        with open(path, 'rb') as fh:
            changed = fh.read() != original
        if changed and a.write:
            write_bytes(path, original)
            res['lines'].append('file: written by the rename, old bytes put back')
        elif changed:
            res['bad'].append('the file changed')
    ok = not res['bad'] and not res['errors'] and 'error' not in res
    print('selftest: %s  errors=%s icon=%s%s' % ('ok' if ok else 'FAILED', res['errors'], res.get('icon'),
                                                 '  ' + res['error'] if 'error' in res else ''))
    for line in res['lines']:
        print('  ' + line)
    for line in res['bad']:
        print('  problem: ' + line)
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
    s = sub.add_parser('selftest', help='open the app hidden and drive it')
    s.add_argument('file')
    s.add_argument('--write', action='store_true', help='also rename a person and undo it (on a copy: the file is put back)')
    day(s)
    a = p.parse_args()
    {'app': cmd_app, 'serve': cmd_serve, 'svg': cmd_svg, 'png': cmd_image, 'pdf': cmd_image, 'share': cmd_share,
     'embed': cmd_embed, 'changes': cmd_changes, 'check': cmd_check, 'selftest': cmd_selftest}[a.cmd](a)


if __name__ == '__main__':
    main()
