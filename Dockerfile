# boiled-claw Dockerfile
FROM python:3.13-slim AS base

# 作業ディレクトリ
WORKDIR /app

# システム依存関係
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# アプリケーションコピー (先にソースを入れる)
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY tests/ ./tests/

# Python依存関係
RUN pip install --no-cache-dir ".[all]"

# Playwrightインストール (ブラウザ自動化)
RUN playwright install --with-deps chromium

# データディレクトリ
RUN mkdir -p /app/data /app/skills

# ポート公開
EXPOSE 18789

# ヘルスチェック
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:18789/health || exit 1

# デフォルトコマンド (Webモード)
CMD ["python", "-m", "src.main", "web", "--host", "0.0.0.0", "--port", "18789"]
