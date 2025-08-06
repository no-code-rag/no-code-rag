#!/usr/bin/env python3
import os
import json
import sqlite3
from pathlib import Path
import faiss
import numpy as np

ROOT = Path("/mydata/llm/vector")
CHUNK_LOG = ROOT / "db/log/chunk_log.jsonl"

CONFIGS = [
    {
        "name": "pdf_word",
        "faiss_index": ROOT / "db/faiss/pdf_word/index.faiss",
        "sqlite_path": ROOT / "db/faiss/pdf_word/metadata.sqlite3",
    },
    {
        "name": "excel_calendar",
        "faiss_index": ROOT / "db/faiss/excel_calendar/index.faiss",
        "sqlite_path": ROOT / "db/faiss/excel_calendar/metadata.sqlite3",
    }
]

def load_valid_uids(chunk_log_path):
    valid_uid_set = set()
    with open(chunk_log_path, encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            valid_uid_set.add(obj["uid"])
    return valid_uid_set

def process_config(conf, valid_uid_set):
    print(f"\n=== ▶ {conf['name']} ===")

    if not conf["sqlite_path"].exists():
        print(f"[SKIP] SQLiteが存在しません: {conf['sqlite_path']}")
        return

    conn = sqlite3.connect(str(conf["sqlite_path"]))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM vector_metadata")
    all_rows = cursor.fetchall()

    db_uid_set = {row["uid"] for row in all_rows}
    ghost_uids = sorted(db_uid_set - valid_uid_set)

    if not ghost_uids and db_uid_set == valid_uid_set:
        print("[INFO] ゴーストなし、UID一致 → 再構成スキップ")
        conn.close()
        return

    print(f"[INFO] ゴーストUID数: {len(ghost_uids)}")

    keep_rows = [dict(row) for row in all_rows if row["uid"] not in ghost_uids]

    if len(keep_rows) == 0:
        cursor.execute("DELETE FROM vector_metadata")
        conn.commit()
        conn.close()
        if conf["faiss_index"].exists():
            conf["faiss_index"].unlink()
            print(f"[DONE] FAISSインデックス削除: {conf['faiss_index']}")
        print("[DONE] DB全削除完了（残件なし）")
        return

    print(f"[INFO] 残存行: {len(keep_rows)}件, 削除対象: {len(ghost_uids)}件")

    cursor.execute("DELETE FROM vector_metadata")
    conn.commit()

    BATCH_SIZE = 1_000_000
    new_index = None
    vec_index = 0

    for i in range(0, len(keep_rows), BATCH_SIZE):
        batch = keep_rows[i:i+BATCH_SIZE]
        for r in batch:
            r["vec_index"] = vec_index
            vec = np.frombuffer(r["vector"], dtype=np.float32)
            if new_index is None:
                d = len(vec)
                new_index = faiss.IndexFlatIP(d)
            new_index.add(np.array([vec], dtype=np.float32))
            cursor.execute(
                "INSERT INTO vector_metadata (vec_index, uid, chunk_index, path, type, vector) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    vec_index, r["uid"], r["chunk_index"],
                    r["path"], r["type"],
                    sqlite3.Binary(vec.tobytes())
                )
            )
            vec_index += 1
        conn.commit()
        print(f"[INFO] 再構成中: {vec_index}件 まで完了")

    conn.close()

    if new_index is None or new_index.ntotal == 0:
        if conf["faiss_index"].exists():
            conf["faiss_index"].unlink()
            print(f"[DONE] FAISS削除済み（中身0件）: {conf['faiss_index']}")
        else:
            print("[SKIP] FAISSファイルがもともと存在しない")
        return

    faiss.write_index(new_index, str(conf["faiss_index"]))
    print(f"[DONE] FAISS再構成完了: {conf['faiss_index']}")
    print(f"[DONE] ゴースト削除完了: {len(ghost_uids)} 件")

def main():
    print("▶️ delete_vector_faiss_with_sqlite_vector 開始")
    valid_uid_set = load_valid_uids(CHUNK_LOG)
    for conf in CONFIGS:
        process_config(conf, valid_uid_set)
    print("✅ 完了")

if __name__ == "__main__":
    main()










































