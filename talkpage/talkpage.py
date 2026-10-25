#!/usr/bin/env python3
"""talkpage: local web pages where the user marks items and writes comments, and a coding agent is woken to answer.

  python talkpage.py serve <root> [--port 8190] [--open PAGE]
  python talkpage.py wait  [--page P] [--settle 4]
  python talkpage.py reply --page P --id ID [--status S] [--text T | --text - | --file F] [--fields JSON]
  python talkpage.py ack   [--page P] [--id ID ...]
  python talkpage.py show  [--page P]

serve: <root> holds the pages; the state of <dir>/<name>.html is kept beside it in <dir>/<name>.talk.json. Only
127.0.0.1 is served. wait: blocks until the user acts on a page, prints what they did and exits; an agent runs it in
the background (Claude Code: Bash run_in_background) so that the exit wakes it. reply: answer an item (the reply
shows on the page). ack: mark what was printed as read without replying. show: all items of a page.
P is a page path under the root or a unique part of one. --port defaults to $TALKPAGE_PORT or 8190.
The page side is talkpage.js / talkpage.css (served at /_talk/); see README.md.
"""
import argparse
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get('TALKPAGE_PORT') or 8190)
ID_RE = re.compile(r'^[^\x00-\x1f\x7f]{1,200}$')     # also other tools' keys, e.g. net:/Power In/Net-(U6-EN)
RESERVED = ('thread', 'status', 'user_rev', 'agent_rev', 'seen_rev')     # item fields the agent cannot set directly
SKIP_DIRS = ('node_modules', '__pycache__')
CLIENT = ('talkpage.js', 'talkpage.css')
TYPES = {'.html': 'text/html', '.htm': 'text/html', '.css': 'text/css', '.js': 'text/javascript',
         '.json': 'application/json', '.txt': 'text/plain', '.md': 'text/plain', '.csv': 'text/plain',
         '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
         '.webp': 'image/webp', '.gif': 'image/gif', '.ico': 'image/x-icon', '.pdf': 'application/pdf',
         '.mp4': 'video/mp4', '.woff2': 'font/woff2'}
FAVICON = ("<link rel=\"icon\" href=\"data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
           "<text y='.9em' font-size='90'>💬</text></svg>\">")


def stamp():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def natural(key):
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', key)]


def check_status(status):
    if status is None:
        return None
    if not isinstance(status, str) or len(status.strip()) > 24:
        raise ValueError('status: a short string ("" clears it)')
    return status.strip()


def waiting(it):
    return it.get('user_rev', 0) > it.get('agent_rev', 0)


def upgrade(it):
    """Items written without revisions (older review files): the item waits if its last message is the user's."""
    it.setdefault('status', '')
    th = it.setdefault('thread', [])
    if 'user_rev' not in it:
        last_agent = max([i for i, m in enumerate(th) if m.get('who') == 'agent'] or [-1])
        for i, m in enumerate(th):
            m.setdefault('rev', 1 if i > last_agent and m.get('who') == 'user' else 0)
        it['user_rev'] = 1 if th and th[-1].get('who') == 'user' else 0
    it.setdefault('agent_rev', 0)
    it.setdefault('seen_rev', 0)


class Store:
    """State of one page: {page, rev, items: {id: item}} plus whatever else the owner keeps in the file.

    item: {title, status, thread: [{who: user|agent, text | status, time, rev}], user_rev, agent_rev, seen_rev, ...}.
    user_rev is the revision of the user's last action meant for the agent (a comment, or a status the page reports),
    agent_rev of the agent's last reply or ack, seen_rev of the last agent message the user has read. The item waits
    for the agent while user_rev > agent_rev. Callers hold Talk.cond."""

    def __init__(self, path, page):
        self.path, self.page = path, page
        self.taken = {}                        # id -> user_rev already handed to a `wait` (memory only)
        self.mtime = None
        self.data = {'page': page, 'rev': 0, 'items': {}}
        self.load()

    def load(self):
        if not os.path.isfile(self.path):
            return
        with open(self.path, encoding='utf-8') as f:
            d = json.load(f)
        d.setdefault('items', {})
        for it in d['items'].values():
            upgrade(it)
        d['rev'] = max(int(d.get('rev') or 0), 1)
        self.data, self.mtime = d, os.path.getmtime(self.path)

    def fresh(self):
        """Pick up an edit made outside the server; a deleted file starts the page over."""
        if not os.path.isfile(self.path):
            if self.mtime is not None:
                self.mtime, self.taken = None, {}
                self.data = {'page': self.page, 'rev': self.data['rev'] + 1, 'items': {}}
        elif os.path.getmtime(self.path) != self.mtime:
            self.load()

    def save(self):
        os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
            f.write('\n')
        for i in range(20):                    # Windows: an editor or sync client may hold the file for a moment
            try:
                os.replace(tmp, self.path)
                break
            except PermissionError:
                if i == 19:
                    raise
                time.sleep(0.05)
        self.mtime = os.path.getmtime(self.path)

    def _next(self):
        self.data['rev'] += 1
        return self.data['rev']

    def item(self, iid, create=False):
        if not isinstance(iid, str) or not ID_RE.match(iid):
            raise ValueError('id: 1-200 characters, no control characters')
        it = self.data['items'].get(iid)
        if it is None:
            if not create:
                raise ValueError('unknown item: %s' % iid)
            it = self.data['items'][iid] = {'status': '', 'thread': [], 'user_rev': 0, 'agent_rev': 0, 'seen_rev': 0}
        return it

    def user(self, iid, title=None, status=None, comment=None, notify=True):
        it = self.item(iid, create=True)
        status = check_status(status)
        comment = (comment or '').strip() if isinstance(comment, str) else ''
        title = str(title).strip()[:200] if title else ''
        changed = status is not None and status != it['status']
        if not changed and not comment:
            if title and title != it.get('title'):
                it['title'] = title
                self._next()
                self.save()
            return it
        rev = self._next()
        if title:
            it['title'] = title
        if changed:
            it['status'] = status
            it['thread'].append({'who': 'user', 'status': status, 'time': stamp(), 'rev': rev})
        if comment:
            it['thread'].append({'who': 'user', 'text': comment, 'time': stamp(), 'rev': rev})
        if comment or notify:
            it['user_rev'] = rev
        self.save()
        return it

    def agent(self, iid, reply=None, status=None, fields=None):
        """A reply and / or a status from the agent; either way the item no longer waits."""
        it = self.item(iid, create=True)
        status = check_status(status)
        if fields is not None and (not isinstance(fields, dict) or any(k in RESERVED for k in fields)):
            raise ValueError('fields: an object without ' + ', '.join(RESERVED))
        rev = self._next()
        it.update(fields or {})
        if status is not None and status != it['status']:
            it['status'] = status
            it['thread'].append({'who': 'agent', 'status': status, 'time': stamp(), 'rev': rev})
        reply = reply.strip() if isinstance(reply, str) else ''
        if reply:
            it['thread'].append({'who': 'agent', 'text': reply, 'time': stamp(), 'rev': rev})
        it['agent_rev'] = rev
        self.save()
        return it

    def ack(self, ids=None):
        done = [k for k, it in self.data['items'].items() if waiting(it) and (ids is None or k in ids)]
        if done:
            rev = self._next()
            for k in done:
                self.data['items'][k]['agent_rev'] = rev
            self.save()
        return sorted(done, key=natural)

    def seen(self, iid):
        it = self.item(iid)
        last = max([m.get('rev', 0) for m in it['thread'] if m.get('who') == 'agent' and m.get('text')] or [0])
        if last > it['seen_rev']:
            it['seen_rev'] = last
            self._next()
            self.save()
        return it

    def view(self):
        return {'page': self.page, 'file': self.path.replace('\\', '/'), 'rev': self.data['rev'],
                'items': self.data['items']}

    def waiting_items(self, undelivered=False):
        out = []
        for k in sorted(self.data['items'], key=natural):
            it = self.data['items'][k]
            delivered = self.taken.get(k, 0) >= it['user_rev']
            if not waiting(it) or undelivered and delivered:
                continue
            new = [m for m in it['thread'] if m.get('who') == 'user' and m.get('rev', 0) > it['agent_rev']]
            out.append({'page': self.page, 'id': k, 'title': it.get('title', ''), 'status': it['status'], 'new': new,
                        'delivered': delivered})
        return out


class Talk:
    """The stores under one root, the agent's presence, and the /_talk/api/ routes. Another server can host stores
    kept at its own paths (attach) and hand /_talk/api/ requests to get() / post()."""

    def __init__(self, root=None, port=PORT):
        self.root = os.path.realpath(root) if root else None
        self.port = port
        self.cond = threading.Condition()
        self.stores = {}
        self.listeners = 0                     # `wait` requests in progress
        self.last_listen = 0.0                 # end of the last one
        self.taken_at = 0.0                    # when a `wait` last took items (the agent is working on them)
        self.last_user_at = 0.0
        if self.root:
            self.scan()
        threading.Thread(target=self._watch_presence, daemon=True).start()

    def scan(self):
        for d, dirs, files in os.walk(self.root):
            dirs[:] = [x for x in dirs if not x.startswith('.') and x not in SKIP_DIRS]
            for f in files:
                page_file = os.path.join(d, f[:-len('.talk.json')] + '.html')
                if f.endswith('.talk.json') and os.path.isfile(page_file):
                    page = os.path.relpath(page_file, self.root).replace('\\', '/')
                    try:
                        self.stores[page] = Store(os.path.join(d, f), page)
                    except (OSError, ValueError) as e:
                        print('talkpage: skipped %s: %s' % (os.path.join(d, f), e), file=sys.stderr)

    def html_pages(self):
        out = []
        for d, dirs, files in os.walk(self.root) if self.root else ():
            dirs[:] = [x for x in dirs if not x.startswith('.') and x not in SKIP_DIRS]
            out += [os.path.relpath(os.path.join(d, f), self.root).replace('\\', '/') for f in files
                    if f.lower().endswith(('.html', '.htm'))]
        return sorted(out)

    def attach(self, page, path):
        """Keep the state of `page` (any key) in `path`, e.g. a review file of another tool."""
        with self.cond:
            s = self.stores[page] = Store(path, page)
            return s

    def page_file(self, page):
        """(page path, file) for a page path from a URL or the command line, or ValueError."""
        p = urllib.parse.unquote(str(page or '')).replace('\\', '/').lstrip('/')
        if p == '' or p.endswith('/'):
            p += 'index.html'
        parts = p.split('/')
        if (not self.root or any(s in ('', '.', '..') or s.startswith('.') for s in parts)
                or not p.lower().endswith(('.html', '.htm'))):
            raise ValueError('page: an .html path under the root: %s' % p)
        full = os.path.realpath(os.path.join(self.root, *parts))
        if not full.startswith(self.root + os.sep) or not os.path.isfile(full):
            raise ValueError('no such page: %s' % p)
        return p, full

    def store(self, page, loose=False):
        """The store of a page; loose also takes a unique part of the path of a page that has a state."""
        if page in self.stores:
            s = self.stores[page]
            s.fresh()
            return s
        try:
            p, full = self.page_file(page)
        except ValueError:
            if not loose or not page:
                raise
            hits = [k for k in self.stores if page in k] or [k for k in self.html_pages() if page in k]
            if len(hits) != 1:
                raise ValueError('page %r matches %s' % (page, ', '.join(hits[:20]) if hits else 'no page'))
            p = hits[0]
            if p not in self.stores:
                p, full = self.page_file(p)
        s = self.stores.get(p)
        if s is None:
            s = self.stores[p] = Store(os.path.splitext(full)[0] + '.talk.json', p)
        s.fresh()
        return s

    def presence(self):
        t = time.time()
        if self.listeners:
            return 'listening'
        if self.taken_at and self.taken_at >= self.last_listen - 0.5 and t - self.taken_at < 3600:
            return 'working'
        return 'listening' if t - self.last_listen < 2.5 else 'absent'

    def _watch_presence(self):
        """Wake the pages when the presence changes by time alone (a `wait` that did not come back)."""
        prev = None
        while True:
            time.sleep(1)
            with self.cond:
                cur = self.presence()
                if cur != prev:
                    self.cond.notify_all()
                    prev = cur

    # ---------------------------------------------------------------- API (bodies are serialised under the lock)
    def get(self, path, q):
        """GET /_talk/api/<name>: (code, JSON bytes) or None."""
        def one(k, d=None):
            return (q.get(k) or [d])[0]
        if path == '/_talk/api/info':
            with self.cond:
                return 200, dump({'tool': 'talkpage', 'root': self.root and self.root.replace('\\', '/'),
                                  'port': self.port, 'presence': self.presence(), 'pages': sorted(self.stores)})
        if path == '/_talk/api/state':
            rev, pres, wait = int(one('rev', -1)), one('presence'), min(float(one('wait', 0)), 55)
            with self.cond:
                s = self.store(one('page'), loose=True)
                if wait > 0:
                    self.cond.wait_for(lambda: s.data['rev'] != rev or pres is not None and self.presence() != pres,
                                       wait)
                return 200, dump(dict(s.view(), presence=self.presence()))
        if path == '/_talk/api/pending':
            return 200, self.pending(one('page'), min(float(one('wait', 0)), 55), one('listen') == '1',
                                     one('take') == '1')
        return None

    def pending(self, page, wait, listen, take):
        """Items waiting for the agent. listen: asked by `wait` (counts as the agent being there, returns only
        items not handed out yet); take: hand them out."""
        def pick():
            return [x for k, s in sorted(self.stores.items()) if not page or page in k
                    for x in s.waiting_items(undelivered=listen)]
        with self.cond:
            if listen:
                self.listeners += 1
                self.cond.notify_all()
            try:
                for s in self.stores.values():
                    s.fresh()
                items = pick()
                if not items and wait > 0:
                    self.cond.wait_for(pick, wait)
                    items = pick()
                if take and items:
                    for x in items:
                        s = self.stores[x['page']]
                        s.taken[x['id']] = s.data['items'][x['id']]['user_rev']
                    self.taken_at = time.time()
                return dump({'now': time.time(), 'last_user_at': self.last_user_at, 'presence': self.presence(),
                             'url': 'http://127.0.0.1:%d/' % self.port, 'items': items})
            finally:
                if listen:
                    self.listeners -= 1
                    self.last_listen = time.time()
                    self.cond.notify_all()

    def post(self, path, body):
        """POST /_talk/api/<name>: (code, JSON bytes) or None."""
        if not isinstance(body, dict):
            raise ValueError('a JSON object is expected')
        if path != '/_talk/api/ack' and not body.get('page'):
            raise ValueError('page is required')
        with self.cond:
            if path == '/_talk/api/user':
                s = self.store(body.get('page'))
                it = s.user(body.get('id'), body.get('title'), body.get('status'), body.get('comment'),
                            body.get('notify', True) is not False)
                if it['user_rev'] == s.data['rev']:
                    self.last_user_at = time.time()
            elif path == '/_talk/api/seen':
                s = self.store(body.get('page'))
                it = s.seen(body.get('id'))
            elif path == '/_talk/api/agent':
                s = self.store(body.get('page'), loose=True)
                it = s.agent(body.get('id'), body.get('reply'), body.get('status'), body.get('fields'))
            elif path == '/_talk/api/ack':
                ids = body.get('ids')
                if ids is not None and not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
                    raise ValueError('ids: a list of item ids')
                stores = [self.store(body['page'], loose=True)] if body.get('page') else list(self.stores.values())
                acked = {s.page: s.ack(ids) for s in stores}
                self.cond.notify_all()
                return 200, dump({'acked': {k: v for k, v in acked.items() if v}})
            else:
                return None
            self.cond.notify_all()
            return 200, dump(dict(it, id=body.get('id'), page=s.page, rev=s.data['rev']))

    def listing(self):
        with self.cond:
            rows = []
            for p, s in sorted(self.stores.items()):
                if not os.path.isfile(s.path):
                    continue
                n = len(s.waiting_items())
                href = '/' + urllib.parse.quote(p) if self.root and not os.path.isabs(p) else None
                name = '<a href="%s">%s</a>' % (href, html.escape(p)) if href else html.escape(p)
                rows.append('<li>%s%s</li>' % (name, ' — エージェント未読 %d' % n if n else ''))
            root = html.escape(self.root or '-')
        return ('<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>talkpage</title>%s'
                '<style>body{font:15px/1.7 system-ui,sans-serif;margin:24px 16px;max-width:860px}'
                '.m{color:#666;font-size:13px}</style></head><body><h1>talkpage</h1>'
                '<p class="m">root: %s ・ エージェント: %s</p><ul>%s</ul>'
                '<p class="m">状態のあるページだけを並べる。ほかのページは root からのパスで開く。</p></body></html>'
                % (FAVICON, root, self.presence(), ''.join(rows) or '<li>（まだ無い）</li>')).encode('utf-8')


def dump(obj):
    return json.dumps(obj, ensure_ascii=False).encode('utf-8')


# ---------------------------------------------------------------- HTTP server
class Handler(BaseHTTPRequestHandler):
    talk = None
    server_version = 'talkpage/1'

    def log_message(self, *_):
        pass

    def _send(self, code, body, ctype='application/json', headers=None):
        if not isinstance(body, bytes):
            body = dump(body)
        text = ctype.startswith('text/') or ctype.endswith(('json', 'javascript'))
        self.send_response(code)
        self.send_header('Content-Type', ctype + ('; charset=utf-8' if text else ''))
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _local(self):
        """Only this server's own pages: refuse another host name (DNS rebinding) and other sites' pages."""
        port = self.server.server_address[1]
        hosts = ('127.0.0.1:%d' % port, 'localhost:%d' % port)
        origin = (self.headers.get('Origin') or '').lower()
        return (self.headers.get('Host') or '').lower() in hosts and (not origin or origin in ['http://' + h for h in hosts])

    def _run(self, fn):
        try:
            if not self._local():
                return self._send(403, {'error': 'only pages of http://127.0.0.1:%d' % self.server.server_address[1]})
            fn()
        except (BrokenPipeError, ConnectionError):
            pass                               # the page went away during a long poll
        except ValueError as e:
            self._send(400, {'error': str(e)})
        except OSError as e:
            self._send(500, {'error': str(e)})

    def do_GET(self):
        self._run(self._get)

    def do_POST(self):
        self._run(self._post)

    def _get(self):
        path, _, query = self.path.partition('?')
        r = self.talk.get(path, urllib.parse.parse_qs(query))
        if r:
            return self._send(*r)
        if path in ('/_talk', '/_talk/'):
            return self._send(200, self.talk.listing(), 'text/html')
        if path.startswith('/_talk/'):
            name = path[len('/_talk/'):]
            if name not in CLIENT:
                return self._send(404, {'error': 'not found'})
            with open(os.path.join(HERE, name), 'rb') as f:
                return self._send(200, f.read(), TYPES[os.path.splitext(name)[1]])
        self._static(path)

    def _static(self, raw):
        root = self.talk.root
        parts = [s for s in urllib.parse.unquote(raw).split('/') if s]
        if not root or any(s in ('.', '..') or s.startswith('.') or '\\' in s for s in parts):
            return self._send(404, {'error': 'not found'})
        full = os.path.realpath(os.path.join(root, *parts))
        if full != root and not full.startswith(root + os.sep):
            return self._send(404, {'error': 'not found'})
        if os.path.isdir(full):
            if not raw.endswith('/'):
                return self._send(301, b'', 'text/plain', {'Location': raw + '/'})
            if not os.path.isfile(os.path.join(full, 'index.html')) and full == root:
                return self._send(200, self.talk.listing(), 'text/html')
            full = os.path.join(full, 'index.html')
        ctype = TYPES.get(os.path.splitext(full)[1].lower())
        if not ctype or not os.path.isfile(full):
            return self._send(404, {'error': 'not found'})
        with open(full, 'rb') as f:
            self._send(200, f.read(), ctype)

    def _post(self):
        if not (self.headers.get('Content-Type') or '').lower().startswith('application/json'):
            return self._send(415, {'error': 'Content-Type: application/json'})
        n = int(self.headers.get('Content-Length') or 0)
        if n > 1000000:
            raise ValueError('body too large')
        body = json.loads(self.rfile.read(n).decode('utf-8')) if n else {}
        r = self.talk.post(self.path.partition('?')[0], body)
        self._send(*r) if r else self._send(404, {'error': 'not found'})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != 'nt'     # on Windows SO_REUSEADDR would let two servers share the port

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


# ---------------------------------------------------------------- command line
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))     # never through a proxy


def call(a, path, body=None, timeout=15, **q):
    url = 'http://127.0.0.1:%d%s' % (a.port, path)
    q = {k: v for k, v in q.items() if v not in (None, '')}
    if q:
        url += '?' + urllib.parse.urlencode(q)
    data = dump(body) if body is not None else None
    req = urllib.request.Request(url, data, {'Content-Type': 'application/json'} if data else {})
    try:
        with _opener.open(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get('error')
        except ValueError:
            msg = str(e)
        raise SystemExit('talkpage: %s' % msg)


def cmd_serve(a):
    root = os.path.realpath(a.root)
    if not os.path.isdir(root):
        raise SystemExit('talkpage: not a folder: %s' % a.root)
    talk = Talk(root, a.port)
    Handler.talk = talk
    try:
        srv = Server(('127.0.0.1', a.port), Handler)
    except OSError as e:
        raise SystemExit('talkpage: cannot listen on 127.0.0.1:%d (%s). Another talkpage? '
                         'http://127.0.0.1:%d/_talk/api/info tells its root.' % (a.port, e, a.port))
    url = 'http://127.0.0.1:%d/' % a.port
    print('talkpage: %s at %s (%d pages with a state). Ctrl-C to stop.' % (root, url, len(talk.stores)), flush=True)
    if a.open:
        page = os.path.relpath(os.path.realpath(a.open), root) if os.path.isabs(a.open) else a.open
        p, _ = talk.page_file(page)
        webbrowser.open(url + urllib.parse.quote(p))
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def me(a, sub):
    port = '' if a.port == 8190 else ' --port %d' % a.port
    return 'python "%s" %s%s' % (os.path.abspath(__file__).replace('\\', '/'), sub, port)


def print_pending(a, d, rest):
    print('talkpage: the user acted on %d item(s)  %s' % (len(d['items']), d['url']))
    by = {}
    for x in d['items']:
        by.setdefault(x['page'], []).append(x)
    for page, xs in by.items():
        print('== page: %s' % page)
        for x in xs:
            print('- %s  [%s]  %s' % (x['id'], x['status'] or '-', x['title']))
            for m in x['new']:
                t = m.get('time', '')[11:]
                if 'text' in m:
                    lines = m['text'].splitlines() or ['']
                    print('    %s comment: %s' % (t, lines[0]))
                    for line in lines[1:]:
                        print('      ' + line)
                else:
                    print('    %s status: %s' % (t, m.get('status') or '(cleared)'))
    if rest:
        print('also waiting (printed before, not answered): ' + ', '.join('%s %s' % (x['page'], x['id']) for x in rest))
    print('reply: ' + me(a, 'reply') + ' --page "<page>" --id <id> [--status S] --text "..."'
          '  (long or non-ASCII text: --text - with the text on stdin)')
    print('read, no reply: ' + me(a, 'ack') + ' --page "<page>" [--id <id> ...]')
    print('then wait again in the background: ' + me(a, 'wait'))


def cmd_wait(a):
    while True:
        try:
            d = call(a, '/_talk/api/pending', timeout=70, page=a.page, wait=50, listen=1)
            if not d['items']:
                continue
            while d['now'] - d['last_user_at'] < a.settle:          # let a run of clicks finish first
                time.sleep(min(1.0, a.settle - (d['now'] - d['last_user_at'])) + 0.05)
                d = call(a, '/_talk/api/pending', page=a.page, listen=1)
            d = call(a, '/_talk/api/pending', page=a.page, listen=1, take=1)
            if not d['items']:
                continue
            shown = {(x['page'], x['id']) for x in d['items']}
            rest = [x for x in call(a, '/_talk/api/pending', page=a.page)['items'] if (x['page'], x['id']) not in shown]
        except (urllib.error.URLError, OSError):                     # the server is not up yet, or restarting
            time.sleep(2)
            continue
        print_pending(a, d, rest)
        return


def cmd_reply(a):
    text = a.text
    if a.file:
        with open(a.file, encoding='utf-8') as f:
            text = f.read()
    elif text == '-':
        text = sys.stdin.buffer.read().decode('utf-8')
    fields = None
    if a.fields and a.fields.startswith('@'):                       # @file: Japanese text without the shell's quoting
        with open(a.fields[1:], encoding='utf-8') as f:
            fields = json.load(f)
    elif a.fields:
        fields = json.loads(a.fields)
    body = {'page': a.page, 'id': a.id, 'reply': text, 'status': a.status, 'fields': fields}
    r = call(a, '/_talk/api/agent', body)
    print('talkpage: %s  %s  [%s]  %d message(s)' % (r['page'], r['id'], r['status'] or '-',
                                                    sum(1 for m in r['thread'] if m.get('text'))))


def cmd_ack(a):
    r = call(a, '/_talk/api/ack', {'page': a.page, 'ids': a.id or None})
    print('talkpage: read ' + ('; '.join('%s: %s' % (p, ', '.join(ids)) for p, ids in r['acked'].items()) or 'nothing'))


def cmd_show(a):
    pages = [a.page] if a.page else call(a, '/_talk/api/info')['pages']
    for p in pages:
        d = call(a, '/_talk/api/state', page=p)
        print('== page: %s  (%s)' % (d['page'], d['file']))
        for k in sorted(d['items'], key=natural):
            it = d['items'][k]
            print('- %s  [%s]%s  %s' % (k, it['status'] or '-', '  *waiting*' if waiting(it) else '',
                                        it.get('title', '')[:80]))
            msgs = [m for m in it['thread'] if m.get('text')]
            if msgs:
                m = msgs[-1]
                print('    last %s %s: %s' % (m['who'], m.get('time', '')[5:16], m['text'].splitlines()[0][:100]))


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--port', type=int, default=PORT)
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('serve', help='serve the pages under a folder')
    s.add_argument('root')
    s.add_argument('--open', help='open this page (path under the root, or the file) in the default browser')
    s = sub.add_parser('wait', help='block until the user acts, print it, exit')
    s.add_argument('--page', help='only this page (path or a unique part of it)')
    s.add_argument('--settle', type=float, default=4.0, help='seconds without new clicks before printing (default 4)')
    s = sub.add_parser('reply', help='answer an item')
    s.add_argument('--page', required=True)
    s.add_argument('--id', required=True)
    s.add_argument('--status', help='set the status ("" clears it)')
    s.add_argument('--text', help='the reply; "-" reads it from stdin (UTF-8)')
    s.add_argument('--file', help='read the reply from this UTF-8 file')
    s.add_argument('--fields', help='JSON object of extra item fields to store, or @file with that JSON (UTF-8)')
    s = sub.add_parser('ack', help='mark what waits as read without replying')
    s.add_argument('--page')
    s.add_argument('--id', action='append', help='only these items (repeat)')
    s = sub.add_parser('show', help='all items of a page')
    s.add_argument('--page')
    for sp in sub.choices.values():                                 # --port also after the subcommand
        sp.add_argument('--port', type=int, default=argparse.SUPPRESS)
    a = p.parse_args()
    {'serve': cmd_serve, 'wait': cmd_wait, 'reply': cmd_reply, 'ack': cmd_ack, 'show': cmd_show}[a.cmd](a)


if __name__ == '__main__':
    main()
