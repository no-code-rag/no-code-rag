# === ベースイメージとユーザー作成 ===
FROM python:3.10

# javaldxエラー回避のためのユーザー作成
RUN useradd -ms /bin/bash libreuser

# === OCR・LibreOffice・日本語フォントなど必要パッケージを一括インストール ===
RUN apt-get update && apt-get install -y \
    libreoffice \
    default-jre \
    fonts-noto-cjk \
    tesseract-ocr \
    tesseract-ocr-jpn \
    ghostscript \
    qpdf \
    poppler-utils \
    libglib2.0-0 \
    libsm6 \
    libxrender1 \
    libxext6 \
    libgl1 \
    ocrmypdf \
    sqlite3 \
 && rm -rf /var/lib/apt/lists/*

# === Pythonパッケージのインストール ===
WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip wheel

# -- LanceDB削除、FAISS・STモデル系インストール --
RUN pip install -r requirements.txt
RUN pip install faiss-cpu sentence-transformers tqdm numpy

# === アプリケーションコードを全コピー ===
COPY . .

# === ユーザー切り替え・環境設定 ===
USER libreuser
ENV HOME=/home/libreuser

# === FastAPI or 任意コマンド起動（要調整）===
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]















