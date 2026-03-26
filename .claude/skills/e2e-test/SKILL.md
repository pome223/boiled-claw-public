---
name: e2e-test
description: boiled-claw の e2e スモークテストを実行する。gateway の pytest / Docker / HTTP ヘルスチェックをまとめて確認し、すべて Pass した場合のみ「push OK」と報告する。
disable-model-invocation: true
---

# e2e-test

boiled-claw の e2e スモークテストを実行するスキル。

## 手順

以下をすべて実行し、最後に結果サマリを出力すること。

### 1. Docker コンテナの再起動（ビルド込み）

```bash
docker compose up -d --build 2>&1
```

- ビルドとコンテナ起動が成功すること
- エラーが出た場合は `docker compose logs --tail=30` で原因を調査する

起動後、ヘルスチェックが通るまで待機する:

```bash
# 最大60秒待つ
for i in $(seq 1 12); do
  STATUS=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:18789/health 2>/dev/null)
  if [ "$STATUS" = "200" ]; then echo "Gateway ready"; break; fi
  echo "Waiting... ($i/12)"
  sleep 5
done
```

- `Gateway ready` が表示されること
- 60秒待っても起動しない場合は `docker compose logs boiled-claw-gateway --tail=50` を確認して報告する

### 2. pytest e2e テストを実行

```bash
.venv/bin/pytest tests/test_e2e.py -v -m e2e 2>&1
```

- 全テストが PASSED であることを確認する
- FAILED / ERROR があれば内容を報告し、原因を調査・修正する

### 3. Docker コンテナの状態確認

```bash
docker compose ps 2>&1
```

- `boiled-claw-gateway` が `Up (healthy)` であることを確認する
- そうでなければ `docker compose logs boiled-claw-gateway --tail=30` でログを確認する

### 4. HTTP ヘルスチェック

```bash
curl -s http://localhost:18789/health
```

- `{"status":"healthy"}` が返ることを確認する

### 5. API プロンプト送信テスト

実際に LLM にプロンプトを送って応答が返ることを確認する。

#### 4-1. 基本応答テスト

```bash
curl -s -X POST http://localhost:18789/agent/run \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"e2e-skill","message":"一言で答えて: 1+1は?"}' 2>&1
```

- `"ok": true` であること
- `"response"` に何らかのテキストが含まれること（空でないこと）
- `"session_id"` が返っていること

#### 4-2. セッション継続テスト

```bash
# ステップ1: 名前を覚えさせる
curl -s -X POST http://localhost:18789/agent/run \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"e2e-skill-sess","message":"私の名前はスキルテスト太郎です。覚えておいてください。"}' 2>&1
```

- レスポンスから `session_id` を取得する

```bash
# ステップ2: 同じ session_id で名前を聞き返す
curl -s -X POST http://localhost:18789/agent/run \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"e2e-skill-sess","session_id":"<上で取得した session_id>","message":"さっき私が名乗った名前を教えて"}' 2>&1
```

- `"response"` に「スキルテスト太郎」が含まれること

#### 4-3. セッション一覧取得テスト

```bash
curl -s http://localhost:18789/sessions/e2e-skill 2>&1
```

- ステータスコード 200 であること
- `"sessions"` キーが存在すること

### 6. CLI 動作確認

CLIのサブコマンド体系・エイリアス・フラグが正しく動作することを確認する。

#### 6-1. ヘルプ表示

```bash
python -m src.main --help 2>&1
```

- `chat`, `web`, `channels`, `bridge`, `status` の5コマンドが表示されること
- `--version`, `-v, --verbose` オプションが表示されること

#### 6-2. レガシーエイリアスの互換性

```bash
python -m src.main cli --help 2>&1
python -m src.main host-bridge --help 2>&1
python -m src.main desktop-bridge --help 2>&1
```

- `cli` → `chat` のヘルプが表示されること
- `host-bridge` → `bridge host` のヘルプが表示されること
- `desktop-bridge` → `bridge desktop` のヘルプが表示されること
- いずれも `No such command` エラーにならないこと

#### 6-3. bridge サブコマンド

```bash
python -m src.main bridge --help 2>&1
```

- `host`, `desktop` のサブコマンドが表示されること

#### 6-4. chat フラグ

```bash
python -m src.main chat --help 2>&1
```

- `--model`, `--dry-run` オプションが表示されること

#### 6-5. dry-run テスト

```bash
python -m src.main chat --dry-run 2>&1
```

- `Config OK. Dry-run mode` が表示されてエラーなく終了すること

#### 6-6. status コマンド

```bash
python -m src.main status 2>&1
```

- Configuration / Bridge Status / Channels の3テーブルが表示されること
- エラーなく終了すること

#### 6-7. 引数なし実行（デフォルト動作）

```bash
GOOGLE_API_KEY= python -m src.main 2>&1
```

- `GOOGLE_API_KEY is not set` のエラーメッセージが表示されること（トレースバックではないこと）
- chat がデフォルトで起動しようとしていることを確認

### 7. AI CLI スキルの動作確認

外部 AI CLI 連携スキルのロードとユーティリティの動作を確認する。

#### 7-1. スキルロード確認

```bash
python3 -c "
import asyncio
from src.skills.runtime import ensure_skills_loaded
from src.skills.base import get_skill_registry

async def main():
    await ensure_skills_loaded()
    registry = get_skill_registry()
    names = [m.name for m in registry.list_skills()]
    expected = {'coding-agent', 'e2e-test', 'code-review', 'multi-llm-judge', 'auto-fix'}
    missing = expected - set(names)
    if missing:
        print(f'FAIL: missing skills: {missing}')
        exit(1)
    print(f'PASS: {len(names)} skills loaded')

asyncio.run(main())
" 2>&1
```

- 終了コード 0
- 5 スキル全てが登録されていること: `coding-agent`, `e2e-test`, `code-review`, `multi-llm-judge`, `auto-fix`

#### 7-2. CLI 検出ユーティリティ

```bash
python3 skills/_utils/run_ai_cli.py --detect 2>&1
```

- 終了コード 0
- 少なくとも 1 つの CLI が見つかること（環境依存; フル構成なら claude, codex, gemini の 3 つ）

#### 7-3. 基本 CLI 呼び出し（利用可能な CLI ごと）

検出された各 CLI に対して、簡単なプロンプトで stdin 経由の応答を確認する:

```bash
python3 skills/_utils/run_ai_cli.py --cli claude --prompt "Say only: ok" --timeout 30 2>&1
python3 skills/_utils/run_ai_cli.py --cli codex --prompt "Say only: ok" --timeout 60 2>&1
python3 skills/_utils/run_ai_cli.py --cli gemini --prompt "Say only: ok" --timeout 120 2>&1
```

- 利用可能な CLI が終了コード 0 かつ stdout が空でないこと
- 利用不可の CLI はスキップ（失敗ではない）

#### 7-4. Codex review モード

```bash
python3 skills/_utils/run_ai_cli.py --cli codex --mode review --review-base main --timeout 60 2>&1
```

- 終了コード 0（diff なしの場合でも正常終了であれば OK）
- 引数パースエラーが出ないこと

### 8. 結果サマリを出力

以下の形式で報告すること:

```
## E2E 結果

| チェック | 結果 |
|---------|------|
| Docker 再起動 | OK / NG |
| pytest e2e (N tests) | PASS / FAIL |
| gateway container | OK / NG |
| HTTP /health | OK / NG |
| API 基本応答 | OK / NG |
| API セッション継続 | OK / NG |
| API セッション一覧 | OK / NG |
| CLI ヘルプ表示 | OK / NG |
| CLI レガシーエイリアス | OK / NG |
| CLI bridge サブコマンド | OK / NG |
| CLI chat フラグ | OK / NG |
| CLI dry-run | OK / NG |
| CLI status コマンド | OK / NG |
| CLI デフォルト動作 | OK / NG |
| AI スキルロード (5件) | OK / NG |
| CLI 検出ユーティリティ | OK / NG (N件検出) |
| Claude CLI 基本応答 | OK / NG / SKIP |
| Codex CLI 基本応答 | OK / NG / SKIP |
| Gemini CLI 基本応答 | OK / NG / SKIP |
| Codex review モード | OK / NG / SKIP |

（失敗がある場合は詳細と対処法を記載）
（CLI 基本応答の SKIP は当該 CLI が未インストールの場合で、失敗とはみなさない）
```

すべて OK の場合のみ「push OK」と報告すること。
