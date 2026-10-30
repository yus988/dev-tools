# fit-lab

[English](README.en.md)

3D プリントなどの造形品の **CAD の設計値と実測値** を、造形のロット（campaign）とクリアランスの版（variant）ごとに、注記付きの画面の画像と一緒に記録する小さなローカルの Web ツール。データはただの JSON と PNG なので、git に入れて差分を見られ、AI エージェントが読み書きできる。ロットが増えると知見の蓄積になる（「PA12 の穴は直径で 0.1–0.2 mm 小さく出る」）。

- 依存なし: サーバーは Python 3.9+ の標準ライブラリ、アプリはただの HTML/JS/CSS（ビルド不要、CDN なし、オフラインで動く）
- `serve.py` の待ち受けは既定で `127.0.0.1`（変えるときは `--host`）
- データ（campaign）はこの repo に入れない。使う側のプロジェクトの workspace に置き、そのフォルダを `--root`（または `FIT_LAB_ROOT`）で渡す

## 2 つの起動方法

アプリは起動時にデータの出どころを自動で判定する。`/api/campaigns` が応答すれば **サーバーモード**、応答しなければ **フォルダモード** を出す。今のモードは上のバーに出る（サーバー / フォルダ / 未接続）。

### 1. サーバーモード（`serve.py`）

```bash
python fit-lab/serve.py --root <data-root>
```

その後 <http://127.0.0.1:8765/> を開く。

| オプション | 既定 | 意味 |
|---|---|---|
| `--root` | `$FIT_LAB_ROOT` | データのルート（campaign を並べたフォルダ）。今のフォルダからの相対パス。環境変数が無いときは必須 |
| `--port` | `8765` | ポート |
| `--host` | `127.0.0.1` | 待ち受けのアドレス |

同梱のサンプル（架空のデータ）で試す:

```bash
python fit-lab/serve.py --root fit-lab/sample --port 8766
```

画面から保存するとサンプルのファイルが書き換わる。元に戻すには `git checkout fit-lab/sample`。

### 2. フォルダモード（静的なページ、サーバーなし）

`index.html`、`app.js`、`style.css` はただの静的なサイトで、バックエンド無しで任意の静的ホスト（または `python -m http.server`）から配れる。その場合、最初のページに **フォルダを開く…** が出る。これは [File System Access API](https://developer.mozilla.org/en-US/docs/Web/API/File_System_API)（Chrome / Edge、安全な文脈: `https://` か `localhost`）を使う。選べるのは次のどちらか。

- データのルート（各サブフォルダに `campaign.json` があるフォルダ）
- 1 つの campaign のフォルダ

campaign・画像・保存は、そのままユーザーのディスクのそのフォルダに入る。保存すると、まず前の `campaign.json` を `campaign.json.bak` にコピーし、それから新しいファイルを書く（ブラウザは一時ファイルに書き、閉じるときに差し替える）。最後のフォルダはそのブラウザの IndexedDB に覚え、1 クリックで開き直せる（ブラウザが改めて許可を求める）。フォルダモードは、サーバーモードの最初のページからも使える。Firefox / Safari はこの API に対応していないので、サーバーモードを使う。

## アプリの使い方

画面の文言は日本語。データのキーは英語のまま（`kind: "hole"` → 穴、`method: "caliper"` → ノギス、`by: "user"` → 本人）。

- **campaign の一覧** — campaign ごとに 1 枚のカード。測定済みのセル数 / 全セル数
- **項目** — 項目ごとに 1 行、variant ごとに 1 組の列: `CAD | 実測 | Δ`。**Δ = 実測 − CAD**。Δ は青（CAD より小さい）/ 赤（大きい）で色を付け、差が大きいほど濃い（±0.02 は無色、その先は 0.05 / 0.1 / 0.2 の段階）。CAD がすべての variant で共通（`"*"`）の項目は、variant の列をまたぐ 1 組で出す
  - **実測のセルは入力欄。** 空のセルにフォーカスすると CAD の値が入る。↑ / ↓（またはセルにフォーカスがある間のマウスホイール）で、ツールバーの刻み（0.01 / 0.05 / 0.1、覚えておく。Shift で ×10）ずつ動く。直接入力もできる。**Enter** で新しい測定値を記録し（今日、ノギス、`user`、仮ではない）、同じ列の下へ移る。**Tab** / Shift+Tab で記録して次 / 前のセルへ移る。**Esc** で取り消す。値を変えなければ何も記録しない。値は小数 2 桁で出す
  - 実測のセルをダブルクリックすると履歴が出る（日付・方法・メモ・仮の編集、記録の削除）。セルには最新の値（日付の順、同じ日なら追加した順）を出す
  - 仮の値（`provisional: true`。例: 音声メモから読んだ、まだ確かめていない値）は黄色の斜体で出し、ツールチップと凡例を付ける
  - 行をクリックすると、画像の欄でその注記が強調される（必要なら画像も切り替わる）。注記をクリックすると、その行が選ばれる
  - 配置: 表と画像の欄の間のバーをドラッグ（画像の欄の既定 ≈ 33 %）、見出しの境目をドラッグで列の幅を変える。どちらもダブルクリックで元に戻す。幅はブラウザごとに覚える（localStorage）。1000 px より狭いと、画像の欄は表の下に並ぶ。画像は欄に収まるように縮める
  - variant とその値は、表の下の折りたためるバリアントの欄と、集計のタブにある
  - **保存**（または Ctrl+S）で campaign 全体を書く。未保存の変更は状態の表示に出る。未保存の編集があるままページを離れようとすると、先に確認する
  - **CSV 出力** — 縦長の形式で、測定値ごとに 1 行（未測定のセルは空の行）。Excel 用に BOM 付きの UTF-8
- **集計・バリアント** — 種類（kind）ごとの Δ の n / 平均 / 標準偏差 / 最小 / 最大を variant 別に出し、variant の値の表と campaign の `notes` も出す。項目 × variant ごとの最新の測定値を使う。切り替えで仮の値を除ける
- **知見（`#/summary`）** — 同じ統計を **すべての** campaign について、材料 + 種類でまとめ、campaign ごとの行も出す。**全件 CSV 出力** ですべての campaign を書き出す
- テーマ: 既定は OS に合わせる。右上のボタンで 自動 / ライト / ダーク を切り替える。文字の色はどちらのテーマでも WCAG AA（≥ 4.5:1）を満たす

## 音声入力（手を使わない入力）

ツールバーの **音声入力** を押し、測りながら値を声で読む。マウスもキーボードも要らない。ブラウザの Web Speech API（`SpeechRecognition`、`ja-JP`、連続）を使い、`localhost` か `https` の **Chrome / Edge** で動く（マイクには安全な文脈が要る）。ほかのブラウザでは、代わりに案内を出す。

> **プライバシー:** Chrome と Edge は、認識のためにマイクの音声をブラウザの提供元のクラウドの音声認識（Google / Microsoft）へ送る。PC の外に出してはいけない内容には音声入力を使わない。

文脈: **今の variant**（ツールバーのバリアント、または声で切り替え）と **カーソルの行**（青いバーで強調）。CAD が共通（`"*"`）の項目は、いつも `*` として記録する。

| 言うこと | 動作 |
|---|---|
| 「1番 8.45」「いちばん はってんよんご」「3番 2.3」 | 今の variant で項目 1（3）を記録。カーソルは次の項目へ |
| 「8.45」「はちてんよんご」 | カーソルの行を記録し、カーソルを次の項目へ |
| 「5番」 | カーソルを項目 5 へ |
| 「次」「前」 | カーソルの移動 |
| 「ブイ1」「V2」「バリアント3」「共通」 | 今の variant の切り替え |
| 「はい」「OK」 | 確認待ちの値の受け入れ |
| 「取り消し」「戻して」 | 最後の音声入力の取り消し（または確認待ちの破棄） |
| 「保存」 | すぐに保存 |

- 数は、数字（全角も）、漢数字（八点四五）、かな（はってんよんご、じゅうに、れい / ゼロ）のどれでもよい。点 / てん が小数点。つなぎの言葉（えーと、ミリ、は / の）は無視する
- **安全:** 値を入れるのは |値 − CAD| ≤ max(1.0 mm, CAD の 15 %) のときだけ。それ以外（または項目に CAD が無いとき）は、表示のバーに「確認: 1番 84.50 ?」と出す。「はい」と言うと記録し、新しい値を言うと置き換える
- 知らせ: 高さが一定の表示のバーに、聞き取っている内容を出す。変わった行が光る。音声のログ（時刻、聞き取った文そのまま、動作）にすべての入力を並べ、1 件ずつ取り消せる取消のボタンを付ける。読み上げは記録した値を `speechSynthesis` で読み返す（既定は OFF。認識は読み返しの声を無視する）
- 音声入力が ON の間は、最後の編集から ~2 秒で自動で保存する。音声の入力は `note: "音声入力"` 付きで保存する
- **ブラウザ:** 認識が動くのは **Google Chrome**（Google の音声認識）と **Microsoft Edge**（Microsoft の音声認識）だけ。ほかの Chromium 系のブラウザ（Vivaldi、Brave、Opera など）は `webkitSpeechRecognition` を持つが、音声認識の先につながらない。マイクの表示は点くが、文字はいつまでも届かない（すぐに終わるか、`network` で失敗する）。デスクトップの Firefox と Safari には使える認識が無い。音声入力には、fit-lab を Chrome か Edge で開く
- **音声の診断**（ツールバーの下の折りたためる欄）は、認識のすべての出来事を時刻付きで残す（start、audiostart、soundstart、speechstart、途中 / 確定の結果、nomatch、error、end、再起動）。使っている仕組み（`SpeechRecognition` / `webkitSpeechRecognition`）、判定したブラウザ、入力の機器、マイクの音量の表示（`getUserMedia` + `AnalyserNode`。何も再生しない）も出す。`network`、`not-allowed`、`service-not-allowed`、`language-not-supported`、`audio-capture` などのエラーは平易な日本語で説明する。**診断をコピー** でログ全体を不具合の報告用にコピーする
- Web Speech はいつも OS / ブラウザの既定のマイクを使い、ページからは機器を選べない。変えるときは、Windows の 設定 > システム > サウンド > 入力、またはブラウザのサイトの設定のマイクで変える
- 再起動の決まり: 普通にセッションが終わると、300 ms 後に認識を再起動する。何も聞かないまま 2 秒以内に終わることが続くと、再起動の間隔を延ばし（1、2、4、8 秒）、5 回続くと説明を出して音声入力を止める。`not-allowed`、`service-not-allowed`、`language-not-supported`、`audio-capture` と 3 回目の `network` のエラーでは、すぐに止める。セッションが動いている間に `start()` は呼ばない
- 読み取りの処理は `voice-parse.js`（DOM を使わない）。単体の試験: `node --test fit-lab/voice-parse.test.js`。マイクの経路そのものは、人にしか確かめられない

## データの置き方

```
<data-root>/
  <campaign-id>/
    campaign.json
    campaign.json.bak      # previous version, written by the server on save
    images/
      img01.png
```

`<campaign-id>` は `^[A-Za-z0-9][A-Za-z0-9._-]*$` に合い、`id` の値と同じにする。

## データの取り決め — `campaign.json`（`fit-lab/1`）

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

決まり:

- **Δ = 実測 − CAD**、単位は `unit`。穴なら負の Δ は小さく出たこと、ボスなら正の Δ は太く出たことを表す
- `cad: {"*": v}` の項目は、variant ごとではなく 1 回だけ測る（`variant: "*"`）
- `measurements` は追記だけの履歴。variant ごとの最新（`date` の順、同じなら配列の順）が今の値
- 知らない追加のフィールドは、保存しても残る（アプリはオブジェクト全体をそのまま書き戻す）
- サーバーは、前のファイルを `campaign.json.bak` にコピーした後、整形した UTF-8（`indent=2`、ASCII 以外もそのまま）をアトミックに書く

## HTTP API

| メソッド | パス | |
|---|---|---|
| GET | `/api/campaigns` | `{root, campaigns: [{id, title, material, process, vendor, ordered, received, variants, items, cells, measured}]}` |
| GET | `/api/campaigns/<id>` | `campaign.json` |
| PUT | `/api/campaigns/<id>` | `campaign.json` の置き換え。本文には `schema: "fit-lab/1"`、`id` = `<id>`、`items` の配列が要る。フォルダが無ければ作る |
| GET | `/data/<id>/<path>` | campaign のフォルダの中のファイル（画像） |

## AI エージェント向け: campaign の追加

1. `<data-root>/<campaign-id>/` を作る（データのルートは `serve.py --root` に渡すフォルダで、この repo ではなくプロジェクトの workspace に置く）。id は `<product>-<material>-lot<N>` のような、中身が分かる小文字の名前にする
2. 画面の画像を PNG で `images/` に置く。それぞれを `images[]` に、実際のピクセルの `width` / `height` 付きで書く
3. `campaign.json` を上の取り決めのとおりに書く。測る寸法ごとに 1 項目、CAD の値は variant ごと（または `"*"`）、`kind` は決まった一覧から、相手の部品ではめあいが決まるときは `mate`。同じ材料の前のロットと同じ `material` の文字列を使い、知見でまとまるようにする
4. 注記: 座標は画像の大きさに対する割合で書く（ピクセルの x / width、ピクセルの y / height）。寸法なら 2 点は測る区間の両端、点の形状ならその点。既定の番号の位置が何かを隠すときは `label_pos` を付ける
5. 人が測る値の `measurements` は空（`[]`）にしておく。チャットで報告された結果を記録するときは追記する（古い記録は書き換えない）
6. 確かめる: `python -c "import json,sys; json.load(open(sys.argv[1], encoding='utf-8'))" <path>/campaign.json` の後、アプリで campaign を開き、すべての注記が対象の形状に乗っているかを見る
7. サーバーが動いているときは、代わりに JSON を `PUT /api/campaigns/<id>` で送ってもよい。schema と id を確かめ、`.bak` を残す

## エージェント向けの注意

- どのデータのルートを使うかを聞く（またはプロジェクトのメモから読む）。同梱の `sample/` を除き、この repo に campaign を書かない
- サーバーは人が見られる所（ターミナルのタブ）で起動し、URL を既定のブラウザで開く
- `PUT /api/campaigns/<id>` はファイル全体を置き換える。先に読み、測定値を追記して、書き戻す。サーバーが残す `.bak` は 1 つだけ
- 音声入力はマイクの音声をブラウザの提供元のクラウドへ送る（上のプライバシーを参照）
