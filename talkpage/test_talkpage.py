"""Tests of talkpage.py: the store, the HTTP routes and the wait / reply commands. python -m unittest test_talkpage"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import talkpage as tp

HERE = os.path.dirname(os.path.abspath(__file__))
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.s = tp.Store(os.path.join(self.dir, 'p.talk.json'), 'p.html')

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_status_comment_reply(self):
        it = self.s.user('C2', 'C2 title', status='NG')
        self.assertTrue(tp.waiting(it))
        self.assertEqual(self.s.waiting_items()[0]['new'][0]['status'], 'NG')
        it = self.s.user('C2', comment='  still on  ')
        self.assertEqual(it['thread'][-1]['text'], 'still on')
        self.assertEqual(len(self.s.waiting_items()[0]['new']), 2)
        it = self.s.agent('C2', reply='looking')
        self.assertFalse(tp.waiting(it))
        self.assertEqual(self.s.waiting_items(), [])
        again = tp.Store(self.s.path, 'p.html')                     # saved beside the page
        self.assertEqual(again.data['items']['C2']['thread'][-1]['text'], 'looking')

    def test_quiet_status_and_ack(self):
        it = self.s.user('B1', status='OK', notify=False)
        self.assertFalse(tp.waiting(it))
        self.s.user('B2', status='OK')
        self.s.user('B3', status='OK')
        self.assertEqual(self.s.ack(['B2']), ['B2'])
        self.assertEqual(self.s.ack(), ['B3'])
        self.assertEqual(self.s.ack(), [])

    def test_seen_and_fields(self):
        self.s.agent('A1', reply='result', status='OK', fields={'note': 'x'})
        it = self.s.data['items']['A1']
        self.assertEqual((it['status'], it['note'], it['seen_rev']), ('OK', 'x', 0))
        self.assertEqual(self.s.seen('A1')['seen_rev'], it['thread'][-1]['rev'])
        with self.assertRaises(ValueError):
            self.s.agent('A1', fields={'thread': []})
        with self.assertRaises(ValueError):
            self.s.user('bad\nid', status='OK')
        with self.assertRaises(ValueError):
            self.s.user('', status='OK')
        self.assertEqual(self.s.user('net:/Power In/Net-(U6-EN)', status='OK')['status'], 'OK')

    def test_deleted_file_starts_over(self):
        self.s.user('C1', status='NG')
        rev = self.s.data['rev']
        os.remove(self.s.path)
        self.s.fresh()
        self.assertEqual((self.s.data['items'], self.s.data['rev']), ({}, rev + 1))
        self.s.user('C2', status='OK')
        self.assertEqual(list(tp.Store(self.s.path, 'p.html').data['items']), ['C2'])

    def test_upgrade_old_file(self):
        with open(self.s.path, 'w', encoding='utf-8') as f:
            json.dump({'items': {'field:C21': {'status': '質問', 'thread': [
                {'who': 'user', 'text': 'why?', 'time': 't'}]}, 'comp:U6': {'status': 'OK', 'thread': [
                    {'who': 'user', 'text': 'q', 'time': 't'}, {'who': 'agent', 'text': 'a', 'time': 't'}]}},
                       'groups': [1]}, f)
        s = tp.Store(self.s.path, 'old')
        self.assertEqual([x['id'] for x in s.waiting_items()], ['field:C21'])
        self.assertEqual(s.waiting_items()[0]['new'][0]['text'], 'why?')
        s.agent('field:C21', reply='because')
        self.assertEqual(s.waiting_items(), [])
        self.assertEqual(s.data['groups'], [1])                        # the owner's keys are kept


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(cls.root, 'notes', 'r1'))
        with open(os.path.join(cls.root, 'notes', 'r1', 'index.html'), 'w', encoding='utf-8') as f:
            f.write('<!doctype html><title>t</title><div data-talk="A1">a</div>')
        os.makedirs(os.path.join(cls.root, 'notes', 'other'))
        with open(os.path.join(cls.root, 'notes', 'other', 'index.html'), 'w', encoding='utf-8') as f:
            f.write('<!doctype html><title>o</title>')
        with open(os.path.join(cls.root, 'secret.txt'), 'w') as f:
            f.write('no')
        cls.talk = tp.Talk(cls.root, 0)
        tp.Handler.talk = cls.talk
        cls.srv = tp.Server(('127.0.0.1', 0), tp.Handler)
        cls.port = cls.talk.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.root)

    def req(self, path, body=None, headers=None, timeout=10, info=False):
        """(code, body) of a request sent like the page does (our Origin); headers: None removes one."""
        h = {'Origin': 'http://127.0.0.1:%d' % self.port}
        if body is not None:
            h['Content-Type'] = 'application/json'
        h.update(headers or {})
        r = urllib.request.Request('http://127.0.0.1:%d%s' % (self.port, path),
                                   json.dumps(body).encode() if body is not None else None,
                                   {k: v for k, v in h.items() if v is not None})
        try:
            with OPENER.open(r, timeout=timeout) as resp:
                data = resp.read()
                out = resp.status, json.loads(data) if 'json' in resp.headers['Content-Type'] else data
                return out + (resp.headers,) if info else out
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')

    def cli(self, *args, **kw):
        return subprocess.run([sys.executable, os.path.join(HERE, 'talkpage.py'), '--port', str(self.port)] + list(args),
                              capture_output=True, text=True, encoding='utf-8', timeout=30, **kw)

    def test_1_static_and_client(self):
        code, body = self.req('/notes/r1/')
        self.assertEqual(code, 200)
        self.assertIn(b'data-talk', body)
        self.assertEqual(self.req('/_talk/talkpage.js')[0], 200)
        self.assertEqual(self.req('/secret.txt')[0], 200)
        self.assertEqual(self.req('/notes/r1/../../secret.txt')[0], 404)
        self.assertEqual(self.req('/%2e%2e/x.html')[0], 404)
        self.assertEqual(self.req('/.git/config')[0], 404)
        self.assertEqual(self.req('/_talk/talkpage.py')[0], 404)

    def test_2_guards(self):
        self.assertEqual(self.req('/_talk/api/info', headers={'Host': 'evil.example:%d' % self.port})[0], 403)
        self.assertEqual(self.req('/_talk/api/user', {'page': 'notes/r1/index.html', 'id': 'A1', 'status': 'OK'},
                                  headers={'Origin': 'https://evil.example'})[0], 403)
        r = urllib.request.Request('http://127.0.0.1:%d/_talk/api/user' % self.port, b'{}', {
            'Content-Type': 'text/plain', 'Origin': 'http://127.0.0.1:%d' % self.port})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            OPENER.open(r, timeout=5)
        self.assertEqual(cm.exception.code, 415)
        self.assertEqual(self.req('/_talk/api/user', {'page': '../x.html', 'id': 'A1', 'status': 'OK'})[0], 400)
        self.assertEqual(self.req('/_talk/api/user', {'page': 'notes/r1/none.html', 'id': 'A1', 'status': 'OK'})[0], 400)

    def test_2a_origins(self):
        port = self.port
        no_origin = {'Origin': None}
        user = {'page': 'notes/r1/index.html', 'id': 'A1', 'status': 'OK'}
        # writes: our Origin (either name) only; none (curl), "null" and another site's are refused
        self.assertEqual(self.req('/_talk/api/user', user, no_origin)[0], 403)
        self.assertEqual(self.req('/_talk/api/user', user, {'Origin': 'null'})[0], 403)
        self.assertEqual(self.req('/_talk/api/ack', {'page': 'other'}, {'Origin': 'http://localhost:%d' % port})[0], 200)
        self.assertEqual(self.req('/_talk/api/ack', {'page': 'other'}, {'Origin': 'http://127.0.0.1:1'})[0], 403)
        # the agent's wait takes items: not by another site's <img>, also without Sec-Fetch-Site and Referer
        cross = {'Origin': None, 'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'no-cors', 'Sec-Fetch-Dest': 'image'}
        self.assertEqual(self.req('/_talk/api/pending?take=1&listen=1', headers=cross)[0], 403)
        self.assertEqual(self.req('/_talk/api/pending?take=1', headers=no_origin)[0], 403)
        self.assertEqual(self.req('/_talk/api/pending?take=1', headers={'Origin': None,
                                                                        'Sec-Fetch-Site': 'same-origin'})[0], 200)
        self.assertEqual(self.req('/_talk/api/pending?take=1', headers={
            'Origin': None, 'Referer': 'http://127.0.0.1:%d/notes/r1/' % port})[0], 200)
        # reads: a client without a browser may read; another site's page may not, nor load our files
        self.assertEqual(self.req('/_talk/api/info', headers=no_origin)[0], 200)
        self.assertEqual(self.req('/_talk/api/info', headers={'Origin': None, 'Referer': 'https://evil.example/'})[0], 403)
        self.assertEqual(self.req('/_talk/api/info', headers=dict(cross, **{'Sec-Fetch-Mode': 'cors'}))[0], 403)
        self.assertEqual(self.req('/secret.txt', headers=dict(cross, **{'Sec-Fetch-Dest': 'script'}))[0], 403)
        # another site may open a page by a link (top level), not frame it
        nav = dict(cross, **{'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'})
        code, _, head = self.req('/notes/r1/', headers=nav, info=True)
        self.assertEqual((code, head['X-Frame-Options']), (200, 'SAMEORIGIN'))
        self.assertEqual(self.req('/notes/r1/', headers=dict(nav, **{'Sec-Fetch-Dest': 'iframe'}))[0], 403)
        self.assertEqual(self.req('/_talk/api/pending?take=1', headers=nav)[0], 403)

    def test_2b_agent_writes_first(self):
        r = self.cli('reply', '--page', 'other', '--id', 'X1', '--status', 'OK')     # a page with no state yet
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('notes/other/index.html  X1  [OK]', r.stdout)
        self.assertIn("page 'zzz' matches no page", self.cli('reply', '--page', 'zzz', '--id', 'X1').stderr)

    def test_3_flow_with_wait(self):
        page = 'notes/r1/index.html'
        waiter = subprocess.Popen([sys.executable, os.path.join(HERE, 'talkpage.py'), '--port', str(self.port), 'wait',
                                   '--settle', '0.5'], stdout=subprocess.PIPE, text=True, encoding='utf-8')
        time.sleep(0.8)
        self.assertEqual(self.req('/_talk/api/state?page=' + page)[1]['presence'], 'listening')
        code, it = self.req('/_talk/api/user', {'page': page, 'id': 'A1', 'title': 'A1 a', 'status': 'NG',
                                                'comment': '鳴らない\n2 行目'}, headers={'Origin': 'http://127.0.0.1:%d' % self.port})
        self.assertEqual(code, 200)
        out = waiter.communicate(timeout=15)[0]
        self.assertEqual(waiter.returncode, 0)
        self.assertIn('- A1  [NG]  A1 a', out)
        self.assertIn('comment: 鳴らない', out)
        self.assertIn('      2 行目', out)
        self.assertIn('status: NG', out)
        self.assertEqual(self.req('/_talk/api/info')[1]['presence'], 'working')
        self.assertTrue(os.path.isfile(os.path.join(self.root, 'notes', 'r1', 'index.talk.json')))

        # a state request with the current rev waits until something changes (the agent's reply)
        rev = self.req('/_talk/api/state?page=' + page)[1]['rev']
        got = {}
        t = threading.Thread(target=lambda: got.update(r=self.req('/_talk/api/state?page=%s&rev=%d&wait=10' % (page, rev))))
        t.start()
        time.sleep(0.3)
        r = self.cli('reply', '--page', 'r1', '--id', 'A1', '--text', '-', input='見ます。`probe.cmd` で確認')
        self.assertEqual(r.returncode, 0, r.stderr)
        t.join(5)
        it = got['r'][1]['items']['A1']
        self.assertEqual(it['thread'][-1], dict(it['thread'][-1], who='agent', text='見ます。`probe.cmd` で確認'))
        self.assertFalse(tp.waiting(it))

        # what was handed to a wait is not handed again; a new comment is
        self.req('/_talk/api/user', {'page': page, 'id': 'A2', 'status': 'OK'})
        w1 = self.cli('wait', '--settle', '0.2')
        self.assertIn('- A2  [OK]', w1.stdout)
        self.req('/_talk/api/user', {'page': page, 'id': 'A1', 'comment': 'again'})
        w2 = self.cli('wait', '--settle', '0.2')
        self.assertIn('comment: again', w2.stdout)
        self.assertIn('also waiting (printed before, not answered): notes/r1/index.html A2', w2.stdout)
        a = self.cli('ack', '--page', 'r1')
        self.assertIn('A1, A2', a.stdout)
        self.assertEqual(self.req('/_talk/api/pending')[1]['items'], [])
        self.assertIn('[NG]', self.cli('show', '--page', 'r1').stdout)


if __name__ == '__main__':
    unittest.main()
