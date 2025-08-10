import os
import json
from typing import Optional
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from llama_cpp import Llama

app = FastAPI()

llm: Optional[Llama] = None
current_model_path: Optional[str] = None
is_generating: bool = False  # 推論中フラグ


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "model": current_model_path,
        "model_loaded": current_model_path is not None,
        "is_generating": is_generating
    }


@app.get("/inference_status")
async def inference_status():
    """現在推論中かどうかを返す"""
    return {"is_generating": is_generating}


@app.post("/load_model")
async def load_model(request: Request):
    """
    モデルロード。JSON {"model": "..."} でも、/load_model?model_path=... でもOK。
    既に同一モデルなら即成功を返す。
    """
    global llm, current_model_path

    # まずクエリを見て、なければJSONを見る（柔軟対応）
    model_path = request.query_params.get("model_path")
    if not model_path:
        try:
            data = await request.json()
        except Exception:
            data = {}
        model_path = data.get("model") or data.get("model_path")

    if not model_path or not os.path.exists(model_path):
        return JSONResponse({"error": "Model path not found"}, status_code=400)

    if current_model_path == model_path and llm is not None:
        return {"status": "model loaded (cached)", "model": current_model_path}

    # llama-cpp-python の生成（分割マッピング方式）
    llm = Llama(
        model_path=model_path,
        n_threads=int(os.getenv("LLAMA_CPP_THREADS", "8")),
        n_ctx=int(os.getenv("LLAMA_CPP_CONTEXT_SIZE", "4096")),
        verbose=False,
        mmap=True
    )
    current_model_path = model_path

    # ダミー推論でメモリウォームアップ
    try:
        _ = llm("Hello", max_tokens=1, temperature=0.0, top_p=0.1)
    except Exception as e:
        return JSONResponse({"error": f"Warm-up failed: {str(e)}"}, status_code=500)

    return {"status": "model loaded", "model": current_model_path}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    OpenAI互換。stream=True の場合は SSE で delta.content を順次送信。
    """
    global llm, current_model_path, is_generating
    if llm is None:
        return JSONResponse({"error": "No model loaded"}, status_code=400)

    if is_generating:
        return JSONResponse({"error": "Model is currently generating"}, status_code=429)

    body = await request.json()
    messages = body.get("messages", [])
    stream = body.get("stream", False)
    max_tokens = body.get("max_tokens", 512)
    temperature = body.get("temperature", 0.7)
    top_p = body.get("top_p", 0.9)

    # ごくシンプルに role を行として連結
    prompt_lines = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if role == "system":
            prompt_lines.append(content)
        elif role == "user":
            prompt_lines.append(f"User: {content}")
        elif role == "assistant":
            prompt_lines.append(f"Assistant: {content}")
    prompt = "\n".join(prompt_lines) + "\n"

    if not stream:
        try:
            is_generating = True
            out = llm(
                prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
            )
        finally:
            is_generating = False
        text = out["choices"][0]["text"]
        return {
            "id": "chatcmpl-123",
            "object": "chat.completion",
            "model": current_model_path,
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": out.get("usage", {}),
        }

    # ストリーム（SSE）
    async def token_stream():
        global is_generating
        is_generating = True
        try:
            # 最初にrole通知（互換用）
            head = {
                "id": "chatcmpl-123",
                "object": "chat.completion",
                "model": current_model_path,
                "choices": [{"delta": {"role": "assistant"}, "index": 0}],
            }
            yield f"data: {json.dumps(head, ensure_ascii=False)}\n\n"

            for tok in llm(prompt, max_tokens=max_tokens, temperature=temperature, top_p=top_p, stream=True):
                piece = tok.get("choices", [{}])[0].get("text", "")
                if not piece:
                    continue
                chunk = {
                    "id": "chatcmpl-123",
                    "object": "chat.completion.chunk",
                    "model": current_model_path,
                    "choices": [{"delta": {"content": piece}, "index": 0, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

            # 終端
            tail = {
                "id": "chatcmpl-123",
                "object": "chat.completion.chunk",
                "model": current_model_path,
                "choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(tail, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            is_generating = False

    return StreamingResponse(token_stream(), media_type="text/event-stream")
