"""Scope-style board inspection app for the Analog Discovery Studio.

Owns the device and serves a scope page plus a JSON API on 127.0.0.1, so the
person and the agent look at the same data. adbench.py cannot open the device
while this runs. Input only: the analog outputs (W1/W2) are never driven.

  python adscope.py [--data <folder>] [--port 8195] [--no-open]

<folder> (default: $AD_BENCH_DATA or the current directory) holds boards/,
procedures/ and runs/; nothing in this script is board- or project-specific.

API (see README.md): GET /api/status /api/frame /api/measure,
POST /api/config /api/run /api/stop /api/single /api/save /api/record
/api/connect /api/quit, voice marks: POST /api/mark, GET /api/marks?since=<id>, spoken prompts: POST /api/say,
and guided probing: GET /api/procedures[/<name>], GET|POST /api/procedure/state,
GET /boards/<board>/<file>.
"""
import argparse
import copy
import json
import math
import os
import queue
import re
import threading
import time
import webbrowser
from urllib.parse import unquote
from ctypes import byref, c_byte, c_double, c_int, create_string_buffer
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import adbench
from adbench import DATA_ENV, HERE, data_root, dwf, last_error, moving_avg_max, save_json, stats
from dwfconstants import (  # noqa: E402  (adbench puts the SDK folder on sys.path)
    DwfStateArmed, DwfStateConfig, DwfStateDone, DwfStatePrefill, DwfTriggerSlopeFall,
    DwfTriggerSlopeRise, acqmodeScanShift, acqmodeSingle, trigsrcDetectorAnalogIn, trigsrcNone,
    trigtypeEdge,
)

PAGE = os.path.join(HERE, 'adscope', 'index.html')
MODES = ('auto', 'normal', 'single', 'roll')
MAX_POINTS = 2000              # per channel per frame (min/max pairs)
AUTO_TIMEOUT = 0.25            # s without a trigger before Auto free-runs
SETTLE_S = 2.0                 # SDK: input offset needs ~2 s after open / range or offset change
MIN_FRAME_S = 0.03             # do not publish triggered frames faster than this
RECORD_MAX_SAMPLES = 5_000_000  # per channel
NO_DEVICE = 'Analog Discovery が見つかりません'


def set_data(root):
    """Point runs/, procedures/ and boards/ at the data folder `root`."""
    global DATA, RUNS_DIR, MARKS_LOG, PROCS_DIR, BOARDS_DIR
    DATA = root
    RUNS_DIR = os.path.join(root, 'runs')
    MARKS_LOG = os.path.join(RUNS_DIR, 'marks.jsonl')
    PROCS_DIR = os.path.join(root, 'procedures')
    BOARDS_DIR = os.path.join(root, 'boards')


set_data(data_root())

DEFAULTS = {
    'mode': 'auto',
    'tdiv': 1e-3,              # s/div, 10 div on screen (triggered modes)
    'roll_s': 10.0,            # Roll window (s)
    'channels': [
        # offset = volts at the screen centre (SDK channel offset); range = 8 div
        {'enabled': True, 'name': 'CH1', 'vdiv': 1.0, 'offset': 2.5, 'shunt_ohm': 0.0},
        {'enabled': True, 'name': 'CH2', 'vdiv': 1.0, 'offset': 2.5, 'shunt_ohm': 0.0},
    ],
    'trigger': {'source': 0, 'edge': 'rise', 'level': 2.0, 'pre': 50.0},
}


# ---------------------------------------------------------------- settings
def _num(v, lo, hi, key):
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ValueError('%s must be a number' % key)
    if not (lo <= v <= hi) or math.isnan(v):
        raise ValueError('%s must be within %g..%g' % (key, lo, hi))
    return v


def merge_settings(cur, part):
    """Validate a partial config and return the merged copy (ValueError on bad input)."""
    s = copy.deepcopy(cur)
    if not isinstance(part, dict):
        raise ValueError('config must be a JSON object')
    if 'mode' in part:
        if part['mode'] not in MODES:
            raise ValueError('mode must be one of ' + ', '.join(MODES))
        s['mode'] = part['mode']
    if 'tdiv' in part:
        s['tdiv'] = _num(part['tdiv'], 1e-6, 1.0, 'tdiv')
    if 'roll_s' in part:
        s['roll_s'] = _num(part['roll_s'], 1.0, 30.0, 'roll_s')
    chans = part.get('channels', [])
    if isinstance(chans, dict):          # {"0": {...}} or {"1": {...}}
        chans = [chans.get(str(i), chans.get(i, {})) for i in range(2)]
    for i, c in enumerate(chans[:2]):
        if not isinstance(c, dict):
            continue
        d = s['channels'][i]
        if 'enabled' in c:
            d['enabled'] = bool(c['enabled'])
        if 'name' in c:
            d['name'] = str(c['name'])[:16] or 'CH%d' % (i + 1)
        if 'vdiv' in c:
            d['vdiv'] = _num(c['vdiv'], 0.001, 10.0, 'vdiv')
        if 'offset' in c:
            d['offset'] = _num(c['offset'], -25.0, 25.0, 'offset')
        if 'shunt_ohm' in c:
            d['shunt_ohm'] = _num(c['shunt_ohm'], 0.0, 1e6, 'shunt_ohm')
    t = part.get('trigger', {})
    if isinstance(t, dict):
        d = s['trigger']
        if 'source' in t:
            if int(t['source']) not in (0, 1):
                raise ValueError('trigger.source must be 0 (CH1) or 1 (CH2)')
            d['source'] = int(t['source'])
        if 'edge' in t:
            if t['edge'] not in ('rise', 'fall'):
                raise ValueError('trigger.edge must be rise or fall')
            d['edge'] = t['edge']
        if 'level' in t:
            d['level'] = _num(t['level'], -25.0, 25.0, 'trigger.level')
        if 'pre' in t:
            d['pre'] = _num(t['pre'], 0.0, 100.0, 'trigger.pre')
    return s


# ---------------------------------------------------------------- data helpers
def decimate(xs, max_points=MAX_POINTS):
    """Min/max decimation so short spikes stay visible; keeps time order in each pair."""
    n = len(xs)
    if n <= max_points:
        return list(xs)
    buckets = max_points // 2
    out = []
    for b in range(buckets):
        seg = xs[b * n // buckets:(b + 1) * n // buckets]
        lo, hi = min(seg), max(seg)
        out += [lo, hi] if seg.index(lo) <= seg.index(hi) else [hi, lo]
    return out


def measure(xs, k, hw_range, hw_offset):
    """mean / min / max / p-p / RMS in display units (k = 1/shunt or 1)."""
    if not xs:
        return None
    n = len(xs)
    mn, mx = min(xs), max(xs)
    mean = sum(xs) / n
    rms = math.sqrt(sum(x * x for x in xs) / n)
    edge = hw_range / 2 * 0.995
    clipped = mx >= hw_offset + edge or mn <= hw_offset - edge
    # "now" = the newest 0.5 % of the record (Roll 10 s -> last 50 ms): the value
    # at the right edge of the screen, which is what a probe reads right now.
    tail = xs[-max(1, n // 200):]
    return {'mean': mean * k, 'min': mn * k, 'max': mx * k, 'pp': (mx - mn) * k, 'rms': rms * k,
            'now': sum(tail) / len(tail) * k, 'clipped': clipped}


def unit_of(ch):
    return 'A' if ch['shunt_ohm'] > 0 else 'V'


def scale_of(ch):
    return 1.0 / ch['shunt_ohm'] if ch['shunt_ohm'] > 0 else 1.0


def write_run(kind, t, data, chans, extra):
    """Write CSV + PNG + summary.json for a frame or a record; returns (dir, summary)."""
    d = os.path.join(RUNS_DIR, time.strftime('%Y%m%d-%H%M%S') + '-scope')
    os.makedirs(d, exist_ok=True)
    series, labels, summary_ch = {}, {}, {}
    for ch, xs in data.items():
        c = chans[ch]
        key = 'ch%d' % (ch + 1)
        series[key + '_v'] = xs
        labels[key + '_v'] = '%s (V)' % c['name']
        summary_ch[key] = {'name': c['name'], 'v': stats(xs)}
        if c['shunt_ohm'] > 0:
            amps = [v / c['shunt_ohm'] for v in xs]
            series[key + '_a'] = amps
            labels[key + '_a'] = '%s (A)' % c['name']
            rate = extra.get('rate_hz') or 1.0
            a = stats(amps)
            a['max_10ms_avg'] = moving_avg_max(amps, max(1, int(rate * 0.010)))
            summary_ch[key]['shunt_ohm'] = c['shunt_ohm']
            summary_ch[key]['a'] = a
    with open(os.path.join(d, kind + '.csv'), 'w', encoding='utf-8') as f:
        cols = list(series)
        f.write('t_s,' + ','.join(cols) + '\n')
        for i in range(len(t)):
            f.write('%.9f,' % t[i] + ','.join('%.6f' % series[c][i] for c in cols) + '\n')
    summary = dict(extra, kind=kind, channels=summary_ch)
    save_json(os.path.join(d, 'summary.json'), summary)
    try:
        adbench.plot(os.path.join(d, kind + '.png'), t, series, labels, '%s %s' % (kind, d))
    except Exception as e:  # matplotlib is optional
        summary['png'] = 'skipped: %s' % e
    return d, summary


# ---------------------------------------------------------------- device worker
class Scope:
    """One worker thread owns hdwf; HTTP handlers only touch the shared state under lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cmds = queue.Queue()
        self.quit = threading.Event()
        self.settings = copy.deepcopy(DEFAULTS)
        self.rev = 1
        self.running = True
        self.dirty = True
        self.hdwf = None
        self.device = {'connected': False, 'name': '', 'sn': '', 'error': ''}
        self.state = 'idle'
        self.applied = {}
        self.frame = None
        self.seq = 0
        self.settle_until = 0.0
        self._hw_key = None
        self._req_key = None          # (enabled, range, offset) per channel as last applied by the live view
        self._last_pub = 0.0
        self._cache = (None, None)
        self.thread = threading.Thread(target=self._loop, daemon=True)

    # ---- called from HTTP threads
    def configure(self, part):
        with self.lock:
            self.settings = merge_settings(self.settings, part)
            self.rev += 1
            self.dirty = True
            return copy.deepcopy(self.settings)

    def run(self):
        with self.lock:
            if self.settings['mode'] == 'single':
                self.settings['mode'] = 'auto'
                self.rev += 1
            self.running = True
            self.dirty = True

    def stop(self):
        with self.lock:
            self.running = False
            self.dirty = True

    def single(self):
        with self.lock:
            self.settings['mode'] = 'single'
            self.rev += 1
            self.running = True
            self.dirty = True

    def call(self, name, timeout, **kw):
        """Run a command on the worker thread and wait for its result."""
        reply = queue.Queue()
        self.cmds.put((name, kw, reply))
        try:
            ok, val = reply.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError('worker did not answer within %.0f s' % timeout)
        if not ok:
            raise RuntimeError(val)
        return val

    def status(self):
        with self.lock:
            return {'device': dict(self.device), 'mode': self.settings['mode'], 'running': self.running,
                    'state': self.state, 'settling': time.time() < self.settle_until,
                    'settings': copy.deepcopy(self.settings), 'rev': self.rev, 'applied': dict(self.applied)}

    def processed(self):
        """Decimated points + measurements of the latest frame (cached per frame)."""
        with self.lock:
            f = self.frame
        if f is None:
            return None, None
        seq, val = self._cache
        if seq == f['seq']:
            return f, val
        chans = []
        for i, c in enumerate(f['settings']['channels']):
            xs = f['data'].get(i)
            k = scale_of(c)
            hw = f['hw'][i]
            m = measure(xs, k, hw['range'], hw['offset']) if xs else None
            pts = [round(v * k, 6) for v in decimate(xs)] if xs else []
            chans.append({'idx': i, 'name': c['name'], 'unit': unit_of(c), 'enabled': bool(xs),
                          'points': pts, 'meas': m, 'hw_range': hw['range'], 'hw_offset': hw['offset']})
        self._cache = (f['seq'], chans)
        return f, chans

    # ---- worker thread
    def _loop(self):
        self._connect()
        try:
            while not self.quit.is_set():
                self._handle_cmds()
                if self.hdwf is None:
                    time.sleep(0.1)
                    continue
                try:
                    self._step()
                except OSError as e:
                    self._lost(str(e))
        finally:
            self._close()

    def _handle_cmds(self):
        while True:
            try:
                name, kw, reply = self.cmds.get_nowait()
            except queue.Empty:
                return
            try:
                reply.put((True, getattr(self, '_cmd_' + name)(**kw)))
            except Exception as e:
                reply.put((False, str(e)))

    def _cmd_connect(self):
        self._connect()
        return dict(self.device)

    def _cmd_record(self, seconds, rate):
        if self.hdwf is None:
            raise RuntimeError(self.device['error'] or NO_DEVICE)
        with self.lock:
            s = copy.deepcopy(self.settings)
            prev = self.state
            self.state = 'recording'
        chans = {i: (c['vdiv'] * 8, c['offset']) for i, c in enumerate(s['channels']) if c['enabled']}
        if not chans:
            raise RuntimeError('no channel enabled')
        # The live view has already settled these ranges/offsets: skip the 2 s wait so the person
        # holding the probes is not kept waiting (same inputs, only the acquisition mode changes).
        req = tuple((c['enabled'], c['vdiv'] * 8, c['offset']) for c in s['channels'])
        settle = 0.2 if req == self._req_key and time.time() > self.settle_until else SETTLE_S
        h = self.hdwf
        dwf.FDwfAnalogInConfigure(h, c_int(0), c_int(0))
        dwf.FDwfAnalogInTriggerSourceSet(h, trigsrcNone)   # record starts at once
        # Keep the live view moving while the instrument records: show the newest screenful of the recording.
        roll = s['mode'] == 'roll'
        span = s['roll_s'] if roll else s['tdiv'] * 10

        def live(bufs, got):
            k = min(got, max(2, int(span * rate)))
            data = {ch: list(buf[got - k:got]) for ch, buf in bufs.items()}
            x0 = -(k - 1) / rate if roll else -s['trigger']['pre'] / 100 * span
            with self.lock:
                self.seq += 1
                self.frame = {'seq': self.seq, 'time': time.time(), 'data': data, 'x0': x0, 'rate_hz': rate,
                              'trig': 'rec', 'settings': s, 'hw': self._hw, 'settling': False}
        try:
            data, lost, corrupted = adbench.record(h, rate, seconds, chans, on_chunk=live, settle_s=settle)
        finally:
            with self.lock:
                self.state = prev
                self.dirty = True                         # back to the live mode
            self._hw_key = None
        return {'data': data, 'lost': lost, 'corrupted': corrupted, 'settings': s}

    def _connect(self):
        self._close()
        n = c_int()
        dwf.FDwfEnum(c_int(0), byref(n))
        dev = {'connected': False, 'name': '', 'sn': '', 'error': ''}
        if n.value == 0:
            dev['error'] = NO_DEVICE
        else:
            name, sn, used = create_string_buffer(64), create_string_buffer(32), c_int()
            dwf.FDwfEnumDeviceName(c_int(0), name)
            dwf.FDwfEnumSN(c_int(0), sn)
            dwf.FDwfEnumDeviceIsOpened(c_int(0), byref(used))
            dev['name'], dev['sn'] = name.value.decode(), sn.value.decode()
            if used.value:
                dev['error'] = '他のプログラムが使用中です（WaveForms / adbench.py を閉じて再接続）'
            else:
                h, err = adbench.try_open_device()
                if h is None:
                    dev['error'] = 'open failed: ' + err
                else:
                    self.hdwf = h
                    dwf.FDwfAnalogOutConfigure(h, c_int(-1), c_int(0))  # outputs stay stopped
                    dev['connected'] = True
                    lo, hi = c_int(), c_int()
                    dwf.FDwfAnalogInBufferSizeInfo(h, byref(lo), byref(hi))
                    fmin, fmax = c_double(), c_double()
                    dwf.FDwfAnalogInFrequencyInfo(h, byref(fmin), byref(fmax))
                    self.bufmax, self.fmax = hi.value, fmax.value
                    self._hw_key = None
        with self.lock:
            self.device = dev
            self.state = 'idle' if dev['connected'] else 'nodevice'
            self.dirty = True

    def _lost(self, err):
        self._close()
        with self.lock:
            self.device['connected'] = False
            self.device['error'] = 'device error: ' + (err or last_error())
            self.state = 'nodevice'

    def _close(self):
        if self.hdwf is not None:
            dwf.FDwfAnalogInConfigure(self.hdwf, c_int(0), c_int(0))
            dwf.FDwfDeviceClose(self.hdwf)
            self.hdwf = None

    def _step(self):
        with self.lock:
            running, dirty = self.running, self.dirty
            s = copy.deepcopy(self.settings)
            self.dirty = False
        if dirty:
            if not running:
                dwf.FDwfAnalogInConfigure(self.hdwf, c_int(0), c_int(0))
                with self.lock:
                    self.state = 'stop'
            elif not any(c['enabled'] for c in s['channels']):
                dwf.FDwfAnalogInConfigure(self.hdwf, c_int(0), c_int(0))
                with self.lock:
                    self.state = 'nochannel'
                    self.running = False
            else:
                self._apply(s)
        with self.lock:
            running = self.running
        if not running:
            time.sleep(0.03)
        elif s['mode'] == 'roll':
            self._poll_roll(s)
        else:
            self._poll_triggered(s)

    def _apply(self, s):
        h = self.hdwf
        dwf.FDwfAnalogInConfigure(h, c_int(0), c_int(0))
        for i, c in enumerate(s['channels']):
            dwf.FDwfAnalogInChannelEnableSet(h, c_int(i), c_int(1 if c['enabled'] else 0))
            dwf.FDwfAnalogInChannelRangeSet(h, c_int(i), c_double(c['vdiv'] * 8))
            dwf.FDwfAnalogInChannelOffsetSet(h, c_int(i), c_double(c['offset']))
        if s['mode'] == 'roll':
            span = s['roll_s']
            dwf.FDwfAnalogInAcquisitionModeSet(h, acqmodeScanShift)
            dwf.FDwfAnalogInTriggerSourceSet(h, trigsrcNone)
        else:
            span = s['tdiv'] * 10
            dwf.FDwfAnalogInAcquisitionModeSet(h, acqmodeSingle)
        dwf.FDwfAnalogInFrequencySet(h, c_double(min(self.fmax, self.bufmax / span)))
        hz = c_double()
        dwf.FDwfAnalogInFrequencyGet(h, byref(hz))
        n = max(16, min(self.bufmax, int(round(hz.value * span))))
        dwf.FDwfAnalogInBufferSizeSet(h, c_int(n))
        if s['mode'] != 'roll':
            t = s['trigger']
            dwf.FDwfAnalogInTriggerSourceSet(h, trigsrcDetectorAnalogIn)
            dwf.FDwfAnalogInTriggerTypeSet(h, trigtypeEdge)
            dwf.FDwfAnalogInTriggerChannelSet(h, c_int(t['source']))
            dwf.FDwfAnalogInTriggerLevelSet(h, c_double(t['level']))
            dwf.FDwfAnalogInTriggerConditionSet(h, DwfTriggerSlopeRise if t['edge'] == 'rise' else DwfTriggerSlopeFall)
            # position is relative to the buffer middle; +n/2/hz puts the trigger on the first sample
            dwf.FDwfAnalogInTriggerPositionSet(h, c_double((0.5 - t['pre'] / 100) * n / hz.value))
            dwf.FDwfAnalogInTriggerAutoTimeoutSet(h, c_double(AUTO_TIMEOUT if s['mode'] == 'auto' else 0))
        hw = []
        for i in range(2):
            r, o = c_double(), c_double()
            dwf.FDwfAnalogInChannelRangeGet(h, c_int(i), byref(r))
            dwf.FDwfAnalogInChannelOffsetGet(h, c_int(i), byref(o))
            hw.append({'range': r.value, 'offset': o.value})
        if not dwf.FDwfAnalogInConfigure(h, c_int(1), c_int(1)):
            raise OSError(last_error())
        key = tuple((c['enabled'], h_['range'], round(h_['offset'], 3)) for c, h_ in zip(s['channels'], hw))
        self._req_key = tuple((c['enabled'], c['vdiv'] * 8, c['offset']) for c in s['channels'])
        if key != self._hw_key:
            self._hw_key = key
            self.settle_until = time.time() + SETTLE_S
        with self.lock:
            self.applied = {'rate_hz': hz.value, 'samples': n, 'span_s': n / hz.value, 'hw': hw,
                            'buffer_max': self.bufmax}
            self.state = 'roll' if s['mode'] == 'roll' else 'armed'
        self._n, self._hz, self._hw = n, hz.value, hw

    def _read(self, s, count):
        data = {}
        for i, c in enumerate(s['channels']):
            if c['enabled']:
                buf = (c_double * count)()
                dwf.FDwfAnalogInStatusData(self.hdwf, c_int(i), buf, c_int(count))
                data[i] = list(buf)
        return data

    def _publish(self, s, data, x0, trig):
        with self.lock:
            self.seq += 1
            self.frame = {'seq': self.seq, 'time': time.time(), 'data': data, 'x0': x0, 'rate_hz': self._hz,
                          'trig': trig, 'settings': s, 'hw': self._hw,
                          'settling': time.time() < self.settle_until}
        self._last_pub = time.time()

    def _poll_triggered(self, s):
        sts = c_byte()
        if not dwf.FDwfAnalogInStatus(self.hdwf, c_int(1), byref(sts)):
            raise OSError(last_error())
        st = sts.value
        if st != DwfStateDone.value:
            with self.lock:
                if st in (DwfStateConfig.value, DwfStatePrefill.value, DwfStateArmed.value):
                    self.state = 'armed'
                else:
                    self.state = 'triggered'
            time.sleep(0.002)
            return
        auto = c_int()
        dwf.FDwfAnalogInStatusAutoTriggered(self.hdwf, byref(auto))
        data = self._read(s, self._n)
        self._publish(s, data, -s['trigger']['pre'] / 100 * self._n / self._hz, 'auto' if auto.value else 'trig')
        with self.lock:
            self.state = 'auto' if auto.value else 'trig'
            if s['mode'] == 'single':
                self.running = False
                self.dirty = True
        wait = MIN_FRAME_S - (time.time() - self._last_pub)
        if wait > 0:
            time.sleep(wait)

    def _poll_roll(self, s):
        sts, valid = c_byte(), c_int()
        if not dwf.FDwfAnalogInStatus(self.hdwf, c_int(1), byref(sts)):
            raise OSError(last_error())
        dwf.FDwfAnalogInStatusSamplesValid(self.hdwf, byref(valid))
        if valid.value > 0:
            data = self._read(s, valid.value)
            self._publish(s, data, -(valid.value - 1) / self._hz, 'roll')
        time.sleep(0.05)


# ---------------------------------------------------------------- HTTP
def frame_json(scope, full=True):
    st = scope.status()
    f, chans = scope.processed()
    s = st['settings']
    if s['mode'] == 'roll':
        screen = [-s['roll_s'], 0.0]
    else:
        span = s['tdiv'] * 10
        screen = [-s['trigger']['pre'] / 100 * span, (1 - s['trigger']['pre'] / 100) * span]
    out = {'time': time.time(), 'connected': st['device']['connected'], 'error': st['device']['error'],
           'mode': s['mode'], 'running': st['running'], 'state': st['state'], 'settling': st['settling'],
           'rev': st['rev'], 'screen': screen, 'say': dict(SAY)}
    if f is None:
        out.update({'seq': 0, 'frame_time': None, 'trig': None, 'channels': []})
        if full:
            out['settings'] = s
        return out
    n = max((len(x) for x in f['data'].values()), default=0)
    out.update({'seq': f['seq'], 'frame_time': f['time'], 'trig': f['trig'], 'rate_hz': f['rate_hz'],
                'samples': n, 'x0': f['x0'], 'x1': f['x0'] + (n - 1) / f['rate_hz'] if n else f['x0'],
                'frame_mode': f['settings']['mode']})
    if full:
        out['settings'] = s
        out['channels'] = chans
    else:
        out['channels'] = [{'idx': c['idx'], 'name': c['name'], 'unit': c['unit'], 'enabled': c['enabled'],
                            **(c['meas'] or {})} for c in chans]
    return out


def save_frame(scope):
    with scope.lock:
        f = scope.frame
    if f is None:
        raise RuntimeError('no frame to save yet')
    n = max(len(x) for x in f['data'].values())
    t = [f['x0'] + i / f['rate_hz'] for i in range(n)]
    extra = {'mode': f['settings']['mode'], 'rate_hz': f['rate_hz'], 'samples': n, 'trig': f['trig'],
             'frame_time': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(f['time'])),
             't_note': 't_s = 0 at the trigger (Roll: at the newest sample)', 'settings': f['settings']}
    return write_run('frame', t, f['data'], f['settings']['channels'], extra)


def do_record(scope, body):
    seconds = _num(body.get('seconds', 2.0), 0.01, 600.0, 'seconds')
    rate = _num(body.get('rate', 200000.0), 1.0, 100e6, 'rate')
    if seconds * rate > RECORD_MAX_SAMPLES:
        raise ValueError('seconds * rate must be <= %d samples' % RECORD_MAX_SAMPLES)
    r = scope.call('record', seconds + 60, seconds=seconds, rate=rate)
    n = max(len(x) for x in r['data'].values())
    t = [i / rate for i in range(n)]
    extra = {'rate_hz': rate, 'seconds': seconds, 'lost': r['lost'], 'corrupted': r['corrupted'],
             'settings': r['settings']}
    return write_run('record', t, r['data'], r['settings']['channels'], extra)


# ---- spoken prompts
# The agent asks the person to place probes and tells them when the measurement runs and ends
# (POST /api/say); the page reads each new one aloud (読み上げ ON) and shows it. See guide.py.
SAY = {'id': 0, 'text': '', 'time': 0.0}
SAY_LOCK = threading.Lock()


def add_say(body):
    text = str(body.get('text', '')).strip()[:200]
    if not text:
        raise ValueError('need text')
    with SAY_LOCK:
        SAY.update({'id': SAY['id'] + 1, 'text': text, 'time': time.time()})
        return dict(SAY)


# ---- voice marks
# The page's speech recognition posts a mark ("当てた", "OK", ...) while the
# person's hands hold the probes. Each mark carries a snapshot of the current
# readings so the agent can poll /api/marks and act on it.
MARKS = []
MARKS_LOCK = threading.Lock()


def add_mark(scope, body):
    text = str(body.get('text', ''))[:200]
    m = frame_json(scope, full=False)
    chans = [{'name': c.get('name'), 'unit': c.get('unit'), 'now': c.get('now'), 'mean': c.get('mean'),
              'min': c.get('min'), 'max': c.get('max')} for c in m.get('channels', [])]
    with MARKS_LOCK:
        mark = {'id': len(MARKS) + 1, 'time': time.time(), 'text': text,
                'kind': str(body.get('kind', 'voice'))[:20], 'channels': chans}
        if body.get('step') is not None:          # procedure step the mark answers
            mark['step'] = str(body['step'])[:64]
        MARKS.append(mark)
    os.makedirs(os.path.dirname(MARKS_LOG), exist_ok=True)
    with open(MARKS_LOG, 'a', encoding='utf-8') as f:
        f.write(json.dumps(mark, ensure_ascii=False) + '\n')
    return mark


def marks_since(since):
    with MARKS_LOCK:
        return [m for m in MARKS if m['id'] > since]


# ---- guided probing
# A procedure (procedures/<name>.json) is data only: steps that say where to put
# the probe on a board mapped by boardmap.py (boards/<board>/pads.json) and how to
# judge the reading. Probe points are resolved here (ref+pad or net -> pixel on
# the board picture) so the page only draws them. Schema: README.md.
PROC_VERSION = 1
ACTIONS = ('connect_gnd', 'power_on', 'power_off', 'probe', 'manual')
PROBE_CH = {1: 'CH1', 2: 'CH2', 'gnd': 'GND クリップ', 'meter': 'テスター'}
NAME_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
PASSIVE_RE = re.compile(r'^(C|R|L|FB)\d')
BOARD_TYPES = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.json': 'application/json'}
PROC_STATE = {}
PROC_LOCK = threading.Lock()


def _natural(ref):
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', ref)]


def load_board(name):
    path = os.path.join(BOARDS_DIR, name, 'pads.json')
    if not NAME_RE.match(name or '') or not os.path.isfile(path):
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def resolve_probe(board, p, step_side, where):
    """Fill ref / pad / net / side / px of one probe; returns (probe, problem or None)."""
    if not isinstance(p, dict):
        return None, '%s: probe はオブジェクトで書く' % where
    ch = p.get('ch')
    if ch not in PROBE_CH:
        return None, '%s: ch は 1, 2, "gnd", "meter" のどれか（%r）' % (where, ch)
    side = p.get('side') or step_side
    pads = board['pads']
    if p.get('ref') is not None and p.get('pad') is not None:
        ref, pad = str(p['ref']), str(p['pad'])
        cands = [q for q in pads if q['ref'] == ref and q['pad'] == pad]
        if not cands:
            return None, '%s: %s の %s 番パッドが基板にない' % (where, ref, pad)
        side = side or cands[0]['side']
        hit = next((q for q in cands if side in q.get('px', {})), None)
        if hit is None:
            return None, '%s: %s %s 番は %s 面から見えない' % (where, ref, pad, side)
        if p.get('net') and hit['net'] != p['net']:
            return None, '%s: %s %s 番のネットは %s（手順では %s）' % (where, ref, pad, hit['net'], p['net'])
    elif p.get('net'):
        net = str(p['net'])
        cands = [q for q in pads if q['net'] == net]
        if not cands:
            return None, '%s: ネット %s が基板にない' % (where, net)
        sides = [side] if side else ['top', 'bottom']
        hit = None
        for sd in sides:
            on = [q for q in cands if sd in q.get('px', {})]
            if on:
                on.sort(key=lambda q: (not PASSIVE_RE.match(q['ref']), _natural(q['ref']), _natural(q['pad'])))
                hit, side = on[0], sd
                break
        if hit is None:
            return None, '%s: ネット %s のパッドが %s 面にない' % (where, net, side)
    else:
        return None, '%s: ref と pad、または net を書く' % where
    out = {'ch': ch, 'ref': hit['ref'], 'pad': hit['pad'], 'net': hit['net'], 'side': side,
           'px': hit['px'][side], 'label': str(p.get('label') or hit['net'] or '%s-%s' % (hit['ref'], hit['pad']))}
    return out, None


def default_say(action, probes):
    def where(p):
        return '%s（%s %s 番）' % (p['label'], p['ref'], p['pad'])
    put = '、'.join('%s を %s' % (PROBE_CH[p['ch']], where(p)) for p in probes if p['ch'] != 'gnd')
    if action == 'connect_gnd':
        g = next((p for p in probes if p['ch'] == 'gnd'), None)
        return 'GND クリップを %s に付ける。付けたら「OK」' % (where(g) if g else 'GND')
    if action == 'power_off':
        return '電源を切る。終わったら「OK」'
    head = '電源を入れる。' if action == 'power_on' else ''
    return head + ('%s に当てて「当てた」' % put if put else '終わったら「OK」')


def check_procedure(proc):
    """Validate and resolve a procedure; returns (resolved copy, board sides, problems)."""
    probs = []
    if not isinstance(proc, dict):
        return None, None, ['JSON の最上位がオブジェクトではない']
    if proc.get('version') != PROC_VERSION:
        probs.append('version が %d ではない（%r）' % (PROC_VERSION, proc.get('version')))
    if not proc.get('title'):
        probs.append('title がない')
    board = load_board(proc.get('board'))
    if board is None:
        probs.append('基板 boards/%s/pads.json がない（boardmap.py で作る）' % proc.get('board'))
        return None, None, probs
    steps = proc.get('steps')
    if not isinstance(steps, list) or not steps:
        probs.append('steps が空')
        return None, None, probs
    ids = [s.get('id') if isinstance(s, dict) else None for s in steps]
    for i, sid in enumerate(ids):
        if not isinstance(sid, str) or not sid:
            probs.append('%d 番目の手順に id がない' % (i + 1))
        elif ids.count(sid) > 1 and ids.index(sid) == i:
            probs.append('手順 id %s が重複' % sid)
    known = {sid for sid in ids if isinstance(sid, str)}
    if proc.get('start') is not None and proc['start'] not in known:
        probs.append('start の手順 %s がない' % proc['start'])
    out_steps = []
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            continue
        sid = s.get('id') or '#%d' % (i + 1)
        where = '手順 %s' % sid
        action = s.get('action') or ('probe' if s.get('probes') else 'manual')
        if action not in ACTIONS:
            probs.append('%s: action %r は不明（%s）' % (where, action, ', '.join(ACTIONS)))
        if s.get('side') not in (None, 'top', 'bottom'):
            probs.append('%s: side は top か bottom' % where)
        probes = []
        for p in s.get('probes') or []:
            r, err = resolve_probe(board, p, s.get('side'), where)
            if err:
                probs.append(err)
            else:
                probes.append(r)
        expect = s.get('expect')
        if expect is not None:
            if not isinstance(expect, dict) or not expect:
                probs.append('%s: expect は {"ch1": [下限, 上限]} の形' % where)
                expect = None
            else:
                for key, rule in expect.items():
                    if key not in ('ch1', 'ch2'):
                        probs.append('%s: expect のキー %s は ch1 か ch2' % (where, key))
                    elif not any(p['ch'] == int(key[2]) for p in probes):
                        probs.append('%s: expect.%s に対応するプローブがない' % (where, key))
                    if isinstance(rule, list):
                        if len(rule) != 2 or not all(isinstance(v, (int, float)) for v in rule) or rule[0] > rule[1]:
                            probs.append('%s: expect.%s は [下限, 上限]' % (where, key))
                    elif isinstance(rule, dict):
                        if rule.get('near') not in known:
                            probs.append('%s: expect.%s.near の手順 %s がない' % (where, key, rule.get('near')))
                        if not isinstance(rule.get('tol'), (int, float)) or rule['tol'] <= 0:
                            probs.append('%s: expect.%s.tol は正の数' % (where, key))
                    else:
                        probs.append('%s: expect.%s は [下限, 上限] か {"near": 手順id, "tol": 幅}' % (where, key))
        if action == 'manual' and not s.get('say'):
            probs.append('%s: manual の手順には say が要る' % where)
        for key in ('next', 'else'):
            t = s.get(key)
            if t is None:
                if key == 'next':
                    probs.append('%s: next がない' % where)
            elif isinstance(t, str):
                if t not in known:
                    probs.append('%s: %s の手順 %s がない' % (where, key, t))
            elif not (isinstance(t, dict) and isinstance(t.get('end'), str) and t['end']):
                probs.append('%s: %s は手順 id か {"end": "結論"}' % (where, key))
        side = s.get('side') or (probes[0]['side'] if probes else None)
        out_steps.append(dict(s, action=action, probes=probes, side=side,
                              say=s.get('say') or default_say(action, probes)))
    resolved = dict(proc, steps=out_steps, start=proc.get('start') or out_steps[0].get('id'))
    return resolved, board['sides'], probs


def load_procedure(name):
    if not NAME_RE.match(name or ''):
        raise ValueError('procedure name must be [A-Za-z0-9_-]')
    path = os.path.join(PROCS_DIR, name + '.json')
    if not os.path.isfile(path):
        raise FileNotFoundError(name)
    try:
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
    except ValueError as e:
        return {'name': name, 'procedure': None, 'sides': None, 'problems': ['JSON として読めない: %s' % e]}
    proc, sides, probs = check_procedure(raw)
    return {'name': name, 'procedure': proc, 'sides': sides, 'problems': probs}


def list_procedures():
    out = []
    if os.path.isdir(PROCS_DIR):
        for fn in sorted(os.listdir(PROCS_DIR)):
            name, ext = os.path.splitext(fn)
            if ext != '.json' or not NAME_RE.match(name):
                continue
            r = load_procedure(name)
            p = r['procedure'] or {}
            out.append({'name': name, 'title': p.get('title', ''), 'board': p.get('board', ''),
                        'steps': len(p.get('steps', [])), 'problems': r['problems']})
    return out


def board_file(rel):
    """Bytes + content type of a file under boards/ (no path traversal)."""
    root = os.path.realpath(BOARDS_DIR)
    p = os.path.realpath(os.path.join(root, unquote(rel)))
    ctype = BOARD_TYPES.get(os.path.splitext(p)[1].lower())
    try:
        inside = os.path.commonpath([p, root]) == root
    except ValueError:                       # another drive
        inside = False
    if not inside or ctype is None or not os.path.isfile(p):
        raise FileNotFoundError(rel)
    with open(p, 'rb') as f:
        return f.read(), ctype


def set_proc_state(body):
    """Store the page's procedure progress; a `result` / `conclusion` is appended to the run log."""
    if not isinstance(body, dict) or not NAME_RE.match(str(body.get('procedure') or '')):
        raise ValueError('procedure (name) is required')
    name = body['procedure']
    with PROC_LOCK:
        run_dir = PROC_STATE.get('run_dir')
        if body.get('event') == 'start' or not run_dir or PROC_STATE.get('procedure') != name:
            run_dir = os.path.join(RUNS_DIR, time.strftime('%Y%m%d-%H%M%S') + '-proc-' + name)
            os.makedirs(run_dir, exist_ok=True)
        state = {k: v for k, v in body.items() if k != 'result'}
        state.update(time=time.time(), run_dir=run_dir)
        PROC_STATE.clear()
        PROC_STATE.update(state)
        lines = []
        if isinstance(body.get('result'), dict):
            lines.append(dict(body['result'], procedure=name, time=state['time']))
        if body.get('event') in ('end', 'abort'):
            lines.append({'event': body['event'], 'procedure': name, 'conclusion': body.get('conclusion'),
                          'time': state['time']})
        if lines:
            with open(os.path.join(run_dir, 'results.jsonl'), 'a', encoding='utf-8') as f:
                for ln in lines:
                    f.write(json.dumps(ln, ensure_ascii=False) + '\n')
        return dict(PROC_STATE)


def get_proc_state():
    with PROC_LOCK:
        return dict(PROC_STATE)


class Handler(BaseHTTPRequestHandler):
    scope = None
    server_version = 'adscope/1'

    def log_message(self, *_):
        pass

    def _send(self, code, obj, ctype='application/json'):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype + ('; charset=utf-8' if 'json' in ctype or 'html' in ctype else ''))
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode('utf-8'))

    def do_GET(self):
        path = self.path.split('?')[0]
        sc = self.scope
        if path in ('/', '/index.html'):
            with open(PAGE, 'rb') as f:
                self._send(200, f.read(), 'text/html')
        elif path == '/api/status':
            self._send(200, sc.status())
        elif path == '/api/frame':
            self._send(200, frame_json(sc))
        elif path == '/api/measure':
            self._send(200, frame_json(sc, full=False))
        elif path == '/api/marks':
            q = self.path.partition('?')[2]
            since = int(dict(kv.partition('=')[::2] for kv in q.split('&') if kv).get('since') or 0)
            self._send(200, {'marks': marks_since(since)})
        elif path == '/api/procedures':
            self._send(200, {'procedures': list_procedures()})
        elif path.startswith('/api/procedures/'):
            try:
                self._send(200, load_procedure(path[len('/api/procedures/'):]))
            except ValueError as e:
                self._send(400, {'error': str(e)})
            except FileNotFoundError:
                self._send(404, {'error': 'no such procedure'})
        elif path == '/api/procedure/state':
            self._send(200, get_proc_state())
        elif path.startswith('/boards/'):
            try:
                body, ctype = board_file(path[len('/boards/'):])
                self._send(200, body, ctype)
            except FileNotFoundError:
                self._send(404, {'error': 'not found'})
        else:
            self._send(404, {'error': 'not found'})

    def do_POST(self):
        path = self.path.split('?')[0]
        sc = self.scope
        try:
            body = self._body()
            if path == '/api/config':
                self._send(200, {'settings': sc.configure(body)})
            elif path == '/api/run':
                sc.run()
                self._send(200, {'ok': True})
            elif path == '/api/stop':
                sc.stop()
                self._send(200, {'ok': True})
            elif path == '/api/single':
                sc.single()
                self._send(200, {'ok': True})
            elif path == '/api/connect':
                self._send(200, {'device': sc.call('connect', 30)})
            elif path == '/api/save':
                d, summary = save_frame(sc)
                self._send(200, {'dir': d, 'summary': summary})
            elif path == '/api/record':
                d, summary = do_record(sc, body)
                self._send(200, {'dir': d, 'summary': summary})
            elif path == '/api/mark':
                self._send(200, add_mark(sc, body))
            elif path == '/api/say':
                self._send(200, add_say(body))
            elif path == '/api/procedure/state':
                self._send(200, set_proc_state(body))
            elif path == '/api/quit':
                self._send(200, {'ok': True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._send(404, {'error': 'not found'})
        except ValueError as e:
            self._send(400, {'error': str(e)})
        except RuntimeError as e:
            self._send(409, {'error': str(e)})


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--port', type=int, default=8195)
    p.add_argument('--no-open', action='store_true', help='do not open the page in the browser')
    p.add_argument('--data', help='folder with boards/, procedures/, runs/ (default: $%s or the current directory)'
                   % DATA_ENV)
    args = p.parse_args()
    set_data(data_root(args.data))
    scope = Scope()
    Handler.scope = scope
    srv = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    srv.daemon_threads = True
    scope.thread.start()
    url = 'http://127.0.0.1:%d/' % args.port
    print('adscope:', url, '(data: %s, Ctrl-C to stop)' % DATA, flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        scope.quit.set()
        scope.thread.join(timeout=30)
        print('adscope: device closed', flush=True)


if __name__ == '__main__':
    main()
