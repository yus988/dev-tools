"""Wait for review items that need the agent on a running kireview page, print them, then exit.

An agent runs this in the background (Claude Code: Bash with run_in_background) after starting kireview; the
exit wakes the agent, which answers with POST /api/review/agent and starts the wait again. Items already pending
at start are printed at once (exit right away), so nothing written while no one was waiting is missed.

    python kireview_wait.py [--port 8199] [--interval 3]
"""
import argparse, json, sys, time, urllib.request

ap = argparse.ArgumentParser()
ap.add_argument('--port', type=int, default=8199)
ap.add_argument('--interval', type=float, default=3.0)
a = ap.parse_args()
url = 'http://127.0.0.1:%d/api/review/pending' % a.port
sys.stdout.reconfigure(encoding='utf-8')

while True:
    try:
        d = json.load(urllib.request.urlopen(url, timeout=10))
    except Exception:            # server restarting: keep waiting
        time.sleep(a.interval)
        continue
    items = d.get('items') or []
    if items:
        print('kireview %s: %d pending' % (d.get('board'), len(items)))
        for it in items:
            print('- id: %s  [%s / %s / %s] %s' % (it.get('id'), it.get('status'), it.get('severity'), it.get('owner'),
                                                  it.get('title') or ''))
            where = '%s (%.2f, %.2f)' % (','.join(it.get('sides') or []), it.get('x', 0), it.get('y', 0))
            print('  at %s  refs %s%s' % (where, ', '.join(it.get('refs') or []) or '-',
                                          ('  near ' + it['near']) if it.get('near') else ''))
            th = it.get('thread') or []
            last = max([i for i, m in enumerate(th) if m.get('who') == 'agent'] or [-1])
            for m in th[last + 1:]:
                print('  user (%s): %s' % (m.get('time'), m.get('text')))
        sys.exit(0)
    time.sleep(a.interval)
