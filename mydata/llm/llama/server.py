#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from llama_cpp import Llama

app = FastAPI(title="llama.cpp API Server", version="1.0.0")

class CompletionRequest(BaseModel):
    prompt: str
    model: str  # First API の /v1/model/list が返す id（例: /models/foo/bar.gguf）
    temperature: float = 0.7
    max_tokens: int = 512
    top_p: float = 0.95
    stop: list[str] = []

def load_model(model_path: str) -> Llama:
    """
    llama.cpp モデルをロード
    model_path: 例 '/models/foo/bar.gguf' （絶対 or 相対）
    """
    if not os.path.isabs(model_path):
        model_path = os.path.abspath(model_path)

    if not os.path.exists(model_path):
        raise HTTPException(status_code=400, detail=f"モデルファイルが見つかりません: {model_path}")

    return Llama(
        model_path=model_path,
        n_ctx=4096,
        n_threads=os.cpu_count(),
        verbose=False
    )

@app.post("/v1/completions")
async def create_completion(request: CompletionRequest):
    llm = load_model(request.model)
    output = llm(
        request.prompt,
        max_tokens=request.max_tokens,
        temperature=request.temperature,
        top_p=request.top_p,
        stop=request.stop
    )
    return {
        "completion": output,
        "model_used": request.model
    }

@app.post("/v1/chat/completions")
async def create_chat_completion(request: CompletionRequest):
    llm = load_model(request.model)
    output = llm.create_chat_completion(
        messages=[{"role": "user", "content": request.prompt}],
        max_tokens=request.max_tokens,
        temperature=request.temperature,
        top_p=request.top_p,
        stop=request.stop
    )
    return {
        "completion": output,
        "model_used": request.model
    }

if __name__ == "__main__":
    import uvicorn
    # 0.0.0.0:8000 で起動（コンテナ外からアクセス可）
    uvicorn.run(app, host="0.0.0.0", port=8000)

