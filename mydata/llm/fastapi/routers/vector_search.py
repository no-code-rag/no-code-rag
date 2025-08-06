# vector_search.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import os
import httpx
import logging
from typing import List

router = APIRouter(prefix="/v1/vector")

MAIN_API_URL = os.getenv("MAIN_API_URL", "http://main:8000/embed_search")

class EmbedQuery(BaseModel):
    query: str
    keywords: List[str] = []
    top_k: int = 50

@router.post("/embed_search")
async def embed_search(req: EmbedQuery):
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                MAIN_API_URL,
                json={
                    "query": req.query,
                    "keywords": req.keywords,
                    "top_k": req.top_k
                }
            )
            response.raise_for_status()
            return response.json()
    except httpx.RequestError as e:
        logging.error(f"❌ ベクトル検索通信失敗: {e}")
        raise HTTPException(status_code=500, detail=f"ベクトル検索通信失敗: {str(e)}")
    except httpx.HTTPStatusError as e:
        logging.error(f"❌ ベクトル検索HTTPエラー: {e.response.text}")
        raise HTTPException(status_code=e.response.status_code, detail=f"ベクトル検索失敗: {e.response.text}")
    except Exception as e:
        logging.error(f"❌ ベクトル検索例外: {e}")
        raise HTTPException(status_code=500, detail=f"不明なエラー: {str(e)}")







