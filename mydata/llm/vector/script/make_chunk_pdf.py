#!/usr/bin/env python3
import os
import re
import json
from pathlib import Path
from tqdm import tqdm
from concurrent.futures import ProcessPoolExecutor, as_completed

from uid_utils import generate_chunk_index

TEXT_ROOT = Path("/mydata/llm/vector/db/text")
CHUNK_DIR = Path("/mydata/llm/vector/db/chunk")
TARGETS_JSONL = Path("/tmp/targets_chunk_pdf.jsonl")

CHUNK_SIZE = 350
CHUNK_OVERLAP = 50

# ✅ MAX-2対応
MAX_WORKERS = max(1, os.cpu_count() - 2)

def clean_text(text: str) -> str:
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def make_chunks(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks

def process_file(txt_path: Path, uid: str, ftype: str):
    rel_path = str(txt_path.relative_to(TEXT_ROOT))
    with open(txt_path, "r", encoding="utf-8") as f:
        raw_text = f.read()

    body_part = raw_text.split("\n\n", 1)[1] if "\n\n" in raw_text else raw_text
    body_chunks = make_chunks(clean_text(body_part))

    out_path = CHUNK_DIR / (rel_path + ".jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as f:
        for i, c in enumerate(body_chunks):
            record = {
                "uid": uid,
                "index": generate_chunk_index(i),
                "path": rel_path,
                "type": ftype,
                "text": c
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return len(body_chunks)

def main():
    if not TARGETS_JSONL.exists():
        print(f"[INFO] 処理対象なし: {TARGETS_JSONL}")
        return

    with TARGETS_JSONL.open("r", encoding="utf-8") as f:
        targets = [json.loads(line) for line in f if line.strip()]

    if not targets:
        print("[INFO] 有効なターゲットなし")
        return

    print(f"▶️ PDFチャンク生成開始: {len(targets)} 件")
    total_chunks = 0
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(
                process_file, TEXT_ROOT / t["rel_path"], uid=t["uid"], ftype=t["type"]
            )
            for t in targets if (TEXT_ROOT / t["rel_path"]).exists()
        ]
        for f in as_completed(futures):
            total_chunks += f.result()

    print(f"✅ PDFチャンク作成完了: 合計 {total_chunks} チャンク")

if __name__ == "__main__":
    main()








































