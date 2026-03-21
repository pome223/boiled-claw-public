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

### 6. 結果サマリを出力

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

（失敗がある場合は詳細と対処法を記載）
```

すべて OK の場合のみ「push OK」と報告すること。
