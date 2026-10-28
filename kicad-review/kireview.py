"""kicad-review: review KiCad 10 schematics and boards with a coding agent, on one local page.

  python kireview.py [A [B]] [BOARD.kicad_pcb] [--board BOARD] [--data DIR] [--port 8199] [--no-open]
                     [--kicad-bin DIR] [--rationale FILE] [--no-diff]
  python kireview.py wait | reply | ack | show ...      (talkpage's commands on this server; see README.md)
  python kireview.py put --file items.json              (board items, POST /api/board/item each)
  python kireview.py group --file groups.json           (schematic groups, POST /api/sch/group each)

A / B: the schematic to review (a project folder, a root .kicad_sch or <repo>@<ref>:<path>); with B the changes from
A to B are listed. BOARD: a .kicad_pcb to review. Either can be chosen in the page too. /sch is the schematic page,
/board the board page. <data> (default: the current folder) holds reviews/ (the review notes), cache/ and figures/.
The work is done in kireview_sch.py, kireview_board.py and kireview_store.py; the comment threads, the agent's wait
and replies are talkpage's (../talkpage/talkpage.py, routes /_talk/api/).
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import webbrowser

import kireview_store as ks
import kireview_board as kb
import kireview_sch as kc

tp = ks.tp
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8199
PAGES = {'/sch': 'sch.html', '/board': 'board.html'}
STATIC = {'/review.js': 'text/javascript', '/review.css': 'text/css'}
FIGURE_TYPES = {'.svg': 'image/svg+xml', '.png': 'image/png', '.webp': 'image/webp', '.jpg': 'image/jpeg'}
TALK_CMDS = ('wait', 'reply', 'ack', 'show')
NAME = re.compile(r'^[\w.@+-]+$')     # review names (board stems, schematic slugs) and figure files


def page_stamp():
    """The newest file time of the page files, so an open page can tell that a newer page is there."""
    t = 0
    for n in list(PAGES.values()) + [p.lstrip('/') for p in STATIC]:
        try:
            t = max(t, int(os.path.getmtime(os.path.join(HERE, n))))
        except OSError:
            pass
    return t


class App:
    """What the server holds: the review stores (talkpage's Talk), the board and the schematic under review."""

    def __init__(self, data, kicad_bin, port, seed=None):
        self.data, self.kicad_bin, self.port = data, kicad_bin, port
        self.talk = tp.Talk(None, port)
        self.cond = self.talk.cond
        self.board, self.board_store, self.sch = None, None, None
        self.seed = seed
        self.open_lock = threading.Lock()
        kc.CACHE = os.path.join(data, 'cache', 'sch')

    # ------------------------------------------------------------ stores and targets
    def store(self, kind, name):
        page = '%s:%s' % (kind, name)
        sub = ('sch',) if kind == 'sch' else ()
        path = os.path.join(self.data, 'reviews', *sub, name + '.json')
        with self.cond:
            s = self.talk.stores.get(page)
            if s is None:
                s = self.talk.stores[page] = ks.ReviewStore(path, page, kind)
            s.fresh()
            return s

    def sch_store(self, slug):
        s = self.store('sch', slug)
        if self.seed:                       # --rationale is for the pair given on the command line only
            with self.cond:
                ks.seed_rationale(s, self.seed)
                self.cond.notify_all()
            self.seed = None
        return s

    def open_board(self, path):
        path = os.path.abspath(str(path or ''))
        if not path.endswith('.kicad_pcb') or not os.path.isfile(path):
            raise ValueError('not a .kicad_pcb file: %s' % path)
        with self.open_lock:
            b = kb.Board(path, self.data, self.kicad_bin)
            b.load()                        # the export: a few seconds for a new version of the file
            s = self.store('board', b.stem)
            with self.cond:
                s.data['board'] = b.path.replace('\\', '/')
                old, self.board, self.board_store = self.board, b, s
                self.cond.notify_all()
            if old:
                old.stop()
            threading.Thread(target=b.watch, daemon=True).start()
        return self.board_info()

    def open_sch(self, a, b):
        if self.sch is None:
            d = kc.Diff(a, b or None, self.kicad_bin, self.sch_store, self.cond)
            st = d.load()
            self.sch = d
            return st
        return self.sch.compare(a, b)

    def board_info(self):
        if not self.board:
            return {'empty': True, 'hint': self.sch.board_hint() if self.sch else None}
        return dict(self.board.info(), review={'page': self.board_store.page,
                                               'file': self.board_store.path.replace('\\', '/')})

    def info(self):
        with self.cond:
            presence = self.talk.presence()
        st = self.sch.state if self.sch else None
        sch = None
        if st:
            sd = st['sides']
            sch = {'page': st['review']['page'], 'single': st['single'],
                   'title': sd['a']['short'] if st['single'] else sd['a']['short'] + ' → ' + sd['b']['short']}
        board = {'page': self.board_store.page, 'stem': self.board.stem,
                 'path': self.board.path.replace('\\', '/')} if self.board else None
        return {'tool': 'kireview', 'port': self.port, 'data': self.data.replace('\\', '/'), 'presence': presence,
                'page_stamp': page_stamp(), 'sch': sch, 'board': board,
                'board_hint': self.sch.board_hint() if self.sch and not self.board else None}

    # ------------------------------------------------------------ talkpage routes: checks before the store
    def check_talk(self, path, body):
        """/_talk/api/user and /agent: the item must belong to the review, the status and fields must be ours."""
        if path not in ('/_talk/api/user', '/_talk/api/agent') or not isinstance(body, dict):
            return
        with self.cond:
            s = self.talk.store(body.get('page'), loose=path == '/_talk/api/agent')
        if not isinstance(s, ks.ReviewStore):
            raise ValueError('not a review: %s' % s.page)
        body['page'] = s.page
        if body.get('status') is not None:
            body['status'] = ks.norm_status(body['status'])
            ks.check_status(body['status'])
        iid = body.get('id')
        if s.kind == 'board':
            if iid not in s.data['items']:
                raise ValueError('unknown item: %s (board items are made with POST /api/board/item)' % iid)
            ks.check_fields('board', body.get('fields'), s.data['items'][iid])
            body.pop('title', None)         # a board item's title is its own
        else:
            if not isinstance(iid, str) or not (iid in s.data['items'] or self.sch and self.sch.is_known(s, iid)):
                raise ValueError('unknown item key: %s' % iid)
            ks.check_fields('sch', body.get('fields'))
        if path == '/_talk/api/user':      # a status alone wakes the agent only when it asks for something
            body['notify'] = body.get('status') in ks.ASKING

    # ------------------------------------------------------------ routes: (code, body[, content type[, cacheable]])
    def get(self, path, q):
        def one(k, d=''):
            return (q.get(k) or [d])[0]
        if path == '/api/info':
            return 200, self.info()
        if path == '/api/board':
            return 200, self.board_info()
        if path == '/api/board/version':
            return (200, self.board.version()) if self.board else (200, {'version': None})
        if path.startswith('/board/svg/'):
            parts = path.split('/')                          # /board/svg/<hash>/<svg|fill>/<file>
            p = self.board.svg_path(parts[3], parts[4], parts[5]) if self.board and len(parts) == 6 else None
            return self._file(p, 'image/svg+xml', True)     # the URL carries the content hash
        if path.startswith('/figures/'):                     # <data>/figures/<review name>/<file>
            parts = [urllib.parse.unquote(x) for x in path.split('/')]
            ok = len(parts) == 4 and NAME.match(parts[2]) and NAME.match(parts[3])
            ctype = FIGURE_TYPES.get(os.path.splitext(parts[-1])[1].lower())
            return self._file(os.path.join(self.data, 'figures', parts[2], parts[3]) if ok and ctype else None, ctype)
        if path == '/api/sch/state':
            return 200, self.sch.state if self.sch else {'empty': True}
        if path.startswith('/sch/svg/'):
            parts = path.split('/')                          # /sch/svg/<a|b>/<file>[?tone=red]
            p = self.sch.svg_path(parts[3], parts[4]) if self.sch and len(parts) == 5 else None
            if not p:
                return 404, {'error': 'not found'}
            with open(p, 'rb') as f:
                body = f.read()
            if one('tone') in kc.TONES:
                body = kc.recolour(body, kc.TONES[one('tone')])
            return 200, body, 'image/svg+xml', True          # the URL carries ?v=<load time>
        if path.startswith('/api/datasheet/'):
            parts = path.split('/')
            p = self.sch.datasheet(parts[3], urllib.parse.unquote(parts[4])) if self.sch and len(parts) == 5 else None
            return self._file(p, kc.DATASHEET_TYPES.get(os.path.splitext(p or '')[1].lower(), 'application/octet-stream'))
        if path == '/api/projects':
            return 200, kc.list_projects(one('dir'))
        if path == '/api/refs':
            return 200, kc.list_refs(one('path'))
        return None

    @staticmethod
    def _file(p, ctype, cache=False):
        if not p or not ctype or not os.path.isfile(p):
            return 404, {'error': 'not found'}
        with open(p, 'rb') as f:
            return 200, f.read(), ctype, cache

    def _need(self, what):
        if what == 'board' and not self.board:
            raise ValueError('no board is open (POST /api/board/open {path})')
        if what == 'sch' and not self.sch:
            raise ValueError('no schematic is open (POST /api/sch/compare {a, b})')

    def post(self, path, body):
        if not isinstance(body, dict):
            raise ValueError('a JSON object is expected')
        if path == '/api/board/open':
            return 200, self.open_board(body.get('path'))
        if path == '/api/sch/compare':
            return 200, self.open_sch((body.get('a') or '').strip(), (body.get('b') or '').strip())
        if path.startswith('/api/board/'):
            self._need('board')
            if path == '/api/board/reload':
                self.board.wake.set()
                return 200, {'status': 'reloading'}
            with self.cond:
                if path == '/api/board/item':
                    out = ks.put_board_item(self.board_store, body)
                elif path == '/api/board/new':
                    out = self.board_new(body)
                else:
                    return None
                self.cond.notify_all()
            return 200, out
        if path.startswith('/api/sch/'):
            self._need('sch')
            if path == '/api/sch/reload':
                return 200, self.sch.load()
            if path == '/api/sch/show-diff':
                return 200, self.sch.set_show_diff(body.get('on'))
            if path == '/api/sch/sheet-pair':
                return 200, self.sch.set_sheet_pair(body.get('a') or '', body.get('b') or '')
            if path == '/api/sch/open':
                return 200, self.sch.open_kicad(body.get('side'))
            if path == '/api/sch/group':
                with self.cond:
                    out = ks.set_group(self.sch.store, body.get('id'), body.get('title') or '', body.get('keys'),
                                       body.get('rationale'))
                    self.cond.notify_all()
                return 200, out
        return None

    def delete(self, path, q):
        one = (q.get('id') or [''])[0]
        if path == '/api/board/item':
            self._need('board')
            with self.cond:
                s = self.board_store
                if one not in s.data['items']:
                    raise ValueError('unknown item id: %s' % one)
                del s.data['items'][one]
                s.touch()
                self.cond.notify_all()
                return 200, {'deleted': one, 'rev': s.data['rev']}
        if path == '/api/sch/group':
            self._need('sch')
            with self.cond:
                out = ks.delete_group(self.sch.store, one)
                self.cond.notify_all()
            return 200, out
        return None

    def board_new(self, body):
        """A question the user asks at a point of the board: item Q<n>, refs = what is there. Callers hold cond."""
        x, y, side, comment = body.get('x'), body.get('y'), body.get('side'), (body.get('comment') or '').strip()
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise ValueError('x and y (board mm) are required')
        if not isinstance(side, str) or not ks.SIDE_KEY.match(side):
            raise ValueError('side: "F", "B", "In1", ...')
        if not comment:
            raise ValueError('comment is required')
        ref, near = kb.nearest(self.board.info()['dump'], x, y, side)
        s = self.board_store
        n = 1
        while 'Q%d' % n in s.data['items']:
            n += 1
        iid = 'Q%d' % n
        first = comment.splitlines()[0]
        s.data['items'][iid] = {'title': first[:60] + ('…' if len(first) > 60 else ''), 'severity': '情報',
                                'owner': '未定', 'sides': [side], 'x': round(x, 3), 'y': round(y, 3), 'r': 1.0,
                                'refs': [ref] if ref else [], 'near': near, 'status': '', 'thread': [],
                                'user_rev': 0, 'agent_rev': 0, 'seen_rev': 0}
        it = s.user(iid, comment=comment)
        self.talk.last_user_at = time.time()
        return dict(it, id=iid, rev=s.data['rev'])


# ---------------------------------------------------------------- HTTP server
class Handler(tp.Handler):
    """talkpage's handler (127.0.0.1 only, own pages only, JSON POSTs) with the review routes."""
    app = None
    server_version = 'kireview/2'
    API = ('/_talk/api/', '/api/')

    def _acts(self, path, q):
        """Also the GETs that run git or walk folders at a path from the query."""
        return super()._acts(path, q) or path in ('/api/refs', '/api/projects')

    def _send(self, code, body, ctype='application/json', cache=False, headers=None):
        if not isinstance(body, bytes):
            body = tp.dump(body)
        text = ctype.startswith('text/') or ctype.endswith(('json', 'javascript'))
        self.send_response(code)
        self.send_header('Content-Type', ctype + ('; charset=utf-8' if text else ''))
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'max-age=86400' if cache else 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'SAMEORIGIN')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _run(self, fn):
        try:
            super()._run(fn)
        except RuntimeError as e:          # kicad-cli or git failed
            self._send(500, {'error': str(e)})

    def do_DELETE(self):
        self._run(self._delete)

    def _get(self):
        path, _, query = self.path.partition('?')
        if path == '/':
            target = '/board' if self.app.board and not self.app.sch else '/sch'
            return self._send(302, b'', 'text/plain', headers={'Location': target})
        name = PAGES.get(path) or (path.lstrip('/') if path in STATIC else None)
        if name:
            with open(os.path.join(HERE, name), 'rb') as f:
                return self._send(200, f.read(), 'text/html' if path in PAGES else STATIC[path])
        q = urllib.parse.parse_qs(query)
        r = self.app.talk.get(path, q) if path.startswith('/_talk/api/') else self.app.get(path, q)
        self._send(*r) if r else self._send(404, {'error': 'not found'})

    def _body(self):
        if not (self.headers.get('Content-Type') or '').lower().startswith('application/json'):
            return None
        n = int(self.headers.get('Content-Length') or 0)
        if n > 1000000:
            raise ValueError('body too large')
        return json.loads(self.rfile.read(n).decode('utf-8')) if n else {}

    def _post(self):
        body = self._body()
        if body is None:
            return self._send(415, {'error': 'Content-Type: application/json'})
        path = self.path.partition('?')[0]
        if path.startswith('/_talk/api/'):
            self.app.check_talk(path, body)
            r = self.app.talk.post(path, body)
        else:
            r = self.app.post(path, body)
        self._send(*r) if r else self._send(404, {'error': 'not found'})

    def _delete(self):
        path, _, query = self.path.partition('?')
        r = self.app.delete(path, urllib.parse.parse_qs(query))
        self._send(*r) if r else self._send(404, {'error': 'not found'})


# ---------------------------------------------------------------- command line
def cmd_post_each(a, route):
    """put / group: POST each object of a JSON file (an object or a list of them) to the running server."""
    with open(a.file, encoding='utf-8') as f:
        data = json.load(f)
    for obj in data if isinstance(data, list) else [data]:
        r = tp.call(a, route, obj)
        print('kireview: %s %s  rev %s' % (route, r.get('id'), r.get('rev')))


def serve(args):
    data = os.path.abspath(args.data)
    pcbs = [t for t in args.targets if t.lower().endswith('.kicad_pcb')]
    schs = [t for t in args.targets if t not in pcbs]
    if len(pcbs) + bool(args.board) > 1 or len(schs) > 2:
        raise SystemExit('kireview: at most one board and two schematics (A, B)')
    seed = None
    if args.rationale:
        with open(args.rationale, encoding='utf-8') as f:
            seed = json.load(f)
    app = App(data, args.kicad_bin, args.port, seed)
    Handler.app = app
    try:
        srv = tp.Server(('127.0.0.1', args.port), Handler)
    except OSError as e:
        raise SystemExit('kireview: cannot listen on 127.0.0.1:%d (%s). Another kireview? '
                         'http://127.0.0.1:%d/api/info tells what it shows.' % (args.port, e, args.port))
    if schs:
        print('kireview: exporting the schematic ...', flush=True)
        st = app.open_sch(schs[0], schs[1] if len(schs) > 1 else None)
        if args.no_diff and not st['single']:
            st = app.sch.set_show_diff(False)
        print('kireview: schematic %d items, %d sheets, review %s' % (len(st['items']), len(st['sheets']),
                                                                       st['review']['file']), flush=True)
    board = args.board or (pcbs[0] if pcbs else None)
    if board:
        print('kireview: exporting the board ...', flush=True)
        b = app.open_board(board)
        print('kireview: board %d layers, %d pads, review %s' % (len(b['layers']), len(b['dump']['pads']),
                                                                 b['review']['file']), flush=True)
    url = 'http://127.0.0.1:%d/' % args.port
    page = 'board' if board and not schs else 'sch'
    print('kireview: %s%s  (Ctrl-C to stop; the agent waits with: python "%s" wait%s)'
          % (url, page, os.path.abspath(__file__).replace('\\', '/'),
             '' if args.port == PORT else ' --port %d' % args.port), flush=True)
    if not args.no_open:
        webbrowser.open(url + page)
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if len(sys.argv) > 1 and sys.argv[1] in TALK_CMDS:
        sys.argv = [tp.__file__, '--port', str(PORT)] + sys.argv[1:]
        return tp.main()
    if len(sys.argv) > 1 and sys.argv[1] in ('put', 'group'):
        p = argparse.ArgumentParser(prog='kireview.py ' + sys.argv[1])
        p.add_argument('--file', required=True, help='JSON: one object or a list of them')
        p.add_argument('--port', type=int, default=PORT)
        a = p.parse_args(sys.argv[2:])
        return cmd_post_each(a, '/api/board/item' if sys.argv[1] == 'put' else '/api/sch/group')
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('targets', nargs='*', help='schematic A [B] (folder, .kicad_sch, <repo>@<ref>:<path>) and / or a .kicad_pcb')
    p.add_argument('--board', help='the .kicad_pcb to review')
    p.add_argument('--data', default='.', help='folder for reviews/, cache/ and figures/ (default: current folder)')
    p.add_argument('--port', type=int, default=PORT)
    p.add_argument('--no-open', action='store_true', help='do not open the page in the browser')
    p.add_argument('--kicad-bin', default=kb.KICAD_BIN, help='KiCad bin folder (kicad-cli, python, eeschema)')
    p.add_argument('--rationale', help='JSON file {item key: intent} to seed the schematic review (kept if already set)')
    p.add_argument('--no-diff', action='store_true', help='two schematics: only put them side by side (saved in the review)')
    serve(p.parse_args())


if __name__ == '__main__':
    main()
