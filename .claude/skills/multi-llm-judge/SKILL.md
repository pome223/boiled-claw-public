---
name: multi-llm-judge
description: 同一プロンプトを複数 AI CLI（Claude Code / Codex / Gemini）に送り、回答を比較・評価する。
disable-model-invocation: true
---

# multi-llm-judge

同一のプロンプトを複数の外部 AI CLI に投げて、回答の比較・評価を行うスキル。

## 手順

### 1. 利用可能な CLI を検出

```bash
echo "=== AI CLI 検出 ==="
which claude 2>/dev/null && echo "claude: OK" || echo "claude: NOT FOUND"
which codex 2>/dev/null && echo "codex: OK" || echo "codex: NOT FOUND"
which gemini 2>/dev/null && echo "gemini: OK" || echo "gemini: NOT FOUND"
```

- 比較には最低 2 つの CLI が必要
- 1 つしかない場合はユーザーに警告した上で単独回答として進める

### 2. プロンプトを準備

ユーザーのプロンプト/タスクをそのまま使う:
- 全 LLM に完全に同じテキストを送る
- コードが関連する場合は該当ファイルの内容をコンテキストに含める
- プロンプトを `/tmp/bc_judge_prompt.txt` に書き出す
- 10,000 文字を超える場合は要約またはチャンク化する
- シークレットやクレデンシャルは絶対に含めない

### 3. 各 CLI に送信

```bash
# Claude Code（非対話 print モード）
cat /tmp/bc_judge_prompt.txt | claude -p 2>&1 | tee /tmp/bc_judge_claude.txt

# Codex（非対話 exec モード）
cat /tmp/bc_judge_prompt.txt | codex exec - 2>&1 | tee /tmp/bc_judge_codex.txt

# Gemini CLI
cat /tmp/bc_judge_prompt.txt | gemini 2>&1 | tee /tmp/bc_judge_gemini.txt
```

- 各 CLI のタイムアウトは 180 秒
- 失敗・タイムアウトした CLI は「利用不可」として記録し、残りで続行する

### 4. 回答を比較・評価

以下の形式でレポートを出力する:

```markdown
## Multi-LLM 比較レポート

### プロンプト
> {送信したプロンプト}

### 回答

#### Claude
{回答内容}

#### Codex
{回答内容}

#### Gemini
{回答内容}

### 分析

#### 一致点
全/大半の LLM が一致した点:
- ...

#### 相違点
LLM 間で異なった点:
- ...

#### 品質評価
| 観点       | Claude | Codex | Gemini |
|-----------|--------|-------|--------|
| 正確性     | ...    | ...   | ...    |
| 網羅性     | ...    | ...   | ...    |
| コード品質  | ...    | ...   | ...    |
| 説明の質   | ...    | ...   | ...    |

#### 推奨
比較に基づく結論: {どの回答が最も優れているか、その理由}
```

### 5. 評価モード

ユーザーが指定できる評価方法:

- **best-of-n**: 最良の回答を選び理由を説明
- **consensus**: 全 LLM が一致した点のみ抽出（高確信度の回答）
- **full-comparison**: 全回答を詳細比較（デフォルト）
- **merge**: 各回答の最良部分を合成して改良版を作成

ユーザーが「consensus で」「merge して」などと指定した場合、そのモードで評価する。
