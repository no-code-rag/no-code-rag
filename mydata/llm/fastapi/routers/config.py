from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from pathlib import Path
import json

router = APIRouter(prefix="/v1/config")

# === 設定ファイルパス（変更：chat_logs → config） ===
CONFIG_FILE = Path("/mydata/llm/fastapi/config/global_config.json")
RAG_PROMPT_DIR = Path("/mydata/llm/fastapi/config/rag_prompt")
CHAR_PROMPT_DIR = Path("/mydata/llm/fastapi/config/prompts")

# === デフォルト値 ===
DEFAULT_CONFIG = {
    "model": None,
    "speaker_uuid": "7ffcb7ce-0000-0000-0000-000000000000",
    "style_id": 0,
    "prompt_id": "default",
    "rag_mode": "use"
}

# === モデル ===
class GlobalConfig(BaseModel):
    model: str
    speaker_uuid: str
    style_id: int
    prompt_id: str = "default"
    rag_mode: str = "use"

# === 設定読み込み・保存 ===
def load_config():
    if CONFIG_FILE.exists():
        try:
            with CONFIG_FILE.open("r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    config = json.loads(content)
                    return {
                        "model": config.get("model", DEFAULT_CONFIG["model"]),
                        "speaker_uuid": config.get("speaker_uuid", DEFAULT_CONFIG["speaker_uuid"]),
                        "style_id": int(config.get("style_id", DEFAULT_CONFIG["style_id"])),
                        "prompt_id": config.get("prompt_id", DEFAULT_CONFIG["prompt_id"]),
                        "rag_mode": config.get("rag_mode", DEFAULT_CONFIG["rag_mode"]),
                    }
        except Exception as e:
            print(f"❌ global_config.json 読み込み失敗: {e}")

    return DEFAULT_CONFIG.copy()

def save_config(model: str, speaker_uuid: str, style_id: int, prompt_id: str, rag_mode: str):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_FILE.open("w", encoding="utf-8") as f:
        json.dump({
            "model": model,
            "speaker_uuid": speaker_uuid,
            "style_id": style_id,
            "prompt_id": prompt_id,
            "rag_mode": rag_mode
        }, f, ensure_ascii=False, indent=2)

# === API ===
@router.get("/")
def get_config():
    try:
        return {"success": True, "data": load_config(), "error": None}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"設定取得失敗: {e}")

@router.post("/")
def update_config(config: GlobalConfig):
    try:
        save_config(config.model, config.speaker_uuid, config.style_id, config.prompt_id, config.rag_mode)
        return {"success": True, "data": None, "error": None}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"設定保存失敗: {e}")

@router.get("/rag_prompt/list")
def list_rag_prompts():
    try:
        if not RAG_PROMPT_DIR.exists():
            return {"success": True, "data": [], "error": None}

        files = sorted(RAG_PROMPT_DIR.glob("*.txt"))
        data = []
        for f in files:
            try:
                with f.open("r", encoding="utf-8") as fp:
                    first_line = fp.readline().strip()
                name = first_line if first_line else f.stem
            except Exception:
                name = f.stem
            data.append({"id": f.stem, "name": name})
        return {"success": True, "data": data, "error": None}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"RAGモード一覧取得失敗: {e}")

@router.get("/prompt/list")
def list_character_prompts():
    try:
        if not CHAR_PROMPT_DIR.exists():
            return {"success": True, "data": [], "error": None}

        files = sorted(CHAR_PROMPT_DIR.glob("*.txt"))
        data = []
        for f in files:
            try:
                with f.open("r", encoding="utf-8") as fp:
                    first_line = fp.readline().strip()
                name = first_line if first_line else f.stem
            except Exception:
                name = f.stem
            data.append({"id": f.stem, "name": name})
        return {"success": True, "data": data, "error": None}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"プロンプト一覧取得失敗: {e}")





