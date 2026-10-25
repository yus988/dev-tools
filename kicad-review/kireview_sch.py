"""Schematic side of kicad-review: the changes between two versions of a KiCad schematic, or one design.

Each side is a project folder, a root .kicad_sch, or a git ref + path in a repo (`<repo>@<ref>:<path>`, exported with
`git archive`; the working tree is not touched). kicad-cli exports every sheet as SVG and the netlist (kicadxml) from a
copy under <data>/cache/sch/, so the compared project folders are only read. Diff turns the two netlists and the
sheet files into review items (changes, and every part of each side); kireview.py serves them at /sch.

The review notes (status, intent, thread per item key) are kept by kireview_store (reviews/sch/<slug>.json).
"""
import hashlib
import io
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tarfile
import threading
import time
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, 'cache', 'sch')     # set by kireview.py: <data>/cache/sch
KICAD_BIN = 'C:/Program Files/KiCad/10.0/bin'
GIT_SPEC = re.compile(r'^(?P<repo>.+?)@(?P<ref>[^:@]+):(?P<path>.*)$')
SKIP_FIELDS = {'Sheetname', 'Sheetfile'}
FIELD_LABELS = {'Value': '値', 'Footprint': 'フットプリント', 'Datasheet': 'データシート',
                'Description': '説明', 'Symbol': 'シンボル', 'Sheet': 'シート', 'dnp': 'DNP',
                'exclude_from_bom': 'BOM 除外', 'exclude_from_board': '基板除外'}
PROJECT_DEPTH = 2              # project picker: sub-folder levels searched below the folder
SKIP_DIRS = ('.git', 'node_modules', '__pycache__')
REF_COMMITS = 30               # ref picker: latest commits touching the project folder
CACHE_KEEP_PER_SPEC = 2        # older snapshots of the same side spec are deleted
TONES = {'red': '#E03C3C'}     # overlay: A drawn in red as a faint ghost under B
LIGHT = 0.75                   # colours lighter than this (fills, background) become white in a tone
CHAR_W = 0.72                  # field text width per character, in font heights (KiCad stroke font, approx.)
SAME_SPOT_MM = 0.01            # labels / power symbols at the same spot in A and B are the same one
DATASHEET_TYPES = {'.pdf': 'application/pdf', '.html': 'text/html', '.htm': 'text/html', '.png': 'image/png',
                   '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.txt': 'text/plain'}


# ---------------------------------------------------------------- s-expressions
_TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


def parse_sexpr(text):
    """Parse a KiCad S-expression file into nested lists (strings unquoted)."""
    stack = [[]]
    for m in _TOKEN.finditer(text):
        t = m.group(0)
        if t == '(':
            stack.append([])
        elif t == ')':
            node = stack.pop()
            stack[-1].append(node)
        elif t[0] == '"':
            stack[-1].append(t[1:-1].replace('\\"', '"').replace('\\\\', '\\'))
        else:
            stack[-1].append(t)
    return stack[0]


def children(node, name):
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == name]


def child(node, name):
    for c in node[1:]:
        if isinstance(c, list) and c and c[0] == name:
            return c
    return None


def nums(node, n):
    return [float(v) for v in node[1:1 + n]]


# ---------------------------------------------------------------- symbol boxes
def _lib_points(sub):
    """Outline points of one lib sub-symbol, in library coordinates (y up)."""
    pts = []
    for g in sub[1:]:
        if not isinstance(g, list) or not g:
            continue
        kind = g[0]
        if kind == 'rectangle':
            pts += [tuple(nums(child(g, 'start'), 2)), tuple(nums(child(g, 'end'), 2))]
        elif kind in ('polyline', 'bezier'):
            p = child(g, 'pts')
            if p:
                pts += [tuple(nums(xy, 2)) for xy in children(p, 'xy')]
        elif kind == 'circle':
            cx, cy = nums(child(g, 'center'), 2)
            r = nums(child(g, 'radius'), 1)[0]
            pts += [(cx - r, cy - r), (cx + r, cy + r)]
        elif kind == 'arc':
            for k in ('start', 'mid', 'end'):
                if child(g, k):
                    pts.append(tuple(nums(child(g, k), 2)))
        elif kind == 'pin':
            at = child(g, 'at')
            x, y = nums(at, 2)
            a = math.radians(float(at[3])) if len(at) > 3 else 0.0
            ln = nums(child(g, 'length'), 1)[0] if child(g, 'length') else 0.0
            pts += [(x, y), (x + ln * math.cos(a), y + ln * math.sin(a))]
    return pts


def lib_outlines(lib_symbols):
    """{lib_id: {unit: [points]}} from a schematic's lib_symbols cache (unit 0 = common)."""
    out = {}
    if not lib_symbols:
        return out
    for sym in children(lib_symbols, 'symbol'):
        units = {}
        for sub in children(sym, 'symbol'):
            m = re.search(r'_(\d+)_(\d+)$', sub[1])
            u = int(m.group(1)) if m else 0
            units.setdefault(u, []).extend(_lib_points(sub))
        out[sym[1]] = units
    return out


def symbol_box(sym, outlines):
    """Bounding box [x0, y0, x1, y1] (sheet mm, y down) of a placed symbol."""
    at = child(sym, 'at')
    x0, y0 = nums(at, 2)
    rot = float(at[3]) if len(at) > 3 else 0.0
    mirror = child(sym, 'mirror')
    unit = int(child(sym, 'unit')[1]) if child(sym, 'unit') else 1
    units = outlines.get(child(sym, 'lib_id')[1], {})
    pts = units.get(0, []) + units.get(unit, [])
    if not pts:
        pts = [(-1.27, -1.27), (1.27, 1.27)]
    c, s = round(math.cos(math.radians(rot))), round(math.sin(math.radians(rot)))
    xs, ys = [], []
    for lx, ly in pts:
        px, py = lx, -ly                     # library y is up
        px, py = px * c + py * s, -px * s + py * c   # rotation is counter-clockwise on screen
        if mirror and mirror[1] == 'x':
            py = -py
        elif mirror and mirror[1] == 'y':
            px = -px
        xs.append(x0 + px)
        ys.append(y0 + py)
    return [min(xs), min(ys), max(xs), max(ys)]


def field_box(prop):
    """Approximate box of a visible field text (sheet mm), or None when the field is hidden or empty."""
    at, eff = child(prop, 'at'), child(prop, 'effects')
    hide = child(prop, 'hide') or (child(eff, 'hide') if eff else None)
    hidden = (hide is not None and (len(hide) < 2 or hide[1] == 'yes')) or bool(eff and 'hide' in eff[1:])
    if not at or len(prop) < 3 or not prop[2] or hidden:
        return None
    x, y = nums(at, 2)
    rot = float(at[3]) if len(at) > 3 else 0.0
    font = child(eff, 'font') if eff else None
    size = nums(child(font, 'size'), 1)[0] if font and child(font, 'size') else 1.27
    text = re.sub(r'[\^_~]\{([^}]*)\}', r'\1', prop[2])
    w, h = max(1, len(text)) * size * CHAR_W, size * 1.25
    just = child(eff, 'justify') if eff else None
    just = just[1:] if just else []
    x0 = x if 'left' in just else x - w if 'right' in just else x - w / 2
    box = [x0, y - h / 2, x0 + w, y + h / 2]
    if round(rot) % 180 == 90:      # vertical text: rotate the box about the anchor
        box = [x - (y - box[1]), y - (box[2] - x), x + (box[3] - y), y + (x - box[0])]
        box = [min(box[0], box[2]), min(box[1], box[3]), max(box[0], box[2]), max(box[1], box[3])]
    return box


def label_box(name, at, size):
    """Approximate box of a global / hierarchical label: the flag extends from its anchor away from the wire."""
    x, y = nums(at, 2)
    rot = round(float(at[3])) % 360 if len(at) > 3 else 0
    w, h = len(name) * size * CHAR_W + size * 1.5, size * 1.6
    if rot == 0:
        return [x, y - h / 2, x + w, y + h / 2]
    if rot == 180:
        return [x - w, y - h / 2, x, y + h / 2]
    if rot == 90:
        return [x - h / 2, y - w, x + h / 2, y]
    return [x - h / 2, y, x + h / 2, y + w]


def sheet_file_boxes(path):
    """({symbol uuid: {box, fields}}, [(sub-sheet name, box)], [label]) for one .kicad_sch file.

    label = {kind: power | global | hierarchical, name (net name), x, y, uuid, box} for power symbols and
    global / hierarchical labels: the things that name a net."""
    with open(path, encoding='utf-8') as f:
        root = parse_sexpr(f.read())[0]
    outlines = lib_outlines(child(root, 'lib_symbols'))
    boxes = {}
    for sym in children(root, 'symbol'):
        uid = child(sym, 'uuid')
        if uid and child(sym, 'lib_id') and child(sym, 'at'):
            fields = {}
            for prop in children(sym, 'property'):
                b = field_box(prop)
                if b:
                    fields[prop[1]] = b
            boxes[uid[1]] = {'box': symbol_box(sym, outlines), 'fields': fields}
    labels = []
    libs = child(root, 'lib_symbols')
    # power symbols: flagged (power) / (power global) in the schematic's symbol cache, whatever library they come from
    power_ids = {sym[1] for sym in children(libs, 'symbol') if child(sym, 'power') is not None or 'power' in sym[2:]} if libs else set()
    for sym in children(root, 'symbol'):
        lib, at, uid = child(sym, 'lib_id'), child(sym, 'at'), child(sym, 'uuid')
        if lib and at and (lib[1] in power_ids or lib[1].startswith('power:')):
            value = [q[2] for q in children(sym, 'property') if q[1] == 'Value']
            if value:
                x, y = nums(at, 2)
                labels.append({'kind': 'power', 'name': value[0], 'x': x, 'y': y, 'uuid': uid[1] if uid else '',
                               'box': boxes[uid[1]]['box'] if uid and uid[1] in boxes else symbol_box(sym, outlines)})
    for kind in ('global_label', 'hierarchical_label'):
        for lab in children(root, kind):
            at, uid = child(lab, 'at'), child(lab, 'uuid')
            if not at or len(lab) < 2 or not isinstance(lab[1], str):
                continue
            eff = child(lab, 'effects')
            font = child(eff, 'font') if eff else None
            size = nums(child(font, 'size'), 1)[0] if font and child(font, 'size') else 1.27
            x, y = nums(at, 2)
            labels.append({'kind': kind.split('_')[0], 'name': lab[1], 'x': x, 'y': y, 'uuid': uid[1] if uid else '',
                           'box': label_box(lab[1], at, size)})
    subs = []
    for sh in children(root, 'sheet'):
        name = [p[2] for p in children(sh, 'property') if p[1] in ('Sheetname', 'Sheet name')]
        if name and child(sh, 'at') and child(sh, 'size'):
            x, y = nums(child(sh, 'at'), 2)
            w, h = nums(child(sh, 'size'), 2)
            subs.append((name[0], [x, y, x + w, y + h]))
    return boxes, subs, labels


def sheet_files(root_sch):
    """Relative paths (from the root's folder) of the root and every sub-sheet file."""
    base = os.path.dirname(root_sch)
    seen, todo = [], [os.path.basename(root_sch)]
    while todo:
        rel = todo.pop(0)
        if rel in seen:
            continue
        seen.append(rel)
        p = os.path.join(base, rel)
        if not os.path.isfile(p):
            continue
        with open(p, encoding='utf-8') as f:
            text = f.read()
        todo += [m.replace('\\', '/') for m in re.findall(r'\(property "Sheetfile" "([^"]+)"', text)]
    return seen


# ---------------------------------------------------------------- netlist
def parse_netlist(path):
    root = ET.parse(path).getroot()
    sheets = {}
    for sh in root.iter('sheet'):
        src = sh.find('title_block/source')
        sheets[sh.get('name')] = {'name': sh.get('name'), 'number': int(sh.get('number')),
                                  'source': src.text if src is not None else ''}
    comps = {}
    for c in root.iter('comp'):
        ref = c.get('ref')
        fields = {}
        for f in c.iter('field'):
            fields[f.get('name')] = f.text or ''
        for p in c.findall('property'):
            name = p.get('name')
            if name in SKIP_FIELDS or name.startswith('ki_'):
                continue
            fields.setdefault(name, p.get('value') or '')
        fields['Value'] = c.findtext('value') or ''
        fields['Footprint'] = c.findtext('footprint') or ''
        lib = c.find('libsource')
        if lib is not None:
            fields['Symbol'] = '%s:%s' % (lib.get('lib'), lib.get('part'))
        sp = c.find('sheetpath')
        comps[ref] = {'ref': ref, 'fields': fields, 'sheet': sp.get('names') if sp is not None else '/',
                      'uuids': (c.findtext('tstamps') or '').split(),
                      'path': sp.get('tstamps') if sp is not None else '/'}
    nets, pin_net = {}, {}
    for n in root.iter('net'):
        name = n.get('name')
        members = set()
        for node in n.iter('node'):
            key = (node.get('ref'), node.get('pin'))
            members.add(key)
            pin_net[key] = name
        nets[name] = members
    return {'sheets': sheets, 'comps': comps, 'nets': nets, 'pin_net': pin_net}


def is_auto_net(name):
    return name.startswith('Net-(') or name.startswith('unconnected-(')


# ---------------------------------------------------------------- sides
def parse_spec(spec):
    m = GIT_SPEC.match(spec)
    if m and os.path.isdir(m.group('repo')):
        return {'kind': 'git', 'repo': os.path.abspath(m.group('repo')), 'ref': m.group('ref'),
                'path': m.group('path').strip('/\\'), 'spec': spec}
    return {'kind': 'folder', 'path': os.path.abspath(spec), 'spec': spec}


def find_root(path):
    """Root .kicad_sch for a folder or file path."""
    if os.path.isfile(path):
        if not path.endswith('.kicad_sch'):
            raise ValueError('not a .kicad_sch file: %s' % path)
        return path
    if not os.path.isdir(path):
        raise ValueError('no such folder or file: %s' % path)
    names = os.listdir(path)
    cands = [n[:-len('.kicad_pro')] + '.kicad_sch' for n in names if n.endswith('.kicad_pro')]
    cands = [n for n in cands if n in names]
    if not cands:
        cands = [n for n in names if n.endswith('.kicad_sch')]
        subs = set()
        for n in cands:
            subs.update(sheet_files(os.path.join(path, n))[1:])
        cands = [n for n in cands if n not in subs]
    if not cands:
        raise ValueError('no KiCad schematic in %s' % path)
    folder = os.path.basename(os.path.normpath(path))
    cands.sort(key=lambda n: (n[:-len('.kicad_sch')] != folder, n))
    return os.path.join(path, cands[0])


def git(repo, *args, binary=False):
    r = subprocess.run(['git', '-C', repo] + list(args), capture_output=True)
    if r.returncode:
        raise ValueError('git %s: %s' % (' '.join(args), r.stderr.decode('utf-8', 'replace').strip()))
    return r.stdout if binary else r.stdout.decode('utf-8', 'replace').strip()


def _rmtree(path):
    def onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=onerror)


def _prune(spec, keep_dir):
    """Delete older snapshots of the same side spec (the cache is ours, no links inside)."""
    entries = []
    for d in os.listdir(CACHE):
        meta = os.path.join(CACHE, d, 'meta.json')
        if os.path.join(CACHE, d) == keep_dir or not os.path.isfile(meta):
            continue
        try:
            with open(meta, encoding='utf-8') as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        if m.get('spec') == spec:
            entries.append((os.path.getmtime(meta), os.path.join(CACHE, d)))
    for _, d in sorted(entries)[:max(0, len(entries) - (CACHE_KEEP_PER_SPEC - 1))]:
        _rmtree(d)


def folder_state(folder):
    """How a working folder relates to git, for display: latest (= the last commit touching it) or with uncommitted edits."""
    r = subprocess.run(['git', '-C', folder, 'rev-parse', '--show-toplevel'], capture_output=True)
    if r.returncode:
        return {'git': False, 'dirty': None, 'last': '', 'text': '最新（今のフォルダ。git 管理外）'}
    try:
        last = git(folder, 'log', '-1', '--format=%cd', '--date=format:%m/%d %H:%M', '--', '.').strip()
        dirty = bool(git(folder, 'status', '--porcelain', '--', '*.kicad_sch', '*.kicad_pro', '*-lib-table').strip())
    except Exception:
        return {'git': True, 'dirty': None, 'last': '', 'text': '最新（今のフォルダ）'}
    text = ('最新（今のフォルダ。%s の記録 ＋ 未記録の変更あり）' % last) if dirty else ('最新（今のフォルダ ＝ %s の記録）' % last)
    return {'git': True, 'dirty': dirty, 'last': last, 'text': text}


def snapshot(sp):
    """Copy the side into cache/<key>/src and return (key dir, root .kicad_sch in the copy, info)."""
    os.makedirs(CACHE, exist_ok=True)
    if sp['kind'] == 'git':
        sha = git(sp['repo'], 'rev-parse', '--verify', sp['ref'] + '^{commit}')
        key = hashlib.sha1(json.dumps(['git', sp['repo'], sha, sp['path']]).encode()).hexdigest()[:16]
        d = os.path.join(CACHE, key)
        ref = sp['ref'] if sha.startswith(sp['ref']) else '%s (%s)' % (sp['ref'], sha[:7])
        folder = os.path.join(sp['repo'], sp['path'])
        if sp['path'].endswith('.kicad_sch'):
            folder = os.path.dirname(folder)
            name = os.path.basename(sp['path'])[:-len('.kicad_sch')]
        else:
            name = os.path.basename(os.path.normpath(folder))
        when = git(sp['repo'], 'log', '-1', '--format=%cd', '--date=format:%m/%d %H:%M', sha).strip()
        info = {'kind': 'git', 'label': '%s @ %s' % (sp['path'], ref), 'short': '%s@%s' % (name, sp['ref']),
                'disp': '%s　%s 時点（%s）' % (name, when, sha[:7]),
                'sha': sha, 'readonly': True, 'folder': folder, 'ref': sp['ref']}
        src = os.path.join(d, 'src')
        if not os.path.isfile(os.path.join(d, 'meta.json')):
            if os.path.isdir(d):
                _rmtree(d)
            kind = git(sp['repo'], 'cat-file', '-t', '%s:%s' % (sha, sp['path']))
            arch = sp['path'] if kind == 'tree' else os.path.dirname(sp['path']) or '.'
            tar = git(sp['repo'], 'archive', '--format=tar', sha, '--', arch, binary=True)
            with tarfile.open(fileobj=io.BytesIO(tar)) as t:
                t.extractall(src, filter='data')
            for dirpath, _, files in os.walk(src):
                for n in files:
                    os.chmod(os.path.join(dirpath, n), stat.S_IREAD)
        root = find_root(os.path.join(src, sp['path']))
        return d, root, info
    orig_root = find_root(sp['path'])
    base = os.path.dirname(orig_root)
    rels = sheet_files(orig_root)
    rels += [n for n in os.listdir(base) if n.endswith(('.kicad_pro', '-lib-table'))]
    stamp = []
    for rel in rels:
        p = os.path.join(base, rel)
        if os.path.isfile(p):
            st = os.stat(p)
            stamp.append([rel, st.st_mtime_ns, st.st_size])
    key = hashlib.sha1(json.dumps(['folder', orig_root, stamp]).encode()).hexdigest()[:16]
    d = os.path.join(CACHE, key)
    src = os.path.join(d, 'src')
    if not os.path.isfile(os.path.join(d, 'meta.json')):
        if os.path.isdir(d):
            _rmtree(d)
        for rel, _, _ in stamp:
            dst = os.path.normpath(os.path.join(src, rel))
            if not dst.startswith(src):
                continue    # sheet file outside the project folder: not copied (limit, see README)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(base, rel), dst)
    info = {'kind': 'folder', 'label': orig_root.replace('\\', '/'), 'readonly': False, 'open': orig_root,
            'short': os.path.basename(base), 'folder': base,
            'disp': '%s　%s' % (os.path.basename(base), folder_state(base)['text'])}
    return d, os.path.join(src, os.path.basename(orig_root)), info


def svg_size(path):
    with open(path, 'rb') as f:
        head = f.read(2048).decode('utf-8', 'replace')
    m = re.search(r'viewBox="([\d.\-]+) ([\d.\-]+) ([\d.]+) ([\d.]+)"', head)
    return [float(v) for v in m.groups()] if m else [0, 0, 297, 210]


def recolour(svg, tone):
    """One-colour copy of a sheet SVG for the overlay: dark colours -> tone, light ones -> white."""
    tone = tone.encode()

    def sub(m):
        r, g, b = (int(m.group(1)[i:i + 2], 16) / 255 for i in (0, 2, 4))
        return b'#FFFFFF' if 0.299 * r + 0.587 * g + 0.114 * b > LIGHT else tone
    return re.sub(rb'#([0-9A-Fa-f]{6})', sub, svg)


def run_cli(kicad_bin, args):
    r = subprocess.run([os.path.join(kicad_bin, 'kicad-cli.exe' if os.name == 'nt' else 'kicad-cli')] + args,
                       capture_output=True)
    if r.returncode:
        raise RuntimeError('kicad-cli %s failed (%d): %s' % (' '.join(args[:3]), r.returncode,
                                                             (r.stderr or r.stdout).decode('utf-8', 'replace')[-800:]))


def prepare_side(spec, kicad_bin):
    sp = parse_spec(spec)
    d, root, info = snapshot(sp)
    meta_path = os.path.join(d, 'meta.json')
    if not os.path.isfile(meta_path):
        svg_dir = os.path.join(d, 'svg')
        os.makedirs(svg_dir, exist_ok=True)
        net_path = os.path.join(d, 'net.xml')
        run_cli(kicad_bin, ['sch', 'export', 'svg', '--no-background-color', '-o', svg_dir, root])
        run_cli(kicad_bin, ['sch', 'export', 'netlist', '--format', 'kicadxml', '-o', net_path, root])
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump({'spec': spec, 'root': root, 'info': info, 'time': time.time()}, f, ensure_ascii=False)
        _prune(spec, d)
    net = parse_netlist(os.path.join(d, 'net.xml'))
    # sheet path -> svg file: kicad-cli names them <root>.svg and <root>-<path with '-' for '/'>.svg
    stem = os.path.splitext(os.path.basename(root))[0]
    files = sorted(n for n in os.listdir(os.path.join(d, 'svg')) if n.endswith('.svg'))
    sheets = {}
    for name, sh in sorted(net['sheets'].items(), key=lambda kv: kv[1]['number']):
        want = stem + ('' if name == '/' else '-' + name.strip('/').replace('/', '-')) + '.svg'
        if want in files:
            fn = want
        else:
            rest = [n for n in files if n not in [s['svg'] for s in sheets.values()]]
            fn = rest[0] if rest else None
        if fn:
            sheets[name] = {'svg': fn, 'box': svg_size(os.path.join(d, 'svg', fn)),
                            'source': sh['source'], 'number': sh['number']}
    # symbol positions per sheet file
    root_dir = os.path.dirname(root)
    file_boxes = {}
    for name, sh in sheets.items():
        src = sh['source'] or os.path.basename(root)
        if src not in file_boxes:
            p = os.path.join(root_dir, src)
            file_boxes[src] = sheet_file_boxes(p) if os.path.isfile(p) else ({}, [], [])
        # hierarchical sheet boxes drawn on this sheet: double-click enters them in the page
        sh['children'] = [{'path': name + sub + '/', 'box': box} for sub, box in file_boxes[src][1]
                          if name + sub + '/' in net['sheets']]
    pos = {}
    for ref, c in net['comps'].items():
        sh = sheets.get(c['sheet'])
        if not sh:
            continue
        units = [file_boxes[sh['source'] or os.path.basename(root)][0].get(u) for u in c['uuids']]
        units = [u for u in units if u]
        if units:
            boxes = [u['box'] for u in units]
            fields = {}
            for u in units:
                for k, v in u['fields'].items():
                    fields.setdefault(k, v)
            pos[ref] = {'sheet': c['sheet'], 'fields': fields,
                        'box': [min(b[0] for b in boxes), min(b[1] for b in boxes),
                                max(b[2] for b in boxes), max(b[3] for b in boxes)]}
    info = dict(info, root_in_cache=root, dir=d)
    if info['readonly']:
        info['open'] = root
    labels = {name: file_boxes[sh['source'] or os.path.basename(root)][2] for name, sh in sheets.items()}
    return {'spec': spec, 'info': info, 'net': net, 'sheets': sheets, 'pos': pos, 'labels': labels}


def datasheet_target(side, ref):
    """('url', link) / ('file', path) / None for a part's Datasheet field. Only fields of this schematic are resolved."""
    c = side['net']['comps'].get(ref)
    v = (c['fields'].get('Datasheet') or '').strip() if c else ''
    if not v or v == '~':
        return None
    if re.match(r'^https?://', v, re.I):
        return ('url', v)
    folder = side['info']['folder']
    v = re.sub(r'^file:/+', '', v).replace('${KIPRJMOD}', folder).replace('$(KIPRJMOD)', folder)
    if '${' in v or '$(' in v:
        return None
    p = os.path.normpath(v if os.path.isabs(v) else os.path.join(folder, v))
    return ('file', p) if os.path.isfile(p) else None


def datasheet_link(side, key, ref):
    t = datasheet_target(side, ref)
    if not t:
        return None
    return t[1] if t[0] == 'url' else '/api/datasheet/%s/%s' % (key, ref)


# ---------------------------------------------------------------- diff
def same_part(ca, cb, ref):
    """The reference names the same kind of part in A and B (different boards reuse references for other parts)."""
    if ref not in ca or ref not in cb:
        return False
    fa, fb = ca[ref]['fields'], cb[ref]['fields']
    return fa.get('Symbol') == fb.get('Symbol') or fa.get('Value') == fb.get('Value')


PASSIVE_REF = re.compile(r'(R|C|L|FB)\d')


def match_nets(na, nb, ca=None, cb=None):
    """One-to-one A net -> B net by shared member pins (same name breaks ties). With the parts given, only pins of
    parts that are the same kind in A and B count, a resistor / capacitor / inductor pin counts a quarter (on another
    board the same reference is often a different resistor), and a pair needs at least half a point."""
    by_pin = {}
    for name, members in nb.items():
        for k in members:
            by_pin[k] = name
    scores = []
    for a, members in na.items():
        cnt = {}
        for k in members:
            b = by_pin.get(k)
            if b is None:
                continue
            if ca is None:
                cnt[b] = cnt.get(b, 0) + 1
            elif same_part(ca, cb, k[0]):
                cnt[b] = cnt.get(b, 0) + (0.25 if PASSIVE_REF.match(k[0]) else 1)
        if a in nb:
            cnt[a] = cnt.get(a, 0) + 0.5
        for b, s in cnt.items():
            if ca is None or s >= 0.5:
                scores.append((-s, a, b))
    scores.sort()
    amap, used = {}, set()
    for _, a, b in scores:
        if a not in amap and b not in used:
            amap[a] = b
            used.add(b)
    return amap


def _net_name(n):
    """Net name without the sheet path KiCad puts in front of local / hierarchical names."""
    return n.rstrip('/').rsplit('/', 1)[-1] if n else n


def label_changes(A, B):
    """Power symbols / global / hierarchical labels that are the same one in A and B (same uuid, or the same
    spot: also a power symbol replaced by another) but name a different net."""
    out = []
    for path in sorted(set(A['labels']) & set(B['labels'])):
        la, lb = A['labels'][path], B['labels'][path]
        used = set()
        for b in lb:
            a = next((a for n, a in enumerate(la) if n not in used and b['uuid'] and a['uuid'] == b['uuid']), None)
            if a is None:
                a = next((a for n, a in enumerate(la) if n not in used and abs(a['x'] - b['x']) < SAME_SPOT_MM
                          and abs(a['y'] - b['y']) < SAME_SPOT_MM), None)
            if a is None:
                continue
            used.add(la.index(a))
            if a['name'] != b['name']:
                out.append({'sheet': path, 'a': a, 'b': b, 'members': {}})
    return out


def _pin_key(p):
    return (0, int(p), '') if p.isdigit() else (1, 0, p)


def align_sheets(A, B, manual=None, min_share=0.3):
    """Pair A's sheets with B's and rename A's paths to the paired B paths, so the drawings pair up and parts are not
    reported as moved to another sheet. Pairs: the same path on both sides; then sheets whose path differs but hold
    mostly the same parts (e.g. /ESP32-S3-MINI-1/ -> /ESP32-S3-WROOM-1/ when comparing two boards); then the manual
    pairs {a path: b path, or '' = no pair} chosen in the page. Each sheet keeps its own path in sheets[...]['path'].
    Sets A['renamed_sheets'] (pairs whose paths differ)."""
    sa, sb = set(A['net']['sheets']), set(B['net']['sheets'])

    def refs_on(side, path):
        return {r for r, c in side['net']['comps'].items() if c['sheet'] == path}

    def parent(path):
        return path.rstrip('/').rsplit('/', 1)[0] + '/'

    pairs = {p: p for p in sa & sb}
    cands = []
    for a in sa - sb:
        ra = refs_on(A, a)
        for b in sb - sa:
            if parent(a) != parent(b):
                continue
            rb = refs_on(B, b)
            share = len(ra & rb) / max(1, len(ra | rb))
            if share >= min_share:
                cands.append((-share, a, b))
    used = set(pairs.values())
    for _, a, b in sorted(cands):
        if a not in pairs and b not in used:
            pairs[a] = b
            used.add(b)
    for a, b in (manual or {}).items():
        if a not in sa or (b and b not in sb):
            continue        # a sheet that no longer exists: the stored pair is ignored
        pairs = {k: v for k, v in pairs.items() if k != a and v != b}
        if b:
            pairs[a] = b
    # unpaired A sheets keep their path, unless B has a sheet of that path (then it is a different sheet)
    mapping = {a: pairs[a] if a in pairs else 'A:' + a if a in sb else a for a in sa}
    A['renamed_sheets'] = sorted((a, b) for a, b in pairs.items() if a != b)
    for k, v in A['sheets'].items():
        v['path'] = k
    for k, v in B['sheets'].items():
        v['path'] = k
    if all(a == b for a, b in mapping.items()):
        return
    prefixes = sorted(sa, key=len, reverse=True)

    def rn_net(name):       # local net names carry the sheet path in front: the longest matching one
        a = next((p for p in prefixes if name.startswith(p)), None)
        return mapping[a] + name[len(a):] if a else name

    net = A['net']
    net['sheets'] = {mapping[k]: dict(v, name=mapping[k]) for k, v in net['sheets'].items()}
    for c in net['comps'].values():
        c['sheet'] = mapping.get(c['sheet'], c['sheet'])
    net['nets'] = {rn_net(k): v for k, v in net['nets'].items()}
    net['pin_net'] = {k: rn_net(v) for k, v in net['pin_net'].items()}
    A['sheets'] = {mapping[k]: dict(v, children=[dict(ch, path=mapping.get(ch['path'], ch['path'])) for ch in v.get('children', [])])
                   for k, v in A['sheets'].items()}
    for p in A['pos'].values():
        p['sheet'] = mapping.get(p['sheet'], p['sheet'])
    A['labels'] = {mapping.get(k, k): v for k, v in A['labels'].items()}


def align_refs(A, B):
    """Give A's parts the references of the same symbols in B (same symbol uuid; the sheet instance too when the uuid
    is used more than once), so a re-annotation is one renumbering item and the real changes stay visible. Sets
    A['renumbered'] [(A reference, B reference)]; each renamed part keeps its A reference in comp['old_ref']. Two
    different boards share no uuids, so nothing is renamed there."""
    ca, cb = A['net']['comps'], B['net']['comps']

    def keys(comps):
        count = {}
        for c in comps.values():
            for u in c['uuids']:
                count[u] = count.get(u, 0) + 1
        out = {}
        for r, c in comps.items():
            for u in c['uuids']:
                out[u if count[u] == 1 else (c['path'], u)] = r
        return out

    kb = keys(cb)
    rmap = {}
    for k, r in keys(ca).items():
        if k in kb and kb[k] != r:
            rmap[r] = kb[k]
    A['renumbered'] = sorted(rmap.items(), key=lambda kv: _ref_key(kv[0]))
    if not rmap:
        return
    taken = set(rmap.values())
    for r in ca:        # a part of A only (deleted) whose reference now names another part keeps 'A:' in front
        if r not in rmap and r in taken:
            rmap[r] = 'A:' + r
    net = A['net']
    net['comps'] = {rmap.get(r, r): dict(c, ref=rmap.get(r, r), old_ref=r) if r in rmap else c for r, c in ca.items()}
    net['nets'] = {n: {(rmap.get(r, r), p) for r, p in mem} for n, mem in net['nets'].items()}
    net['pin_net'] = {(rmap.get(r, r), p): n for (r, p), n in net['pin_net'].items()}
    A['pos'] = {rmap.get(r, r): v for r, v in A['pos'].items()}


def _ref_key(r):
    m = re.match(r'^(.*?)(\d+)$', r)
    return (m.group(1), int(m.group(2)), r) if m else (r, 0, r)


def diff(A, B):
    na, nb = A['net'], B['net']
    items = []

    def add(kind, key, sheet, title, detail='', ref=None, refs=None, fields=None):
        refs = refs or ([ref] if ref else [])
        it = {'kind': kind, 'key': key, 'sheet': sheet, 'title': title, 'detail': detail, 'refs': refs,
              'fields': fields or [], 'mark': not kind.startswith('net_'),
              'ds': {'a': datasheet_link(A, 'a', ref) if ref else None, 'b': datasheet_link(B, 'b', ref) if ref else None},
              'a': [dict(A['pos'][r], ref=r) for r in refs if r in A['pos']],
              'b': [dict(B['pos'][r], ref=r) for r in refs if r in B['pos']]}
        items.append(it)

    if A.get('renumbered'):
        rn = A['renumbered']
        by_prefix = {}
        for old, new in rn:
            by_prefix.setdefault(_ref_key(old)[0], []).append('%s→%s' % (old, new))
        first = B['pos'].get(rn[0][1])
        add('renumbered', 'renumber', first['sheet'] if first else '/', '番号の付け直し: %d 個' % len(rn),
            '同じ部品（UUID が同じ）を A の番号 → B の番号で対応づけて比べています（A の図の番号は旧番号のまま）。\n' +
            '\n'.join('  '.join(v) for v in by_prefix.values()), refs=[new for _, new in rn])
    for old, new in A.get('renamed_sheets', []):
        add('sheet_renamed', 'sheet:' + new, new, 'シート名変更: %s → %s' % (old, new),
            '同じ部品が多いシートを同じものとして並べる（部品の所属シートは変更として数えない）')
    for s in sorted(set(na['sheets']) - set(nb['sheets'])):
        add('sheet_removed', 'sheet:' + s, s, 'シート削除: %s' % s)
    for s in sorted(set(nb['sheets']) - set(na['sheets'])):
        add('sheet_added', 'sheet:' + s, s, 'シート追加: %s' % s)

    ca, cb = na['comps'], nb['comps']
    for ref in sorted(set(cb) - set(ca)):
        f = cb[ref]['fields']
        pins = sorted([k[1] for k in nb['pin_net'] if k[0] == ref], key=_pin_key)
        conn = ', '.join('%s: %s' % (p, nb['pin_net'][(ref, p)]) for p in pins)
        add('comp_added', 'comp:' + ref, cb[ref]['sheet'], '%s 追加  %s' % (ref, f.get('Value', '')),
            '%s%s\n接続: %s' % (f.get('Footprint', ''), ('  LCSC ' + f['LCSC']) if f.get('LCSC') else '', conn), ref)
    for ref in sorted(set(ca) - set(cb)):
        f = ca[ref]['fields']
        add('comp_removed', 'comp:' + ref, ca[ref]['sheet'], '%s 削除  %s' % (ref, f.get('Value', '')), f.get('Footprint', ''), ref)
    for ref in sorted(set(ca) & set(cb)):
        fa, fb = dict(ca[ref]['fields']), dict(cb[ref]['fields'])
        fa['Sheet'], fb['Sheet'] = ca[ref]['sheet'], cb[ref]['sheet']
        lines, changed = [], []
        for k in sorted(set(fa) | set(fb), key=lambda k: (k not in FIELD_LABELS, list(FIELD_LABELS).index(k) if k in FIELD_LABELS else 0, k)):
            if fa.get(k, '') != fb.get(k, ''):
                changed.append(k)
                lines.append('%s: %s → %s' % (FIELD_LABELS.get(k, k), fa.get(k, '') or '(なし)', fb.get(k, '') or '(なし)'))
        if lines:
            title = '%s 変更  %s' % (ref, fb.get('Value', ''))
            add('comp_changed', 'field:' + ref, cb[ref]['sheet'], title, '\n'.join(lines), ref, fields=changed)

    amap = match_nets(na['nets'], nb['nets'], ca, cb)
    moved, folded = {}, set()
    for (ref, pin), a_net in na['pin_net'].items():
        if ref not in cb:
            continue
        b_net = nb['pin_net'].get((ref, pin))
        if b_net is None:
            moved.setdefault(ref, []).append((pin, a_net, '(ピンなし)'))
        elif amap.get(a_net) != b_net:
            moved.setdefault(ref, []).append((pin, a_net, b_net))
    for (ref, pin), b_net in nb['pin_net'].items():
        if ref in ca and (ref, pin) not in na['pin_net']:
            moved.setdefault(ref, []).append((pin, '(ピンなし)', b_net))
    # pin moves explained by a renamed power symbol / label are folded into one item per label
    labs = label_changes(A, B)

    def centre(ref):
        p = B['pos'].get(ref) or A['pos'].get(ref)
        return (p['sheet'], (p['box'][0] + p['box'][2]) / 2, (p['box'][1] + p['box'][3]) / 2) if p else (None, 0, 0)

    for ref in sorted(moved):
        sheet, cx, cy = centre(ref)
        keep = []
        for pin, a_net, b_net in moved[ref]:
            # only a label on the part's own sheet explains the move (elsewhere the part itself was rewired)
            cands = [l for l in labs if l['sheet'] == sheet and _net_name(a_net) == l['a']['name'] and _net_name(b_net) == l['b']['name']]
            if not cands:
                keep.append((pin, a_net, b_net))
                continue
            best = min(cands, key=lambda l: math.hypot(l['b']['x'] - cx, l['b']['y'] - cy))   # nearest one
            best['members'].setdefault(ref, []).append(pin)
        if keep:
            moved[ref] = keep
        else:
            del moved[ref]
            folded.add('pin:' + ref)
    for ref in sorted(moved):
        rows = sorted(moved[ref], key=lambda r: _pin_key(r[0]))
        groups = {}
        for pin, a_net, b_net in rows:
            groups.setdefault((a_net, b_net), []).append(pin)
        detail = '\n'.join('ピン %s: %s → %s' % (', '.join(p), a, b) for (a, b), p in groups.items())
        add('pin_moved', 'pin:' + ref, cb[ref]['sheet'], '%s 接続変更  %s' % (ref, cb[ref]['fields'].get('Value', '')), detail, ref)
    names = {'power': '電源シンボル', 'global': 'グローバルラベル', 'hierarchical': '階層ラベル'}
    for l in labs:
        a, b = l['a'], l['b']
        members = [{'ref': r, 'pins': sorted(p, key=_pin_key),
                    'title': '%s %s  ピン %s' % (r, cb[r]['fields'].get('Value', ''), ', '.join(sorted(p, key=_pin_key))),
                    'a': [dict(A['pos'][r], ref=r)] if r in A['pos'] else [], 'b': [dict(B['pos'][r], ref=r)] if r in B['pos'] else []}
                   for r, p in sorted(l['members'].items())]
        what = names[b['kind']] + ('（%s から置き換え）' % names[a['kind']] if a['kind'] != b['kind'] else
                                    '（置き換え）' if a['uuid'] != b['uuid'] else '')
        detail = what + ('\nつながる部品: ' + ', '.join(m['title'] for m in members) if members else '')
        items.append({'kind': 'label_changed', 'key': 'label:%s:%g,%g' % (l['sheet'], b['x'], b['y']), 'sheet': l['sheet'],
                      'title': 'ラベル変更: %s → %s' % (a['name'], b['name']), 'detail': detail, 'refs': [],
                      'fields': [], 'mark': True, 'ds': {'a': None, 'b': None}, 'label': [a['name'], b['name']],
                      'members': members, 'folds': sorted('pin:' + m['ref'] for m in members if 'pin:' + m['ref'] in folded),
                      'a': [{'sheet': l['sheet'], 'box': a['box'], 'fields': {}}],
                      'b': [{'sheet': l['sheet'], 'box': b['box'], 'fields': {}}]})

    def net_sheet(members, comps):
        cnt = {}
        for r, _ in members:
            if r in comps:
                cnt[comps[r]['sheet']] = cnt.get(comps[r]['sheet'], 0) + 1
        return max(cnt, key=cnt.get) if cnt else '/'

    def members_text(members):
        return ', '.join('%s.%s' % k for k in sorted(members, key=lambda k: (k[0], _pin_key(k[1]))))

    bmatched = set(amap.values())
    for a, b in sorted(amap.items()):
        if a != b and not (is_auto_net(a) and is_auto_net(b)):
            mem = nb['nets'][b]
            add('net_renamed', 'net:' + b, net_sheet(mem, cb), 'ネット名変更: %s → %s' % (a, b), members_text(mem),
                refs=sorted({r for r, _ in mem if r in B['pos']}))
    for a in sorted(set(na['nets']) - set(amap)):
        if not is_auto_net(a):
            mem = na['nets'][a]
            add('net_removed', 'net:' + a, net_sheet(mem, ca), 'ネット削除: %s' % a, members_text(mem),
                refs=sorted({r for r, _ in mem}))
    for b in sorted(set(nb['nets']) - bmatched):
        if not is_auto_net(b):
            mem = nb['nets'][b]
            add('net_added', 'net:' + b, net_sheet(mem, cb), 'ネット追加: %s' % b, members_text(mem),
                refs=sorted({r for r, _ in mem}))
    return items


def _parts(net):
    """(references in R1, R2, R10 order, {ref: [(pin, net name)]})."""
    pins_of = {}
    for (ref, pin), name in net['pin_net'].items():
        pins_of.setdefault(ref, []).append((pin, name))
    refs = sorted(net['comps'], key=lambda r: (re.sub(r'\d+', '', r), int(re.sub(r'\D', '', r) or 0), r))
    return refs, pins_of


def _part_detail(f, pins):
    """Footprint, LCSC / MPN, description and the net of every pin of one part."""
    lines = [f.get('Footprint', '')]
    extra = '  '.join('%s %s' % (k, f[k]) for k in ('LCSC', 'MPN') if f.get(k))
    if extra:
        lines.append(extra)
    if f.get('Description'):
        lines.append(f['Description'])
    pins = sorted(pins, key=lambda r: _pin_key(r[0]))
    if pins:
        lines.append('接続: ' + ', '.join('%s: %s' % p for p in pins))
    return '\n'.join(l for l in lines if l)


def part_items(A):
    """Single-design mode: every part is a review item (grouped by sheet in the page)."""
    net, items = A['net'], []
    refs, pins_of = _parts(net)
    for ref in refs:
        c = net['comps'][ref]
        f = c['fields']
        items.append({'kind': 'part', 'key': 'comp:' + ref, 'sheet': c['sheet'], 'title': '%s  %s' % (ref, f.get('Value', '')),
                      'detail': _part_detail(f, pins_of.get(ref, [])), 'refs': [ref], 'fields': [], 'mark': True,
                      'ds': {'a': datasheet_link(A, 'a', ref), 'b': None},
                      'a': [dict(A['pos'][ref], ref=ref)] if ref in A['pos'] else [], 'b': []})
    return items


def side_parts(S, k, quiet):
    """Two-design mode: every part of side k ('a' / 'b') is a review item too, so any part in the drawing can be
    asked about. quiet: the diff is shown, so the page lists a part only once it is selected or reviewed."""
    net, items, other = S['net'], [], 'b' if k == 'a' else 'a'
    refs, pins_of = _parts(net)
    for ref in refs:
        c = net['comps'][ref]
        f = c['fields']
        items.append({'kind': 'part', 'key': 'part:%s:%s' % (k, ref), 'sheet': c['sheet'],
                      'title': '%s %s%s  %s' % (k.upper(), ref, '（旧 %s）' % c['old_ref'] if c.get('old_ref') else '', f.get('Value', '')),
                      'detail': _part_detail(f, pins_of.get(ref, [])), 'refs': [ref], 'fields': [], 'mark': True,
                      'ds': {k: datasheet_link(S, k, ref), other: None},
                      k: [dict(S['pos'][ref], ref=ref)] if ref in S['pos'] else [], other: [],
                      'side': k, 'quiet': quiet})
    return items


def _slug(text):
    return re.sub(r'[^\w.@+-]+', '_', text).strip('_') or 'review'



# ---------------------------------------------------------------- state
class Diff:
    """The schematic pair (or one design) under review. stores(slug) gives the review store of a slug (created and
    attached by the server); cond is the lock of the stores (talkpage's Talk.cond)."""

    def __init__(self, spec_a, spec_b, kicad_bin, stores, cond):
        self.specs = {'a': spec_a, 'b': spec_b}
        self.kicad_bin = kicad_bin
        self.stores, self.cond = stores, cond
        self.lock = threading.Lock()
        self.sides = {}
        self.state = None
        self.store = None
        self.known = set()          # item keys of the current state (changes, parts, folded pins)

    def option(self, name, default):
        with self.cond:
            return self.store.data.get(name, default)

    def set_option(self, name, value):
        with self.cond:
            self.store.data[name] = value
            self.store.touch()
            self.cond.notify_all()

    def load(self):
        with self.lock:
            sides = {k: prepare_side(s, self.kicad_bin) for k, s in self.specs.items() if s}
            A, B = sides['a'], sides.get('b')
            single = B is None
            info = {k: {'spec': v['spec'], 'label': v['info']['label'], 'kind': v['info']['kind'],
                        'short': v['info']['short'], 'disp': v['info'].get('disp', v['info']['short']),
                        'folder': v['info']['folder'].replace('\\', '/'),
                        'ref': v['info'].get('ref', ''), 'readonly': v['info']['readonly']} for k, v in sides.items()}
            # the review file also holds this pair's settings (diff on / off, manual sheet pairs): read it first
            slug = _slug(info['a']['short'] if single else info['a']['short'] + '__' + info['b']['short'])
            self.store = self.stores(slug)
            show_diff = bool(self.option('show_diff', True))
            paths = {k: [n for n, _ in sorted(v['net']['sheets'].items(), key=lambda kv: kv[1]['number'])] if v else []
                     for k, v in (('a', A), ('b', B))}
            if single:
                items = part_items(A)
            else:
                align_sheets(A, B, dict(self.option('sheet_pairs', {})))
                align_refs(A, B)
                items = (diff(A, B) if show_diff else []) + side_parts(A, 'a', show_diff) + side_parts(B, 'b', show_diff)
            names = sorted(set(A['sheets']) | set(B['sheets'] if B else {}),
                           key=lambda n: (n != '/', ((B and B['sheets'].get(n)) or A['sheets'][n])['number'], n))
            sheets = []
            for n in names:
                sa, sb = A['sheets'].get(n), B['sheets'].get(n) if B else None
                sheets.append({'name': n,
                               'a': dict(sa, url='/sch/svg/a/' + sa['svg']) if sa else None,
                               'b': dict(sb, url='/sch/svg/b/' + sb['svg']) if sb else None,
                               'apath': sa.get('path', n) if sa else None, 'bpath': sb['path'] if sb else None})
            parts = {k: [{'ref': r, 'sheet': p['sheet'], 'box': p['box']} for r, p in sorted(v['pos'].items())] if v else []
                     for k, v in (('a', A), ('b', B))}
            # pin items folded into a label change keep their notes readable (and reachable for the agent)
            self.known = {it['key'] for it in items} | {k for it in items for k in it.get('folds', [])}
            self.sides = sides
            self.state = {'loaded': time.time(), 'single': single, 'sides': info, 'sheets': sheets, 'items': items,
                          'show_diff': show_diff, 'paths': paths, 'parts': parts,
                          'review': {'slug': slug, 'page': self.store.page, 'file': self.store.path.replace('\\', '/')}}
            return self.state

    def is_known(self, store, key):
        """A key the page or the agent may write: an item, folded pin or group of the loaded pair; in the review of an
        earlier pair, the items it already has."""
        if key.startswith('group:'):
            return any('group:' + g['id'] == key for g in store.data.get('groups', []))
        if store is self.store:
            return key in self.known
        return key in store.data['items']

    def set_show_diff(self, on):
        """Two designs: list the differences (True) or only put the drawings side by side (False)."""
        self.set_option('show_diff', bool(on))
        return self.load()

    def set_sheet_pair(self, a, b):
        """Pair A's sheet a (its own path) with B's sheet b, or b = '' to show a unpaired. Kept in the review file."""
        st = self.state
        if st['single']:
            raise ValueError('sheet pairs need two designs')
        if a not in st['paths']['a'] or (b and b not in st['paths']['b']):
            raise ValueError('no such sheet: %s' % (a if a not in st['paths']['a'] else b))
        pairs = self.option('sheet_pairs', {})
        # a sheet is in one pair only: an older pair with the same B sheet goes (that A sheet pairs automatically again)
        pairs = {k: v for k, v in pairs.items() if k != a and not (b and v == b)}
        pairs[a] = b
        self.set_option('sheet_pairs', pairs)
        return self.load()

    def compare(self, spec_a, spec_b):
        """Switch the sides (B empty = one design); on failure the previous pair stays loaded."""
        if not spec_a:
            raise ValueError('A を指定してください')
        old = self.specs
        self.specs = {'a': spec_a, 'b': spec_b or None}
        try:
            return self.load()
        except Exception:
            self.specs = old
            raise

    def svg_path(self, side, name):
        s = self.sides.get(side)
        if not s or '/' in name or '\\' in name or not name.endswith('.svg'):
            return None
        p = os.path.join(s['info']['dir'], 'svg', name)
        return p if os.path.isfile(p) else None

    def datasheet(self, side, ref):
        s = self.sides.get(side)
        t = datasheet_target(s, ref) if s else None
        if not t or t[0] != 'file':
            return None
        return t[1]

    def board_hint(self):
        """The .kicad_pcb of the newer side's project folder (B, or A for one design), if there is one."""
        s = self.sides.get('b') or self.sides.get('a')
        if not s:
            return None
        folder = s['info']['folder']
        stem = os.path.splitext(os.path.basename(s['info'].get('root_in_cache') or ''))[0]
        cands = [os.path.join(folder, stem + '.kicad_pcb')] if stem else []
        try:
            cands += sorted(os.path.join(folder, n) for n in os.listdir(folder) if n.endswith('.kicad_pcb'))
        except OSError:
            return None
        return next((p.replace('\\', '/') for p in cands if os.path.isfile(p)), None)

    def open_kicad(self, side):
        s = self.sides.get(side)
        if not s:
            raise ValueError('side must be a or b')
        target = s['info']['open']
        exe = os.path.join(self.kicad_bin, 'eeschema.exe' if os.name == 'nt' else 'eeschema')
        flags = 0x00000008 | 0x00000200 if os.name == 'nt' else 0   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([exe, target], creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {'opened': target.replace('\\', '/'), 'readonly': s['info']['readonly']}


def list_projects(folder):
    """KiCad projects (*.kicad_pro) in a folder and PROJECT_DEPTH levels below. Names only, no file contents."""
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise ValueError('no such folder: %s' % folder)
    out = []
    for dirpath, dirs, files in os.walk(folder):
        depth = 0 if dirpath == folder else os.path.relpath(dirpath, folder).count(os.sep) + 1
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.endswith('-backups')) if depth < PROJECT_DEPTH else []
        for n in sorted(files):
            stem = n[:-len('.kicad_pro')]
            if n.endswith('.kicad_pro') and os.path.isfile(os.path.join(dirpath, stem + '.kicad_sch')):
                pcb = os.path.join(dirpath, stem + '.kicad_pcb')
                out.append({'name': os.path.relpath(os.path.join(dirpath, stem), folder).replace('\\', '/'),
                            'path': dirpath.replace('\\', '/'),
                            'pcb': pcb.replace('\\', '/') if os.path.isfile(pcb) else None})
    return {'dir': folder.replace('\\', '/'), 'projects': out}


def list_refs(path):
    """Tags and recent commits of the git repo holding a project folder (read-only git commands)."""
    path = os.path.abspath(path)
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    if not os.path.isdir(folder):
        raise ValueError('no such folder: %s' % folder)
    r = subprocess.run(['git', '-C', folder, 'rev-parse', '--show-toplevel'], capture_output=True)
    if r.returncode:
        return {'repo': None}
    repo = os.path.abspath(r.stdout.decode('utf-8', 'replace').strip())
    rel = os.path.relpath(path, repo).replace('\\', '/')
    tags = []
    for line in git(repo, 'for-each-ref', '--sort=-creatordate', '--format=%(refname:short)%09%(creatordate:format:%m/%d %H:%M)',
                    'refs/tags').splitlines():
        name, _, date = line.partition('\t')
        tags.append({'name': name, 'date': date})
    commits = []
    for line in git(repo, 'log', '-%d' % REF_COMMITS, '--format=%h%x09%cd%x09%s', '--date=format:%m/%d %H:%M', '--', rel).splitlines():
        sha, date, subject = (line.split('\t', 2) + ['', ''])[:3]
        commits.append({'sha': sha, 'date': date, 'subject': subject})
    return {'repo': repo.replace('\\', '/'), 'rel': rel, 'tags': tags, 'commits': commits, 'state': folder_state(folder)}
