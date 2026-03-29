---
name: computer-use
description: 見えているブラウザ UI を browser-first で扱い、単発は computer_*、長めの作業は computer_operator へつなぐ。
disable-model-invocation: true
---

# computer-use

見えているブラウザ UI を browser-first で操作するためのスキル。

## 手順

### 1. まず観測する

- 「このブラウザ」「このタブ」「画面を見ながら」の依頼では、最初に `computer_observe` を使う
- current tab が使えるか、desktop fallback が必要かを先に把握する

### 2. 単発か長期戦かを分ける

- 単発のクリック・入力なら `computer_click` / `computer_fill` を優先する
- observe → act → verify を何度も繰り返す task は `sessions_spawn` で `computer_operator` に渡す

```text
sessions_spawn(
  task="...",
  agent_id="computer_operator",
  mode="run"
)
```

- 明らかに多段で検証が重い場合は control loop を使う

### 3. surface の優先順位を守る

- 「このブラウザ」なら current-tab selector を最優先する
- current-tab が使えないときだけ managed browser fallback を検討する
- visible UI を selector で表せないときだけ desktop target を使う

### 4. 結果確認

- tool の `success` を見ずに「できた」と言わない
- 変化を聞かれたら必要に応じて再観測する
- Host Bridge / Desktop Bridge が足りない場合はそのまま明示する

## ガードレール

- ユーザーが「このブラウザ」と言ったら勝手に別ブラウザを開かない
- パスワードやトークンを無断で入力しない
- current-tab と desktop の両方が使えないなら止まって報告する
