"""Review page for a KiCad PCB: review items on the board, each with a comment thread between user and agent.

  python kireview.py <board.kicad_pcb> [--data DIR] [--port 8199] [--no-open] [--kicad-bin DIR]

<data> (default: the current folder) holds reviews/<stem>.json and cache/. The work is done in kireview_board.py;
this file only serves the page and its API.
"""
import argparse
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kireview_board import KICAD_BIN, Api, Board, Review

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html')


class Handler(BaseHTTPRequestHandler):
    api = None
    server_version = 'kireview/1'

    def log_message(self, *_):
        pass

    def _send(self, code, obj, ctype='application/json', cache=False):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype + ('; charset=utf-8' if 'json' in ctype or 'html' in ctype else ''))
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'max-age=86400' if cache else 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _route(self, method):
        path, _, query = self.path.partition('?')
        try:
            if method == 'GET' and path in ('/', '/index.html'):
                with open(PAGE, 'rb') as f:
                    self._send(200, f.read(), 'text/html')
                return
            if method == 'POST':
                n = int(self.headers.get('Content-Length') or 0)
                r = self.api.post(path, json.loads(self.rfile.read(n).decode('utf-8')) if n else {})
            elif method == 'DELETE':
                r = self.api.delete(path, query)
            else:
                r = self.api.get(path, query)
            if r:
                self._send(*r)
            else:
                self._send(404, {'error': 'not found'})
        except ValueError as e:
            self._send(400, {'error': str(e)})
        except (RuntimeError, OSError) as e:
            self._send(500, {'error': str(e)})

    def do_GET(self):
        self._route('GET')

    def do_POST(self):
        self._route('POST')

    def do_DELETE(self):
        self._route('DELETE')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('board', help='the .kicad_pcb to review')
    p.add_argument('--data', default='.', help='folder for reviews/ and cache/ (default: current folder)')
    p.add_argument('--port', type=int, default=8199)
    p.add_argument('--no-open', action='store_true', help='do not open the page in the browser')
    p.add_argument('--kicad-bin', default=KICAD_BIN, help='KiCad bin folder (kicad-cli, python)')
    args = p.parse_args()
    if not os.path.isfile(args.board) or not args.board.endswith('.kicad_pcb'):
        p.error('not a .kicad_pcb file: %s' % args.board)
    data = os.path.abspath(args.data)
    board = Board(args.board, data, args.kicad_bin)
    print('kireview: exporting ...', flush=True)
    board.load()
    review = Review(os.path.join(data, 'reviews', board.stem + '.json'), board.path.replace('\\', '/'))
    threading.Thread(target=board.watch, daemon=True).start()
    Handler.api = Api(board, review)
    print('kireview: %d layers, %d pads, review %s' % (len(board.meta['files']), len(board.dump['pads']), review.path),
          flush=True)
    srv = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    srv.daemon_threads = True
    url = 'http://127.0.0.1:%d/' % args.port
    print('kireview:', url, '(Ctrl-C to stop)', flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == '__main__':
    main()
