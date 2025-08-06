import logging
import sqlite3
import json
from pathlib import Path
from typing import List, Dict, Any
from fastapi import FastAPI
from pydantic import BaseModel
import faiss
import numpy as np
from sentence_transformers import SentenceTransformer
from fastapi.middleware.cors import CORSMiddleware

# FastAPI 初期化
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 埋め込みモデル読み込み
MODEL_PATH = "/mydata/llm/vector/models/legal-bge-m3"
model = SentenceTransformer(MODEL_PATH).to("cpu")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logging.info(f"[INFO] 埋め込みモデル読み込み完了: {MODEL_PATH}")

# ベクトル・メタDBパス
FAISS_INDEXES = {
    "pdf_word": Path("/mydata/llm/vector/db/faiss/pdf_word/index.faiss"),
    "excel_calendar": Path("/mydata/llm/vector/db/faiss/excel_calendar/index.faiss"),
}
SQLITE_PATHS = {
    "pdf_word": Path("/mydata/llm/vector/db/faiss/pdf_word/metadata.sqlite3"),
    "excel_calendar": Path("/mydata/llm/vector/db/faiss/excel_calendar/metadata.sqlite3"),
}
CHUNK_DIR = Path("/mydata/llm/vector/db/chunk")

# リクエストスキーマ
class EmbedRequest(BaseModel):
    query: str
    top_k: int = 30
    keywords: List[str] = []

# チャンク読込関数
def load_chunk_text(path: str, index: int) -> str:
    chunk_file = CHUNK_DIR / f"{path}.jsonl"
    if not chunk_file.exists():
        logging.warning(f"[WARN] チャンクファイルなし: {chunk_file}")
        return ""
    try:
        with open(chunk_file, encoding="utf-8") as f:
            for line in f:
                obj = json.loads(line)
                if obj.get("index") == index:
                    return obj.get("text", "")
    except Exception as e:
        logging.error(f"[ERROR] チャンク読込失敗: {chunk_file} → {e}")
    return ""

# ベクトル検索
@app.post("/embed_search")
async def embed_search(req: EmbedRequest) -> Dict[str, Any]:
    logging.info(f"[INFO] クエリ: {req.query} (top_k={req.top_k})")

    # ベクトル化
    embedding = np.array(model.encode([req.query], normalize_embeddings=True), dtype=np.float32)
    if embedding.ndim == 1:
        embedding = embedding.reshape(1, -1)

    hits = []

    # DBごとに検索
    for db_group, sqlite_path in SQLITE_PATHS.items():
        if not sqlite_path.exists() or not FAISS_INDEXES[db_group].exists():
            logging.warning(f"[WARN] DB見つからず: {db_group}")
            continue

        index = faiss.read_index(str(FAISS_INDEXES[db_group]))
        D, I = index.search(embedding, req.top_k)

        with sqlite3.connect(str(sqlite_path)) as conn:
            cur = conn.cursor()
            for score, vec_index in zip(D[0], I[0]):
                if vec_index == -1:
                    continue
                cur.execute("SELECT uid, chunk_index, path, type FROM vector_metadata WHERE vec_index=?", (int(vec_index),))
                row = cur.fetchone()
                if not row:
                    continue
                uid, chunk_index, path, dtype = row
                text = load_chunk_text(path, chunk_index)
                if not text.strip():
                    continue
                hits.append({
                    "vec_index": int(vec_index),
                    "uid": uid,
                    "chunk_index": int(chunk_index),
                    "path": path,
                    "type": dtype,
                    "score": float(score),
                    "source": db_group,
                    "text": text
                })

        logging.info(f"[INFO] ヒット件数: {len(hits)} 件 → {db_group}")

    if not hits:
        return {"hits": [], "filtered_hits": [], "grouped_chunks": [], "context_text": ""}

    # スコア調整＋フィルタ
    adjusted_hits = []
    for h in hits:
        score = h["score"]
        if any(kw in h["text"] for kw in req.keywords):
            score += 0.1
        h["adjusted_score"] = round(score, 4)
        if score >= 0.675:
            adjusted_hits.append(h)

    logging.info(f"[INFO] filtered_hits: {len(adjusted_hits)} 件（score >= 0.675）")

    # チャンク選定（Word/PDF・Excel/Calendar）
    grouped_chunks = []
    seen = set()

    # Word/PDF: 上位3件＋前後1、続く3件はそのまま
    word_hits = sorted([h for h in adjusted_hits if h["source"] == "pdf_word"], key=lambda x: -x["adjusted_score"])
    top3 = word_hits[:3]
    next3 = word_hits[3:6]

    for target in top3:
        for offset in [-1, 0, 1]:
            idx = target["chunk_index"] + offset
            uid = f"{target['path']}:{idx}"
            if uid in seen:
                continue
            seen.add(uid)
            text = load_chunk_text(target["path"], idx)
            if text.strip():
                grouped_chunks.append({
                    "path": target["path"],
                    "chunk_index": idx,
                    "text": text,
                    "type": target["type"],
                    "score": target["adjusted_score"]
                })

    for h in next3:
        uid = f"{h['path']}:{h['chunk_index']}"
        if uid in seen:
            continue
        seen.add(uid)
        grouped_chunks.append({
            "path": h["path"],
            "chunk_index": h["chunk_index"],
            "text": h["text"],
            "type": h["type"],
            "score": h["adjusted_score"]
        })

    # Excel/Calendar: 上位15件
    excel_hits = sorted([h for h in adjusted_hits if h["source"] == "excel_calendar"], key=lambda x: -x["adjusted_score"])[:15]
    for h in excel_hits:
        uid = f"{h['path']}:{h['chunk_index']}"
        if uid in seen:
            continue
        seen.add(uid)
        grouped_chunks.append({
            "path": h["path"],
            "chunk_index": h["chunk_index"],
            "text": h["text"],
            "type": h["type"],
            "score": h["adjusted_score"]
        })

    logging.info(f"[INFO] grouped_chunks: {len(grouped_chunks)} 件")

    # context_text 構築
    context_parts = []
    for chunk in sorted(grouped_chunks, key=lambda x: -x["score"]):
        label = f"[FILE] {chunk['path']}（{chunk['type']}）"
        context_parts.append(label)
        context_parts.append(chunk["text"])

    context_text = "\n\n".join(context_parts)
    logging.info(f"[INFO] context_text 文字数: {len(context_text)} 文字")

    return {
        "context_text": context_text
    }

# ヘルスチェック
@app.get("/")
async def root():
    return {"message": "RAG Search API OK"}




















































