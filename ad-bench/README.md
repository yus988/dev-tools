# ad-bench — Analog Discovery の計測の補助

[English](README.en.md)

Digilent WaveForms SDK（`dwf.dll`。SDK 3.25.1 と Analog Discovery Studio で作った）の Python（ctypes）のドライバー。プローブは人がつなぎ、計測器はエージェントが動かす。記録の開始時に、測る機器へ TCP で 1 行を送ることもできる（任意）。CSV / PNG / `summary.json` を `<data>/runs/<timestamp>-<command>/` に集める。

先に WaveForms の画面を閉じる。機器を開けるのは 1 つのプログラムだけ。

## 必要なもの

- Windows、Python 3.9+
- **WaveForms（Digilent）を別に入れる**（SDK 付き）。`dwf.dll` が読み込めること。SDK の Python のサンプル（`dwfconstants.py`）は `C:\Program Files\Digilent\WaveFormsSDK\samples\py` から読む。Digilent のものはこの repo に何も入れていない
- `matplotlib`（PNG のグラフ）。`Pillow` と KiCad 10 は `boardmap.py` だけに要る

## データのフォルダ

プロジェクトのデータはこの repo に置かない。どのスクリプトも 1 つのデータのフォルダを読み書きする。フォルダは `--data <folder>`、無ければ `$AD_BENCH_DATA`、それも無ければ今のフォルダ。

```
<data>/
  boards/<board>/top.jpg, bottom.jpg, pads.json   # boardmap.py output
  procedures/<name>.json                           # guided probing procedures
  runs/<timestamp>-<command>/                      # measurements (keep them out of git)
  runs/marks.jsonl                                 # adscope voice marks
```

このフォルダは、測るプロジェクトの workspace に置く。

## コマンドと配線（Analog Discovery Studio のブレッドボード）

| コマンド | 配線 | 得られるもの |
|---|---|---|
| `python adbench.py devices` | USB だけ | 機器の一覧 |
| `python adbench.py monitor --ch1-name VSYS --ch2-name 3V3` | 1+ / 2+ を電源の線に、1− / 2− → 基板の GND | 0.3 秒ごとの直流の値（平均、最小..最大）の表示。`monitor.csv` に残す |
| `python adbench.py selftest` | W1 → 1+、1− → GND | 記録の経路の確認（~1.0 Vpp になるはず） |
| `python adbench.py impedance --ref 10` | W1 と 1+ → モーターの線 A。モーターの線 B → 2+ と 10 Ω の抵抗の片方。抵抗のもう片方 → GND。1−、2− → GND。モーターは基板から外し、回転子は動かないように押さえる | 100 Hz–100 kHz の Rs、Xs、Ls。1–20 kHz の Ls の中央値 |
| `python adbench.py record --ch2-shunt 0.05 --tcp <host>:<port> --tcp-line '<line>'` | 1+ → 電源の線、1− → 基板の GND。電池の − の線に 0.05 Ω のシャント抵抗を入れ、その両端に 2+ / 2−。基板の GND = オシロスコープの GND | 送った行で機器が動いている間の、電源の線の最小 / 最大と電池の電流（ピーク、10 ms 平均の最大） |

`record` の既定: 1 チャンネルあたり 200 kS/s、3 秒、CH1 の幅 5 V で中心 4 V（1.5–6.5 V を覆う）、CH2 の幅 0.5 V。スパイクが幅を超えうるときは `--ch1-range` / `--ch1-offset` を上げる。`--tcp-line` は、記録の開始から `--send-delay`（0.3 秒）後に `--tcp HOST:PORT` へ送る（改行を足す）。返事（改行まで、または 1.5 秒まで）は `summary.json` の `events` に入る。1 行の JSON を受ける機器には、`--tcp-kv "cmd=ping n=1"` で `{"cmd": "ping", "n": 1}` を組み立てる（数は数になる）。cmd / PowerShell での JSON の引用符の扱いを避けられる。`--tcp` と、`--tcp-line` / `--tcp-kv` のどちらかを渡さない限り、何も送らない。

## 注意

- インピーダンスのモード 0 = W1-C1-DUT-C2-R-GND（SDK の `FDwfAnalogImpedanceModeSet`）。モーターが回らないように `--amp` は小さく保つ（既定 0.1 V）。逆起電力で低い周波数の点がずれる
- シャントの電流は CH2 の電圧 / `--ch2-shunt`。電池の保護回路は、おおむね 10 ms ほど続く過電流で切れることが多いので、その上限と比べる値は `max_10ms_avg`（10 ms の移動平均の最大）

## adscope — オシロスコープのページと HTTP API

`adscope.py` は、基板を調べるための小さなオシロスコープ（電源の線、シャントの電流、電源を入れたときの動き）。機器を開いたまま、`127.0.0.1` でページと JSON の API を出す。人はページを見て、エージェントは同じデータを HTTP で読む。入力だけで、W1/W2 は出力しない（接続時に止める）。

```
python adscope.py --data <folder>               # opens http://127.0.0.1:8195/ in the default browser
python adscope.py --data <folder> --port 8195 --no-open
```

終了は Ctrl-C か `curl -X POST -H "Origin: http://127.0.0.1:8195" http://127.0.0.1:8195/api/quit`。どちらも機器を閉じる。**adscope が動いている間は、`adbench.py` のコマンドは機器を開けない**（開けるのは 1 つのプログラムだけ）。WaveForms の画面も閉じたままにする。機器が無い・使用中のときは、ページに理由と再接続のボタン（`/api/connect`）を出す。自動では再試行しない。

### 安全

- 待ち受けは `127.0.0.1` だけ。CORS のヘッダーは付けない
- 次は 403: `Host` が `127.0.0.1:<port>` / `localhost:<port>` 以外（DNS rebinding）。`Origin` が `http://127.0.0.1:<port>` / `http://localhost:<port>` 以外（別のサイトのページ。`null` も）。ブラウザが `Sec-Fetch-Site: cross-site` / `same-site` を付けた要求（別のサイトからは、リンクでページを開くことだけを通す。`<img>`・`<script>`・iframe・`fetch` は通さない）。`Origin` も自分の `Referer` も無い POST
- ブラウザの外のクライアントは、POST に `Origin: http://127.0.0.1:<port>` を付ける（`guide.py` は付けている）。本文のある POST は `Content-Type: application/json` だけ受け付ける（ほかは 415。別のサイトのフォームからは送れない）。読むだけの GET は、ヘッダー無しの curl でも読める
- ページは `X-Frame-Options: SAMEORIGIN` で、別のサイトの枠に入れて押させることもできない
- 試験: `ad-bench` のフォルダで `python -m unittest test_adscope`（機器は開かない。WaveForms の SDK は要る）

### 画面

- 10 × 8 div の格子、CH1 は黄、CH2 は水色。左端の印 = 各チャンネルの 0 V（または 0 A）。上の橙の印 = トリガーの時刻。破線と右端の印 = トリガーのレベル
- モード: **Auto**（トリガーが無いまま 0.25 秒たつと自走）、**Normal**、**Single**（1 回取り込んで Stop）、**Roll**（スキャンしながら流す。1–30 秒の窓が流れる。トリガー無し。電源のボタンを押しながら電源の線を見るとき用）
- 時間軸 1 µs/div … 1 s/div。サンプリングの速さは、10 div が機器のバッファ（AD Studio は 8192 サンプル、最大 100 MS/s）に収まるように決める
- チャンネルごと: ON/OFF、名前、V/div（0.01–5）、中心（画面の中心の電圧 = SDK のチャンネルのオフセット）、シャント Ω（> 0 で電流 = V / R を出す）。SDK は 8 div を覆うハードウェアの範囲を選ぶ。AD Studio の範囲は ~5.5 V 幅と ~60 V 幅の 2 つなので、≤ 0.5 V/div では中心 ± 2.75 V だけを測る。測定の欄の **範囲外** は、入力が範囲の端に当たったことを表す（V/div を上げるか、中心を動かす）。範囲・オフセットを変えた後の 2 秒は 入力安定待ち… を出す（SDK: 入力のオフセットが落ち着くのに ~2 秒かかる）
- 測定は、表示している記録全体について行う（間引いた線ではなく元の分解能）: 平均、最小、最大、p-p、RMS
- 現在の波形を保存 で今の記録を書く。Record N 秒 は、選んだ速さで `acqmodeRecord` の取り込み（adbench の `record()`。その 2 秒の安定待ちを含む）を行い、保存して、ライブのモードに戻る。どちらも `<data>/runs/<timestamp>-scope/` に `frame|record.csv`、`.png`、`summary.json` として書く
- 名前と設定はブラウザ（localStorage）に残し、ページを開いたときに当て直す（モードは除く）

### HTTP API

| メソッド / パス | 本文 | 返すもの |
|---|---|---|
| `GET /api/status` | | 機器（名前、SN、接続、エラー）、mode、running、state、settings、applied（速さ、サンプル数、ハードウェアの範囲/オフセット） |
| `GET /api/frame` | | settings + チャンネルごとに ≤ 2000 点（min/max で間引き、表示の単位）+ 測定値。`x0`/`x1` = 最初/最後の点の時刻（秒、0 = トリガー、Roll では 0 = 最新）。`screen` = 見えている時間の幅。`trig` = `trig` / `auto` / `roll` |
| `GET /api/measure` | | 測定値だけ。チャンネルの名前と単位付き |
| `POST /api/config` | 設定の一部 | まとめた後の設定（値が不正なら 400） |
| `POST /api/run` / `stop` / `single` | | `{"ok": true}` |
| `POST /api/save` | | 出力のフォルダ + 要約 |
| `POST /api/record` | `{"seconds": 2, "rate": 200000}` | 終わるまで待つ。出力のフォルダ + 要約（チャンネルごとの最小 / 最大 / 平均の V、`shunt_ohm` > 0 なら A と `max_10ms_avg` も） |
| `POST /api/connect` | | 機器を開き直す |
| `POST /api/quit` | | サーバーを止め、機器を閉じる |

設定（`/api/config` ではどのキーも省ける。`channels` は 2 つのオブジェクトの配列か `{"0": {...}, "1": {...}}`）:

```json
{"mode": "auto|normal|single|roll", "tdiv": 0.001, "roll_s": 10,
 "channels": [{"enabled": true, "name": "VSYS", "vdiv": 0.5, "offset": 4.0, "shunt_ohm": 0}, {...}],
 "trigger": {"source": 0, "edge": "rise|fall", "level": 2.0, "pre": 50}}
```

`trigger.level` は入力の電圧。`pre` は画面のうちトリガーより前の部分（%）。

声の印と手順の案内:

| メソッド / パス | 本文 | 返すもの |
|---|---|---|
| `POST /api/mark` | `{"text": "当てた", "kind": "voice", "step": "vsys"}`（`step` は省ける） | 印: id、時刻、text、kind、step と、その時点のチャンネルごとの `now` / 平均 / 最小 / 最大。`runs/marks.jsonl` にも追記する |
| `GET /api/marks?since=<id>` | | `id` より後の印 |
| `POST /api/say` | `{"text": "プローブを当ててください"}` | `{id, text, time}`。ページは帯で出し、読み上げる（読み上げ ON のとき）。`GET /api/frame` は最新のものを `say` で持つ |
| `GET /api/procedures` | | `procedures/*.json`: name、title、board、steps、`problems`（確かめた結果のエラー、日本語） |
| `GET /api/procedures/<name>` | | 確かめた手順。すべてのプローブを解決したもの（`ref`、`pad`、`net`、`side`、`px`）、基板の `sides`、`problems` |
| `POST /api/procedure/state` | ページから | 進み具合を保存する。`result` / 終了は `runs/<timestamp>-proc-<name>/results.jsonl` に追記する |
| `GET /api/procedure/state` | | procedure、`step`（今の id）、`say`、それまでの `results`、`done`、`conclusion`、`run_dir`、`event` |
| `GET /boards/<board>/<file>` | | `boards/` の下の `.jpg` / `.png` / `.json` だけ |

## 手順モード（guided probing）

両手でプローブと基板を持つ人のためのモード。ページが手順を順に進め、基板の絵の上でプローブを当てる所を示し、人が「当てた」と言うと値を記録し、判定して次へ進む。

- **手順** のタブを開き（横の欄が広がる: 基板の絵 + 今の手順）、手順を選んで **開始** を押す。マイクを ON にする（🎤 音声）
- 言葉: 「当てた」「OK」「次」「はい」 = 記録 / はい。「NG」「だめ」「いいえ」 = いいえ（はい/いいえの手順だけ）。「戻る」 = 1 つ前の手順へ（その結果は捨てる）。「やり直し」 = 最後に記録した手順を今の値で測り直し、判定し直す。絵の下のボタンも同じ動作
- 記録する値は、声の印の各チャンネルの `now`（最新の値）。そのため、`runs/marks.jsonl` の印にも同じ数と手順の id が入る
- 輪: CH1 は黄、CH2 は水色、GND のクリップは白、テスターは桃色。前の手順の点は薄く描く。絵をクリックすると、今のプローブの周りを 2 倍に拡大する（手順が変わっても拡大のまま。もう一度クリックで戻る）。絵は手順に合わせて表 / 裏を切り替える。裏 = 基板を裏返して見た面
- 🔈 読み上げ は、各指示とエージェントからの知らせ（`POST /api/say`）を読み上げる（ja-JP）。一度押すまで OFF で、その後はブラウザに残す。読み上げている間は、聞き取った声を無視する
- ほかから送られた印（エージェント: `POST /api/mark {"text": "当てた"}`）も、動いている手順を進める。別の `step` を指しているときは除く
- 問題のある手順は ⚠ 付きで並び、開始できない。問題は手順の選択の下に出す

## エージェントの計測で人がプローブを持つとき（guide.py）

プローブを持っている間、人はほかに何もできない。そのため、エージェントのスクリプトは人を何も分からないまま待たせない。`guide.probe(prompt, ready)` は指示（「…に当ててください」）を言い、ライブの値で `ready` が 0.5 秒続くまで見て、「計測中」と言う。`guide.done()` は「計測終わり」と言う。2 つの間は短くする。`probe()` の直後に始めた記録は、ライブの表示がすでに同じ範囲・オフセットなら、2 秒の入力の安定待ちを省く。

エージェントは `GET /api/procedure/state` で実行を追う（または `runs/*-proc-<name>/results.jsonl` の末尾を読む: 記録した手順ごとに 1 行で `step`、`kind`、`pass`、`values` {ch1, ch2}、`limits`、`answer`、`probes`、`mark`、最後の行は `{"event": "end" | "abort", "conclusion": ...}`）。

### 新しい基板・新しい手順の追加

1. 基板の図（基板の版ごとに 1 回）。KiCad 10 を入れておく:

   ```
   python boardmap.py <path>\board.kicad_pcb <name> --data <folder>   # -> <folder>/boards/<name>/top.jpg, bottom.jpg, pads.json
   ```

   `pads.json` はすべてのパッドを並べる: `ref`、`pad`、`net`、`value`、`side`、`tht`、`px.top` / `px.bottom`（絵の中のピクセル。裏は下から見たとおりに左右を反転）。基板のファイルは読むだけ。
2. データのフォルダに `procedures/<new_name>.json` を書き（名前: `[A-Za-z0-9_-]`、形式は下）、`board` と手順を決める。`adscope.py` / `index.html` には、基板や手順に固有のものは何も無い。
3. ページを読み込み直す（サーバーの再起動は不要）。基板と合わないものがあれば、選択の欄に ⚠ と問題が出る。

形式（`version` 1）:

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

- プローブの `ch`: `1`、`2`、`"gnd"`（クリップ）、`"meter"`（テスター。manual の手順用）。当てる所は `ref` + `pad`（`net` を足すと基板と照合する）か、`net` だけ（+ 任意で `side`）で書く。`net` だけなら、サーバーがその面のその net のパッドを、受動部品（C / R / L / FB）を優先して選ぶ。そのため、基板の版が変わっても手順が動き続ける。`label` の既定は net の名前
- プローブは 1 本ずつ: GND のクリップは専用の `connect_gnd` の手順で一度だけ付け、その後は CH1 だけを動かす。CH2 は 2 点を同時に見る必要があるときだけ使い、そのことを `say` に書く
- 手順の判定のしかた: `expect` があれば測定（「当てた」で記録し、すべての条件を満たす必要がある）。`expect` が無く `else` があれば、はい/いいえの質問（「OK」 → `next`、「NG」 → `else`）。どちらでもなければ確認（「OK」 → `next`）。`else` の無い測定は、外れても `next` へ進む（✘ を出す）
- `expect` のチャンネルごとの条件（`ch1` / `ch2`、電圧。チャンネルにシャントがあれば電流）: `[low, high]`、または `{"near": "<step id>", "tol": 0.3}` = 前の手順で同じチャンネルが読んだ値の ±tol の中
- `next` / `else`: 手順の id、または `{"end": "結論の文"}` でその結論で終える

```
O='Origin: http://127.0.0.1:8195'; J='Content-Type: application/json'     # POST に要る（「安全」）
curl -s http://127.0.0.1:8195/api/measure
curl -s -H "$O" -H "$J" http://127.0.0.1:8195/api/config -d '{"channels":{"0":{"name":"VSYS","vdiv":1,"offset":2.5},"1":{"name":"3V3","vdiv":1,"offset":2.5}}}'
curl -s -H "$O" -H "$J" http://127.0.0.1:8195/api/config -d '{"mode":"roll","roll_s":10}'
curl -s -H "$O" -H "$J" http://127.0.0.1:8195/api/config -d '{"mode":"normal","tdiv":0.001,"trigger":{"source":0,"edge":"rise","level":2.0,"pre":20}}'
curl -s -X POST -H "$O" http://127.0.0.1:8195/api/single
curl -s -X POST -H "$O" http://127.0.0.1:8195/api/save
curl -s -H "$O" -H "$J" http://127.0.0.1:8195/api/record -d '{"seconds":2,"rate":200000}'
```

## エージェント向けの注意

- スクリプトはどこからでも `python <dev-tools>/ad-bench/adscope.py --data <folder>` で実行する（`adbench.py`、`boardmap.py` も同じ）。`<folder>` はプロジェクトのデータのフォルダで、この repo にはしない
- 機器を開けるのは 1 つのプログラムだけ: `adbench.py` を動かす前に adscope を止め（`POST /api/quit`）、人に WaveForms の画面を閉じてもらう
- adscope と adbench は入力を読むだけ。例外は `adbench.py selftest` / `impedance`（W1 を出力する）と `record --tcp`（測る機器へ 1 行を送る）。人がその計測を頼んだとき以外は、機器へ何も送らない
- 人がプローブを持つときは、`guide.py` で声で案内する（`guide.probe(...)` → 測定 → `guide.done()`）。最初の指示の前に、設定と接続の確認をすべて済ませ、記録は必要な長さだけにする
- 結論は `runs/` からプロジェクトのメモへ写す。`runs/` そのものは git に入れる前提ではない
