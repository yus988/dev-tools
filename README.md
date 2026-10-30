# dev-tools

[English](README.en.md)

ハードウェア・製品の開発と検証に使う小さなツールの置き場所。特定の会社や案件に依存しない。ツールごとにフォルダと README がある。

| ツール | 内容 |
|---|---|
| [fit-lab](fit-lab/README.md) | 3D プリントなどの造形品の、CAD の設計値と実測値を、造形のロットとクリアランスの版ごとに記録するローカルの Web ツール。注記付きの画像を添えられ、ロットを重ねると知見の蓄積になる（音声での入力も可） |
| [ad-bench](ad-bench/README.md) | Digilent Analog Discovery を WaveForms SDK で動かす計測の補助。コマンドでの監視・記録・インピーダンス測定、HTTP API 付きのブラウザのオシロスコープ（`adscope`）、基板の画像の上でプローブの当て所を案内する手順、プローブを持つ人への音声の案内（`guide.py`） |

## 別の repo に分けたツール

| ツール | 内容 |
|---|---|
| [kicad-review](https://github.com/yus988/kicad-review) | KiCad 10 の回路図と基板を、コーディングエージェントとブラウザで一緒にレビューするローカルのツール（2026-10 に分けた） |
| [talkpage](https://github.com/yus988/talkpage) | ユーザーが項目に状態やコメントを付けると、待っているエージェントが起きて返信する、ローカルのページの基盤。kicad-review もこれを使う（2026-10 に分けた） |

## 共通の前提

- Python 3.9 以上（各ツールの README に書いていなければ、標準ライブラリだけ）
- Web のページは Chromium 系のブラウザ（Chrome・Edge）で使う。音声の機能はブラウザの Web Speech API を使う
- 第三者のソフト（Digilent WaveForms、KiCad など）は別に入れておく。第三者のバイナリはこの repo に置かない

## 案件のデータはこの repo に置かない

ツールは案件のデータを持たない。造形の記録、基板の画像、手順、測定の結果は、使う側のプロジェクトのフォルダに置き、そのフォルダをツールに渡す（`--root` / `--data`、または各 README に書いた環境変数）。この repo には中立なサンプルだけを置く。

## ほかの人が使うとき

作者が自分の作業のために作ったもので、主に AI のエージェント（Claude Code / Codex）に動かしてもらう前提のため、README は短い。エージェントにツールの README を読ませて、起動させるのが想定の使い方。issue や要望は受け付けていない。ライセンスは MIT（`LICENSE`）。

## PC での置き場所

エージェントは `~/.agent-core/tools.conf` でこの clone を見つける。

```
dev_tools=<この clone のパス>
```

```bash
git clone https://github.com/yus988/dev-tools.git <path>
```
