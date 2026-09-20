"""OpenVINO inference HTTP service, replacing llama-server + sidecar gateway.

Listens on 127.0.0.1:18765 with APIs compatible with the desk-pet frontend:
- GET  /api/health          health check (polled by frontend + settings panel)
- POST /api/chat            pet chat API (SSE true streaming, token by token)
- POST /v1/chat/completions OpenAI-compatible API (fallback)
- POST /api/shutdown        graceful shutdown
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

# ── Encoding setup ──

def _configure_stream_encoding(stream) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")

_configure_stream_encoding(sys.stdout)
_configure_stream_encoding(sys.stderr)

# ── Constants ──

SKILL_NAME = "local-minicpm-pet-openvino"
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 18765
MODEL_NAME = "MiniCPM5-1B-OpenVINO"

OPENVINO_ROOT = Path(os.environ.get("USERPROFILE", "~")) / ".openvino"
MODELS_DIR = OPENVINO_ROOT / "models"
LOG_DIR = OPENVINO_ROOT / "log"

# ── Logging ──

LOG_DIR.mkdir(parents=True, exist_ok=True)
_log_file = LOG_DIR / f"{SKILL_NAME}-server-{time.strftime('%Y%m%d-%H%M%S')}.log"


def log(msg: str):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [server pid={os.getpid()}] {msg}"
    print(line, flush=True)
    with open(_log_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── State ──

class ServerState:
    def __init__(self):
        self.status = "starting"  # starting/downloading/loading/ok/error
        self.error: Optional[str] = None
        self.progress: str = ""
        self.pipe = None
        self.tokenizer = None
        self.model_dir: Optional[str] = None
        self.device: str = "CPU"
        self.start_time = time.time()

    @property
    def uptime_s(self) -> int:
        return int(time.time() - self.start_time)


_state = ServerState()

# ── Device selection ──

def _pick_device() -> str:
    # Priority: OPENVINO_DEVICE env var > auto-detect
    env_device = os.environ.get("OPENVINO_DEVICE", "").upper()
    if env_device in ("NPU", "GPU", "CPU"):
        log(f"Using device from OPENVINO_DEVICE env: {env_device}")
        return env_device

    try:
        import openvino as ov
        devices = ov.Core().available_devices
        for d in devices:
            if "GPU" in d:
                log(f"Detected GPU device: {d}")
                return d
        log("No GPU found, using CPU")
        return "CPU"
    except Exception as e:
        log(f"Device detection failed: {e}, fallback to CPU")
        return "CPU"


# ── Model management ──

def _get_info() -> dict:
    info_candidates = [
        Path(__file__).resolve().parent.parent / "info.json",
        OPENVINO_ROOT / "temp" / SKILL_NAME / "info.json",
    ]
    for p in info_candidates:
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    return {"models": []}


def _check_model_ready(model_dir: Path, required_files: list) -> bool:
    if not model_dir.exists():
        return False
    for rf in required_files:
        if not (model_dir / rf).exists():
            return False
    return True


def _download_model(model_info: dict) -> Path:
    _state.status = "downloading"
    model_id = model_info["model_id"]
    dir_name = model_info["dir_name"]
    target_dir = MODELS_DIR / dir_name
    partial_dir = MODELS_DIR / f"{dir_name}.partial"

    if _check_model_ready(target_dir, model_info.get("required_files", [])):
        log(f"Model already present: {target_dir}")
        return target_dir

    log(f"Downloading model: {model_id} -> {partial_dir}")
    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    from modelscope import snapshot_download
    snapshot_download(model_id, local_dir=str(partial_dir))

    required = model_info.get("required_files", [])
    if not _check_model_ready(partial_dir, required):
        raise RuntimeError(f"Model download incomplete, missing files in {partial_dir}")

    if target_dir.exists():
        import shutil
        shutil.rmtree(target_dir)
    partial_dir.rename(target_dir)
    log(f"Model ready: {target_dir}")
    return target_dir


def _load_model(model_dir: Path):
    _state.status = "loading"
    log(f"Loading model from {model_dir}")

    import openvino_genai

    device = _pick_device()
    _state.pipe = openvino_genai.LLMPipeline(str(model_dir), device)
    _state.tokenizer = _state.pipe.get_tokenizer()
    _state.model_dir = str(model_dir)
    _state.device = device
    log(f"Model loaded on {device}")


def _ensure_models():
    try:
        info = _get_info()
        models = info.get("models", [])
        if not models:
            raise RuntimeError("No models configured in info.json")

        model_info = models[0]
        model_dir = _download_model(model_info)
        _load_model(model_dir)
        _state.status = "ok"
        log("Server ready")
    except Exception as e:
        _state.status = "error"
        _state.error = str(e)
        log(f"Model init failed: {e}\n{traceback.format_exc()}")


# ── Inference ──

_SENTINEL = object()

def _do_inference(messages: list, thinking: bool = False, max_tokens: int = 512) -> dict:
    """Non-streaming inference (for /v1/chat/completions)."""
    import openvino_genai

    if not _state.pipe or not _state.tokenizer:
        raise RuntimeError("model not loaded")

    tokenized_prompt = _state.tokenizer.apply_chat_template(
        messages,
        add_generation_prompt=True,
        extra_context={"enable_thinking": thinking},
    )

    config = openvino_genai.GenerationConfig()
    config.max_new_tokens = max_tokens
    config.do_sample = True
    config.temperature = 0.9 if thinking else 0.7
    config.top_p = 0.95

    if not thinking:
        soc = openvino_genai.StructuredOutputConfig()
        soc.regex = r"[^<].*"
        config.structured_output_config = soc

    result = _state.pipe.generate(tokenized_prompt, config)
    text = result.texts[0] if hasattr(result, "texts") else str(result)

    thinking_content = None
    answer_content = text

    if thinking and "<think>" in text:
        think_start = text.find("<think>")
        think_end = text.find("</think>")
        if think_start != -1 and think_end != -1:
            thinking_content = text[think_start + len("<think>"):think_end].strip()
            answer_content = text[think_end + len("</think>"):].strip()

    return {
        "content": answer_content,
        "thinking": thinking_content,
    }


def _do_inference_streaming(messages: list, thinking: bool = False, max_tokens: int = 512) -> queue.Queue:
    """Streaming inference, returns a queue emitting subword text chunks.

    Queue items:
    - str: one subword text chunk
    - _SENTINEL: generation done
    - Exception: generation error
    """
    import openvino_genai

    if not _state.pipe or not _state.tokenizer:
        raise RuntimeError("model not loaded")

    token_queue: queue.Queue = queue.Queue()

    tokenized_prompt = _state.tokenizer.apply_chat_template(
        messages,
        add_generation_prompt=True,
        extra_context={"enable_thinking": thinking},
    )

    config = openvino_genai.GenerationConfig()
    config.max_new_tokens = max_tokens
    config.do_sample = True
    config.temperature = 0.9 if thinking else 0.7
    config.top_p = 0.95

    if not thinking:
        soc = openvino_genai.StructuredOutputConfig()
        soc.regex = r"[^<].*"
        config.structured_output_config = soc

    def streamer_callback(subword: str) -> bool:
        token_queue.put(subword)
        return False  # False = continue generation

    def run_generate():
        try:
            _state.pipe.generate(tokenized_prompt, config, streamer=streamer_callback)
        except Exception as e:
            token_queue.put(e)
        finally:
            token_queue.put(_SENTINEL)

    threading.Thread(target=run_generate, daemon=True).start()
    return token_queue


# ── FastAPI app ──

app = FastAPI(title="MiniCPM OpenVINO Server")


@app.get("/api/health")
def health():
    """Polled by the frontend and settings panel to check service status."""
    resp = {
        "ok": _state.status == "ok",
        "status": _state.status,
        "alive": _state.status == "ok",
        "pid": os.getpid(),
        "uptime_s": _state.uptime_s,
        "model_name": MODEL_NAME,
        "model_dir": _state.model_dir,
        "device": _state.device,
        "persona": "default",
        "adapter": None,
        "llama_server": {"status": "ok" if _state.status == "ok" else _state.status},
    }
    if _state.error:
        resp["error"] = _state.error
    if _state.progress:
        resp["progress"] = _state.progress
    return resp


# ── /api/chat — main frontend entry (SSE true streaming) ──

class ChatRequest(BaseModel):
    messages: list[dict]
    stream: bool = True
    max_new_tokens: int = 768
    temperature: float = 0.6
    top_p: float = 0.95
    top_k: int = 0
    repetition_penalty: float = 1.05
    thinking: bool = False
    silent: bool = False
    disable_adapter: bool = False


@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    """Frontend chat API, token-by-token SSE streaming.

    Request example:
        POST /api/chat
        {"messages": [{"role": "user", "content": "hello"}],
         "stream": true, "max_new_tokens": 768,
         "temperature": 0.6, "thinking": false}

    Response example (SSE text/event-stream):
        data: {"event":"delta","content":"Hel"}\n\n
        data: {"event":"delta","content":"lo"}\n\n
        data: {"event":"delta","content":"! I am MiniCPM"}\n\n

    With thinking=true, think events come before delta:
        data: {"event":"think","content":"Let me think..."}\n\n
        data: {"event":"delta","content":"The answer is..."}\n\n

    On error:
        data: {"event":"error","message":"model inference failed"}\n\n
    """
    if _state.status != "ok":
        friendly_msg = "Model not ready yet, please retry shortly" if _state.status in ("downloading", "loading", "starting") else "Model failed to load, run with --debug to diagnose"
        error_event = json.dumps({"event": "error", "message": friendly_msg})
        return StreamingResponse(
            iter([f"data: {error_event}\n\n"]),
            media_type="text/event-stream",
        )

    messages = req.messages
    thinking = req.thinking
    max_tokens = req.max_new_tokens
    if thinking and max_tokens < 1280:
        max_tokens = 1280

    try:
        token_queue = _do_inference_streaming(messages, thinking=thinking, max_tokens=max_tokens)
    except Exception as e:
        log(f"[INTERNAL] Inference startup error: {e}\n{traceback.format_exc()}")
        error_event = json.dumps({"event": "error", "message": "Failed to start model inference, check device support"})
        return StreamingResponse(
            iter([f"data: {error_event}\n\n"]),
            media_type="text/event-stream",
        )

    async def generate_sse():
        in_think = False
        think_ended = False
        buf = ""

        while True:
            try:
                item = await asyncio.get_event_loop().run_in_executor(
                    None, token_queue.get, True, 120.0
                )
            except Exception:
                yield f"data: {json.dumps({'event': 'error', 'message': 'Inference timed out, retry or try a smaller max_new_tokens'})}\n\n"
                return

            if item is _SENTINEL:
                # If we accumulated thinking content but never saw </think>,
                # flush remaining buffer as delta
                if buf and not think_ended:
                    yield f"data: {json.dumps({'event': 'delta', 'content': buf})}\n\n"
                return

            if isinstance(item, Exception):
                log(f"[INTERNAL] Streaming error: {item}\n{traceback.format_exc()}")
                yield f"data: {json.dumps({'event': 'error', 'message': 'Error during model inference, please retry'})}\n\n"
                return

            # item is a subword string from the streamer
            buf += item

            if thinking and not think_ended:
                # Detect <think> tag to start emitting think events
                if not in_think and "<think>" in buf:
                    # Discard anything before and including <think>
                    idx = buf.index("<think>") + len("<think>")
                    buf = buf[idx:]
                    in_think = True

                if in_think:
                    # Check if </think> appeared
                    if "</think>" in buf:
                        idx = buf.index("</think>")
                        think_chunk = buf[:idx]
                        buf = buf[idx + len("</think>"):]
                        if think_chunk:
                            yield f"data: {json.dumps({'event': 'think', 'content': think_chunk})}\n\n"
                        in_think = False
                        think_ended = True
                        # Flush remaining buf as delta
                        if buf.strip():
                            yield f"data: {json.dumps({'event': 'delta', 'content': buf})}\n\n"
                            buf = ""
                    else:
                        # Emit accumulated think content, keep last 20 chars
                        # as buffer in case </think> spans across chunks
                        if len(buf) > 20:
                            emit = buf[:-20]
                            buf = buf[-20:]
                            yield f"data: {json.dumps({'event': 'think', 'content': emit})}\n\n"
                continue

            # Non-thinking mode or after </think>: emit as delta
            if buf:
                yield f"data: {json.dumps({'event': 'delta', 'content': buf})}\n\n"
                buf = ""

    return StreamingResponse(generate_sse(), media_type="text/event-stream")


# ── /v1/chat/completions — OpenAI-compatible API (fallback) ──

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "minicpm5-1b-openvino"
    messages: list[ChatMessage]
    max_tokens: int = 512
    temperature: float = 0.7
    stream: bool = False


@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionRequest):
    if _state.status != "ok":
        raise HTTPException(
            status_code=503,
            detail=f"Model not ready (status={_state.status})"
        )

    if req.stream:
        raise HTTPException(status_code=501, detail="Use /api/chat for streaming")

    messages = [{"role": m.role, "content": m.content} for m in req.messages]
    thinking = req.temperature > 0.8

    try:
        result = _do_inference(messages, thinking=thinking, max_tokens=req.max_tokens)
    except Exception as e:
        log(f"Inference error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": result["content"],
            },
            "finish_reason": "stop",
        }],
    }


# ── /api/adapters — frontend adapter list (returns empty) ──

@app.get("/api/adapters")
def list_adapters():
    """OpenVINO backend has no LoRA adapter support, returns an empty list."""
    return {"items": [], "current": None}


# ── /api/update-check — frontend update check (returns no update) ──

@app.get("/api/update-check")
def update_check():
    return {"available": False, "local_revision": "openvino-1.0", "remote_revision": "openvino-1.0"}


@app.post("/api/shutdown")
def shutdown():
    log("Shutdown requested via API")
    threading.Timer(1.0, lambda: os._exit(0)).start()
    return {"ok": True, "message": "Shutting down in 1s"}


# ── Entry point ──

def main():
    log(f"Starting HTTP server on {SERVER_HOST}:{SERVER_PORT}")

    init_thread = threading.Thread(target=_ensure_models, daemon=True)
    init_thread.start()

    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="warning")


if __name__ == "__main__":
    main()
