# dev-tools のエージェント向け規則

この repo は組織・案件に依存しない開発・検証ツールの置き場所（root の `.guard-generic` により、commit 検査が組織の名前・製品名を止める）。ツールを使う・直す時は次に従う。

## 案件固有のものを入れない（切り分け方）

| 案件に固有のもの | 置く場所 |
|---|---|
| データ（基板の画像・座標、手順、測定結果、campaign） | 使う側の workspace。ツールには `--data` / `--root` / 環境変数で渡す |
| 機器への指令（送る内容・ポート・配線） | 使う側の workspace のデータの置き場所の `README.md`（例: ad-bench の `--tcp` + `--tcp-kv`） |
| 案件だけの処理（独自の解析・機器の操作の手順をまとめたスクリプト） | 使う側の workspace の adapter（この repo のモジュールを import して使う。依存の向きは「workspace → dev-tools」だけ） |
| 製品名・型番・社内パス・IP・MAC を含む例 | 使う側の workspace。この repo の README・サンプルは中立な名前にする |

- この repo のコードを変えるのは、**どの案件でも使える機能**を足す・直す時だけ。案件の都合で必要になった機能は、引数・設定・データで差し替えられる汎用の形（例: 「指定の宛先へ 1 行送る」）にしてから入れる。
- README は日本語の `README.md` を主にし、英語の `README.en.md` を並べる（それぞれの先頭に相手へのリンク）。GitHub は `README.md` だけを表示する。
- 大きくなり、単独でも使えるツールは、別の repo に分ける（kicad-review・talkpage は 2026-10 に分けた。この repo の README の「別の repo に分けたツール」に足す）。
- 新しいツールを足す時は、フォルダごとに `README.md`（目的・前提・起動・データの渡し方）を置き、上の表の README と、core（agent-core-config）の skill と「ツール別の規則」の表にも足す。
- 第三者のバイナリ・SDK は入れない（各 README に別途導入と書く）。
- サーバーは既定で `127.0.0.1` で待ち受ける。
- この repo は public になりうる。push はユーザーが明示した時だけ（core の AGENTS.md「Git push / PR ポリシー」）。
