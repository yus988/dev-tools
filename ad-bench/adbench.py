"""Analog Discovery bench helper (WaveForms SDK via ctypes).

The person wires the probes; this script drives the instrument, optionally
sends one line to a device under test over TCP when the record starts, and
saves CSV + PNG + a JSON summary under --out (default
<data>/runs/<timestamp>-<command>/; <data> = --data, $AD_BENCH_DATA or the
current directory).

  python adbench.py devices
  python adbench.py selftest                      # W1 -> 1+, 1- -> GND
  python adbench.py monitor --ch1-name VSYS --ch2-name 3V3   # live DC readout
  python adbench.py impedance --ref 10            # motor R / L (see README)
  python adbench.py record --seconds 3 --ch1-offset 4 --ch2-shunt 0.05 \
         --tcp <host>:<port> --tcp-line '<one line the device understands>'

Nothing is sent anywhere unless --tcp and one of --tcp-line / --tcp-kv are given.
--tcp-kv "cmd=ping n=1" sends {"cmd": "ping", "n": 1} (easier to type in cmd / PowerShell than raw JSON).
"""
import argparse
import json
import math
import os
import socket
import sys
import threading
import time
from ctypes import byref, c_byte, c_double, c_int, cdll, create_string_buffer, sizeof

SDK_PY = r'C:\Program Files\Digilent\WaveFormsSDK\samples\py'
sys.path.insert(0, SDK_PY)
from dwfconstants import (  # noqa: E402
    AnalogOutNodeCarrier, DwfAnalogImpedanceReactance, DwfAnalogImpedanceResistance,
    DwfAnalogImpedanceSeriesInductance, DwfParamOnClose, DwfStateArmed, DwfStateConfig,
    DwfStateDone, DwfStatePrefill, acqmodeRecord, funcSine, hdwfNone,
)

dwf = cdll.dwf
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_ENV = 'AD_BENCH_DATA'     # folder holding runs/ (and boards/, procedures/ for adscope)


def data_root(path=None):
    """The data folder: `path`, else $AD_BENCH_DATA, else the current directory."""
    return os.path.abspath(path or os.environ.get(DATA_ENV) or os.getcwd())


# ---------------------------------------------------------------- helpers
def last_error():
    buf = create_string_buffer(512)
    dwf.FDwfGetLastErrorMsg(buf)
    return buf.value.decode(errors='replace')


def try_open_device():
    """Open the first device; returns (hdwf, '') or (None, error message)."""
    dwf.FDwfParamSet(DwfParamOnClose, 0)
    hdwf = c_int()
    dwf.FDwfDeviceOpen(c_int(-1), byref(hdwf))
    if hdwf.value == hdwfNone.value:
        return None, last_error()
    return hdwf, ''


def open_device():
    hdwf, err = try_open_device()
    if hdwf is None:
        sys.exit('failed to open an Analog Discovery: ' + err)
    return hdwf


def out_dir(args, name):
    d = args.out or os.path.join(data_root(args.data), 'runs', time.strftime('%Y%m%d-%H%M%S') + '-' + name)
    os.makedirs(d, exist_ok=True)
    return d


def save_json(path, obj):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def tcp_send(target, line):
    """Send one text line to HOST:PORT and return the reply (up to a newline or 1.5 s)."""
    host, _, port = target.rpartition(':')
    s = socket.create_connection((host, int(port)), timeout=3)
    s.settimeout(0.5)
    s.sendall((line + '\n').encode())
    data = b''
    end = time.time() + 1.5
    while time.time() < end:
        try:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
            if data.endswith(b'\n'):
                break
        except socket.timeout:
            pass
    s.close()
    return data.decode('utf-8', 'replace').strip()


def kv_to_json(text):
    """'cmd=pwm_tone hz=80 amp=1.0' -> '{"cmd": "pwm_tone", "hz": 80, "amp": 1.0}' (numbers become numbers)."""
    req = {}
    for part in (text or '').split():
        k, _, v = part.partition('=')
        for conv in (int, float):
            try:
                req[k] = conv(v)
                break
            except ValueError:
                pass
        else:
            req[k] = v
    return json.dumps(req)


# ---------------------------------------------------------------- commands
def cmd_devices(_args):
    n = c_int()
    dwf.FDwfEnum(c_int(0), byref(n))
    ver = create_string_buffer(32)
    dwf.FDwfGetVersion(ver)
    print('WaveForms SDK', ver.value.decode(), '/ devices:', n.value)
    for i in range(n.value):
        name = create_string_buffer(64)
        sn = create_string_buffer(32)
        dwf.FDwfEnumDeviceName(c_int(i), name)
        dwf.FDwfEnumSN(c_int(i), sn)
        print(' ', i, name.value.decode(), sn.value.decode())


def record(hdwf, rate, seconds, channels, on_start=None, on_chunk=None, settle_s=2.0):
    """acqmodeRecord on the given channels: {idx: (range_v, offset_v)}.
    settle_s: wait after setting the ranges/offsets (2 s per the SDK sample; shorter is fine when they did not change).
    on_chunk(bufs, got), if given, is called about every 0.1 s with the buffers filled so far.
    Returns ({idx: list}, lost, corrupted)."""
    n = int(rate * seconds)
    bufs = {ch: (c_double * n)() for ch in channels}
    for ch in range(2):
        dwf.FDwfAnalogInChannelEnableSet(hdwf, c_int(ch), c_int(1 if ch in channels else 0))
    for ch, (rng, off) in channels.items():
        dwf.FDwfAnalogInChannelRangeSet(hdwf, c_int(ch), c_double(rng))
        dwf.FDwfAnalogInChannelOffsetSet(hdwf, c_int(ch), c_double(off))
    dwf.FDwfAnalogInAcquisitionModeSet(hdwf, acqmodeRecord)
    dwf.FDwfAnalogInFrequencySet(hdwf, c_double(rate))
    dwf.FDwfAnalogInRecordLengthSet(hdwf, c_double(seconds))
    dwf.FDwfAnalogInConfigure(hdwf, c_int(1), c_int(0))
    time.sleep(settle_s)              # let the input offsets settle (SDK sample)
    dwf.FDwfAnalogInConfigure(hdwf, c_int(0), c_int(1))
    started = False
    got = 0
    lost = corrupted = False
    sts = c_byte()
    avail, nlost, ncorr = c_int(), c_int(), c_int()
    last_chunk = 0.0
    while got < n:
        dwf.FDwfAnalogInStatus(hdwf, c_int(1), byref(sts))
        if got == 0 and sts.value in (DwfStateConfig.value, DwfStatePrefill.value, DwfStateArmed.value):
            continue
        if not started:
            started = True
            if on_start:
                threading.Thread(target=on_start, daemon=True).start()
        dwf.FDwfAnalogInStatusRecord(hdwf, byref(avail), byref(nlost), byref(ncorr))
        got += nlost.value
        lost |= bool(nlost.value)
        corrupted |= bool(ncorr.value)
        if avail.value == 0:
            continue
        take = min(avail.value, n - got)
        for ch, buf in bufs.items():
            dwf.FDwfAnalogInStatusData(hdwf, c_int(ch), byref(buf, sizeof(c_double) * got), c_int(take))
        got += take
        if on_chunk and time.time() - last_chunk >= 0.1:
            last_chunk = time.time()
            on_chunk(bufs, got)
    return {ch: list(buf) for ch, buf in bufs.items()}, lost, corrupted


def stats(xs):
    return {'min': min(xs), 'max': max(xs), 'mean': sum(xs) / len(xs)}


def moving_avg_max(xs, window):
    if window <= 1 or len(xs) < window:
        return max(xs)
    acc = sum(xs[:window])
    best = acc
    for i in range(window, len(xs)):
        acc += xs[i] - xs[i - window]
        best = max(best, acc)
    return best / window


def plot(path, t, series, ylabel_map, title):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(len(series), 1, figsize=(11, 3.2 * len(series)), sharex=True)
    if len(series) == 1:
        axes = [axes]
    for ax, (name, ys) in zip(axes, series.items()):
        ax.plot(t, ys, linewidth=0.6)
        ax.set_ylabel(ylabel_map.get(name, name))
        ax.grid(True, alpha=0.3)
    axes[-1].set_xlabel('time (s)')
    axes[0].set_title(title)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def cmd_record(args):
    channels = {0: (args.ch1_range, args.ch1_offset)}
    if not args.no_ch2:
        channels[1] = (args.ch2_range, args.ch2_offset)
    d = out_dir(args, 'record')
    events = []

    def on_start():
        line = args.tcp_line or (kv_to_json(args.tcp_kv) if args.tcp_kv else None)
        if args.tcp and line:
            time.sleep(args.send_delay)
            resp = tcp_send(args.tcp, line)
            events.append({'t_after_start_s': args.send_delay, 'to': args.tcp, 'sent': line, 'reply': resp})
            print('sent', line, '->', resp)

    hdwf = open_device()
    try:
        data, lost, corrupted = record(hdwf, args.rate, args.seconds, channels, on_start)
    finally:
        dwf.FDwfDeviceClose(hdwf)
    n = len(data[0])
    t = [i / args.rate for i in range(n)]
    series = {'ch1_v': data[0]}
    summary = {'rate_hz': args.rate, 'seconds': args.seconds, 'lost': lost, 'corrupted': corrupted,
               'ch1_v': stats(data[0]), 'events': events}
    if 1 in data:
        series['ch2_v'] = data[1]
        summary['ch2_v'] = stats(data[1])
        if args.ch2_shunt:
            amps = [v / args.ch2_shunt for v in data[1]]
            series['ch2_a'] = amps
            win = max(1, int(args.rate * 0.010))
            summary['ch2_a'] = stats(amps)
            summary['ch2_a']['max_10ms_avg'] = moving_avg_max(amps, win)
    with open(os.path.join(d, 'record.csv'), 'w', encoding='utf-8') as f:
        cols = list(series)
        f.write('t_s,' + ','.join(cols) + '\n')
        for i in range(n):
            f.write('%.7f,' % t[i] + ','.join('%.5f' % series[c][i] for c in cols) + '\n')
    save_json(os.path.join(d, 'summary.json'), summary)
    plot(os.path.join(d, 'record.png'), t, series,
         {'ch1_v': 'CH1 (V)', 'ch2_v': 'CH2 (V)', 'ch2_a': 'CH2 current (A)'}, 'record ' + d)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print('saved to', d)


def cmd_monitor(args):
    """Live DC readout of CH1 / CH2 for wiring checks and quick probing.
    Prints one line per --interval and logs it to monitor.csv; Ctrl-C to stop."""
    d = out_dir(args, 'monitor')
    names = [args.ch1_name, args.ch2_name]
    n = int(args.rate * args.window)
    hdwf = open_device()
    log = open(os.path.join(d, 'monitor.csv'), 'w', encoding='utf-8')
    log.write('t_s,' + ','.join('%s_mean,%s_min,%s_max' % (x, x, x) for x in names) + '\n')
    bufs = [(c_double * n)() for _ in range(2)]
    sts = c_byte()
    try:
        for ch in range(2):
            dwf.FDwfAnalogInChannelEnableSet(hdwf, c_int(ch), c_int(1))
            dwf.FDwfAnalogInChannelRangeSet(hdwf, c_int(ch), c_double(args.range))
            dwf.FDwfAnalogInChannelOffsetSet(hdwf, c_int(ch), c_double(args.offset))
        dwf.FDwfAnalogInFrequencySet(hdwf, c_double(args.rate))
        dwf.FDwfAnalogInBufferSizeSet(hdwf, c_int(n))
        dwf.FDwfAnalogInConfigure(hdwf, c_int(1), c_int(0))
        time.sleep(1)
        t0 = time.time()                  # --seconds counts readings, not the settle wait
        print('monitor: CH1=%s CH2=%s, %.0f-%.0f V span, Ctrl-C to stop. log: %s'
              % (names[0], names[1], args.offset - args.range / 2, args.offset + args.range / 2, d))
        while not args.seconds or time.time() - t0 < args.seconds:
            dwf.FDwfAnalogInConfigure(hdwf, c_int(0), c_int(1))
            while True:
                dwf.FDwfAnalogInStatus(hdwf, c_int(1), byref(sts))
                if sts.value == DwfStateDone.value:
                    break
            row, text = [], []
            for ch in range(2):
                dwf.FDwfAnalogInStatusData(hdwf, c_int(ch), bufs[ch], c_int(n))
                s = stats(bufs[ch])
                row += [s['mean'], s['min'], s['max']]
                text.append('%s %6.3f V (%6.3f..%6.3f)' % (names[ch], s['mean'], s['min'], s['max']))
            t = time.time() - t0
            log.write('%.2f,' % t + ','.join('%.4f' % v for v in row) + '\n')
            log.flush()
            print('%7.1f s  %s' % (t, '   '.join(text)), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        log.close()
        dwf.FDwfDeviceClose(hdwf)


def cmd_selftest(args):
    """W1 1 kHz 1 Vpp sine looped to CH1 (wire W1 -> 1+, 1- -> GND)."""
    hdwf = open_device()
    try:
        dwf.FDwfAnalogOutNodeEnableSet(hdwf, c_int(0), AnalogOutNodeCarrier, c_int(1))
        dwf.FDwfAnalogOutNodeFunctionSet(hdwf, c_int(0), AnalogOutNodeCarrier, funcSine)
        dwf.FDwfAnalogOutNodeFrequencySet(hdwf, c_int(0), AnalogOutNodeCarrier, c_double(1000))
        dwf.FDwfAnalogOutNodeAmplitudeSet(hdwf, c_int(0), AnalogOutNodeCarrier, c_double(0.5))
        dwf.FDwfAnalogOutConfigure(hdwf, c_int(0), c_int(1))
        data, lost, corrupted = record(hdwf, 100000, 0.05, {0: (5.0, 0.0)})
        dwf.FDwfAnalogOutReset(hdwf, c_int(0))
    finally:
        dwf.FDwfDeviceClose(hdwf)
    s = stats(data[0])
    ok = 0.85 <= s['max'] - s['min'] <= 1.15
    print('CH1 p-p %.3f V (expect ~1.0)  lost=%s corrupted=%s  -> %s'
          % (s['max'] - s['min'], lost, corrupted, 'OK' if ok else 'CHECK WIRING'))


def cmd_impedance(args):
    """Mode 0: W1 & 1+ on DUT top, DUT bottom = 2+ = R_ref top, R_ref bottom = GND (1-, 2- to GND)."""
    d = out_dir(args, 'impedance')
    hdwf = open_device()
    rows = []
    try:
        dwf.FDwfDeviceAutoConfigureSet(hdwf, c_int(3))
        dwf.FDwfAnalogImpedanceReset(hdwf)
        dwf.FDwfAnalogImpedanceModeSet(hdwf, c_int(0))
        dwf.FDwfAnalogImpedanceReferenceSet(hdwf, c_double(args.ref))
        dwf.FDwfAnalogImpedanceFrequencySet(hdwf, c_double(args.start))
        dwf.FDwfAnalogImpedanceAmplitudeSet(hdwf, c_double(args.amp))
        dwf.FDwfAnalogImpedanceConfigure(hdwf, c_int(1))
        time.sleep(2)
        sts = c_byte()
        for i in range(args.steps):
            hz = args.start * (args.stop / args.start) ** (i / (args.steps - 1))
            dwf.FDwfAnalogImpedanceFrequencySet(hdwf, c_double(hz))
            time.sleep(0.01)
            dwf.FDwfAnalogInConfigure(hdwf, c_int(1), c_int(1))
            while True:
                if dwf.FDwfAnalogImpedanceStatus(hdwf, byref(sts)) == 0:
                    sys.exit(last_error())
                if sts.value == 2:
                    break
            r, x, ls = c_double(), c_double(), c_double()
            dwf.FDwfAnalogImpedanceStatusMeasure(hdwf, DwfAnalogImpedanceResistance, byref(r))
            dwf.FDwfAnalogImpedanceStatusMeasure(hdwf, DwfAnalogImpedanceReactance, byref(x))
            dwf.FDwfAnalogImpedanceStatusMeasure(hdwf, DwfAnalogImpedanceSeriesInductance, byref(ls))
            warn = 0
            for ch in range(2):
                w = c_int()
                dwf.FDwfAnalogImpedanceStatusWarning(hdwf, c_int(ch), byref(w))
                warn |= w.value << (2 * ch)
            rows.append({'hz': hz, 'rs_ohm': r.value, 'xs_ohm': x.value, 'ls_h': ls.value, 'warn': warn})
            print('%9.1f Hz  Rs %8.4f ohm  Xs %8.4f ohm  Ls %8.2f uH%s'
                  % (hz, r.value, x.value, ls.value * 1e6, '  (range warning)' if warn else ''))
        dwf.FDwfAnalogImpedanceConfigure(hdwf, c_int(0))
    finally:
        dwf.FDwfDeviceClose(hdwf)
    with open(os.path.join(d, 'impedance.csv'), 'w', encoding='utf-8') as f:
        f.write('hz,rs_ohm,xs_ohm,ls_h,warn\n')
        for r in rows:
            f.write('%.2f,%.6f,%.6f,%.9e,%d\n' % (r['hz'], r['rs_ohm'], r['xs_ohm'], r['ls_h'], r['warn']))
    band = [r['ls_h'] for r in rows if args.l_band[0] <= r['hz'] <= args.l_band[1] and not r['warn']]
    band.sort()
    summary = {
        'ref_ohm': args.ref, 'amp_v': args.amp,
        'rs_at_lowest_hz_ohm': rows[0]['rs_ohm'] if rows else None,
        'ls_median_uh': (band[len(band) // 2] * 1e6) if band else None,
        'ls_band_hz': args.l_band, 'points_in_band': len(band),
    }
    save_json(os.path.join(d, 'summary.json'), summary)
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(2, 1, figsize=(9, 7), sharex=True)
        hz = [r['hz'] for r in rows]
        ax[0].loglog(hz, [abs(r['rs_ohm']) for r in rows], label='Rs')
        ax[0].loglog(hz, [abs(r['xs_ohm']) for r in rows], label='|Xs|')
        ax[0].set_ylabel('ohm')
        ax[0].legend()
        ax[0].grid(True, which='both', alpha=0.3)
        ax[1].semilogx(hz, [r['ls_h'] * 1e6 for r in rows])
        ax[1].set_ylabel('Ls (uH)')
        ax[1].set_xlabel('Hz')
        ax[1].grid(True, which='both', alpha=0.3)
        fig.tight_layout()
        fig.savefig(os.path.join(d, 'impedance.png'), dpi=110)
    except Exception as e:  # plotting is optional
        print('plot skipped:', e)
    print(json.dumps(summary, indent=2))
    print('saved to', d)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--data', help='data folder; runs go to <data>/runs/ (default: $%s or the current directory)' % DATA_ENV)
    p.add_argument('--out', help='output directory (default: <data>/runs/<timestamp>-<command>)')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('devices')
    sub.add_parser('selftest')
    pi = sub.add_parser('impedance')
    pi.add_argument('--ref', type=float, default=10.0, help='reference resistor on the breadboard (ohm)')
    pi.add_argument('--amp', type=float, default=0.1, help='W1 amplitude (V); keep small so the motor does not turn')
    pi.add_argument('--start', type=float, default=100.0)
    pi.add_argument('--stop', type=float, default=100000.0)
    pi.add_argument('--steps', type=int, default=41)
    pi.add_argument('--l-band', type=float, nargs=2, default=[1000.0, 20000.0], help='band for the Ls median (Hz)')
    pm = sub.add_parser('monitor')
    pm.add_argument('--ch1-name', default='CH1')
    pm.add_argument('--ch2-name', default='CH2')
    pm.add_argument('--range', type=float, default=10.0, help='span (V) for both channels')
    pm.add_argument('--offset', type=float, default=2.5, help='centre (V); default covers -2.5..7.5 V')
    pm.add_argument('--rate', type=float, default=100000.0)
    pm.add_argument('--window', type=float, default=0.02, help='seconds per reading (min/max over it)')
    pm.add_argument('--interval', type=float, default=0.3)
    pm.add_argument('--seconds', type=float, default=0.0, help='0 = until Ctrl-C')
    pr = sub.add_parser('record')
    pr.add_argument('--rate', type=float, default=200000.0, help='samples/s per channel')
    pr.add_argument('--seconds', type=float, default=3.0)
    pr.add_argument('--ch1-range', type=float, default=5.0, help='CH1 span (V); CH1 = VSYS')
    pr.add_argument('--ch1-offset', type=float, default=4.0, help='CH1 centre (V)')
    pr.add_argument('--ch2-range', type=float, default=0.5, help='CH2 span (V); CH2 = shunt voltage')
    pr.add_argument('--ch2-offset', type=float, default=0.0)
    pr.add_argument('--ch2-shunt', type=float, default=0.0, help='shunt (ohm) to convert CH2 to amps; 0 = volts only')
    pr.add_argument('--no-ch2', action='store_true')
    pr.add_argument('--tcp', metavar='HOST:PORT', help='with --tcp-line, the line is sent here once recording starts')
    snd = pr.add_mutually_exclusive_group()
    snd.add_argument('--tcp-line', help='one text line to send (a newline is added); the reply goes into summary.json')
    snd.add_argument('--tcp-kv', help='key=value pairs sent as one JSON line, e.g. "cmd=ping n=1" (no JSON quoting needed)')
    pr.add_argument('--send-delay', type=float, default=0.3, help='seconds after the record starts')
    args = p.parse_args()
    {'devices': cmd_devices, 'selftest': cmd_selftest, 'impedance': cmd_impedance, 'record': cmd_record,
     'monitor': cmd_monitor}[args.cmd](args)


if __name__ == '__main__':
    main()
