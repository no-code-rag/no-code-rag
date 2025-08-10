FROM python:3.11-slim

# llama.cpp のビルドに必要なツール & ライブラリ
RUN apt-get update && apt-get install -y \
    build-essential cmake curl git pkg-config \
    libopenblas-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依存関係ファイルを先にコピー
COPY requirements.txt /app/requirements.txt

# 依存関係インストール（llama-cpp-pythonはGitHub最新版）
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "llama-cpp-python[server] @ git+https://github.com/abetlen/llama-cpp-python.git"

# server.py は起動時にマウント
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
