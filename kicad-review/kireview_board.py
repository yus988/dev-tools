"""Board side of kicad-review: export a KiCad PCB for the review page and find what is at a point.

The board is copied to <data>/cache/board/<stem>/<hash>/ and kicad-cli exports one SVG per layer from the copy: copper
without zone fills (tracks, pads and vias stay readable), and the copper with the fills a second time, drawn faintly
under it in the page. kireview_dump.py (run with KiCad's python) lists footprints and pads. The project folder is
only read. When the board file changes (saved in KiCad) the export runs again in the background (Board.watch).
The review items live in kireview_store (reviews/<stem>.json); kireview.py serves the routes.
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

HERE = os.path.dirname(os.path.abspath(__file__))
DUMP = os.path.join(HERE, 'kireview_dump.py')
KICAD_BIN = 'C:/Program Files/KiCad/10.0/bin'
EXTRA_LAYERS = ('F.Silkscreen', 'B.Silkscreen', 'Edge.Cuts', 'F.Courtyard', 'B.Courtyard')
CROP_MARGIN = 2.0             # mm around the board outline kept in the layer SVGs
NEAR_PAD_MM = 1.5             # a click this close to a pad names the pad
WATCH_S = 2.0
CACHE_KEEP = 2                # current export and the one before
EXPORT_V = 2                  # cache format: an export of another version is made again


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
    """Copy, dump and plot the board into <data>/cache/board/<stem>/<hash>/ unless that export exists. Returns its
    meta: crop box, layer SVG files (zone fills stripped) and fills (copper layers with their zone fills)."""
    stem = os.path.splitext(os.path.basename(board))[0]
    with open(board, 'rb') as f:
        raw = f.read()
    h = hashlib.sha1(raw).hexdigest()[:12]
    base = os.path.join(data, 'cache', 'board', stem)
    d = os.path.join(base, h)
    meta_path = os.path.join(d, 'meta.json')
    if os.path.isfile(meta_path):
        with open(meta_path, encoding='utf-8') as f:
            meta = json.load(f)
        if meta.get('v') == EXPORT_V:
            return meta
    if os.path.isdir(d):
        _rmtree(d)
    os.makedirs(os.path.join(d, 'svg'))
    os.makedirs(os.path.join(d, 'fill'))
    copy = os.path.join(d, stem + '.kicad_pcb')
    with open(copy, 'w', encoding='utf-8', newline='') as f:
        f.write(strip_fills(raw.decode('utf-8')))
    filled = os.path.join(d, 'fill', stem + '.kicad_pcb')          # as saved: zones with their fills
    with open(filled, 'wb') as f:
        f.write(raw)
    pro = os.path.splitext(board)[0] + '.kicad_pro'
    if os.path.isfile(pro):
        shutil.copy2(pro, os.path.join(d, stem + '.kicad_pro'))
    py = os.path.join(kicad_bin, 'python.exe' if os.name == 'nt' else 'python3')
    run([py, DUMP, copy, os.path.join(d, 'dump.json')], d)
    with open(os.path.join(d, 'dump.json'), encoding='utf-8') as f:
        dump = json.load(f)
    layers = dump['layers'] + list(EXTRA_LAYERS)
    cli = os.path.join(kicad_bin, 'kicad-cli.exe' if os.name == 'nt' else 'kicad-cli')
    opts = ['--exclude-drawing-sheet', '--page-size-mode', '1', '--drill-shape-opt', '2']
    run([cli, 'pcb', 'export', 'svg', '--mode-multi', '-l', ','.join(layers), '-o', os.path.join(d, 'svg')] + opts + [copy], d)
    run([cli, 'pcb', 'export', 'svg', '--mode-multi', '-l', ','.join(dump['layers']), '-o', os.path.join(d, 'fill')]
        + opts + [filled], d)
    bb = dump['bbox'] or _parts_box(dump)
    crop = [bb[0] - CROP_MARGIN, bb[1] - CROP_MARGIN, bb[2] + CROP_MARGIN, bb[3] + CROP_MARGIN]
    files, fills = {}, {}
    for name in layers:
        fn = '%s-%s.svg' % (stem, name.replace('.', '_'))
        for sub, out in (('svg', files), ('fill', fills)):
            fp = os.path.join(d, sub, fn)
            if os.path.isfile(fp):
                crop_svg(fp, crop)
                out[name] = fn
    os.remove(filled)
    meta = {'v': EXPORT_V, 'hash': h, 'board': board.replace('\\', '/'), 'time': time.time(), 'crop': crop,
            'files': files, 'fills': fills}
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
        self.stopped = threading.Event()

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
        return os.path.join(self.data, 'cache', 'board', self.stem, h)

    def watch(self):
        while not self.stopped.is_set():
            forced = self.wake.wait(WATCH_S)
            self.wake.clear()
            if self.stopped.is_set():
                return
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

    def stop(self):
        """Another board was opened: end the watch thread."""
        self.stopped.set()
        self.wake.set()

    def version(self):
        with self.lock:
            return {'version': self.meta['hash'], 'status': self.status, 'error': self.error}

    def info(self):
        with self.lock:
            m, d = self.meta, self.dump
            return {'board': self.path.replace('\\', '/'), 'stem': self.stem, 'version': m['hash'],
                    'status': self.status, 'error': self.error, 'bbox': d['bbox'], 'crop': m['crop'],
                    'copper': d['layers'],
                    'layers': [{'name': n, 'url': '/board/svg/%s/svg/%s' % (m['hash'], fn),
                                'fill': '/board/svg/%s/fill/%s' % (m['hash'], m['fills'][n]) if n in m.get('fills', {}) else None}
                               for n, fn in m['files'].items()],
                    'dump': d}

    def svg_path(self, h, sub, name):
        if (not re.match(r'^[0-9a-f]+$', h) or sub not in ('svg', 'fill') or '/' in name or '\\' in name
                or not name.endswith('.svg')):
            return None
        p = os.path.join(self.dir(h), sub, name)
        return p if os.path.isfile(p) else None
