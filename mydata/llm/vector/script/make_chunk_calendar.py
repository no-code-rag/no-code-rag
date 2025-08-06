#!/usr/bin/env python3
import os
import json
from pathlib import Path
from tqdm import tqdm
from concurrent.futures import ProcessPoolExecutor, as_completed

from uid_utils import generate_chunk_index  # ✅ インデックス付番用

TEXT_ROOT = Path("/mydata/llm/vector/db/text")
CHUNK_DIR = Path("/mydata/llm/vector/db/chunk")
TARGETS_JSONL = Path("/tmp/targets_chunk_calendar.jsonl")

# ✅ MAX-2対応
MAX_WORKERS = max(1, os.cpu_count() - 2)

def process_file(txt_path: Path, uid: str, ftype: str):
    rel_path = str(txt_path.relative_to(TEXT_ROOT))

    with open(txt_path, "r", encoding="utf-8") as f:
        text = f.read().strip()

    record = {
        "uid": uid,
        "index": generate_chunk_index(0),
        "path": rel_path,
        "type": ftype,
        "text": text
    }

    out_path = CHUNK_DIR / (rel_path + ".jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return 1

def main():
    if not TARGETS_JSONL.exists():
        print(f"[INFO] 処理対象なし: {TARGETS_JSONL}")
        return

    with TARGETS_JSONL.open("r", encoding="utf-8") as f:
        targets = [json.loads(line) for line in f if line.strip()]

    if not targets:
        print("[INFO] 有効なターゲットなし")
        return

    print(f"▶️ Calendarチャンク生成開始: {len(targets)} 件")
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

    print(f"✅ Calendarチャンク作成完了: 合計 {total_chunks} チャンク")

if __name__ == "__main__":
    main()









































