"""Board review core: export a KiCad PCB for the review page, the review items and the HTTP API.

The board is copied to <data>/cache/<stem>/<hash>/ (zone fills stripped) and kicad-cli exports one SVG per layer
from the copy; kireview_dump.py (run with KiCad's python) lists footprints and pads. The project folder is only read.
When the board file changes (saved in KiCad) the export runs again in the background (Board.watch).

Importable without a server: Board (export + watch), Review (items in <data>/reviews/<stem>.json) and Api, which
answers the routes below and returns None for any other path, so another server can mount it.

API: GET /api/board, GET /api/board/version, GET /svg/<hash>/<file>, POST /api/reload,
review: GET /api/review, GET /api/review/pending, POST /api/review/item {id, title, ...},
DELETE /api/review/item?id=, POST /api/review/agent {id, reply?, status?},
POST /api/review/user {id, status?, comment?}, POST /api/review/new {x, y, side, comment}.
"""
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from urllib.parse import parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
DUMP = os.path.join(HERE, 'kireview_dump.py')
KICAD_BIN = 'C:/Program Files/KiCad/10.0/bin'
SEVERITIES = ('必須', '推奨', '任意', '情報')
OWNERS = ('人', 'エージェント', '未定')
# the page offers 修正済み / 質問 / このまま; 自分で修正 / 修正を依頼 stay readable for older reviews
STATUSES = ('未確認', 'このまま', '自分で修正', '修正を依頼', '質問', '修正済み')
ASKING = ('修正を依頼', '質問')           # a user status that waits for the agent
SIDE_KEY = re.compile(r'^(F|B|In\d+)$')
EXTRA_LAYERS = ('F.Silkscreen', 'B.Silkscreen', 'Edge.Cuts')
CROP_MARGIN = 2.0             # mm around the board outline kept in the layer SVGs
NEAR_PAD_MM = 1.5             # a click this close to a pad names the pad
WATCH_S = 2.0
CACHE_KEEP = 2                # current export and the one before
# ask / ideal / background are the item's three sections (依頼 / 理想 / 経緯); text / action are the older pair
ITEM_FIELDS = ('title', 'severity', 'owner', 'sides', 'x', 'y', 'r', 'w', 'h', 'refs', 'ask', 'ideal', 'background',
               'text', 'action', 'status', 'thread', 'near')


# ---------------------------------------------------------------- export
def strip_fills(t):
    """Drop every (filled_polygon ...) so zones plot unfilled: tracks, pads and vias stay readable."""
    out, i = [], 0
    while True:
        j = t.find('(filled_polygon', i)
        if j < 0:
            return ''.join(out) + t[i:]
        out.append(t[i:j])
        d, k = 0, j
        while True:
            c = t[k]
            d += c == '('
            d -= c == ')'
            k += 1
            if d == 0:
                break
        i = k


def _rmtree(path):
    def onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=onerror)


def run(cmd, cwd):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True)
    if r.returncode:
        raise RuntimeError('%s failed (%d): %s' % (os.path.basename(cmd[0]), r.returncode,
                                                   (r.stderr or r.stdout).decode('utf-8', 'replace')[-800:]))


def crop_svg(path, box):
    """Shrink the page of a layer SVG to box (board mm) and drop a full-page background rect, if any.

    With --page-size-mode 1 the SVG page is the board's page: viewBox units per mm = viewBox width / width in mm
    (1 in KiCad 10) and the page origin is the board origin, so board mm map to user units by that factor alone."""
    with open(path, encoding='utf-8') as f:
        svg = f.read()
    m = re.search(r'width="([\d.]+)mm" height="([\d.]+)mm" viewBox="([\d.\-]+) ([\d.\-]+) ([\d.]+) ([\d.]+)"', svg)
    if not m:
        raise RuntimeError('unexpected SVG header: %s' % path)
    wmm, hmm, vx, vy, vw, vh = (float(v) for v in m.groups())
    k = vw / wmm
    x0, y0, x1, y1 = box

    def bg(r):
        a = dict(re.findall(r'(\w+)="([^"]*)"', r.group(0)))
        full = abs(float(a.get('width', 0)) - vw) < 0.01 and abs(float(a.get('height', 0)) - vh) < 0.01
        return '' if full else r.group(0)
    svg = re.sub(r'<rect[^>]*/>', bg, svg)
    svg = svg[:m.start()] + 'width="%.4fmm" height="%.4fmm" viewBox="%.4f %.4f %.4f %.4f"' % (
        x1 - x0, y1 - y0, vx + x0 * k, vy + y0 * k, (x1 - x0) * k, (y1 - y0) * k) + svg[m.end():]
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(svg)


def export(board, data, kicad_bin):
    """Copy, dump and plot the board into <data>/cache/<stem>/<hash>/ unless that export exists. Returns its meta."""
    stem = os.path.splitext(os.path.basename(board))[0]
    with open(board, 'rb') as f:
        raw = f.read()
    h = hashlib.sha1(raw).hexdigest()[:12]
    base = os.path.join(data, 'cache', stem)
    d = os.path.join(base, h)
    meta_path = os.path.join(d, 'meta.json')
    if os.path.isfile(meta_path):
        with open(meta_path, encoding='utf-8') as f:
            return json.load(f)
    if os.path.isdir(d):
        _rmtree(d)
    os.makedirs(os.path.join(d, 'svg'))
    copy = os.path.join(d, stem + '.kicad_pcb')
    with open(copy, 'w', encoding='utf-8', newline='') as f:
        f.write(strip_fills(raw.decode('utf-8')))
    pro = os.path.splitext(board)[0] + '.kicad_pro'
    if os.path.isfile(pro):
        shutil.copy2(pro, os.path.join(d, stem + '.kicad_pro'))
    py = os.path.join(kicad_bin, 'python.exe' if os.name == 'nt' else 'python3')
    run([py, DUMP, copy, os.path.join(d, 'dump.json')], d)
    with open(os.path.join(d, 'dump.json'), encoding='utf-8') as f:
        dump = json.load(f)
    layers = dump['layers'] + list(EXTRA_LAYERS)
    cli = os.path.join(kicad_bin, 'kicad-cli.exe' if os.name == 'nt' else 'kicad-cli')
    run([cli, 'pcb', 'export', 'svg', '--mode-multi', '-l', ','.join(layers), '-o', os.path.join(d, 'svg'),
         '--exclude-drawing-sheet', '--page-size-mode', '1', '--drill-shape-opt', '2', copy], d)
    bb = dump['bbox'] or _parts_box(dump)
    crop = [bb[0] - CROP_MARGIN, bb[1] - CROP_MARGIN, bb[2] + CROP_MARGIN, bb[3] + CROP_MARGIN]
    files = {}
    for name in layers:
        fn = '%s-%s.svg' % (stem, name.replace('.', '_'))
        p = os.path.join(d, 'svg', fn)
        if os.path.isfile(p):
            crop_svg(p, crop)
            files[name] = fn
    meta = {'hash': h, 'board': board.replace('\\', '/'), 'time': time.time(), 'crop': crop, 'files': files}
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False)
    # keep this export and the newest other one (the cache holds only files written here, no links)
    old = sorted((os.path.getmtime(os.path.join(base, n, 'meta.json')) if os.path.isfile(os.path.join(base, n, 'meta.json')) else 0, n)
                 for n in os.listdir(base) if n != h and os.path.isdir(os.path.join(base, n)))
    for _, n in old[:max(0, len(old) - (CACHE_KEEP - 1))]:
        _rmtree(os.path.join(base, n))
    return meta


def _parts_box(dump):
    """Board without an outline: the footprints' extent."""
    bs = [f['bbox'] for f in dump['footprints']] or [[0, 0, 100, 100]]
    return [min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs)]


def side_layer(side):
    return 'F.Cu' if side == 'F' else 'B.Cu' if side == 'B' else side + '.Cu'


def _inside(poly, x, y):
    c = False
    for i in range(len(poly)):
        (x0, y0), (x1, y1) = poly[i - 1], poly[i]
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            c = not c
    return c


def nearest(dump, x, y, side):
    """What was clicked: the nearest pad on that side's copper within NEAR_PAD_MM, else the smallest footprint whose
    box holds the point (that side's first). Returns (ref text, description) or (None, '')."""
    layer = side_layer(side)
    best = None
    for p in dump['pads']:
        if layer not in p['layers']:
            continue
        dist = 0.0 if p.get('poly') and _inside(p['poly'], x, y) else math.hypot(p['x'] - x, p['y'] - y)
        if dist <= NEAR_PAD_MM and (best is None or dist < best[0]):
            best = (dist, p)
    if best:
        p = best[1]
        return '%s.%s' % (p['ref'], p['num']), '%s %s %s' % (p['ref'], p['num'], p['net'] or '(ネットなし)')
    hits = [f for f in dump['footprints'] if f['bbox'][0] <= x <= f['bbox'][2] and f['bbox'][1] <= y <= f['bbox'][3]]
    if hits:
        f = min(hits, key=lambda f: (f['side'] != side, (f['bbox'][2] - f['bbox'][0]) * (f['bbox'][3] - f['bbox'][1])))
        return f['ref'], '%s (%s) %s' % (f['ref'], f['value'], '表' if f['side'] == 'F' else '裏')
    return None, ''


class Board:
    """The board under review: the last good export, re-exported in a background thread when the file changes."""

    def __init__(self, path, data, kicad_bin):
        self.path = os.path.abspath(path)
        self.data = data
        self.kicad_bin = kicad_bin
        self.stem = os.path.splitext(os.path.basename(self.path))[0]
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.meta, self.dump = None, None
        self.status, self.error = 'ok', ''
        self.stamp = None

    def _stamp(self):
        st = os.stat(self.path)
        return (st.st_mtime_ns, st.st_size)

    def load(self):
        stamp = self._stamp()
        meta = export(self.path, self.data, self.kicad_bin)
        with open(os.path.join(self.dir(meta['hash']), 'dump.json'), encoding='utf-8') as f:
            dump = json.load(f)
        with self.lock:
            self.meta, self.dump, self.stamp = meta, dump, stamp
            self.status, self.error = 'ok', ''

    def dir(self, h):
        return os.path.join(self.data, 'cache', self.stem, h)

    def watch(self):
        while True:
            forced = self.wake.wait(WATCH_S)
            self.wake.clear()
            try:
                if not forced and self._stamp() == self.stamp:
                    continue
            except OSError:         # the editor is replacing the file: next round
                continue
            with self.lock:
                self.status = 'reloading'
            try:
                self.load()
            except Exception as e:  # keep serving the last good export; the page shows the error
                with self.lock:
                    self.status, self.error = 'error', str(e)
                    try:
                        self.stamp = self._stamp()
                    except OSError:
                        pass

    def version(self):
        with self.lock:
            return {'version': self.meta['hash'], 'status': self.status, 'error': self.error}

    def info(self):
        with self.lock:
            m, d = self.meta, self.dump
            return {'board': self.path.replace('\\', '/'), 'stem': self.stem, 'version': m['hash'],
                    'status': self.status, 'error': self.error, 'bbox': d['bbox'], 'crop': m['crop'],
                    'copper': d['layers'],
                    'layers': [{'name': n, 'url': '/svg/%s/%s' % (m['hash'], fn)} for n, fn in m['files'].items()],
                    'dump': d}

    def svg_path(self, h, name):
        if not re.match(r'^[0-9a-f]+$', h) or '/' in name or '\\' in name or not name.endswith('.svg'):
            return None
        p = os.path.join(self.dir(h), 'svg', name)
        return p if os.path.isfile(p) else None


# ---------------------------------------------------------------- review
def _id_key(i):
    m = re.match(r'^(.*?)(\d+)$', i)
    return (m.group(1), int(m.group(2)), i) if m else (i, 0, i)


class Review:
    """Review items of one board. Besides the documented fields an item keeps status_rev (review revision of the
    user's last status change) and agent_rev (of the agent's last reply), so a request made after the last reply is
    pending even without a comment."""

    def __init__(self, path, board):
        self.path = path
        self.lock = threading.Lock()
        self.data = {'board': board, 'items': {}}
        if os.path.isfile(path):
            with open(path, encoding='utf-8') as f:
                self.data = json.load(f)
        self.data.setdefault('items', {})
        self.data['board'] = board
        self.rev = int(self.data.get('rev', 0))

    def _save(self):
        self.rev += 1
        self.data['rev'] = self.rev
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def get(self):
        with self.lock:
            return {'board': self.data['board'], 'file': self.path.replace('\\', '/'), 'rev': self.rev,
                    'items': self.data['items']}

    @staticmethod
    def _pending(it):
        th = it.get('thread') or []
        if th and th[-1].get('who') == 'user':
            return True
        return it.get('status') in ASKING and it.get('status_rev', 0) > it.get('agent_rev', 0)

    def pending(self):
        with self.lock:
            items = [dict(it, id=k) for k, it in sorted(self.data['items'].items(), key=lambda kv: _id_key(kv[0]))
                     if self._pending(it)]
            return {'board': self.data['board'], 'rev': self.rev, 'items': items}

    def _item(self, iid):
        it = self.data['items'].get(iid)
        if it is None:
            raise ValueError('unknown item id: %s' % iid)
        return it

    @staticmethod
    def _check(it):
        if it.get('severity') not in SEVERITIES:
            raise ValueError('severity must be one of ' + ', '.join(SEVERITIES))
        if it.get('owner') not in OWNERS:
            raise ValueError('owner must be one of ' + ', '.join(OWNERS))
        if it.get('status') not in STATUSES:
            raise ValueError('status must be one of ' + ', '.join(STATUSES))
        sides = it.get('sides')
        if not isinstance(sides, list) or not sides or not all(isinstance(s, str) and SIDE_KEY.match(s) for s in sides):
            raise ValueError('sides: a non-empty list of "F", "B", "In1", "In2", ...')
        for k in ('x', 'y'):
            if not isinstance(it.get(k), (int, float)):
                raise ValueError('%s (board mm) is required' % k)
        if not isinstance(it.get('refs', []), list) or not isinstance(it.get('thread', []), list):
            raise ValueError('refs and thread are lists')

    def put(self, body):
        """Create or replace an item; status and thread are kept unless given."""
        iid = str(body.get('id') or '').strip()
        if not re.match(r'^[\w.-]+$', iid):
            raise ValueError('id: letters, digits, _ . - only')
        if not str(body.get('title') or '').strip():
            raise ValueError('title is required')
        with self.lock:
            old = self.data['items'].get(iid) or {}
            it = {'severity': '情報', 'owner': '未定', 'refs': [], 'text': '', 'action': '',
                  'status': old.get('status', '未確認'), 'thread': old.get('thread', [])}
            for k in ('status_rev', 'agent_rev'):
                if k in old:
                    it[k] = old[k]
            it.update({k: body[k] for k in ITEM_FIELDS if k in body})
            if 'r' not in it and 'w' not in it:
                it['r'] = 1.0
            self._check(it)
            self.data['items'][iid] = it
            self._save()
            return dict(it, id=iid, rev=self.rev)

    def delete(self, iid):
        with self.lock:
            self._item(iid)
            del self.data['items'][iid]
            self._save()
            return {'deleted': iid, 'rev': self.rev}

    def post(self, who, iid, status=None, text=None):
        if status is not None and status not in STATUSES:
            raise ValueError('status must be one of ' + ', '.join(STATUSES))
        with self.lock:
            it = self._item(iid)
            if status is not None:
                it['status'] = status
            if text and text.strip():
                it.setdefault('thread', []).append({'who': who, 'text': text.strip(),
                                                    'time': time.strftime('%Y-%m-%d %H:%M:%S')})
            if who == 'agent':
                it['agent_rev'] = self.rev + 1       # the revision _save is about to write
            elif status is not None:
                it['status_rev'] = self.rev + 1
            self._save()
            return dict(it, id=iid, rev=self.rev)

    def new(self, x, y, side, comment, dump):
        """A question the user asks at a point of the board: item Q<n>, refs = what is there."""
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise ValueError('x and y (board mm) are required')
        if not isinstance(side, str) or not SIDE_KEY.match(side):
            raise ValueError('side: "F", "B", "In1", ...')
        if not (comment or '').strip():
            raise ValueError('comment is required')
        ref, near = nearest(dump, x, y, side)
        first = comment.strip().splitlines()[0]
        with self.lock:
            n = 1
            while 'Q%d' % n in self.data['items']:
                n += 1
            iid = 'Q%d' % n
            it = {'title': first[:60] + ('…' if len(first) > 60 else ''), 'severity': '情報', 'owner': '未定',
                  'sides': [side], 'x': round(x, 3), 'y': round(y, 3), 'r': 1.0, 'refs': [ref] if ref else [],
                  'near': near, 'text': '', 'action': '', 'status': '質問',
                  'thread': [{'who': 'user', 'text': comment.strip(), 'time': time.strftime('%Y-%m-%d %H:%M:%S')}],
                  'status_rev': self.rev + 1}
            self.data['items'][iid] = it
            self._save()
            return dict(it, id=iid, rev=self.rev)


# ---------------------------------------------------------------- API
class Api:
    """The board / review routes. Each method returns (code, body, content type, cacheable) or None (not ours)."""

    def __init__(self, board, review):
        self.board, self.review = board, review

    def get(self, path, query):
        if path == '/api/board':
            return 200, self.board.info(), 'application/json', False
        if path == '/api/board/version':
            return 200, self.board.version(), 'application/json', False
        if path == '/api/review':
            return 200, self.review.get(), 'application/json', False
        if path == '/api/review/pending':
            return 200, self.review.pending(), 'application/json', False
        if path.startswith('/svg/'):
            parts = path.split('/')
            p = self.board.svg_path(parts[2], parts[3]) if len(parts) == 4 else None
            if not p:
                return 404, {'error': 'not found'}, 'application/json', False
            with open(p, 'rb') as f:
                return 200, f.read(), 'image/svg+xml', True        # the URL carries the content hash
        return None

    def post(self, path, body):
        if path == '/api/reload':
            self.board.wake.set()
            return 200, {'status': 'reloading'}, 'application/json', False
        if path == '/api/review/item':
            out = self.review.put(body)
        elif path == '/api/review/agent':
            out = self.review.post('agent', body.get('id') or '', body.get('status'), body.get('reply'))
        elif path == '/api/review/user':
            out = self.review.post('user', body.get('id') or '', body.get('status'), body.get('comment'))
        elif path == '/api/review/new':
            out = self.review.new(body.get('x'), body.get('y'), body.get('side'), body.get('comment'),
                                  self.board.info()['dump'])
        else:
            return None
        return 200, out, 'application/json', False

    def delete(self, path, query):
        if path == '/api/review/item':
            return 200, self.review.delete((parse_qs(query).get('id') or [''])[0]), 'application/json', False
        return None
