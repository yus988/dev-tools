"""Tests of serve.py's HTTP guards (which requests are refused) and the campaign PUT. python -m unittest test_serve"""
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import serve

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
CAMPAIGN = {"schema": serve.SCHEMA, "id": "c1", "title": "t", "items": []}


class GuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        serve.Handler.root = Path(cls.dir)
        cls.srv = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.dir)

    def req(self, path, body=None, headers=None, info=False):
        """(code, body) of a request sent like the app does (our Origin); body means PUT; headers: None removes one."""
        h = {"Origin": f"http://127.0.0.1:{self.port}"}
        if body is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                   json.dumps(body).encode() if body is not None else None,
                                   {k: v for k, v in h.items() if v is not None}, method="PUT" if body else "GET")
        try:
            with OPENER.open(r, timeout=10) as resp:
                data = resp.read()
                out = resp.status, json.loads(data) if "json" in resp.headers["Content-Type"] else data
                return out + (resp.headers,) if info else out
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_put(self):
        port = self.port
        self.assertEqual(self.req("/api/campaigns/c1", CAMPAIGN)[0], 200)
        self.assertEqual(self.req("/api/campaigns/c1", CAMPAIGN, {"Origin": f"http://localhost:{port}"})[0], 200)
        self.assertEqual(self.req("/api/campaigns/c1", CAMPAIGN, {"Origin": None})[0], 403)          # curl, no Origin
        self.assertEqual(self.req("/api/campaigns/c1", CAMPAIGN, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.req("/api/campaigns/c1", CAMPAIGN, {"Origin": "null"})[0], 403)
        self.assertEqual(self.req("/api/campaigns/c1", CAMPAIGN, {"Host": f"evil.example:{port}",          # rebinding
                                                                  "Origin": f"http://evil.example:{port}"})[0], 403)
        self.assertEqual(self.req("/api/campaigns/c1")[1]["title"], "t")

    def test_reads(self):
        port = self.port
        self.assertEqual(self.req("/api/campaigns", headers={"Origin": None})[0], 200)            # curl may read
        self.assertEqual(self.req("/api/campaigns", headers={"Host": f"evil.example:{port}"})[0], 403)
        cross = {"Origin": None, "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
        self.assertEqual(self.req("/api/campaigns", headers=cross)[0], 403)
        nav = dict(cross, **{"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"})
        code, _, head = self.req("/", headers=nav, info=True)                  # a link from another site opens it
        self.assertEqual((code, head["X-Frame-Options"]), (200, "SAMEORIGIN"))
        self.assertEqual(self.req("/", headers=dict(nav, **{"Sec-Fetch-Dest": "iframe"}))[0], 403)
        self.assertEqual(self.req("/%61pi/campaigns", headers=nav)[0], 403)   # the API by an escaped path

    def test_specific_host(self):
        """--host <address>: that name is answered too (main adds it)."""
        names = serve.Handler.names
        serve.Handler.names = names + ("192.0.2.7",)
        try:
            self.assertEqual(self.req("/api/campaigns", headers={"Host": f"192.0.2.7:{self.port}",
                                                                 "Origin": f"http://192.0.2.7:{self.port}"})[0], 200)
        finally:
            serve.Handler.names = names


if __name__ == "__main__":
    unittest.main()
