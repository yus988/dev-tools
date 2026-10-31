"""Tests of adscope's HTTP guards (which requests are refused), with a stand-in for the scope: no device is opened.
python -m unittest test_adscope   (needs the WaveForms SDK installed, as adscope imports it)"""
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

try:
    import adscope
except OSError as e:                    # the WaveForms SDK (dwf) is not installed
    raise unittest.SkipTest('WaveForms SDK: %s' % e)

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class StubScope:
    def status(self):
        return {'stub': True}

    def run(self):
        pass

    def stop(self):
        pass


class GuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        adscope.set_data(cls.dir)
        adscope.Handler.scope = StubScope()
        cls.srv = adscope.ThreadingHTTPServer(('127.0.0.1', 0), adscope.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.dir)

    def req(self, path, body=None, headers=None, info=False):
        """(code, body) of a request sent like guide.py does (our Origin); headers: None removes one."""
        h = {'Origin': 'http://127.0.0.1:%d' % self.port}
        if body is not None:
            h['Content-Type'] = 'application/json'
        h.update(headers or {})
        data = (body if isinstance(body, bytes) else json.dumps(body).encode()) if body is not None else None
        r = urllib.request.Request('http://127.0.0.1:%d%s' % (self.port, path), data,
                                   {k: v for k, v in h.items() if v is not None})
        try:
            with OPENER.open(r, timeout=10) as resp:
                data = resp.read()
                out = resp.status, json.loads(data) if 'json' in resp.headers['Content-Type'] else data
                return out + (resp.headers,) if info else out
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')

    def test_writes(self):
        port = self.port
        self.assertEqual(self.req('/api/run', {})[0], 200)
        self.assertEqual(self.req('/api/run', {}, {'Origin': 'http://localhost:%d' % port})[0], 200)
        self.assertEqual(self.req('/api/say', {'text': 'x'})[1]['text'], 'x')
        self.assertEqual(self.req('/api/run', {}, {'Origin': None})[0], 403)                 # curl without Origin
        self.assertEqual(self.req('/api/run', {}, {'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.req('/api/run', {}, {'Origin': 'null'})[0], 403)
        self.assertEqual(self.req('/api/run', {}, {'Origin': None, 'Referer': 'https://evil.example/'})[0], 403)
        self.assertEqual(self.req('/api/run', {}, {'Origin': None, 'Referer': 'http://127.0.0.1:%d/' % port})[0], 200)
        self.assertEqual(self.req('/api/run', {}, {'Host': 'evil.example:%d' % port,
                                                   'Origin': 'http://evil.example:%d' % port})[0], 403)
        # another site's form (text/plain) is refused by Origin; with our Origin the body must still be JSON
        form = b'{"text": "=x"}'
        self.assertEqual(self.req('/api/say', form, {'Origin': 'https://evil.example', 'Content-Type': 'text/plain'})[0],
                         403)
        self.assertEqual(self.req('/api/say', form, {'Content-Type': 'text/plain'})[0], 415)

    def test_reads(self):
        port = self.port
        self.assertEqual(self.req('/api/status', headers={'Origin': None})[1], {'stub': True})   # curl may read
        self.assertEqual(self.req('/api/status', headers={'Host': 'evil.example:%d' % port})[0], 403)
        cross = {'Origin': None, 'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'no-cors', 'Sec-Fetch-Dest': 'image'}
        self.assertEqual(self.req('/api/status', headers=cross)[0], 403)
        nav = dict(cross, **{'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'})
        code, _, head = self.req('/', headers=nav, info=True)                  # a link from another site opens it
        self.assertEqual((code, head['X-Frame-Options']), (200, 'SAMEORIGIN'))
        self.assertEqual(self.req('/', headers=dict(nav, **{'Sec-Fetch-Dest': 'iframe'}))[0], 403)
        self.assertEqual(self.req('/api/status', headers=nav)[0], 403)


if __name__ == '__main__':
    unittest.main()
