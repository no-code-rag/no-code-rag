from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from fastapi.responses import StreamingResponse, JSONResponse
import os
import requests
import io
from pydub import AudioSegment
import re
import json
import asyncio
import base64

router = APIRouter(prefix="/v1/voice")

VOICEVOX_HOST = os.getenv("VOICEVOX_HOST", "http://voicevox:50021")

# ---------- utils ----------
def split_sentences(text: str):
    parts = re.split(r'[。！？]\s*|\n+', text)
    return [p.strip() for p in parts if p.strip()]

def _vvx_get(endpoint: str):
    try:
        r = requests.get(f"{VOICEVOX_HOST}{endpoint}")
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=f"VOICEVOX接続エラー: {e}")

# ---------- speakers ----------
@router.get("/speakers")
def get_speakers():
    """
    フロントが期待する形:
      { "success": True, "data": [ {speaker_uuid, name, styles:[{id,name}, ...]} ], "error": None }
    """
    speakers = _vvx_get("/speakers")
    return {"success": True, "data": speakers, "error": None}

# ---------- request model ----------
class SynthesisRequest(BaseModel):
    text: str
    speaker_uuid: str
    style_id: int

# ---------- TTS streaming (文ごとMP3 base64) ----------
@router.post("/synthesize_stream")
async def synthesize_stream(body: SynthesisRequest):
    speaker_param = body.style_id
    sentences = split_sentences(body.text)

    async def event_stream():
        for sentence in sentences:
            if not sentence:
                continue
            try:
                # audio_query
                q = requests.post(
                    f"{VOICEVOX_HOST}/audio_query",
                    params={"text": sentence, "speaker": speaker_param},
                    timeout=30,
                )
                q.raise_for_status()

                # synthesis (wav)
                s = requests.post(
                    f"{VOICEVOX_HOST}/synthesis",
                    params={"speaker": speaker_param},
                    json=q.json(),
                    timeout=60,
                )
                s.raise_for_status()

                # wav -> mp3 (文ごと)
                wav_bytes = io.BytesIO(s.content)
                audio = AudioSegment.from_file(wav_bytes, format="wav")
                mp3_bytes = io.BytesIO()
                audio.export(mp3_bytes, format="mp3")
                mp3_bytes.seek(0)

                b64 = base64.b64encode(mp3_bytes.read()).decode("ascii")
                yield f"data: {json.dumps({'sentence': sentence, 'mp3_b64': b64}, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.02)
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")

