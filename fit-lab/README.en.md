# fit-lab

[日本語](README.md)

A small local web tool for recording **CAD design values vs. measured values** of 3D-printed parts, per print lot (campaign) and per clearance variant, with annotated screenshots. Data is plain JSON + PNG so it can live in git, be diffed, and be read or written by AI agents. Over many lots it becomes a knowledge base ("PA12 holes print 0.1–0.2 mm small on diameter").

- No dependencies: Python 3.9+ standard library for the server, plain HTML/JS/CSS for the app (no build step, no CDN, works offline).
- `serve.py` binds to `127.0.0.1` by default (`--host` to change it) and refuses requests from other sites' pages (Safety under "HTTP API").
- The data (campaigns) is not part of this repo: keep it in the workspace of the project that uses it and pass its folder with `--root` (or `FIT_LAB_ROOT`).

## Two ways to run

The app auto-detects its data source at start-up: if `/api/campaigns` answers it uses **server mode**, otherwise it shows **folder mode**. The current mode is shown in the top bar (サーバー / フォルダ / 未接続).

### 1. Server mode (`serve.py`)

```bash
python fit-lab/serve.py --root <data-root>
```

Then open <http://127.0.0.1:8765/>.

| option | default | meaning |
|---|---|---|
| `--root` | `$FIT_LAB_ROOT` | data root (folder of campaigns), relative to the current directory; required when the variable is not set |
| `--port` | `8765` | port |
| `--host` | `127.0.0.1` | bind address. A specific address other than `127.0.0.1` / `localhost` makes the app reachable from other computers (that `Host` name is accepted too); with `0.0.0.0` only the names `127.0.0.1` / `localhost` are answered |

Try it with the bundled sample (fake data):

```bash
python fit-lab/serve.py --root fit-lab/sample --port 8766
```

Saving from the UI rewrites the sample files; use `git checkout fit-lab/sample` to reset.

### 2. Folder mode (static page, no server)

`index.html`, `app.js` and `style.css` are a plain static site: they can be served by any static host (or `python -m http.server`) with no backend. In that case the start page offers **フォルダを開く…**, which uses the [File System Access API](https://developer.mozilla.org/en-US/docs/Web/API/File_System_API) (Chrome / Edge, secure context: `https://` or `localhost`). Pick either

- a data root (a folder whose sub-folders each contain `campaign.json`), or
- a single campaign folder.

Campaigns, images and saves then go directly to that folder on the user's disk. Saving copies the previous `campaign.json` to `campaign.json.bak` first, then writes the new file (the browser writes to a temporary file and swaps it in on close). The last folder is remembered in IndexedDB of that browser and can be re-opened with one click (the browser asks for permission again). Folder mode is also available from the start page in server mode. Firefox / Safari do not support the API; use server mode there.

## Using the app

UI labels are Japanese; the data keeps English keys (`kind: "hole"` → 穴, `method: "caliper"` → ノギス, `by: "user"` → 本人).

- **Campaigns** — one card per campaign with measured / total cells.
- **項目 (Items)** — one row per item, one column group per variant: `CAD | 実測 | Δ` where **Δ = measured − CAD**. Δ is coloured blue (smaller than CAD) / red (larger), darker with magnitude (±0.02 neutral, then 0.05 / 0.1 / 0.2 steps). Items whose CAD is shared by all variants (`"*"`) show one group spanning the variant columns.
  - **Measured cells are inputs.** Focusing an empty cell pre-fills the CAD value; ↑ / ↓ (or the mouse wheel while the cell is focused) step it by the toolbar's 刻み (0.01 / 0.05 / 0.1, remembered; Shift = ×10). Typing works too. **Enter** records a new measurement (today, ノギス, by `user`, not provisional) and moves down the same column, **Tab** / Shift+Tab record and move to the next / previous cell, **Esc** cancels. Nothing is recorded unless the value was changed. Values show 2 decimals.
  - Double-click a measured cell for its history (edit date / method / note / provisional, delete entries). The cell shows the latest value (by date, then insertion order).
  - Provisional values (`provisional: true`, e.g. read from a voice memo, not yet confirmed) are shown in yellow italics, with a tooltip and a legend entry.
  - Click a row to highlight its callout in the image panel (switching image if needed); click a callout to select its row.
  - Layout: drag the bar between the table and the image panel (default image panel ≈ 33 %), drag header borders to resize columns; double-click either to reset. Widths are remembered per browser (localStorage). Below 1000 px the image panel stacks under the table. Images are scaled to fit the panel.
  - The variants and their parameters are in a collapsible バリアント section below the table and on the summary tab.
  - **保存** (or Ctrl+S) writes the whole campaign. The status label shows unsaved changes; leaving the page with unsaved edits asks first.
  - **CSV 出力** — long format, one row per measurement (plus empty rows for unmeasured cells), UTF-8 with BOM for Excel.
- **集計・バリアント (Summary)** — n / mean / sd / min / max of Δ by kind, broken down by variant, plus the variant parameter table and the campaign `notes`. Uses the latest measurement per item × variant; a toggle excludes provisional values.
- **知見 (Knowledge base, `#/summary`)** — the same statistics over **all** campaigns, grouped by material + kind, with per-campaign rows. **全件 CSV 出力** dumps every campaign.
- Theme: follows the OS by default; the top-right button cycles 自動 / ライト / ダーク. All text colours meet WCAG AA (≥ 4.5:1) in both themes.

## Voice entry (hands-free)

Press **音声入力** in the toolbar and read values aloud while measuring; no mouse or keyboard needed. It uses the browser's Web Speech API (`SpeechRecognition`, `ja-JP`, continuous) and works in **Chrome / Edge** on `localhost` or `https` (the microphone needs a secure context). Other browsers show a message instead.

> **Privacy:** Chrome and Edge send the microphone audio to the browser vendor's cloud speech service (Google / Microsoft) for recognition. Do not use voice entry for anything that must not leave the machine.

Context: the **current variant** (toolbar バリアント, or by voice) and a **cursor row** (highlighted with a blue bar). Items whose CAD is shared (`"*"`) are always recorded as `*`.

| say | action |
|---|---|
| 「1番 8.45」「いちばん はってんよんご」「3番 2.3」 | record item 1 (3) for the current variant; the cursor moves to the next item |
| 「8.45」「はちてんよんご」 | record the cursor row, then move the cursor to the next item |
| 「5番」 | move the cursor to item 5 |
| 「次」「前」 | move the cursor |
| 「ブイ1」「V2」「バリアント3」「共通」 | switch the current variant |
| 「はい」「OK」 | accept a value waiting for confirmation |
| 「取り消し」「戻して」 | undo the last voice entry (or drop a pending confirmation) |
| 「保存」 | save now |

- Numbers may be digits (also full-width), kanji (八点四五) or kana (はってんよんご, じゅうに, れい / ゼロ); 点 / てん is the decimal point. Fillers (えーと, ミリ, は / の) are ignored.
- **Safety:** a value is applied only when |value − CAD| ≤ max(1.0 mm, 15 % of CAD). Otherwise (or when the item has no CAD) the live bar shows 「確認: 1番 84.50 ?」; say 「はい」 to record it or say a new value to replace it.
- Feedback: a fixed-height live bar shows what is being heard; the changed row flashes; the voice log (time, raw transcript, action) lists every entry with a 取消 button for per-entry undo; 読み上げ reads the recorded value back via `speechSynthesis` (off by default; recognition ignores the read-back).
- While voice entry is on, changes are saved automatically ~2 s after the last edit. Voice entries are stored with `note: "音声入力"`.
- **Browsers:** recognition only works in **Google Chrome** (Google's service) and **Microsoft Edge** (Microsoft's service). Other Chromium browsers (Vivaldi, Brave, Opera, …) expose `webkitSpeechRecognition` but cannot reach a speech service: the microphone indicator turns on, yet no text ever arrives (sessions end at once, or fail with `network`). Firefox and Safari on desktop have no usable engine. Open fit-lab in Chrome or Edge for voice entry.
- **音声の診断** (collapsible panel under the toolbar) logs every recognition event with a timestamp (start, audiostart, soundstart, speechstart, interim / final results, nomatch, error, end, restarts), shows the engine (`SpeechRecognition` / `webkitSpeechRecognition`), the detected browser, the input device and a live microphone level meter (`getUserMedia` + `AnalyserNode`; nothing is played back). Errors such as `network`, `not-allowed`, `service-not-allowed`, `language-not-supported`, `audio-capture` are explained in plain Japanese. **診断をコピー** copies the whole log for a bug report.
- Web Speech always uses the OS / browser default microphone; a page cannot choose the device. Change it in Windows Settings > System > Sound > Input, or in the browser's site settings for the microphone.
- Restart policy: after a normal session end the recogniser restarts after 300 ms; if sessions end within 2 s without hearing anything, restarts back off (1, 2, 4, 8 s) and voice entry stops after 5 in a row with an explanation. `not-allowed`, `service-not-allowed`, `language-not-supported`, `audio-capture` and a third `network` error stop it at once. `start()` is never called while a session is running.
- The parser is `voice-parse.js` (no DOM). Unit tests: `node --test fit-lab/voice-parse.test.js`. The microphone path itself can only be tested by a person.

## Data layout

```
<data-root>/
  <campaign-id>/
    campaign.json
    campaign.json.bak      # previous version, written by the server on save
    images/
      img01.png
```

`<campaign-id>` must match `^[A-Za-z0-9][A-Za-z0-9._-]*$` and equal the `id` field.

## Data contract — `campaign.json` (`fit-lab/1`)

```jsonc
{
  "schema": "fit-lab/1",                 // required, exact string
  "id": "demo-pa12-lot1",                // required, = folder name
  "title": "Demo case — PA12 lot 1",
  "project": "demo",
  "material": "PA12 Black",             // grouping key of the knowledge base: keep spelling consistent
  "process": "MJF/SLS",
  "vendor": "…",
  "ordered": "2026-09-01",               // ISO dates
  "received": "2026-09-10",
  "notes": "…",                          // optional free text about the lot

  "variants": [                          // clearance variants printed in this lot; may be empty
    { "id": "V1", "label": "tight", "params": { "fit_magnet_allowance": 0.30 } }
  ],

  "images": [                            // annotated screenshots; width/height in pixels of the file
    { "id": "img01", "file": "images/img01.png", "caption": "…", "width": 1600, "height": 1200 }
  ],

  "items": [
    {
      "no": 1,                           // unique integer, shown on the callout
      "name": "磁石穴の幅 (8 mm 方向)",
      "part": "upper case",
      "feature": "pocket_magnet",        // CAD feature / body name (free text)
      "kind": "hole",                    // "hole" | "boss" | "gap" | "height" | "other"
      "direction": "X",
      "unit": "mm",
      "cad": { "V1": 8.30, "V2": 8.35, "V3": 8.40 },   // per variant, or { "*": 26.78 } = same for all variants
      "mate": { "name": "magnet 8x4x2", "value": 8.0 }, // optional: the mating part's size
      "image": "img01",                  // optional: images[].id
      "annotation": {                    // optional; coordinates normalised 0..1 of the image (x right, y down)
        "type": "dim",                   // "dim": two points, dimension line with arrows
        "points": [[0.41, 0.52], [0.58, 0.52]],
        "label_pos": [0.5, 0.47]         // optional: where the numbered badge goes (default: beside the line)
      },
      // or { "type": "point", "points": [[x, y]], "label_pos": [x, y] }  → dot + leader + badge
      "note": "…",
      "measurements": [
        {
          "variant": "V1",               // a variants[].id, or "*" for items whose cad is { "*": … }
          "value": 8.20,
          "date": "2026-09-12",
          "by": "user",
          "method": "caliper",
          "note": "…",                   // optional
          "provisional": true            // optional; omit when false
        }
      ]
    }
  ]
}
```

Rules:

- **Δ = measured − CAD**, in `unit`. For a hole, negative Δ means it printed smaller; for a boss, positive means fatter.
- Items with `cad: {"*": v}` are measured once (`variant: "*"`), not per variant.
- `measurements` is an append-only history; the latest per variant (by `date`, ties by array order) is the current value.
- Unknown extra fields are preserved on save (the app round-trips the whole object).
- The server writes pretty-printed UTF-8 (`indent=2`, non-ASCII kept), atomically, after copying the previous file to `campaign.json.bak`.

## HTTP API

| method | path | |
|---|---|---|
| GET | `/api/campaigns` | `{root, campaigns: [{id, title, material, process, vendor, ordered, received, variants, items, cells, measured}]}` |
| GET | `/api/campaigns/<id>` | `campaign.json` |
| PUT | `/api/campaigns/<id>` | replace `campaign.json`; body must have `schema: "fit-lab/1"`, `id` = `<id>`, `items` list. Creates the folder if missing. |
| GET | `/data/<id>/<path>` | files inside a campaign folder (images) |

Safety:

- 403 for: a `Host` other than this server's names (`127.0.0.1:<port>` / `localhost:<port>`, a specific `--host`) (DNS rebinding); an `Origin` other than this server (another site's page, `null` too); requests the browser marks `Sec-Fetch-Site: cross-site` / `same-site` (another site may only open the app by a link — no `<img>`, `<script>`, iframe or `fetch`); a PUT with neither `Origin` nor our own `Referer`.
- Clients without a browser (curl, scripts) send `Origin: http://127.0.0.1:<port>` on PUTs; read-only GETs need no header. No CORS headers; pages send `X-Frame-Options: SAMEORIGIN`.
- Tests: `python -m unittest test_serve` in the `fit-lab` folder.

## For AI agents: adding a campaign

1. Create `<data-root>/<campaign-id>/` (the data root is the folder passed to `serve.py --root`, kept in the project's workspace, not in this repo). Use a descriptive, lowercase id such as `<product>-<material>-lot<N>`.
2. Put the screenshots in `images/` as PNG. Record each in `images[]` with its real pixel `width` / `height`.
3. Write `campaign.json` exactly per the contract above: one item per measured dimension, CAD values per variant (or `"*"`), `kind` from the fixed list, `mate` when a mating part defines the fit. Use the same `material` string as earlier lots of the same material so the knowledge base groups them.
4. Annotations: give coordinates as fractions of the image size (pixel x / width, pixel y / height). For a dimension, the two points are the ends of the measured span; for a point feature, the point itself. Set `label_pos` when the default badge position would cover something.
5. Leave `measurements` empty (`[]`) for values the human will measure; append entries (never rewrite old ones) when recording results reported in chat.
6. Validate: `python -c "import json,sys; json.load(open(sys.argv[1], encoding='utf-8'))" <path>/campaign.json`, then open the campaign in the app and check every callout lands on its feature.
7. When the server is running you may instead `PUT /api/campaigns/<id>` with the JSON (send `Origin: http://127.0.0.1:<port>`); it validates the schema/id and keeps a `.bak`.

## Notes for agents

- Ask which data root to use (or read it from the project's notes); never write campaigns into this repo except the bundled `sample/`.
- Start the server where the person can see it (a terminal tab) and open the URL in the default browser.
- `PUT /api/campaigns/<id>` replaces the whole file: read it first, append measurements, write it back. The server keeps one `.bak`.
- Voice entry sends microphone audio to the browser vendor's cloud (see Privacy above).
