# boiled-claw Dockerfile
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 作業ディレクトリ
WORKDIR /app

# システム依存関係
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

# アプリケーションコピー
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Python依存関係 (Web起動に必要な最小セット)
RUN pip install .

# オプション: ブラウザ自動化をコンテナイメージに含める場合のみ有効化
ARG INSTALL_BROWSER=false
RUN if [ "$INSTALL_BROWSER" = "true" ]; then \
      pip install ".[browser]" && playwright install --with-deps chromium; \
    fi

# データディレクトリ
RUN mkdir -p /app/data /app/skills

# ポート公開
EXPOSE 18789

# ヘルスチェック
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:18789/health || exit 1

# デフォルトコマンド (Webモード)
CMD ["python", "-m", "src.main", "web", "--host", "0.0.0.0", "--port", "18789"]
