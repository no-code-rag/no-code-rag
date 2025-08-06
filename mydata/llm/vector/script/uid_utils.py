#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
uid_utils.py（最終決定版）
UID発番・インデックス付番・ログ操作・相対パス取得・突き合わせに特化
"""

import os
import json
import hashlib
import orjson
from pathlib import Path
from typing import List, Dict, Any, Set

# ====== 1. UID生成（テキスト用・一元管理） ======
def generate_uid(file_path: Path) -> str:
    stat = file_path.stat()
    uid_source = f"{file_path.resolve()}::{file_path.name}::{stat.st_mtime}::{stat.st_size}"
    return hashlib.sha256(uid_source.encode("utf-8")).hexdigest()

# ====== 2. チャンク用インデックス発番 ======
def generate_chunk_index(count: int) -> int:
    return count

# ====== 3. JSONL操作 ======
def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line.strip()) for line in f if line.strip()]

def write_jsonl_atomic_sync(path: Path, data: List[Dict[str, Any]]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        for item in data:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
    print(f"[INFO] fsync付き書き込み完了: {path.name}（{len(data)} 件）")

# ====== 4. パス操作 ======
def get_relative_path(file_path: Path, base_path: Path) -> str:
    return str(file_path.relative_to(base_path)).replace("\\", "/")

# ====== 5. 汎用ヘルパー ======
def ensure_dir_exists(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)

# ====== 6. チャンクログ再構築（軽量） ======
def rebuild_chunk_log_fast(chunk_dir: Path, log_path: Path) -> int:
    entries = []
    for chunk_file in chunk_dir.rglob("*.jsonl"):
        try:
            with chunk_file.open("r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    obj = orjson.loads(line)
                    entries.append({
                        "uid": obj["uid"],
                        "index": obj["index"],
                        "path": obj["path"],
                        "type": obj.get("type", "unknown")
                    })
        except Exception as e:
            print(f"[WARN] チャンクログ構築失敗: {chunk_file} ({e})")
            continue

    write_jsonl_atomic_sync(log_path, entries)
    print(f"[INFO] チャンクログ更新: {log_path.name}（{len(entries)} 件・fsync済）")
    return len(entries)

# ====== 7. 空フォルダー削除 ======
def remove_empty_dirs(base_dir: Path, exclude: tuple = ()):
    for dir_path in sorted(base_dir.rglob("*"), reverse=True):
        if dir_path.is_dir():
            if dir_path.name in exclude:
                continue
            if not any(dir_path.iterdir()):
                try:
                    dir_path.rmdir()
                    print(f"[DEL] 空フォルダー削除: {dir_path}")
                except Exception as e:
                    print(f"[WARN] 空フォルダー削除失敗: {dir_path} ({e})")

# ====== 8. UIDログ突き合わせ（辞書ベース） ======
def load_uid_index_map(log_path: Path) -> Dict[str, List[int]]:
    """
    ログを読み込み、UID → index[] の辞書に変換
    ベクトル登録済UID一覧の取得にも使える
    """
    result: Dict[str, List[int]] = {}
    if not log_path.exists():
        return result
    for entry in read_jsonl(log_path):
        uid = entry["uid"]
        idx = entry.get("index", 0)
        result.setdefault(uid, []).append(idx)
    return result

def extract_uids(log_path: Path) -> Set[str]:
    """
    ログを読み込み、UIDの集合（Set[str]）を返す
    """
    if not log_path.exists():
        return set()
    return {entry["uid"] for entry in read_jsonl(log_path)}

# ====== 9. UUID生成（UID + index → SHA256 64文字） ======
def generate_uuid(uid: str, index: int) -> str:
    """
    UIDとインデックスからUUID（SHA256 64文字）を生成
    """
    return hashlib.sha256(f"{uid}-{index}".encode("utf-8")).hexdigest()




