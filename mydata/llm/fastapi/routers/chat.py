# chat.py
import os
import json
import httpx
import asyncio
import logging
import re
from pathlib import Path
from typing import List
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

class Message(BaseModel):
    role: str
    content: str
    model: str = ""
    speaker_uuid: str = ""
    style_id: int = 0
    room_id: str = ""

class CompletionRequest(BaseModel):
    model: str
    messages: List[Message]
    stream: bool = True
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

async def extract_keywords(query: str, model: str) -> List[str]:
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
                        "- 出力する単語は、入力文に**実際に現れた名詞**のみとすること（言い換え・類義語は禁止）\n"
                        "- 名詞以外（動詞・形容詞・副詞など）は含めない\n"
                        "- 抽象語・汎用語（例：事案、問題、ケース、内容 など）は含めない\n"
                        "- 単語の順番は、文中の出現順と一致させてください\n"
                        "- 出力はJSON配列のみ。コードブロック（```）や説明は禁止\n\n"
                        "✅良い例：\n"
                        "入力：死刑判決が出されたケースについて教えてください\n"
                        "出力：[\"死刑判決\"]\n\n"
                        "❌悪い例：\n"
                        "[\"冤罪\"] ← 文中に存在しない\n"
                        "[\"刑罰\", \"判例\"] ← 類義語\n"
                        "```[\"死刑\"]``` ← コードブロックは禁止\n"
                    )
                },
                {
                    "role": "user",
                    "content": f"質問：「{query}」"
                },
            ],
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
    user_message = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
    if not user_message:
        raise HTTPException(status_code=400, detail="ユーザーメッセージがありません")

    if req.room_id:
        save_streamed_message(req.room_id, role="user", content=user_message, model=req.model)

    character_raw = load_base_prompt(req.prompt_id).strip()
    rag_template_raw = load_rag_instruction(req.rag_mode)
    if "{context_text}" not in rag_template_raw:
        rag_template_raw += "\n\n【RAGチャンク】\n{context_text}"

    context_text = ""
    if req.rag_mode != "off":
        keywords = await extract_keywords(user_message, req.model)
        logging.info(f"[INFO] キーワード抽出結果（{len(keywords)}件）: {', '.join(keywords)}")
        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(SEARCH_API_URL, json={
                    "query": user_message,
                    "keywords": keywords,
                    "top_k": 50
                })
                res.raise_for_status()
                context_text = res.json().get("context_text", "")
                logging.info(f"[RAGチャンク先頭500文字]\n{context_text[:500]}")
        except Exception as e:
            logging.warning(f"[WARN] context_text取得失敗: {e}")
            context_text = ""

    rag_filled = rag_template_raw.replace("{context_text}", context_text.strip())
    user_prompt = (
        "以下のルールに従って回答せよ。\n"
        "【第１階層】上位ルールは【キャラクター設定】であり、回答全体を通じて人格・口調・語彙・価値観を一貫させること。\n"
        "【第２階層】下位ルールの【補足情報（RAG）】の取り扱い指示は**【絶対に厳守】**せよ。内容を無視・軽視・改変することは許されない。なお、下位ルールを厳守するためであれば、上位ルールに反することを認める。\n\n"
        f"【上位ルール】\n{character_raw}\n\n"
        f"【ユーザーからの質問】\n{user_message.strip()}\n\n"
        f"【下位ルール】\n{rag_filled.strip()}\n\n"
        "※RAG情報に基づく引用や根拠の明示が求められる場合は、必ず明記すること。その上で、適切に盛り込みつつ、キャラクターとして自然な形で語れ。"
    )

    logging.info(f"[PROMPT文字数]: {len(user_prompt)}")

    messages = [{"role": "user", "content": user_prompt}]
    payload = {
        "model": req.model,
        "messages": messages,
        "stream": True
    }

    try:
        async with httpx.AsyncClient(timeout=None) as client:
            res = await client.post(f"{LLM_HOST}/v1/chat/completions", json=payload)
            res.raise_for_status()

            async def stream():
                buffer = ""
                async for line in res.aiter_lines():
                    if line.startswith("data: "):
                        data = line.replace("data: ", "").strip()
                        if data == "[DONE]":
                            if req.room_id and buffer.strip():
                                save_streamed_message(req.room_id, "assistant", buffer.strip(), req.model)
                            break
                        try:
                            delta = json.loads(data)["choices"][0]["delta"]
                            content = delta.get("content", "")
                            if content:
                                buffer += content
                                yield line + "\n"
                        except:
                            continue
            return StreamingResponse(stream(), media_type="text/event-stream")
    except Exception as e:
        logging.error(f"[ERROR] LLM応答失敗: {e}")
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")









































