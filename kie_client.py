from __future__ import annotations

import asyncio
import json
import mimetypes
import uuid
from typing import Any

import httpx


KIE_CHAT_MODELS = ("gemini-3-flash", "gemini-2.5-flash")
KIE_VISION_MODELS = KIE_CHAT_MODELS
KIE_TRANSCRIPTION_MODELS = ("elevenlabs/speech-to-text", *KIE_CHAT_MODELS)
KIE_IMAGE_MODELS = (
    "seedream/4.5-text-to-image",
    "bytedance/seedream-v4-text-to-image",
    "google/imagen4-fast",
    "google/imagen4-ultra",
)
KIE_IMAGE_EDIT_MODELS = (
    "seedream/4.5-edit",
    "bytedance/seedream-v4-edit",
    "google/nano-banana-edit",
)


class KIEError(RuntimeError):
    pass


class KIEQuotaError(KIEError):
    pass


def model_base_url(base_url: str, model: str) -> str:
    return f"{base_url.rstrip('/')}/{model}/v1"


def _validate(status_code: int, payload: dict[str, Any], context: str) -> dict[str, Any]:
    detail = payload.get("msg") or payload.get("message") or str(payload)
    if status_code != 200:
        raise KIEError(f"{context}: status={status_code} message={detail}")
    code = payload.get("code")
    if code not in (None, 200, "200"):
        if any(item in str(detail).lower() for item in ("billing", "quota", "balance", "credit")):
            raise KIEQuotaError(f"KIE API Error: {detail}")
        raise KIEError(f"{context}: {detail}")
    return payload.get("data") if isinstance(payload.get("data"), dict) else payload


def _extract_chat_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text") or "") for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip()
    return ""


async def chat(
    api_key: str,
    base_url: str,
    model: str,
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.7,
) -> str:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "max_tokens": 4096, "temperature": temperature, "stream": False}
    try:
        async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
            response = await client.post(f"{model_base_url(base_url, model)}/chat/completions", headers=headers, json=payload)
        text = _extract_chat_text(_validate(response.status_code, response.json(), "KIE chat failed"))
        if not text:
            raise KIEError("KIE chat returned empty content")
        return text
    except (KIEError, KIEQuotaError):
        raise
    except Exception as exc:
        raise KIEError(f"KIE chat request failed: {exc}") from exc


async def chat_structured(
    api_key: str,
    base_url: str,
    model: str,
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.7,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "max_tokens": 4096, "temperature": temperature, "stream": False}
    try:
        async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
            response = await client.post(f"{model_base_url(base_url, model)}/chat/completions", headers=headers, json=payload)
        resp_json = response.json()
        validated = _validate(response.status_code, resp_json, "KIE chat failed")
        text = _extract_chat_text(validated)
        if not text:
            raise KIEError("KIE chat returned empty content")
        choices = resp_json.get("choices") or []
        finish_reason = choices[0].get("finish_reason") if choices else None
        return {
            "text": text,
            "raw_payload": resp_json,
            "http_status": response.status_code,
            "finish_reason": finish_reason,
            "diagnostics": {"provider": "KIE", "model": model},
        }
    except (KIEError, KIEQuotaError):
        raise
    except Exception as exc:
        raise KIEError(f"KIE chat request failed: {exc}") from exc


async def upload_file(api_key: str, upload_base_url: str, file_bytes: bytes, filename: str, upload_path: str) -> str:
    headers = {"Authorization": f"Bearer {api_key}"}
    files = {"file": (filename, file_bytes, mimetypes.guess_type(filename)[0] or "application/octet-stream")}
    data = {"uploadPath": upload_path, "fileName": filename}
    try:
        async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
            response = await client.post(f"{upload_base_url.rstrip('/')}/api/file-stream-upload", headers=headers, data=data, files=files)
        payload = _validate(response.status_code, response.json(), "KIE upload failed")
        url = payload.get("downloadUrl") or payload.get("fileUrl")
        if not url:
            raise KIEError("KIE upload returned no file URL")
        return str(url)
    except (KIEError, KIEQuotaError):
        raise
    except Exception as exc:
        raise KIEError(f"KIE upload failed: {exc}") from exc


async def create_task(api_key: str, base_url: str, model: str, input_payload: dict[str, Any]) -> str:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
            response = await client.post(f"{base_url.rstrip('/')}/api/v1/jobs/createTask", headers=headers, json={"model": model, "input": input_payload})
        task_id = _validate(response.status_code, response.json(), "KIE task creation failed").get("taskId")
        if not task_id:
            raise KIEError("KIE task creation returned no taskId")
        return str(task_id)
    except (KIEError, KIEQuotaError):
        raise
    except Exception as exc:
        raise KIEError(f"KIE task creation failed: {exc}") from exc


async def poll_task(api_key: str, base_url: str, task_id: str, timeout_sec: int = 180) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}"}
    deadline = asyncio.get_running_loop().time() + timeout_sec
    delay = 2.0
    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        while True:
            response = await client.get(f"{base_url.rstrip('/')}/api/v1/jobs/recordInfo", headers=headers, params={"taskId": task_id})
            payload = _validate(response.status_code, response.json(), f"KIE task polling failed: {task_id}")
            state = str(payload.get("state") or payload.get("status") or "").lower()
            if state in {"success", "succeed", "succeeded"} or payload.get("successFlag") == 1:
                return payload
            if state in {"fail", "failed", "error"}:
                raise KIEError(str(payload.get("failMsg") or payload.get("errorMessage") or "KIE task failed"))
            if asyncio.get_running_loop().time() >= deadline:
                raise KIEError(f"KIE task timed out: {task_id}")
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 8.0)


def task_result(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("response"), dict):
        return payload["response"]
    result = payload.get("resultJson")
    if isinstance(result, str) and result:
        return json.loads(result)
    return result if isinstance(result, dict) else {}


async def download_url(api_key: str, base_url: str, url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            response = await client.post(
                f"{base_url.rstrip('/')}/api/v1/common/download-url",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"url": url},
            )
        if response.status_code == 200:
            return str(response.json().get("data") or url)
    except Exception:
        pass
    return url


async def download_binary(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
        response = await client.get(url)
    if response.status_code != 200:
        raise KIEError(f"KIE result download failed: status={response.status_code}")
    return response.content


async def remaining_credits(api_key: str, base_url: str) -> float:
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        response = await client.get(f"{base_url.rstrip('/')}/api/v1/chat/credit", headers={"Authorization": f"Bearer {api_key}"})
    raw_payload = response.json()
    payload = _validate(response.status_code, raw_payload, "KIE credits check failed")
    value: Any = raw_payload.get("data") if isinstance(raw_payload, dict) else payload
    if isinstance(value, dict):
        value = next((value.get(key) for key in ("remainingCredits", "remaining_credits", "credits", "balance", "creditBalance") if value.get(key) is not None), None)
    if value is None:
        raise KIEError("KIE credits response has no balance")
    return float(value)


def _shape(prompt: str) -> str:
    source = (prompt or "").lower()
    if any(item in source for item in ("tarot", "card", "poster", "cover", "vertical", "portrait orientation", "full body")):
        return "3:4"
    if any(item in source for item in ("horizontal", "wide", "banner", "landscape orientation", "widescreen")):
        return "4:3"
    return "1:1"


def image_input(model: str, prompt: str) -> dict[str, Any]:
    ratio = _shape(prompt)
    if model == "google/imagen4-fast":
        return {"prompt": prompt, "aspect_ratio": ratio, "num_images": "1"}
    if model in {"google/imagen4-ultra", "google/imagen4"}:
        return {"prompt": prompt, "aspect_ratio": ratio}
    if model == "bytedance/seedream-v4-text-to-image":
        return {"prompt": prompt, "image_size": "square_hd", "image_resolution": "1K", "max_images": 1}
    if model == "seedream/4.5-text-to-image":
        return {"prompt": prompt, "aspect_ratio": ratio, "quality": "basic"}
    raise KIEError(f"Unsupported KIE image model: {model}")


def image_edit_input(model: str, prompt: str, source_url: str) -> dict[str, Any]:
    if model == "google/nano-banana-edit":
        return {"prompt": prompt, "image_urls": [source_url], "output_format": "png", "image_size": "1:1"}
    if model == "bytedance/seedream-v4-edit":
        return {"prompt": prompt, "image_urls": [source_url], "image_size": "square_hd", "image_resolution": "1K", "max_images": 1}
    if model == "seedream/4.5-edit":
        return {"prompt": prompt, "image_urls": [source_url], "aspect_ratio": _shape(prompt), "quality": "basic"}
    raise KIEError(f"Unsupported KIE image edit model: {model}")


async def generate_image(api_key: str, base_url: str, model: str, prompt: str) -> bytes:
    task = await create_task(api_key, base_url, model, image_input(model, prompt))
    result = task_result(await poll_task(api_key, base_url, task))
    urls = result.get("resultUrls") or result.get("result_urls") or []
    if not urls:
        raise KIEError("KIE image generation returned no result URLs")
    return await download_binary(await download_url(api_key, base_url, str(urls[0])))


async def edit_image(api_key: str, base_url: str, upload_base_url: str, model: str, prompt: str, image_bytes: bytes) -> bytes:
    filename = f"image_edit_{uuid.uuid4().hex[:12]}.jpg"
    source_url = await upload_file(api_key, upload_base_url, image_bytes, filename, "images")
    task = await create_task(api_key, base_url, model, image_edit_input(model, prompt, source_url))
    result = task_result(await poll_task(api_key, base_url, task))
    urls = result.get("resultUrls") or result.get("result_urls") or []
    if not urls:
        raise KIEError("KIE image edit returned no result URLs")
    return await download_binary(await download_url(api_key, base_url, str(urls[0])))


async def transcribe(api_key: str, base_url: str, upload_base_url: str, model: str, audio_bytes: bytes, filename: str) -> str:
    url = await upload_file(api_key, upload_base_url, audio_bytes, filename, "audio")
    if model != "elevenlabs/speech-to-text":
        return await chat(
            api_key, base_url, model,
            [{"role": "system", "content": "Ты сервис точной транскрибации речи."}, {"role": "user", "content": [{"type": "text", "text": "Транскрибируй аудио на русском. Верни только текст."}, {"type": "image_url", "image_url": {"url": url}}]}],
            temperature=0.0,
        )
    task = await create_task(api_key, base_url, model, {"audio_url": url, "language_code": "ru", "tag_audio_events": False, "diarize": False})
    result = task_result(await poll_task(api_key, base_url, task))
    for key in ("text", "transcript", "transcription", "content", "result"):
        if isinstance(result.get(key), str) and result[key].strip():
            return result[key].strip()
    raise KIEError("KIE transcription returned no text")
