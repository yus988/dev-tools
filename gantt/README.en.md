# gantt (Agent Gantt)

[日本語](README.md)

A small Gantt chart tool. An agent writes a JSON file, a person adjusts it in a desktop window by dragging bars and editing fields, and the same renderer writes a static SVG / PNG / PDF (to embed in an HTML report, or to hand to people who do not have the app). The person's edits are saved to the file at once and leave a change history for the agent. The desktop window is called "Agent Gantt" (title bar `Agent Gantt — <data title>`) and switches between several charts (`*.gantt.json`) in one window.

- Requirements: `gantt.py` uses the Python 3.9+ standard library only, except the desktop window (`app` / `selftest`), which needs [pywebview](https://pywebview.flowrl.com/) (on Windows it uses Edge WebView2; install it separately). `png` / `pdf` use Edge or Chrome
- The UI is one HTML file (`app.html`, no CDN), the same in the desktop window and in a browser (`serve`)
- Project data is not kept in this repo: put `*.gantt.json` in the workspace that uses it and pass the path. Only a neutral sample (`sample.gantt.json`) lives here

## Commands

```
python gantt.py app [FILE] [--day N]                 desktop window (without FILE: the last file)
python gantt.py serve FILE [--port 8195] [--host H]  the same UI in a browser (http://127.0.0.1:8195/)
python gantt.py svg FILE [-o OUT] [--day N] [--no-legend]
python gantt.py png FILE [-o OUT] [--day N] [--scale 2]
python gantt.py pdf FILE [-o OUT] [--day N]          one page sized to the chart
python gantt.py share FILE                           write the files listed in the data's "share"
python gantt.py embed FILE PAGE [--day N]            put the SVG between <!-- gantt:begin --> and <!-- gantt:end --> in an HTML page
python gantt.py changes FILE [--peek] [--all]        what the person changed (that the agent has not read yet)
python gantt.py check FILE                           validate
python gantt.py selftest FILE [--write]              open the window hidden and drive it (--write: renaming a person too)
```

- `app` without FILE opens the file opened last; with no history it shows pywebview's open-file dialog (`*.gantt.json`). The recent files and the window's settings are kept in `~/.gantt/last.json` (the window has no localStorage). The window's own state goes to `~/.gantt/webview` (outside AppData on purpose: packaged shells redirect AppData)

| `last.json` key | Meaning |
|---|---|
| `file` | The file opened last (absolute path) |
| `recent` | Recent files, newest first, up to 8 (files no longer there drop out at the next open) |
| `day` / `rows` | The day width and the row scale (0.7–3.0, default 1) chosen by hand |
| `text` | The text scale (0.8–2.0, default 1.3). The first version's `scale` is read as this |
| `label_width` | The title column's width (px at text scale 1, 120–1200); `null` means the data's `label_width` |
| `fit` | "全体表示" (fit to the window) on / off (default on) |
| `by_owner` | The "担当ごと" (by person) view rather than "項目順" (item order) |

- Environment for the desktop window (Windows): `python -m venv ~/.gantt/venv`, `~/.gantt/venv/Scripts/pip install pywebview`. To start without a console: `pythonw.exe gantt.py app FILE`
- `svg` / `png` / `pdf` write `<name>.svg` etc. beside the data file when OUT is omitted (`.gantt` is dropped from the name). PNG and PDF are made by headless Edge / Chrome from a white page with the title and "更新 YYYY-MM-DD HH:MM" above the chart. The browser is `$GANTT_BROWSER`, else Edge, else Chrome in their standard places; if none is found or it fails the command says why and exits 1. PDF text stays text
- `embed` replaces only what is between the markers (everything else is byte-for-byte unchanged); without the markers it does nothing and exits 1. Run it every time the report is rebuilt

## Data (`*.gantt.json`, UTF-8)

The file is the single source of truth, read and written by the agent and by the window alike. The window applies each change as "read the file as it is now, apply the operation, write" (under a lock), so an edit the agent made meanwhile is not lost.

```json
{
 "title": "Sample plan", "from": "2026-11-02", "to": "2027-01-10",
 "link_base": "http://example.localhost/",
 "legend": {"plan": "Work"},
 "off": [{"from": "2026-12-28", "to": "2027-01-04", "label": "Holidays"}],
 "lines": [{"on": "2026-11-20", "label": "Spec freeze"}],
 "rows": [
  {"id": "g1", "group": "Deadlines"},
  {"id": "client", "title": "Client", "owner": "A", "link": "plans/client.html", "note": "free text", "items": [
    {"on": "2026-11-09", "label": "Scope", "kind": "ext"},
    {"from": "2026-12-28", "to": "2027-01-04", "label": "Holidays", "kind": "off"}]}
 ]
}
```

| Key | Meaning |
|---|---|
| `title` / `from` / `to` | Title, and the range shown (inclusive). Week headers start on Mondays |
| `link_base` | Base URL for relative row `link`s; without it, the data file's folder (`file:`). Anything but `http` / `https` / `file` is ignored |
| `legend` | Overrides of the kind names in the legend (the defaults are Japanese) |
| `off` / `lines` | Off ranges (light shade and a name), vertical dashed lines (dates) |
| `day` | Pixels per day for exported pictures (6–40, default 14); `--day` wins. The window opens at this width unless it remembers one chosen by hand |
| `label_width` | Width of the title column (default 240 px). The owner column (80 px) is left out when no row has an owner |
| `rows` | In display order. A row with `group` is a group header (no items). Every row has a unique `id` (rows without one get `r1`, `r2`, … on load, written at the next save) |
| items | Bar = `from` and `to` (inclusive, `from <= to`); milestone = `on`. `label`, `note`, `kind` (table below; unknown → `plan`) |
| `share` | Absolute paths of the files to write for sharing (see "Sharing") |
| `log` / `seen` | Change history written by the window (newest 500) and the newest log time the agent has read |

Unknown keys are kept as they are. Saving writes 1-space indented JSON with every element of `items` / `off` / `lines` / `log` and every row without items on one line (readable git diffs).

| `kind` | Default name | Look |
|---|---|---|
| `plan` | 作業 | blue fill |
| `tbd` | 要相談 | blue hatch, dashed outline |
| `due` | 締め切り・発注 | orange, bold |
| `ext` | 外の期日 | red, bold |
| `wait` | 待ち | vertical stripes |
| `off` | 休み | grey fill, no outline |
| `done` | 済み | green |

All colours are CSS custom properties (`--rec`, `--recs`, `--warn`, …) with light fallbacks: embedded in a page, the chart follows that page's theme (dark too); a standalone SVG / PNG / PDF uses the fallbacks.

## In the window

The toolbar is one row, 46 px high; it fits a 1280 px window, and when space runs short the legend folds into a "凡例" button. Buttons have one size and nothing moves when a state changes. Messages such as "保存 HH:MM" float over the chart's lower right corner.

| Button / field | What it does |
|---|---|
| ファイル (file) | Recent files (up to 8, the current one ticked), "ファイルの選択…" (open-file dialog), and "共有用に書き出し" when the data has `share`. Choosing a file switches the window to it without a restart (search and person filter are cleared; undo is per file). Not in `serve` (a browser has no file dialog, and serve shows one file) |
| 行の追加 / 区切り追加 | Add a row / group header below the row open in the drawer (else at the end) |
| 取り消し | Undo (Ctrl+Z) |
| 期間 − / ＋ | Horizontal zoom: the day width only (steps of 6–40 px per day). Ctrl + wheel zooms smoothly and keeps the date under the mouse in place |
| 行 − / ＋ | Vertical zoom: the row, group and strip heights (0.7–3.0). Alt + wheel zooms smoothly and keeps the row under the mouse in place. Rows never get lower than the text (row height = the larger of the row and text scales) |
| 文字 − / ＋ | The text size, with the title and owner columns and the date header, in steps of about 10 % (0.8–2.0; the window's default is 1.3). Ctrl + plus / minus do the same |
| 全体表示 (fit) | Fit to the window (on by default): the day width (any value 6–40) makes the whole range fill the width and the row scale (0.7–3.0) makes all rows fill the height. The text size is left alone, so where rows would get lower than the text, the chart scrolls. Refitted when the window is resized. Changing 期間 or 行 turns it off (文字 does not); turning it off goes back to the sizes chosen by hand |
| 今日 | Scroll to today |
| 検索 (search) | Shows only the rows whose title, owner or note, or a bar's / milestone's name or note, contain the text (ignoring case and full / half width). Group headers stay when a row under them stays; a header whose name matches keeps all its rows. Matching items get a yellow marker; the box shows the number of rows. Ctrl+F focuses it, Esc or × clears it |
| 担当 (person) | Shows only one person's rows (combines with the search). People are listed by number of rows, "未定" (rows without an owner) last, each with its row count, e.g. "アルバイト（3 件）". The list starts with how people are told apart ("People" below) |
| 項目順 / 担当ごと | The view switch (the one in use is filled). "項目順": the data's order. "担当ごと": rows under a header per person, in data order ("People" below). A view only: the data's order is not changed. Dragging and the drawer work in both |
| 凡例 (legend) | The kind colours; folded into a button when there is no room (hover or click to show) |

The chart:

- The date header (months, weeks, the strip with off and line names) stays at the top and the title and owner columns stay at the left while scrolling (frozen panes, as in a spreadsheet)
- Drag the line between the title columns and the timeline to change the title column's width (at least 120 px; double-click for the data's width). It is a window setting (`label_width` in `last.json`), not written to the data
- Hovering a bar or milestone shows its details: the row title, the name, the dates with the number of days (e.g. `11/2（月）〜11/13（金）・12 日（平日 10 日）`, 平日 = weekdays), the kind, the owner and the note. Hovering a row title shows the owner, the note and the link target. Not while dragging or scrolling
- Drag a bar's body to move it (whole days); drag within 5 px of an end to resize (minimum 1 day). The dates being set are shown while dragging. Releasing without moving a day is a click (opens the drawer on the right)
- Double-click an empty part of a row to add a 7-day "新しい作業" from that day
- Click a row title for the drawer (title, owner, link, note; name, kind and dates of each bar / milestone; move, add and delete the row). Fields are saved on commit (`change`). Esc or ✕ closes it
- Ctrl+Z undoes (inside a field: undoes typing). Delete removes the selected item (with a confirmation)
- When the file changes outside, the window reloads within a second and shows "外で更新 HH:MM"; an operation that collides with an outside change reloads instead
- Undo reverts the operations this window wrote, newest first, and refuses when the file was rewritten elsewhere since
- In `serve` (a browser), "共有用に書き出し" is the toolbar's "書き出し" button (only when the data has `share`); the settings are kept in the server and in localStorage

### People (担当)

- How rows are grouped: the owner text is split into people. Text in （） / () does not count; `・` `、` `,` `/` and spaces separate people; a trailing さん is dropped; the same name is the same person (write the same name on another row and it counts as that person's row). `田中さん・佐藤（要相談）` → 田中, 佐藤; `佐藤（A-1 候補）` → 佐藤
- The count: "アルバイト（3 件）" is the number of rows with that name in their owner text
- One row for several people: write `アルバイトA・アルバイトB` as its owner. The row counts for both and shows under both headers in "担当ごと"
- Renaming: in "担当ごと", click a person's header; the drawer offers "名前の変更". It replaces the name in every row's owner text (the other people and the bracketed text stay: renaming アルバイト to 田中 turns `アルバイト（未定）` into `田中（未定）`; renaming アルバイト to 佐藤 turns `田中・アルバイト` into `田中・佐藤`). All rows change in one operation, so one 取り消し undoes it; the log names the rows (`担当の名前「アルバイト」→「田中」: 「…」、「…」（2 行）`). To split one person into two, rename to `アルバイトA・アルバイトB`
- A single row's owner is edited in that row's drawer (it suggests the people already there)
- "未定" holds the rows with an empty owner (and rows that say 未定); renaming does not change empty owners

## Rendering (`render`)

`render(data, day=14, interactive=False, legend=False, today=None, base_dir=None, zoom=False, text_scale=1.0, row_scale=1.0, label_width=None, query=None, owner=None, by_owner=False)` returns the SVG string. `svg` / `png` / `pdf` / `embed` / `share` do not use `text_scale` and after, so their output is byte-for-byte what it was.

| Argument | Meaning |
|---|---|
| `day` | Pixels per day (6–40, fractions to 0.01 allowed) |
| `text_scale` | The text scale (0.8–2.0, to 0.01): text, the date header's height, the horizontal bar insets, the title and owner column widths and the text-fitting widths |
| `row_scale` | The row scale (0.7–3.0, to 0.01): row, group and strip heights and the bar height. Below `text_scale`, `text_scale` is used (rows never get lower than their text) |
| `label_width` | The title column's width (px at text scale 1, 120–1200), over the data's `label_width` |
| `query` | Search (the rules above). A matching item gets a `class="qh"` marker behind it (colour `--gantt-find`) |
| `owner` | Only that person's rows (`未定`: rows without an owner) |
| `by_owner` | Rows under a header per person |

Filtered or regrouped, the rows are drawn without gaps and `data-r` (the row `id`) and `data-i` (the item index) stay the data's own, which is what dragging and editing in the window use. An `interactive` SVG carries `data-hh` (the header and strip height) and `data-tc` (the title column width), with which the window freezes the header and the columns, and has no `<title>` tooltips (the window shows its own details). Also available: `view_rows` (the rows shown), `people` / `owner_parts` (owner text → people), `rename_owner` (one person's new name in an owner text), `owners` / `members` (people with row counts / row ids), `fit_view` (the day width and row scale that fill a window).

## For agents: reading the person's edits

```
python gantt.py changes FILE          the person's edits, one per line (time  text); moves the read mark (seen)
python gantt.py changes FILE --peek   without moving it
python gantt.py changes FILE --all    the whole history
```

Each window operation is logged as one Japanese line in `log` (e.g. `「Enclosure design」の「Detail design」: 11/16〜11/27 → 11/18〜11/29`). Read `changes` before editing the file and take the person's edits into account. If the person undoes something the agent has already read, a line "元に戻す: …" is added.

## Sharing

For people without the app, hand over exported files. Put `"share": ["~/Drive/plan.pdf", "~/Drive/plan.png"]` (absolute paths, `~` expanded, `.pdf` / `.png` / `.svg`) in the data, and `python gantt.py share FILE` (the window's "共有用に書き出し" button does the same) writes them all. The intended use is to export into a cloud-synced folder and link to that file from chat.

- Each file is first rendered to a temporary file beside the data file, then copied over the target in one step (a sync client never sees a half-written file). The target name is the same every time, so a link keeps working
- A missing target folder is an error (it is not created). Every path written is printed

## Safety (`serve`)

It listens on `127.0.0.1` by default and refuses requests from other sites' pages with 403: a `Host` other than this server's names (`127.0.0.1:<port>` / `localhost:<port>`, a specific `--host`) (DNS rebinding); an `Origin` other than this server (`null` too); requests the browser marks `Sec-Fetch-Site: cross-site` / `same-site` (another site may only open the page by a link); a POST with none of our `Origin`, a same-origin mark or our own `Referer`. POST bodies must be `Content-Type: application/json` (415). A client without a browser sends `Origin: http://127.0.0.1:<port>` on POSTs.

```
curl -H "Origin: http://127.0.0.1:8195" -H "Content-Type: application/json" -d "[null]" http://127.0.0.1:8195/api/get
```

The API is `POST /api/<get|apply|poll|undo|prefs|open_link|share>` with a JSON array of arguments as the body. `get` / `apply` / `poll` / `undo` take `day` and `view` (`{text, rows, label_width, query, owner, by_owner, fit, w, h}`; with `fit` the day width and the row scale are chosen for the visible `w` × `h` px and returned as `day` / `rows` / `fitted`) as their last two arguments. Renaming a person is the `apply` operation `{"op": "rename_person", "from": "アルバイト", "to": "田中"}`. `prefs` reads and sets the window's settings. The desktop window calls the same methods through pywebview's `js_api`, plus `recent` / `open_file` / `pick` (switching files), which are not served over HTTP.

## Tests

```
python test_gantt.py
```

Covers operations, rendering (byte-identical output at the default arguments, the text and row scales, the title column width, search, the person filter, the by-person view, fractional day widths, fitting a window), renaming a person, the save format, `embed`, `changes`, the window's API and recent files, the HTTP guards and `share` / `png` / `pdf` (skipped without a browser).

The window is checked by `python gantt.py selftest FILE` (with the Python that has pywebview): it opens a hidden 1280 × 900 window and checks that the toolbar fits in one row, that the fitted chart fills the width, the text size, search, the person filter, the 項目順 / 担当ごと switch (a drag in the by-person view targets the real row and item; nothing is saved), Ctrl + wheel and Alt + wheel, that the header and the columns stay put while scrolling, the hover details, the column handle and refitting after a resize, and reports errors from the page. It writes neither FILE nor `last.json`. With `--write` it also renames a person and undoes it (this writes the file, so use a copy; the file gets its old bytes back afterwards).
