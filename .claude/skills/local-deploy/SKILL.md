---
name: local-deploy
description: 現在のブランチをローカルにデプロイし、Host Bridge 経由のブラウザ自動化が動作することを確認する。「ローカルデプロイ」「ローカルに立ち上げて」「デプロイして動作確認」で起動。
disable-model-invocation: true
---

# local-deploy

現在のブランチをローカル環境にデプロイし、Gateway + Host Bridge の疎通を確認するスキル。

## 手順

### 1. Host Bridge をホスト OS 上で起動

```bash
# 既存プロセスがあれば確認
lsof -i :8766 2>/dev/null | head -5

# なければ起動（バックグラウンド）
BRIDGE_ALLOW_REMOTE_BIND=true \
BROWSER_ALLOW_LOOPBACK=true \
.venv/bin/python -m src.main host-bridge --host 0.0.0.0 --port 8766 &

# 起動確認（3秒待機）
sleep 3 && lsof -i :8766
```

- `0.0.0.0` でバインドする（Docker からの `host.docker.internal` 接続を受けるため）
- `BRIDGE_ALLOW_REMOTE_BIND=true` が必要

### 2. .env の確認

以下の値が設定されていることを確認する（なければ修正）:

```
HOST_BRIDGE_ENABLED=true
HOST_BRIDGE_URL=http://host.docker.internal:8766/sse
BRIDGE_ALLOW_REMOTE_BIND=true
BROWSER_ALLOW_LOOPBACK=true
```

### 3. Docker コンテナのビルド＆起動

```bash
docker compose up --build -d boiled-claw-gateway boiled-claw-mcp-sample
```

- ビルドが成功すること
- エラー時は `docker compose logs --tail=30` で確認

### 4. 起動確認

```bash
# ヘルスチェック（最大30秒待機）
for i in $(seq 1 6); do
  STATUS=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:18789/health 2>/dev/null)
  if [ "$STATUS" = "200" ]; then echo "Gateway ready"; break; fi
  echo "Waiting... ($i/6)"
  sleep 5
done

# Docker → Host Bridge 疎通確認
docker exec boiled-claw-gateway curl -s -m 5 http://host.docker.internal:8766/sse | head -3
```

- Gateway が `200` を返すこと
- Docker から Host Bridge の SSE エンドポイントに接続できること（`event: endpoint` が返る）

### 5. ブラウザ自動化テスト

WebSocket 経由でブラウザ操作を実行する:

```bash
.venv/bin/python skills/local-runtime-smoke/scripts/ws_smoke.py \
  --message "browser_navigateツールを使ってhttps://tenki.jp/forecast/3/16/にアクセスし、browser_extract_textでページのテキストを取得してください" \
  --expect-tool-prefix host.browser. \
  --timeout 120
```

または `test_weather.py` を使用:

```bash
PYTHONUNBUFFERED=1 .venv/bin/python test_weather.py
```

### 6. 結果サマリを出力

```
## Local Deploy 結果

| チェック | 結果 |
|---------|------|
| Host Bridge 起動 | OK / NG |
| .env 設定 | OK / NG |
| Docker ビルド＆起動 | OK / NG |
| Gateway ヘルスチェック | OK / NG |
| Docker → Host Bridge 疎通 | OK / NG |
| ブラウザ自動化テスト | OK / NG |

（失敗がある場合は詳細と対処法を記載）
```

## トラブルシューティング

### `Invalid Host header`
- Host Bridge が `127.0.0.1` にバインドされている → `0.0.0.0` で再起動
- `BRIDGE_ALLOW_REMOTE_BIND=true` が設定されているか確認

### Approval タイムアウト
- WebSocket クライアントが `tools.approval_request` に対して `{"event": "tools.approval", "request_id": "...", "approved": true}` を返す必要がある
- `decision` ではなく `approved` (bool) が正しいフィールド名

### Host Bridge にブラウザがない
- `.venv` に Playwright がインストールされているか確認: `.venv/bin/python -c "from playwright.sync_api import sync_playwright; print('OK')"`
- ブラウザ未インストールなら: `.venv/bin/playwright install chromium`
