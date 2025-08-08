# main.py (fix: SELECT uuid -> uid)
import logging
import sqlite3
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import List, Dict, Any
from fastapi import FastAPI
from pydantic import BaseModel
import faiss
import numpy as np
from sentence_transformers import SentenceTransformer
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MODEL_PATH = "/mydata/llm/vector/models/legal-bge-m3"
model = SentenceTransformer(MODEL_PATH).to("cpu")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logging.info(f"[INFO] 埋め込みモデル読み込み完了: {MODEL_PATH}")

FAISS_INDEXES = {
    "pdf_word": Path("/mydata/llm/vector/db/faiss/pdf_word/index.faiss"),
    "excel_calendar": Path("/mydata/llm/vector/db/faiss/excel_calendar/index.faiss"),
}
SQLITE_PATHS = {
    "pdf_word": Path("/mydata/llm/vector/db/faiss/pdf_word/metadata.sqlite3"),
    "excel_calendar": Path("/mydata/llm/vector/db/faiss/excel_calendar/metadata.sqlite3"),
}
CHUNK_DIR = Path("/mydata/llm/vector/db/chunk")

JP_TOKEN = re.compile(r"[ぁ-んァ-ン一-龥A-Za-z0-9]+")

def _normalize_score(s: float, lo=0.5, hi=0.9) -> float:
    s = max(lo, min(hi, s))
    return (s - lo) / (hi - lo)

def _build_bigrams(words: List[str]) -> List[str]:
    ws = [w for w in words if w]
    return ["".join(p) for p in zip(ws, ws[1:])]

def _keyword_score(text: str, kws: List[str], bigrams: List[str]) -> float:
    if not kws:
        return 0.0
    hit = sum(1 for k in kws if k and k in text)
    base = hit / max(1, len(kws))
    bigram_bonus = 0.2 if any(b and b in text for b in bigrams) else 0.0
    return min(1.0, base + bigram_bonus)

def _compute_adjacency_bonus(cands: List[Dict[str, Any]]) -> List[float]:
    by_doc = defaultdict(list)
    for i, c in enumerate(cands):
        by_doc[c["path"]].append((i, c["chunk_index"], c.get("_base", 0.0)))
    bonus = [0.0] * len(cands)
    for items in by_doc.values():
        items.sort(key=lambda x: x[1])
        for j, (i_idx, idx, bscore) in enumerate(items):
            for k in (-2, -1, 1, 2):
                jj = j + k
                if 0 <= jj < len(items):
                    _, idx2, b2 = items[jj]
                    if abs(idx2 - idx) == 1 and b2 >= 0.55:
                        bonus[i_idx] = max(bonus[i_idx], 0.15)
                    elif abs(idx2 - idx) == 2 and b2 >= 0.60:
                        bonus[i_idx] = max(bonus[i_idx], 0.10)
    return bonus

def _extract_keywords_from_query(q: str, limit=12) -> List[str]:
    toks = [t for t in JP_TOKEN.findall(q) if len(t) >= 2]
    seen, out = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t); out.append(t)
        if len(out) >= limit: break
    return out

def rerank_candidates(
    query: str,
    base_candidates: List[Dict[str, Any]],
    given_keywords: List[str] = None,
    use_adjacency: bool = False,
    final_topk: int = 8,
    weights=(0.6, 0.3, 0.1),
) -> List[Dict[str, Any]]:
    if not base_candidates:
        return []
    kws = given_keywords or []
    if not kws:
        kws = _extract_keywords_from_query(query)
    bigrams = _build_bigrams(kws)

    w_embed, w_kw, w_adj = weights
    gated: List[Dict[str, Any]] = []
    for c in base_candidates:
        kw = _keyword_score(c["text"], kws, bigrams)
        if kw > 0.0 or c["score"] >= 0.80:
            c["_embed"] = _normalize_score(c["score"])
            c["_kw"] = kw
            c["_base"] = w_embed * c["_embed"] + w_kw * c["_kw"]
            gated.append(c)

    if not gated:
        gated = base_candidates[:]

    adj_bonus = [0.0] * len(gated)
    if use_adjacency:
        adj_bonus = _compute_adjacency_bonus(gated)

    for c, bn in zip(gated, adj_bonus):
        c["_adj"] = bn if use_adjacency else 0.0
        c["_rerank"] = c["_base"] + w_adj * c["_adj"]

    seen_pi = set()
    uniq = []
    for c in sorted(gated, key=lambda x: x["_rerank"], reverse=True):
        key = (c["path"], c["chunk_index"])
        if key in seen_pi:
            continue
        seen_pi.add(key)
        c["adjusted_score"] = round(float(c["_rerank"]), 4)
        for k in ("_embed","_kw","_base","_adj","_rerank"):
            c.pop(k, None)
        uniq.append(c)
        if len(uniq) >= max(1, final_topk):
            break
    return uniq

class EmbedRequest(BaseModel):
    query: str
    top_k: int = 30
    keywords: List[str] = []

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

@app.post("/embed_search")
async def embed_search(req: EmbedRequest) -> Dict[str, Any]:
    logging.info(f"[INFO] クエリ: {req.query} (top_k={req.top_k})")

    embedding = np.array(model.encode([req.query], normalize_embeddings=True), dtype=np.float32)
    if embedding.ndim == 1:
        embedding = embedding.reshape(1, -1)

    step1_hits: List[Dict[str, Any]] = []

    for db_group, sqlite_path in SQLITE_PATHS.items():
        if not sqlite_path.exists() or not FAISS_INDEXES[db_group].exists():
            logging.warning(f"[WARN] DB見つからず: {db_group}")
            continue

        index = faiss.read_index(str(FAISS_INDEXES[db_group]))
        k_search = max(req.top_k, 50)
        D, I = index.search(embedding, k_search)

        with sqlite3.connect(str(sqlite_path)) as conn:
            cur = conn.cursor()
            for score, vec_index in zip(D[0], I[0]):
                if vec_index == -1:
                    continue
                # ★ fix: uid カラムを使用
                cur.execute(
                    "SELECT uid, chunk_index, path, type FROM vector_metadata WHERE vec_index=?",
                    (int(vec_index),),
                )
                row = cur.fetchone()
                if not row:
                    continue
                uid, chunk_index, path, dtype = row
                text = load_chunk_text(path, int(chunk_index))
                if not text.strip():
                    continue
                step1_hits.append({
                    "vec_index": int(vec_index),
                    "uid": uid,
                    "chunk_index": int(chunk_index),
                    "path": path,
                    "type": dtype,
                    "score": float(score),
                    "source": db_group,
                    "text": text,
                })

        logging.info(f"[INFO] ヒット件数: {len([h for h in step1_hits if h['source']==db_group])} 件 → {db_group}")

    if not step1_hits:
        return {"context_text": ""}

    pdf_candidates = [h for h in step1_hits if h["source"] == "pdf_word"]
    exlcal_candidates = [h for h in step1_hits if h["source"] == "excel_calendar"]

    reranked_pdf = rerank_candidates(
        req.query, pdf_candidates, given_keywords=req.keywords,
        use_adjacency=True, final_topk=6, weights=(0.6, 0.3, 0.1)
    )
    reranked_exlcal = rerank_candidates(
        req.query, exlcal_candidates, given_keywords=req.keywords,
        use_adjacency=False, final_topk=15, weights=(0.7, 0.3, 0.0)
    )

    logging.info(f"[INFO] filtered(pdf_word Top6): {len(reranked_pdf)} / filtered(excel_calendar Top15): {len(reranked_exlcal)}")

    grouped_chunks: List[Dict[str, Any]] = []
    seen = set()

    # PDF/Word: Top3に前後±1付与 → 次の3件はそのまま
    word_hits_sorted = sorted(reranked_pdf, key=lambda x: -x["adjusted_score"])
    top3 = word_hits_sorted[:3]
    next3 = word_hits_sorted[3:6]

    for target in top3:
        for offset in (-1, 0, 1):
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
                    "score": target["adjusted_score"],
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
            "score": h["adjusted_score"],
        })

    # Excel/Calendar: Top15 をスコア高い順で
    for h in sorted(reranked_exlcal, key=lambda x: -x["adjusted_score"]):
        uid = f"{h['path']}:{h['chunk_index']}"
        if uid in seen:
            continue
        seen.add(uid)
        grouped_chunks.append({
            "path": h["path"],
            "chunk_index": h["chunk_index"],
            "text": h["text"],
            "type": h["type"],
            "score": h["adjusted_score"],
        })

    logging.info(f"[INFO] grouped_chunks: {len(grouped_chunks)} 件")

    context_parts = []
    for chunk in sorted(grouped_chunks, key=lambda x: -x["score"]):
        label = f"[FILE] {chunk['path']}（{chunk['type']}）"
        context_parts.append(label)
        context_parts.append(chunk["text"])
    context_text = "\n\n".join(context_parts)
    logging.info(f"[INFO] context_text 文字数: {len(context_text)} 文字")

    return {"context_text": context_text}

@app.get("/")
async def root():
    return {"message": "RAG Search API OK"}




















































