# gantt

[English](README.en.md)

小さなガントチャート。エージェントが JSON のファイルを書き、人がデスクトップの窓でバーをドラッグして直し、同じ描画から静的な SVG・PNG・PDF を書き出す（HTML のレポートへの埋め込み、アプリの無い相手への共有）。人の直しはファイルにその場で保存され、変更の履歴がエージェントに残る。

- 依存: `gantt.py` は Python 3.9+ の標準ライブラリだけ。デスクトップの窓（`app` / `selftest`）だけは [pywebview](https://pywebview.flowrl.com/) が要る（Windows は Edge WebView2 を使う。別に導入する）。`png` / `pdf` は Edge か Chrome を使う
- 画面は 1 つの HTML（`app.html`、CDN なし）で、デスクトップの窓でもブラウザ（`serve`）でも同じ
- 案件のデータはこの repo に入れない。使う側の workspace に `*.gantt.json` を置き、そのパスを渡す。ここには中立なサンプル（`sample.gantt.json`）だけ

## 起動

```
python gantt.py app [FILE] [--day N]                 デスクトップの窓
python gantt.py serve FILE [--port 8195] [--host H]  ブラウザで同じ画面（http://127.0.0.1:8195/）
python gantt.py svg FILE [-o OUT] [--day N] [--no-legend]
python gantt.py png FILE [-o OUT] [--day N] [--scale 2]
python gantt.py pdf FILE [-o OUT] [--day N]          チャートの大きさの 1 ページ
python gantt.py share FILE                           データの "share" に書いたファイルを書き出す
python gantt.py embed FILE PAGE [--day N]            HTML の <!-- gantt:begin --> と <!-- gantt:end --> の間に SVG を入れる
python gantt.py changes FILE [--peek] [--all]        人が直した所（エージェントがまだ読んでいないもの）
python gantt.py check FILE                           検証
python gantt.py selftest FILE                        窓を隠して開き、画面が読み込めるか確かめる
```

- `app` は FILE を省くと前回のファイル（`~/.gantt/last.json`）、無ければファイルを選ぶ窓を出す。前回の拡大率もそこに残す。窓の状態は `~/.gantt/webview` に置く（AppData の外。パッケージ化された環境が AppData を振り替えるため）
- デスクトップの窓用の環境の例（Windows）: `python -m venv ~/.gantt/venv`、`~/.gantt/venv/Scripts/pip install pywebview`。コンソールを出さずに起動するなら `pythonw.exe gantt.py app FILE`
- `svg` / `png` / `pdf` の出力先は、省くとデータの横の `<名前>.svg` など（`.gantt` は名前から外す）。PNG と PDF は、チャートの上に題名と「更新 YYYY-MM-DD HH:MM」を付けた白い背景のページを Edge / Chrome（ヘッドレス）で出す。ブラウザは環境変数 `GANTT_BROWSER`、無ければ標準の場所の Edge、Chrome を探し、見つからなければ理由を示して終了コード 1 で終わる。PDF の文字は文字のまま
- `embed` は、マーカーの間だけを書き換える（ほかはバイト単位で同じ）。マーカーが無ければ何もせず終了コード 1。レポートを作り直すたびに実行する

## データ（`*.gantt.json`、UTF-8）

ファイルが唯一の正本。エージェントも人（窓）も同じファイルを読み書きする。窓の操作は「今のファイルを読み、操作を当て、書く」（排他あり）なので、エージェントが同時に書いた内容は消えない。

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

| キー | 意味 |
|---|---|
| `title` / `from` / `to` | 題名と、表示する範囲（両端を含む）。週の見出しは月曜から |
| `link_base` | 行の `link` が相対のときの基準の URL。無ければデータのフォルダ（`file:`）。`http` / `https` / `file` 以外は無視 |
| `legend` | 種類の凡例の名前の上書き（既定は日本語） |
| `off` / `lines` | 休みの範囲（薄い影と名前）、縦の破線（期日の線） |
| `day` | 図に書き出すときの 1 日の幅（px、6〜40、既定 14）。`--day` があればそちら。窓は、拡大・縮小した幅を覚えていなければこの幅で開く |
| `label_width` | 項目の列の幅（既定 240 px）。担当が 1 つもなければ担当の列（80 px）は出さない |
| `rows` | 表示順。`group` のある行は区切り（項目なし）。どの行も重ならない `id` を持つ（無ければ読み込み時に `r1`、`r2`… を振り、次の保存で書く） |
| 項目 | バー＝`from` と `to`（両端を含む、`from <= to`）、期日＝`on`。`label`、`note`、`kind`（下表。不明は `plan`） |
| `share` | 共有用に書き出すファイルの絶対パスの一覧（下の「共有」） |
| `log` / `seen` | 窓が書く変更の履歴（新しい 500 件）と、エージェントが読み終えた最新の時刻 |

未知のキーはそのまま残す。保存は JSON を 1 スペースで整え、`items` / `off` / `lines` / `log` の要素と項目の無い行を 1 行にまとめる（git の差分が読める）。

| `kind` | 既定の名前 | 見た目 |
|---|---|---|
| `plan` | 作業 | 青の塗り |
| `tbd` | 要相談 | 青の斜線と破線の枠 |
| `due` | 締め切り・発注 | 橙、太字 |
| `ext` | 外の期日 | 赤、太字 |
| `wait` | 待ち | 縦縞 |
| `off` | 休み | 灰の塗り、枠なし |
| `done` | 済み | 緑 |

色はすべて CSS のカスタムプロパティ（`--rec`、`--recs`、`--warn`、…）で、既定値は明るい配色。埋め込んだ先のページのテーマ（ダーク含む）に追従し、単体の SVG・PNG・PDF は既定値で描く。

## 窓の操作

- バーの本体をドラッグで移動（日単位）、端から 5 px 以内で長さの変更（最小 1 日）。ドラッグ中は新しい日付を表示。ほぼ動かさずに離すとクリック（右の引き出しを開く）
- 行の空いた所をダブルクリックで、その日から 7 日の「新しい作業」を足す
- 行の項目名をクリックで引き出し（項目・担当・リンク・メモ、各バー／期日の名前・種類・日付、行の上下移動・追加・削除）。入力欄は確定（change）で保存。Esc か ✕ で閉じる
- Ctrl+Z で元に戻す（入力欄の中では入力の取り消し）。項目を選んで Delete で削除（確認あり）。縮小・拡大（6〜32 px/日）、今日へ移動
- 外でファイルが変わると 1 秒以内に読み直して「外で更新 HH:MM」。操作が外の変更とぶつかったら読み直す
- 元に戻すは、この窓が書いた操作を新しい順に戻す。ファイルがその後ほかで書き換わっていたら戻さない
- 「共有用に書き出し」のボタンは、データに `share` があるときだけ出る

## エージェント向け: 人の直しを受け取る

```
python gantt.py changes FILE          人が直した所を 1 行ずつ（時刻  内容）。読んだ印（seen）を進める
python gantt.py changes FILE --peek   印を進めない
python gantt.py changes FILE --all    履歴の全部
```

窓の操作は日本語の 1 行（例: `「Enclosure design」の「Detail design」: 11/16〜11/27 → 11/18〜11/29`）で `log` に残る。エージェントは、ファイルを直す前に `changes` で人の直しを読み、取り込む。読んだ後に人が元に戻した場合は「元に戻す: …」の行が付く。

## 共有

アプリの無い人へは、書き出したファイルを渡す。データに `"share": ["~/Drive/plan.pdf", "~/Drive/plan.png"]`（絶対パス、`~` は展開、`.pdf` / `.png` / `.svg`）を書くと、`python gantt.py share FILE`（窓の「共有用に書き出し」も同じ）が全部を書き出す。クラウドの同期フォルダに書き出し、チャットからそのファイルへリンクを張る使い方を想定している。

- 書き出しは、まずデータの横の一時ファイルに作り、それを宛先へ 1 回でコピーする（同期ソフトが作りかけのファイルを見ない）。宛先のファイル名は毎回同じなので、張ったリンクは変わらない
- 宛先のフォルダが無ければ作らずにエラー。書いたパスを 1 行ずつ表示する

## 安全（`serve`）

待ち受けは既定で `127.0.0.1`。別のサイトのページから触れないよう、次は 403: `Host` がこのサーバーの名前（`127.0.0.1:<port>` / `localhost:<port>`、特定の `--host`）以外（DNS rebinding）。`Origin` がこのサーバー以外（`null` も）。ブラウザが `Sec-Fetch-Site: cross-site` / `same-site` を付けた要求（リンクでページを開くことだけを通す）。`Origin`・同一サイトの印・自分の `Referer` のどれも無い POST。POST の本文は `Content-Type: application/json` のみ（415）。ブラウザの外のクライアントは、POST に `Origin: http://127.0.0.1:<port>` を付ける。

```
curl -H "Origin: http://127.0.0.1:8195" -H "Content-Type: application/json" -d "[null]" http://127.0.0.1:8195/api/get
```

API は `POST /api/<get|apply|poll|undo|open_link|share>`、本文は引数の JSON の配列。デスクトップの窓は同じメソッドを pywebview の `js_api` で呼ぶ。

## 試験

```
python test_gantt.py
```

操作・描画・保存形式・`embed`・`changes`・窓の API・HTTP の安全・`share` / `png` / `pdf`（ブラウザが無ければ飛ばす）を確かめる。窓の読み込みは `python gantt.py selftest FILE`（pywebview の環境の Python で）。
