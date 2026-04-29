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

### 7. 新機能ユニットテスト

最近追加された機能のユニットテストをまとめて実行する。
gateway 不要・Docker 不要で、ホスト上の `.venv` だけで動く。

#### 7-1. Computer use ツール（observe / click / fill / evaluate / trajectory）

```bash
.venv/bin/pytest tests/test_computer_tools.py -v 2>&1
```

- 全テストが PASSED であること（16 tests 想定）
- 主要パス: surface 優先順位、recovery ループ、re-observe、trajectory 記録

`tests/test_computer_evals.py` が存在する場合はそちらも実行する:

```bash
.venv/bin/pytest tests/test_computer_evals.py -v 2>&1
```

- partial pass / skipped evaluation / trajectory ordering がカバーされていること

#### 7-2. Physical AI ツール（simulation / ROS2 action / dispatch）

```bash
.venv/bin/pytest tests/test_physical_ai_tools.py -v 2>&1
```

- 全テストが PASSED であること（6 tests 想定）
- 主要パス: validated run 記録、"ready" status 拒否、unvalidated dispatch 拒否、store reload 後の dispatch 継続

#### 7-3. Self-improvement ツール（canary / benchmark / package / cleanup）

```bash
.venv/bin/pytest tests/test_self_improvement_tools.py -v 2>&1
```

- 全テストが PASSED であること（5 tests 想定）
- 主要パス: worktree 作成、benchmark 失敗報告、キャッシュ再利用、approved memory 記録、cleanup

#### 7-4. Skills runtime（skill loading / execute）

```bash
.venv/bin/pytest tests/test_skills_runtime.py -v 2>&1
```

- 全テストが PASSED であること（2 tests 想定）
- computer-use スキルのロードと instruction 取得がカバーされていること

#### 7-5. Agent ツール登録確認

```bash
.venv/bin/pytest tests/test_agent.py -v 2>&1
```

- root_agent に以下のツールが全て登録されていること:
  - `computer_observe`, `computer_evaluate`, `computer_click`, `computer_fill`, `computer_trajectory_recent`
  - `physical_ai_submit_simulation`, `physical_ai_build_ros2_action`, `physical_ai_dispatch_ros2_action`
  - `self_improvement_prepare_canary`, `self_improvement_run_benchmarks`, `self_improvement_package_candidate`, `self_improvement_cleanup_canary`

#### 7-6. 全ユニットテスト一括実行（e2e 以外）

```bash
.venv/bin/pytest -q -m 'not e2e' 2>&1
```

- 全テストが PASSED であること
- FAILED / ERROR があれば内容を報告する

### 8. AI CLI スキルの動作確認

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
    expected = {'coding-agent', 'e2e-test', 'code-review', 'multi-llm-judge', 'auto-fix', 'computer-use'}
    missing = expected - set(names)
    if missing:
        print(f'FAIL: missing skills: {missing}')
        exit(1)
    print(f'PASS: {len(names)} skills loaded')

asyncio.run(main())
" 2>&1
```

- 終了コード 0
- 6 スキル全てが登録されていること: `coding-agent`, `e2e-test`, `code-review`, `multi-llm-judge`, `auto-fix`, `computer-use`

#### 8-2. CLI 検出ユーティリティ

```bash
python3 skills/_utils/run_ai_cli.py --detect 2>&1
```

- 終了コード 0
- 少なくとも 1 つの CLI が見つかること（環境依存; フル構成なら claude, codex, gemini の 3 つ）

#### 8-3. 基本 CLI 呼び出し（利用可能な CLI ごと）

検出された各 CLI に対して、簡単なプロンプトで stdin 経由の応答を確認する:

```bash
python3 skills/_utils/run_ai_cli.py --cli claude --prompt "Say only: ok" --timeout 30 2>&1
python3 skills/_utils/run_ai_cli.py --cli codex --prompt "Say only: ok" --timeout 60 2>&1
python3 skills/_utils/run_ai_cli.py --cli gemini --prompt "Say only: ok" --timeout 120 2>&1
```

- 利用可能な CLI が終了コード 0 かつ stdout が空でないこと
- 利用不可の CLI はスキップ（失敗ではない）

#### 8-4. argv 構築ユニットテスト（CLI 不要）

外部 CLI を実際に呼ばず、`run_ai_cli.py` の引数組み立てだけを検証する。
ネットワークや CLI の状態に依存しないため false negative が起きない:

```bash
python3 -c "
import sys, importlib.util
spec = importlib.util.spec_from_file_location('run_ai_cli', 'skills/_utils/run_ai_cli.py')
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

# claude: stdin, no prompt in argv
args, stdin = mod._build_args_and_input('claude', 'test', 'default', [], None, False)
assert args == ['claude', '-p'] and stdin == 'test'

# codex exec: stdin via '-'
args, stdin = mod._build_args_and_input('codex', 'test', 'default', [], None, False)
assert args == ['codex', 'exec', '-'] and stdin == 'test'

# codex review --base: prompt excluded (mutually exclusive)
args, stdin = mod._build_args_and_input('codex', 'ignored', 'review', [], 'main', False)
assert args == ['codex', 'review', '--base', 'main'] and stdin is None

# codex review --uncommitted: prompt excluded
args, stdin = mod._build_args_and_input('codex', '', 'review', [], None, True)
assert args == ['codex', 'review', '--uncommitted'] and stdin is None

# gemini: stdin
args, stdin = mod._build_args_and_input('gemini', 'test', 'default', [], None, False)
assert args == ['gemini'] and stdin == 'test'

print('PASS: all argv construction checks passed')
" 2>&1
```

- 終了コード 0
- 全 5 パターンの assertion が通ること

### 9. 結果サマリを出力

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
| Computer use ツール (N tests) | PASS / FAIL |
| Computer use evals (N tests) | PASS / FAIL / SKIP |
| Physical AI ツール (N tests) | PASS / FAIL |
| Self-improvement ツール (N tests) | PASS / FAIL |
| Skills runtime (N tests) | PASS / FAIL |
| Agent ツール登録 (N tests) | PASS / FAIL |
| 全ユニットテスト (non-e2e) | PASS / FAIL |
| AI スキルロード (6件) | OK / NG |
| CLI 検出ユーティリティ | OK / NG (N件検出) |
| Claude CLI 基本応答 | OK / NG / SKIP |
| Codex CLI 基本応答 | OK / NG / SKIP |
| Gemini CLI 基本応答 | OK / NG / SKIP |
| argv 構築テスト | OK / NG |

（失敗がある場合は詳細と対処法を記載）
（CLI 基本応答の SKIP は当該 CLI が未インストールの場合で、失敗とはみなさない）
（Computer use evals の SKIP は test_computer_evals.py が未マージの場合で、失敗とはみなさない）
```

すべて OK の場合のみ「push OK」と報告すること。
