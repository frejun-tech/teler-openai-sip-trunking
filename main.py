"""
Inbound AI calls: Teler SIP trunk -> OpenAI Realtime API (SIP).

Caller dials Teler number -> Teler trunk sends INVITE (TLS 5061, SRTP)
-> sip:<project>@sip.api.openai.com -> OpenAI fires realtime.call.incoming
-> this server accepts the call and opens the control WebSocket.

Audio never touches this server; it flows as SRTP between Teler and OpenAI.

Setup:
    pip install fastapi uvicorn requests websockets python-dotenv "openai>=1.99"
    uvicorn teler_openai_inbound:app --port 8000
    ngrok http 8000   -> register https://<ngrok-domain>/openai-webhook in OpenAI

.env:
    OPENAI_API_KEY=sk-...
    OPENAI_WEBHOOK_SECRET=whsec_...
    AI_MODEL=gpt-realtime-2.1
    AI_VOICE=alloy
    AI_INSTRUCTIONS=You are a helpful phone assistant for FreJun.
    AI_GREETING=Thank you for calling FreJun, how can I help you?
    TRANSCRIBE_MODEL=whisper-1
    REFER_TARGET=            # optional, e.g. tel:+919XXXXXXXXX to enable human transfer
"""

import asyncio
import json
import logging
import os

import requests
import websockets
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request, Response
from openai import InvalidWebhookSignatureError, OpenAI

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("teler-openai-inbound")

API_KEY = os.environ["OPENAI_API_KEY"]
MODEL = os.getenv("AI_MODEL", "gpt-realtime-2.1")
VOICE = os.getenv("AI_VOICE", "alloy")
INSTRUCTIONS = os.getenv("AI_INSTRUCTIONS", "You are a helpful phone assistant.")
GREETING = os.getenv("AI_GREETING", "Thank you for calling, how can I help you?")
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "whisper-1")
REFER_TARGET = os.getenv("REFER_TARGET", "").strip()

AUTH = {"Authorization": f"Bearer {API_KEY}"}
client = OpenAI(api_key=API_KEY, webhook_secret=os.environ["OPENAI_WEBHOOK_SECRET"])
app = FastAPI()
seen_webhooks: set[str] = set()  # dedupe retried deliveries (use Redis with multiple workers)


# ---------------------------------------------------------------- webhook
@app.post("/openai-webhook")
async def openai_webhook(request: Request, background: BackgroundTasks):
    body = await request.body()
    try:
        event = client.webhooks.unwrap(body, request.headers)
    except InvalidWebhookSignatureError:
        log.warning("Invalid webhook signature")
        return Response("invalid signature", status_code=400)

    webhook_id = request.headers.get("webhook-id", "")
    if webhook_id in seen_webhooks:
        return Response(status_code=200)
    seen_webhooks.add(webhook_id)

    if event.type == "realtime.call.incoming":
        call_id = getattr(event.data, "call_id", None)
        if not call_id:  # dashboard "Send test event" has no live call
            log.info("Test event received, acknowledged")
            return Response(status_code=200)
        sip_headers = getattr(event.data, "sip_headers", []) or []
        log.info("Incoming call %s, SIP headers: %s", call_id, sip_headers)
        # Respond to OpenAI immediately; accept + control socket run in background
        background.add_task(handle_call, call_id)

    return Response(status_code=200)


# ---------------------------------------------------------------- call control
def accept_call(call_id: str) -> bool:
    r = requests.post(
        f"https://api.openai.com/v1/realtime/calls/{call_id}/accept",
        headers={**AUTH, "Content-Type": "application/json"},
        json={
            "type": "realtime",
            "model": MODEL,
            "instructions": INSTRUCTIONS,
            "audio": {"output": {"voice": VOICE}},  # no audio.format: SIP negotiates codecs
        },
        timeout=15,
    )
    if r.status_code != 200:
        log.error("Accept failed for %s: %s %s", call_id, r.status_code, r.text)
        return False
    log.info("Accepted call %s", call_id)
    return True


def refer_call(call_id: str, target: str) -> None:
    r = requests.post(
        f"https://api.openai.com/v1/realtime/calls/{call_id}/refer",
        headers={**AUTH, "Content-Type": "application/json"},
        json={"target_uri": target},
        timeout=15,
    )
    log.info("REFER %s -> %s: %s %s", call_id, target, r.status_code, r.text)


def session_update() -> dict:
    session = {
        "type": "realtime",
        "audio": {"input": {"transcription": {"model": TRANSCRIBE_MODEL}}},
    }
    if REFER_TARGET:
        session["tools"] = [{
            "type": "function",
            "name": "transfer_to_human",
            "description": "Transfer the caller to a human agent when they ask for a person.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        }]
        session["tool_choice"] = "auto"
    return {"type": "session.update", "session": session}


async def control_socket(call_id: str) -> None:
    url = f"wss://api.openai.com/v1/realtime?call_id={call_id}"
    async with websockets.connect(url, additional_headers=AUTH) as ws:
        await ws.send(json.dumps(session_update()))
        await ws.send(json.dumps({
            "type": "response.create",
            "response": {"instructions": f"Say to the caller: {GREETING}"},
        }))

        async for raw in ws:
            ev = json.loads(raw)
            etype = ev.get("type", "")

            if etype == "conversation.item.input_audio_transcription.completed":
                log.info("[%s] caller: %s", call_id, ev.get("transcript", "").strip())
            elif etype == "response.output_audio_transcript.done":
                log.info("[%s] agent:  %s", call_id, ev.get("transcript", "").strip())
            elif etype == "response.function_call_arguments.done" and ev.get("name") == "transfer_to_human":
                log.info("[%s] transfer requested: %s", call_id, ev.get("arguments"))
                await asyncio.to_thread(refer_call, call_id, REFER_TARGET)
            elif etype == "error":
                log.error("[%s] realtime error: %s", call_id, ev.get("error"))

    log.info("[%s] control socket closed (call ended)", call_id)


async def handle_call(call_id: str) -> None:
    if not await asyncio.to_thread(accept_call, call_id):
        return
    try:
        await control_socket(call_id)
    except Exception as e:
        log.error("[%s] control socket error: %s", call_id, e)


@app.get("/health")
async def health():
    return {"status": "ok"}