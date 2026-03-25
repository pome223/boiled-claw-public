---
name: code-review
description: git diff を外部 AI CLI（Claude Code / Codex / Gemini）に送ってレビューし、結果を集約・比較する。
disable-model-invocation: true
---

# code-review

git の差分を外部 AI CLI に送ってコードレビューを実施するスキル。

## 手順

### 1. 利用可能な CLI を検出

```bash
echo "=== AI CLI 検出 ==="
which claude 2>/dev/null && echo "claude: OK" || echo "claude: NOT FOUND"
which codex 2>/dev/null && echo "codex: OK" || echo "codex: NOT FOUND"
which gemini 2>/dev/null && echo "gemini: OK" || echo "gemini: NOT FOUND"
```

- 最低1つの CLI が利用可能であること
- 利用不可の CLI はスキップする

### 2. 差分を取得

ユーザーの指示に応じてスコープを決める:

```bash
# 未ステージの変更
git diff

# ステージ済みの変更
git diff --cached

# ブランチ比較
git diff main...HEAD

# 特定のコミット範囲
git diff <from>..<to>
```

- 差分が空の場合は「レビュー対象なし」と報告して終了
- 差分が 8000 文字を超える場合は、変更量の多いファイルを優先して切り詰める
- `.env`、シークレット、クレデンシャルを含む行は除外する
- `API_KEY`、`SECRET`、`TOKEN`、`PASSWORD` を含む行をフィルタする

### 3. レビュープロンプトを作成

差分内容とレビュー観点を含むプロンプトを一時ファイルに書き出す:

```
以下の git diff をレビューしてください。観点: {ユーザー指定の観点 or セキュリティ、パフォーマンス、可読性}
問題を severity (critical/warning/info), ファイル名, 行番号, 説明のリストで報告してください。

---
{diff内容}
---
```

これを `/tmp/bc_review_prompt.txt` に保存する。

### 4. 各 CLI にレビューを送信

利用可能な各 CLI に順番に送信する:

```bash
# Claude Code（非対話 print モード）
cat /tmp/bc_review_prompt.txt | claude -p 2>&1 | tee /tmp/bc_review_claude.txt

# Codex（専用 review サブコマンドがあればそちらを優先）
codex review --base main "セキュリティとエラーハンドリングに注目" 2>&1 | tee /tmp/bc_review_codex.txt
# または汎用プロンプト:
# cat /tmp/bc_review_prompt.txt | codex exec - 2>&1 | tee /tmp/bc_review_codex.txt

# Gemini CLI
cat /tmp/bc_review_prompt.txt | gemini 2>&1 | tee /tmp/bc_review_gemini.txt
```

- 各 CLI のタイムアウトは 120 秒
- 失敗した CLI はエラーを記録してスキップする

### 5. 結果を集約して報告

以下の形式でレポートを出力する:

```markdown
## Code Review Report

### サマリ
- レビュアー: {利用した CLI 一覧}
- スコープ: {ブランチ名 / コミット範囲}
- 総指摘数: N

### レビュアー別指摘

#### Claude
- [critical] file.py:42 — SQL インジェクションのリスク
- [warning] utils.py:15 — 未使用の import

#### Codex
- [warning] file.py:42 — パラメータ化クエリを検討
...

#### Gemini
...

### コンセンサス（2+ レビュアーが一致）
- file.py:42 — 安全でないクエリ（Claude, Codex）

### 意見の相違（人間の判断が必要）
- ...
```

### 6. 結果の保存（オプション）

ユーザーが希望する場合、レビュー結果を memory に保存する。
