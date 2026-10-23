"""Voice-guided probing for agent measurement scripts, so the person holding the probes never waits blind.

    import guide
    guide.probe('CH2 を D6 の 4 番に当ててください', lambda ch: ch[1]['max'] > 1.5)   # says it, waits, says 計測中
    ... measure (keep it short: the person's hands are busy until 計測終わり) ...
    guide.done()                                                                     # says 計測終わり

The prompts go through adscope (POST /api/say); its page shows them and reads them aloud with 読み上げ ON.
`ready` gets {channel idx: live measurement (now / mean / min / max / pp / rms)} from GET /api/measure and returns
True once the probes are on the right points; it must hold for `stable_s` before 計測中.
"""
import json
import time
import urllib.request

BASE = 'http://127.0.0.1:8195'


def _req(path, body=None, timeout=5):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={'Content-Type': 'application/json'},
                                 method='GET' if body is None else 'POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def say(text):
    """Show and speak one prompt on the adscope page."""
    return _req('/api/say', {'text': text})


def channels():
    """{idx: measurement dict} of the live view right now (channels without a reading are left out)."""
    m = _req('/api/measure')
    return {c['idx']: c for c in m.get('channels', []) if c.get('enabled') and c.get('max') is not None}


def probe(prompt, ready, stable_s=0.5, repeat_s=30.0, timeout_s=900.0, start='計測中'):
    """Ask for the probes, wait until ready(channels) has held for stable_s, then announce `start`.
    The prompt is repeated every repeat_s while waiting. Returns the channels at that moment; raises TimeoutError."""
    say(prompt)
    t0 = last_say = time.time()
    since = None
    while time.time() - t0 < timeout_s:
        try:
            ch = channels()
            ok = bool(ready(ch))
        except (KeyError, TypeError, OSError, ValueError):
            ch, ok = {}, False
        now = time.time()
        if ok:
            since = since or now
            if now - since >= stable_s:
                say(start)
                return ch
        else:
            since = None
            if now - last_say >= repeat_s:
                say(prompt)
                last_say = now
        time.sleep(0.1)
    say('時間切れ。プローブを外してください')
    raise TimeoutError(prompt)


def done(text='計測終わり'):
    say(text)
