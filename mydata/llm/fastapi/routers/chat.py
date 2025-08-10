# chat.py
import os
import json
import httpx
import asyncio
import logging
import re
import time
from pathlib import Path
from typing import List, Optional, Callable, Awaitable
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from .chat_room import save_streamed_message

router = APIRouter(prefix="/v1/chat")
logging.basicConfig(level=logging.INFO)

LLM_HOST = os.getenv("LLM_HOST", "http://llama:8000")
SEARCH_API_URL = "http://vector:8000/embed_search"
PROMPT_DIR = Path("/mydata/llm/fastapi/config/prompts")
RAG_PROMPT_DIR = Path("/mydata/llm/fastapi/config/rag_prompt")
GLOBAL_CONFIG_PATH = Path("/mydata/llm/fastapi/config/global_config.json")

# 同時ロード防止
_model_load_lock = asyncio.Lock()
# 推論順序保証用（アプリ側シリアライズ）
_model_infer_lock = asyncio.Lock()

# ======== 追加: 推論状態確認ユーティリティ ========

async def _fetch_inference_status(client: httpx.AsyncClient) -> Optional[bool]:
    try:
        r = await client.get(f"{LLM_HOST}/inference_status")
        if r.status_code == 200:
            data = r.json()
            return bool(data.get("is_generating"))
    except Exception:
        pass
    return None

async def wait_until_inference_idle(timeout_sec: int = 180, poll_sec: float = 0.5) -> None:
    """
    /inference_status をポーリングして is_generating=False になるまで待機。
    タイムアウトしたら504を投げる。
    """
    deadline = time.time() + timeout_sec
    async with httpx.AsyncClient(timeout=10) as client:
        while time.time() < deadline:
            status = await _fetch_inference_status(client)
            # 取得できなかった場合は小休止して再試行（サーバが落ちている可能性も考慮）
            if status is None:
                await asyncio.sleep(poll_sec)
                continue
            if status is False:
                return
            await asyncio.sleep(poll_sec)
    raise HTTPException(status_code=504, detail="inference wait timeout")

async def send_with_retry_nonstream(
    payload: dict,
    total_timeout_sec: int = 180,
    backoff_initial: float = 0.5,
    backoff_max: float = 3.0
) -> httpx.Response:
    """
    非ストリーム呼び出しで /v1/chat/completions を叩く。
    429(Model is currently generating) の間は /inference_status を見ながらリトライ。
    """
    deadline = time.time() + total_timeout_sec
    backoff = backoff_initial
    async with httpx.AsyncClient(timeout=None) as client:
        while True:
            # まずサーバが空くまで待つ
            await wait_until_inference_idle(timeout_sec=max(1, int(deadline - time.time())))
            try:
                res = await client.post(f"{LLM_HOST}/v1/chat/completions", json=payload)
            except Exception as e:
                # 一時的な接続断もリトライ対象
                if time.time() >= deadline:
                    raise HTTPException(status_code=504, detail=f"LLM request timeout: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.6, backoff_max)
                continue

            if res.status_code == 429:
                if time.time() >= deadline:
                    raise HTTPException(status_code=504, detail="generation busy timeout")
                # 少し待って再試行（/inference_status は wait_until_inference_idle 内で見ている）
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.6, backoff_max)
                continue

            # 200以外はそのままエラー
            res.raise_for_status()
            return res

async def stream_with_retry(
    payload: dict,
    total_timeout_sec: int = 180,
    poll_sec: float = 0.5
) -> StreamingResponse:
    """
    ストリーム呼び出し。開始前にサーバが空くまで待つ。
    開始時に429の場合はタイムアウトまで待ってリトライ。
    """
    deadline = time.time() + total_timeout_sec

    async def do_stream():
        nonlocal deadline
        while True:
            # 開始前待機
            await wait_until_inference_idle(timeout_sec=max(1, int(deadline - time.time())), poll_sec=poll_sec)
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("POST", f"{LLM_HOST}/v1/chat/completions", json=payload) as res:
                        if res.status_code == 429:
                            if time.time() >= deadline:
                                raise HTTPException(status_code=504, detail="generation busy timeout")
                            # 状態が空くまで再ループ
                            await asyncio.sleep(poll_sec)
                            continue
                        res.raise_for_status()

                        buffer = ""
                        async for line in res.aiter_lines():
                            if not line:
                                continue
                            if line.startswith("data: "):
                                data = line.replace("data: ", "").strip()
                                if data == "[DONE]":
                                    yield "data: [DONE]\n\n"
                                    break
                                try:
                                    delta = json.loads(data)["choices"][0]["delta"]
                                    content = delta.get("content", "")
                                    if content:
                                        buffer += content
                                    yield line + "\n"
                                except Exception:
                                    yield line + "\n"
                        return  # 正常終了
            except HTTPException:
                raise
            except Exception as e:
                # 接続系エラーはタイムアウトまで再試行
                if time.time() >= deadline:
                    raise HTTPException(status_code=504, detail=f"stream error timeout: {e}")
                await asyncio.sleep(poll_sec)
                continue

    return StreamingResponse(do_stream(), media_type="text/event-stream")

# ======== 既存モデル ========

class Message(BaseModel):
    role: str
    content: str
    model: str = ""
    speaker_uuid: str = ""
    style_id: int = 0
    room_id: str = ""

class CompletionRequest(BaseModel):
    model: str = ""  # UIから来ても無視（config優先）
    messages: List[Message]
    stream: bool = False
    speaker_uuid: str = ""
    style_id: int = 0
    room_id: str = ""
    prompt_id: str = "rag_default"
    rag_mode: str = "use"

def load_prompt_text(path: Path) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[1:]).strip() if len(lines) > 1 else ""

def load_base_prompt(prompt_id: str) -> str:
    path = PROMPT_DIR / f"{prompt_id}.txt"
    if not path.exists():
        path = PROMPT_DIR / "rag_default.txt"
    return load_prompt_text(path)

def load_rag_instruction(rag_mode: str) -> str:
    path = RAG_PROMPT_DIR / f"{rag_mode}.txt"
    if not path.exists():
        path = RAG_PROMPT_DIR / "use.txt"
    return load_prompt_text(path)

def read_model_from_global_config() -> str:
    try:
        data = json.loads(GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
        return (data.get("model") or "").strip()
    except Exception:
        return ""

async def get_llama_health() -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{LLM_HOST}/health")
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return None

async def is_target_ready(target_model: str) -> bool:
    health = await get_llama_health()
    if not health:
        return False
    cur_model = (health.get("model") or "").strip()
    loaded = bool(health.get("model_loaded"))
    return (cur_model == target_model) and loaded

async def ensure_model_loaded_and_ready(target_model: str, timeout_sec: int = 180):
    """
    必要時のみ /load_model を実行し、/health で
    - model が target_model と一致
    - model_loaded が True
    になるまで待機
    """
    if not target_model:
        raise HTTPException(status_code=400, detail="モデル未設定（global_config.json）")

    async with _model_load_lock:
        # 既にロード済みなら即返す
        if await is_target_ready(target_model):
            return

        # ロード要求
        async with httpx.AsyncClient(timeout=None) as client:
            resp = await client.post(f"{LLM_HOST}/load_model", json={"model": target_model})
            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code, detail=f"load_model failed: {resp.text}")

        # ロード完了待ち
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if await is_target_ready(target_model):
                return
            await asyncio.sleep(1)

        raise HTTPException(status_code=504, detail=f"model load timeout: {target_model}")

async def extract_keywords(query: str, model: str) -> List[str]:
    """
    キーワード抽出も LLM を叩くので、その前に ensure_model_loaded_and_ready が
    completions() で呼ばれている前提。ここでも保険としてロード確認。
    """
    if not await is_target_ready(model):
        raise HTTPException(status_code=503, detail="モデルがまだロードされていません")

    try:
        def clean_json_codeblock(text: str) -> str:
            match = re.search(r"```(?:json)?\s*(\[[\s\S]+?\])\s*```", text)
            if match:
                return match.group(1).strip()
            return text.strip()

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "あなたのタスクは、ユーザーからの質問文に**実際に登場する名詞のみ**を抽出し、"
                        "検索用キーワードとして最大5個までJSON配列で返すことです。\n\n"
                        "🔒制約条件：\n"
                        "- 出力する単語は、入力文に**実際に現れた名詞**のみとすること\n"
                        "- 名詞以外は含めない\n"
                        "- 抽象語・汎用語は含めない\n"
                        "- 順番は文中の出現順と一致\n"
                        "- 出力はJSON配列のみ（コードブロック禁止）"
                    )
                },
                {"role": "user", "content": f"質問：「{query}」"},
            ],
            "stream": False,
        }

        async with httpx.AsyncClient() as client:
            res = await client.post(f"{LLM_HOST}/v1/chat/completions", json=payload, timeout=20)
            res.raise_for_status()

            content_raw = res.json()["choices"][0]["message"]["content"]
            content = clean_json_codeblock(content_raw)

            if not content or not content.startswith("["):
                logging.warning(f"[WARN] keyword抽出 応答形式異常: {content_raw}")
                return []
            return json.loads(content)

    except Exception as e:
        logging.warning(f"[WARN] keyword抽出失敗: {e}")
        return []

@router.post("/completions")
async def completions(req: CompletionRequest):
    selected_model = read_model_from_global_config()
    print(f"[INFO] 使用モデル(from config): {selected_model}", flush=True)
    if not selected_model:
        raise HTTPException(status_code=400, detail="モデル未設定（global_config.json）")

    # モデルロード完了まで待機
    await ensure_model_loaded_and_ready(selected_model)

    user_message = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
    if not user_message:
        raise HTTPException(status_code=400, detail="ユーザーメッセージがありません")

    if req.room_id:
        save_streamed_message(req.room_id, role="user", content=user_message, model=selected_model)

    # 推論順序保証（アプリ側で直列化）＋ サーバ側のis_generatingも監視
    async with _model_infer_lock:
        # （追加）サーバが空くまで事前待機
        await wait_until_inference_idle(timeout_sec=180, poll_sec=0.5)

        character_raw = load_base_prompt(req.prompt_id).strip()
        rag_template_raw = load_rag_instruction(req.rag_mode)
        if "{context_text}" not in rag_template_raw:
            rag_template_raw += "\n\n【RAGチャンク】\n{context_text}"

        context_text = ""
        if req.rag_mode != "off":
            keywords = await extract_keywords(user_message, selected_model)
            logging.info(f"[INFO] キーワード抽出結果（{len(keywords)}件）: {', '.join(keywords)}")
            try:
                async with httpx.AsyncClient() as client:
                    res = await client.post(
                        SEARCH_API_URL,
                        json={"query": user_message, "keywords": keywords, "top_k": 50}
                    )
                    res.raise_for_status()
                    context_text = res.json().get("context_text", "")
                    logging.info(f"[RAGチャンク先頭500文字]\n{context_text[:500]}")
            except Exception as e:
                logging.warning(f"[WARN] context_text取得失敗: {e}")
                context_text = ""

        rag_filled = rag_template_raw.replace("{context_text}", context_text.strip())
        user_prompt = (
            f"【上位ルール】\n{character_raw}\n\n"
            f"【ユーザーからの質問】\n{user_message.strip()}\n\n"
            f"【下位ルール】\n{rag_filled.strip()}"
        )
        logging.info(f"[PROMPT文字数]: {len(user_prompt)}")

        # ===== 日本語固定（最小改修：messagesだけ差し替え） =====
        system_preamble = (
            "以後の応答は厳格に日本語のみで行うこと。英単語・英文は出力しない。"
            "もし混在した場合は即座に自然な日本語へ言い換えること。"
        )
        messages = [
            {"role": "system", "content": system_preamble},
            {"role": "system", "content": character_raw},  # 既存キャラ指示もsystemで適用
            {"role": "user",   "content": user_prompt},    # RAG込み本文はそのまま
        ]

        payload = {
            "model": selected_model,
            "messages": messages,
            "stream": req.stream
        }

        try:
            if req.stream:
                # ストリームは専用のリトライロジックでSSEを返す
                resp = await stream_with_retry(payload, total_timeout_sec=180, poll_sec=0.5)
                # 保存（ストリーム時は下流でまとめて保存済み。ここではそのまま返す）
                return resp
            else:
                # 非ストリームは429リトライ込みで実行
                res = await send_with_retry_nonstream(payload, total_timeout_sec=180)
                data = res.json()
                try:
                    content = data["choices"][0]["message"]["content"]
                    if req.room_id and content.strip():
                        save_streamed_message(req.room_id, "assistant", content.strip(), selected_model)
                except Exception:
                    pass
                return data

        except httpx.HTTPStatusError as e:
            logging.error(f"[ERROR] LLM応答失敗: {e.response.text}")
            raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
        except HTTPException:
            raise
        except Exception as e:
            logging.error(f"[ERROR] LLM応答失敗: {e}")
            raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")








































