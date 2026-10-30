# ad-bench — Analog Discovery bench helper

[日本語](README.md)

Python (ctypes) driver for the Digilent WaveForms SDK (`dwf.dll`; written
against SDK 3.25.1 and an Analog Discovery Studio). The person wires the
probes; the agent runs the instrument, optionally sends one line to the device
under test over TCP when a record starts, and collects CSV / PNG /
`summary.json` under `<data>/runs/<timestamp>-<command>/`.

Close the WaveForms GUI first: only one program can open the device.

## Requirements

- Windows, Python 3.9+.
- **WaveForms (Digilent) installed separately**, with its SDK: `dwf.dll` must be
  loadable and the SDK's Python samples (`dwfconstants.py`) are taken from
  `C:\Program Files\Digilent\WaveFormsSDK\samples\py`. Nothing from Digilent
  is included in this repo.
- `matplotlib` (PNG plots); `Pillow` and KiCad 10 for `boardmap.py` only.

## Data folder

Project data is not kept in this repo. Every script reads and writes one data
folder, given by `--data <folder>`, else `$AD_BENCH_DATA`, else the current
directory:

```
<data>/
  boards/<board>/top.jpg, bottom.jpg, pads.json   # boardmap.py output
  procedures/<name>.json                           # guided probing procedures
  runs/<timestamp>-<command>/                      # measurements (keep them out of git)
  runs/marks.jsonl                                 # adscope voice marks
```

Keep that folder in the workspace of the project being measured.

## Commands and wiring (Analog Discovery Studio breadboard)

| Command | Wiring | What it gives |
|---|---|---|
| `python adbench.py devices` | USB only | device list |
| `python adbench.py monitor --ch1-name VSYS --ch2-name 3V3` | 1+ / 2+ on the rails, 1− / 2− → board GND | live DC readout (mean, min..max) every 0.3 s, logged to `monitor.csv` |
| `python adbench.py selftest` | W1 → 1+, 1− → GND | checks the record path (expects ~1.0 Vpp) |
| `python adbench.py impedance --ref 10` | W1 and 1+ → motor lead A; motor lead B → 2+ and one end of a 10 Ω resistor; resistor other end → GND; 1−, 2− → GND. Motor unplugged from the board, rotor held still | Rs, Xs, Ls over 100 Hz–100 kHz; Ls median over 1–20 kHz |
| `python adbench.py record --ch2-shunt 0.05 --tcp <host>:<port> --tcp-line '<line>'` | 1+ → the supply rail, 1− → board GND; a 0.05 Ω shunt in the battery − lead, 2+ / 2− across it; board GND = scope GND | rail min / max and battery current (peak, max 10 ms average) while the device runs whatever the line starts |

Defaults for `record`: 200 kS/s per channel, 3 s, CH1 span 5 V centred on 4 V
(covers 1.5–6.5 V), CH2 span 0.5 V. Raise `--ch1-range` / `--ch1-offset` if a
spike can exceed the span. `--tcp-line` is sent to `--tcp HOST:PORT` (a
newline is added) `--send-delay` (0.3 s) after recording starts; the reply (up
to a newline or 1.5 s) goes into `summary.json` `events`. For a device that
takes one JSON line, `--tcp-kv "cmd=ping n=1"` builds `{"cmd": "ping", "n": 1}`
for you (numbers become numbers), which avoids JSON quoting in cmd / PowerShell.
Nothing is sent unless `--tcp` and one of `--tcp-line` / `--tcp-kv` are given.

## Notes

- Impedance mode 0 = W1-C1-DUT-C2-R-GND (SDK `FDwfAnalogImpedanceModeSet`).
  Keep `--amp` small (0.1 V default) so the motor does not turn; back-EMF would
  distort the low-frequency points.
- Shunt current is CH2 volts / `--ch2-shunt`. Battery protectors typically
  trip on an over-current lasting around 10 ms, so `max_10ms_avg` (the highest
  10 ms moving average) is the number to compare against their limit.

## adscope — scope page + HTTP API

`adscope.py` is a small oscilloscope for board inspection (power rails, shunt
current, power-on behaviour). It owns the device and serves a page and a JSON
API on `127.0.0.1`, so the person watches the page while the agent reads the
same data over HTTP. Input only: W1/W2 are never driven (they are stopped on
connect).

```
python adscope.py --data <folder>               # opens http://127.0.0.1:8195/ in the default browser
python adscope.py --data <folder> --port 8195 --no-open
```

Stop with Ctrl-C or `curl -X POST http://127.0.0.1:8195/api/quit`; both close
the device. **While adscope runs, `adbench.py` commands cannot open the device**
(only one program can); the WaveForms GUI must stay closed too. If the device is
missing or busy, the page shows the reason and a 再接続 button (`/api/connect`);
it is never retried automatically.

### Screen

- 10 × 8 div grid, CH1 yellow, CH2 cyan. Left-edge markers = 0 V (or 0 A) of
  each channel; the orange mark at the top = trigger time; the dashed line and
  right-edge marker = trigger level.
- Modes: **Auto** (free-runs after 0.25 s without a trigger), **Normal**,
  **Single** (one capture, then Stop), **Roll** (scan-shift, scrolling window
  1–30 s, no trigger; for watching a rail while pressing the power button).
- Timebase 1 µs/div … 1 s/div. The sample rate is set so the 10 div hold up to
  the device buffer (8192 samples on the AD Studio, max 100 MS/s).
- Per channel: on/off, name, V/div (0.01–5), 中心 (V at the screen centre =
  SDK channel offset), シャント Ω (> 0 shows amps = V / R). The SDK picks the
  hardware range covering 8 div: the AD Studio has a ~5.5 V span and a ~60 V
  span, so at ≤ 0.5 V/div only centre ± 2.75 V is measured. **範囲外** in the
  measurement box means the input hit the range edge (raise V/div or move the
  centre). 入力安定待ち… is shown for 2 s after a range / offset change (SDK:
  the input offset needs ~2 s to settle).
- Measurements over the displayed record (full resolution, not the decimated
  trace): mean, min, max, p-p, RMS.
- 現在の波形を保存 writes the current record; Record N 秒 runs an
  `acqmodeRecord` capture (adbench `record()`, includes its 2 s settle wait) at
  the chosen rate, saves it, and returns to the live mode. Both go to
  `<data>/runs/<timestamp>-scope/` as `frame|record.csv`, `.png`, `summary.json`.
- Names and settings are kept in the browser (localStorage) and re-applied
  when the page is opened (except the mode).

### HTTP API

| Method / path | Body | Returns |
|---|---|---|
| `GET /api/status` | | device (name, SN, connected, error), mode, running, state, settings, applied (rate, samples, hardware range/offset) |
| `GET /api/frame` | | settings + per channel ≤ 2000 points (min/max decimated, display units) + measurements; `x0`/`x1` = time of the first/last point (s, 0 = trigger, Roll: 0 = newest); `screen` = visible time span; `trig` = `trig` / `auto` / `roll` |
| `GET /api/measure` | | measurements only, with channel names and units |
| `POST /api/config` | partial settings | merged settings (400 on bad values) |
| `POST /api/run` / `stop` / `single` | | `{"ok": true}` |
| `POST /api/save` | | output dir + summary |
| `POST /api/record` | `{"seconds": 2, "rate": 200000}` | blocks until done; output dir + summary (per channel min / max / mean in V, plus A and `max_10ms_avg` when `shunt_ohm` > 0) |
| `POST /api/connect` | | retries opening the device |
| `POST /api/quit` | | stops the server and closes the device |

Settings (all keys optional in `/api/config`; `channels` may be a list of two
objects or `{"0": {...}, "1": {...}}`):

```json
{"mode": "auto|normal|single|roll", "tdiv": 0.001, "roll_s": 10,
 "channels": [{"enabled": true, "name": "VSYS", "vdiv": 0.5, "offset": 4.0, "shunt_ohm": 0}, {...}],
 "trigger": {"source": 0, "edge": "rise|fall", "level": 2.0, "pre": 50}}
```

`trigger.level` is in input volts; `pre` is the pre-trigger part of the screen (%).

Voice marks and guided probing:

| Method / path | Body | Returns |
|---|---|---|
| `POST /api/mark` | `{"text": "当てた", "kind": "voice", "step": "vsys"}` (`step` optional) | the mark: id, time, text, kind, step, and per channel `now` / mean / min / max at that moment; also appended to `runs/marks.jsonl` |
| `GET /api/marks?since=<id>` | | marks after `id` |
| `POST /api/say` | `{"text": "プローブを当ててください"}` | `{id, text, time}`; the page shows it as a banner and reads it aloud (読み上げ ON). `GET /api/frame` carries the latest as `say` |
| `GET /api/procedures` | | `procedures/*.json`: name, title, board, steps, `problems` (validation errors in Japanese) |
| `GET /api/procedures/<name>` | | the validated procedure with every probe resolved (`ref`, `pad`, `net`, `side`, `px`), the board `sides`, and `problems` |
| `POST /api/procedure/state` | from the page | stores progress; a `result` / end is appended to `runs/<timestamp>-proc-<name>/results.jsonl` |
| `GET /api/procedure/state` | | procedure, `step` (current id), `say`, `results` so far, `done`, `conclusion`, `run_dir`, `event` |
| `GET /boards/<board>/<file>` | | `.jpg` / `.png` / `.json` under `boards/` only |

## 手順モード（guided probing）

For a person who holds the probe and the board with both hands: the page walks
through a procedure, shows where to put the probe on the board picture, records
the reading when the person says 「当てた」, judges it and moves on.

- Open the **手順** tab (the side panel widens: board picture + current step),
  pick a procedure, press **開始**. Turn the mic on (🎤 音声).
- Words: 「当てた」「OK」「次」「はい」 = record / yes; 「NG」「だめ」「いいえ」 = no
  (only on yes/no steps); 「戻る」 = back one step (its result is discarded);
  「やり直し」 = measure the last recorded step again with the readings now and
  re-judge it. The buttons under the picture do the same.
- The reading recorded is each channel's `now` (newest value) in the voice mark,
  so the mark in `runs/marks.jsonl` carries the same numbers and the step id.
- Rings: CH1 yellow, CH2 cyan, GND clip white, tester pink; the previous step's
  points are drawn faintly. Click the picture to zoom 2× around the current
  probe (stays zoomed across steps; click again to go back). The picture
  switches to top / bottom by the step. Bottom = the board seen turned over.
- 🔈 読み上げ reads each instruction and each agent prompt (`POST /api/say`) aloud
  (ja-JP); OFF until pressed once, then kept in the browser. While it talks,
  recognised speech is ignored.
- Marks posted by someone else (the agent: `POST /api/mark {"text": "当てた"}`)
  drive the running procedure too, unless they name another `step`.
- A procedure with problems is listed with ⚠ and cannot be started; the page
  shows the problems under the picker.

## エージェントの計測で人がプローブを持つとき（guide.py）

The person cannot do anything else while holding the probes, so an agent script
must not leave them waiting blind: `guide.probe(prompt, ready)` says the prompt
(「…に当ててください」), watches the live readings until `ready` holds for 0.5 s,
then says 「計測中」; `guide.done()` says 「計測終わり」. Keep the part between
the two short. A record started right after `probe()` skips the 2 s input
settle when the live view already uses the same ranges/offsets.

The agent follows a run with `GET /api/procedure/state` (or by tailing
`runs/*-proc-<name>/results.jsonl`: one line per recorded step with `step`,
`kind`, `pass`, `values` {ch1, ch2}, `limits`, `answer`, `probes`, `mark`, and
a last line `{"event": "end" | "abort", "conclusion": ...}`).

### 新しい基板・新しい手順を追加する

1. Board map (once per board revision), with KiCad 10 installed:

   ```
   python boardmap.py <path>\board.kicad_pcb <name> --data <folder>   # -> <folder>/boards/<name>/top.jpg, bottom.jpg, pads.json
   ```

   `pads.json` lists every pad: `ref`, `pad`, `net`, `value`, `side`, `tht`,
   and `px.top` / `px.bottom` (pixel in the picture; bottom is mirrored as seen
   from below). The board file is only read.
2. Write `procedures/<new_name>.json` in the data folder (name:
   `[A-Za-z0-9_-]`, schema below), set `board` and the steps. Nothing in `adscope.py` / `index.html` is board- or
   procedure-specific.
3. Reload the page (no server restart): the picker shows ⚠ and the problems if
   something does not match the board.

Schema (`version` 1):

```jsonc
{
 "version": 1,
 "title": "基板 A: 電源が入らない個体の切り分け",
 "board": "board_a",                // folder under <data>/boards/
 "start": "gnd",                    // optional; default = first step
 "steps": [
  {"id": "vsys",                    // unique
   "action": "power_on",            // connect_gnd | power_on | power_off | probe | manual (display + default text)
   "say": "…「当てた」",              // instruction; optional except for manual (a default is made from action + probes)
   "side": "top",                   // optional; default = side of the first probe (no probe: keep the last side)
   "probes": [{"ch": 1, "ref": "C21", "pad": "1", "net": "VSYS_4V", "label": "VSYS"}],
   "expect": {"ch1": [3.0, 4.4]},   // optional
   "next": "v3v3",                  // pass
   "else": "batt"}                  // fail
 ]
}
```

- Probe `ch`: `1`, `2`, `"gnd"` (the clip), `"meter"` (a multimeter, for manual
  steps). Give the point as `ref` + `pad` (add `net` to have it checked against
  the board), or as `net` alone (+ optional `side`): the server picks a pad of
  that net on that side, preferring a passive (C / R / L / FB), so the step
  keeps working across board revisions. `label` defaults to the net name.
- One probe at a time: attach the GND clip once in its own `connect_gnd` step
  and move only CH1 afterwards. Use CH2 only when two points must be seen at the
  same moment, and say so in `say`.
- How a step is judged: with `expect` it is a measurement (「当てた」records,
  every rule must hold); without `expect` but with `else` it is a yes/no
  question (「OK」 → `next`, 「NG」 → `else`); otherwise a confirmation
  (「OK」 → `next`). A failed measurement without `else` still goes to `next`
  (shown ✘).
- `expect` rule per channel (`ch1` / `ch2`, volts — amps when the channel has
  a shunt): `[low, high]`, or `{"near": "<step id>", "tol": 0.3}` = within ±tol
  of what the same channel read at that earlier step.
- `next` / `else`: a step id, or `{"end": "結論の文"}` to finish with that
  conclusion.

```
curl -s http://127.0.0.1:8195/api/measure
curl -s -X POST http://127.0.0.1:8195/api/config -d '{"channels":{"0":{"name":"VSYS","vdiv":1,"offset":2.5},"1":{"name":"3V3","vdiv":1,"offset":2.5}}}'
curl -s -X POST http://127.0.0.1:8195/api/config -d '{"mode":"roll","roll_s":10}'
curl -s -X POST http://127.0.0.1:8195/api/config -d '{"mode":"normal","tdiv":0.001,"trigger":{"source":0,"edge":"rise","level":2.0,"pre":20}}'
curl -s -X POST http://127.0.0.1:8195/api/single
curl -s -X POST http://127.0.0.1:8195/api/save
curl -s -X POST http://127.0.0.1:8195/api/record -d '{"seconds":2,"rate":200000}'
```

## Notes for agents

- Run the scripts from anywhere as `python <dev-tools>/ad-bench/adscope.py --data <folder>` (same for `adbench.py`, `boardmap.py`); `<folder>` is the project's data folder, never this repo.
- Only one program can open the device: stop adscope (`POST /api/quit`) before running `adbench.py`, and ask the person to close the WaveForms GUI.
- adscope and adbench only read inputs, except `adbench.py selftest` / `impedance` (drive W1) and `record --tcp` (sends one line to the device under test). Do not send anything to a device unless the person asked for that measurement.
- When a person holds the probes, guide them by voice with `guide.py` (`guide.probe(...)` → measure → `guide.done()`), finish all settings and connection checks before the first prompt, and record only as long as needed.
- Copy conclusions from `runs/` into the project's notes; `runs/` itself is not meant for git.
