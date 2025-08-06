#!/usr/bin/env python3
import os
import json
import sqlite3
import faiss
import numpy as np
from pathlib import Path
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from sentence_transformers import SentenceTransformer

# === 設定 ===
ROOT = Path("/mydata/llm/vector")
CHUNK_DIR = ROOT / "db/chunk"
CHUNK_LOG = ROOT / "db/log/chunk_log.jsonl"
SQLITE_PATH = ROOT / "db/faiss/pdf_word/metadata.sqlite3"
FAISS_PATH = ROOT / "db/faiss/pdf_word/index.faiss"
MODEL_PATH = "/mydata/llm/vector/models/legal-bge-m3"

model = SentenceTransformer(MODEL_PATH)
VECTOR_DIM = model.get_sentence_embedding_dimension()
THREAD_WORKERS = max(1, os.cpu_count() - 2)
BATCH_CHUNK_SIZE = 500
TIMEOUT_SEC = 300

def init_sqlite():
    with sqlite3.connect(SQLITE_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vector_metadata (
                vec_index INTEGER PRIMARY KEY,
                uid TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                path TEXT NOT NULL,
                type TEXT NOT NULL,
                vector BLOB NOT NULL
            )
        """)

def get_existing_uids_from_db():
    if not SQLITE_PATH.exists():
        return set()
    with sqlite3.connect(SQLITE_PATH) as conn:
        rows = conn.execute("SELECT DISTINCT uid FROM vector_metadata").fetchall()
        return {r[0] for r in rows}

def load_chunk_log():
    if not CHUNK_LOG.exists():
        print("[INFO] chunk_log.jsonl が存在しません")
        return []
    chunks = []
    with CHUNK_LOG.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
                if e.get("type") in ("pdf", "word"):
                    chunks.append(e)
            except:
                continue
    return chunks

def collect_target_chunks(all_chunks, existing_uids):
    return [c for c in all_chunks if c["uid"] not in existing_uids]

def load_chunk_texts(target_chunks):
    enriched = []
    for c in target_chunks:
        uid, index, rel_path, ftype = c["uid"], c["index"], c["path"], c["type"]
        chunk_file = CHUNK_DIR / (rel_path + ".jsonl")
        if not chunk_file.exists():
            continue
        try:
            with chunk_file.open("r", encoding="utf-8") as f:
                for line in f:
                    entry = json.loads(line)
                    if entry.get("index") == index:
                        text = entry.get("text", "").strip()
                        if text:
                            enriched.append({
                                "uid": uid,
                                "index": index,
                                "path": rel_path,
                                "type": ftype,
                                "text": text
                            })
                        break
        except:
            continue
    return enriched

def encode_batch(texts):
    return model.encode(
        texts,
        convert_to_numpy=True,
        normalize_embeddings=True
    ).tolist()

def insert_to_sqlite(start_index, metas, embeddings):
    with sqlite3.connect(SQLITE_PATH) as conn:
        cur = conn.cursor()
        for offset, (meta, vec) in enumerate(zip(metas, embeddings)):
            cur.execute(
                "INSERT INTO vector_metadata VALUES (?, ?, ?, ?, ?, ?)",
                (
                    start_index + offset,
                    meta["uid"],
                    meta["index"],
                    meta["path"],
                    meta["type"],
                    sqlite3.Binary(np.array(vec, dtype=np.float32).tobytes())
                )
            )
        conn.commit()

def main():
    print("▶️ make_vector_pdf_word 開始（ログなし高速版）")
    init_sqlite()

    all_chunks = load_chunk_log()
    if not all_chunks:
        print("✅ chunk_log が空のため終了")
        return

    existing_uids = get_existing_uids_from_db()
    target_chunks_meta = collect_target_chunks(all_chunks, existing_uids)

    if not target_chunks_meta:
        print("✅ 新規登録対象なし")
        return

    target_chunks = load_chunk_texts(target_chunks_meta)
    print(f"[INFO] 登録対象チャンク数: {len(target_chunks)} 件")

    if FAISS_PATH.exists():
        index = faiss.read_index(str(FAISS_PATH))
        vec_index = index.ntotal
        print(f"[INFO] 既存FAISSあり: {vec_index}件から再開")
    else:
        index = faiss.IndexFlatIP(VECTOR_DIM)
        vec_index = 0
        print(f"[INFO] 新規FAISS作成（次元数: {VECTOR_DIM}）")

    for i in range(0, len(target_chunks), BATCH_CHUNK_SIZE):
        batch = target_chunks[i:i + BATCH_CHUNK_SIZE]
        texts = [c["text"] for c in batch]
        metas = batch

        emb = []
        with ThreadPoolExecutor(max_workers=THREAD_WORKERS) as ex:
            futures = [ex.submit(encode_batch, [t]) for t in texts]
            for f in tqdm(as_completed(futures), total=len(futures),
                          desc=f"ベクトル生成中({i // BATCH_CHUNK_SIZE + 1}バッチ目)"):
                try:
                    emb.extend(f.result(timeout=TIMEOUT_SEC))
                except TimeoutError:
                    print("⚠️ タイムアウト発生、スキップ")

        index.add(np.array(emb, dtype=np.float32))
        insert_to_sqlite(vec_index, metas, emb)
        vec_index += len(emb)

    faiss.write_index(index, str(FAISS_PATH))
    print(f"✅ Vector登録完了: 新規登録 {len(target_chunks)} 件 / 総計 {len(all_chunks)} 件")

if __name__ == "__main__":
    main()












































































































