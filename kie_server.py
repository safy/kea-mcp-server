#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minimal stdio MCP server wrapping the kie.ai unified Jobs API for image generation.

No third-party dependencies — Python standard library only.

Protocol: MCP over stdio = newline-delimited JSON-RPC 2.0 messages.
Auth: reads the API key from the KIE_API_KEY environment variable.
Optional: KIE_SAVE_DIR env var sets a default folder to download results into.

Exposed tools:
  - kie_generate_image : create an image task, poll until done, return URL(s) (and
                         optionally download the file locally).
  - kie_get_task       : query the status/result of a previously created task id.

Docs: https://docs.kie.ai/  (createTask + recordInfo)
"""

import sys
import os
import json
import time
import urllib.request
import urllib.error
import urllib.parse

API_BASE = "https://api.kie.ai/api/v1/jobs"
CREATE_URL = API_BASE + "/createTask"
RECORD_URL = API_BASE + "/recordInfo"

SERVER_NAME = "kie"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL = "2025-06-18"


def log(*a):
    """Log to stderr only — stdout is reserved for the JSON-RPC protocol."""
    print("[kie-mcp]", *a, file=sys.stderr, flush=True)


def api_key():
    k = os.environ.get("KIE_API_KEY", "").strip()
    return k


def _http(method, url, body=None):
    """Return (status_code, parsed_json_or_text)."""
    headers = {
        "Authorization": "Bearer " + api_key(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace") if e.fp else ""
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw or str(e)
    except Exception as e:
        return 0, str(e)


def create_task(prompt, model, aspect_ratio, resolution, output_format, image_input):
    inp = {"prompt": prompt}
    if image_input:
        inp["image_input"] = image_input
    if aspect_ratio:
        inp["aspect_ratio"] = aspect_ratio
    if resolution:
        inp["resolution"] = resolution
    if output_format:
        inp["output_format"] = output_format
    body = {"model": model, "input": inp}
    status, js = _http("POST", CREATE_URL, body)
    if status != 200 or not isinstance(js, dict):
        raise RuntimeError("createTask HTTP %s: %s" % (status, js))
    if js.get("code") not in (200, 0, None) and js.get("code") != 200:
        # kie wraps errors in code/msg
        if js.get("code") != 200:
            raise RuntimeError("createTask error %s: %s" % (js.get("code"), js.get("msg")))
    data = js.get("data") or {}
    task_id = data.get("taskId") or data.get("task_id")
    if not task_id:
        raise RuntimeError("createTask returned no taskId: %s" % js)
    return task_id


def get_record(task_id, retries=4):
    url = RECORD_URL + "?" + urllib.parse.urlencode({"taskId": task_id})
    last = None
    for attempt in range(retries):
        status, js = _http("GET", url)
        if status == 200 and isinstance(js, dict):
            return js.get("data") or {}
        last = "recordInfo HTTP %s: %s" % (status, js)
        # transient network/server errors → retry
        if status in (0, 429, 500, 502, 503, 504):
            time.sleep(2 * (attempt + 1))
            continue
        raise RuntimeError(last)
    raise RuntimeError(last)


def extract_urls(data):
    rj = data.get("resultJson")
    if not rj:
        return []
    if isinstance(rj, str):
        try:
            rj = json.loads(rj)
        except Exception:
            return []
    if isinstance(rj, dict):
        return rj.get("resultUrls") or rj.get("result_urls") or []
    return []


def download(url, save_dir, basename):
    os.makedirs(save_dir, exist_ok=True)
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1] or ".png"
    path = os.path.join(save_dir, basename + ext)
    req = urllib.request.Request(url, headers={"User-Agent": "kie-mcp"})
    with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
        f.write(r.read())
    return path


def tool_generate_image(args):
    if not api_key():
        return "Ошибка: переменная окружения KIE_API_KEY не задана. Создай ключ на https://kie.ai/api-key и пропиши его (см. mcp/README.md)."
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return "Ошибка: параметр prompt обязателен."
    model = args.get("model") or "nano-banana-2"
    aspect_ratio = args.get("aspect_ratio") or "1:1"
    resolution = args.get("resolution") or "1K"
    output_format = args.get("output_format") or "png"
    image_input = args.get("image_input") or []
    save_dir = args.get("save_dir") or os.environ.get("KIE_SAVE_DIR") or ""
    timeout_sec = float(args.get("timeout_sec") or os.environ.get("KIE_TIMEOUT_SEC") or 180)

    task_id = create_task(prompt, model, aspect_ratio, resolution, output_format, image_input)
    deadline = time.time() + timeout_sec
    state = "waiting"
    data = {}
    while time.time() < deadline:
        data = get_record(task_id)
        state = (data.get("state") or "").lower()
        if state in ("success", "fail"):
            break
        time.sleep(3)

    if state == "fail":
        return "Задача %s завершилась с ошибкой: %s %s" % (task_id, data.get("failCode"), data.get("failMsg"))
    if state != "success":
        return ("Задача %s ещё выполняется (state=%s). Превышен таймаут ожидания. "
                "Проверь позже через kie_get_task с этим task_id.") % (task_id, state)

    urls = extract_urls(data)
    if not urls:
        return "Задача %s завершилась, но без resultUrls. Сырые данные: %s" % (task_id, json.dumps(data, ensure_ascii=False))

    lines = ["Готово. task_id=%s, модель=%s" % (task_id, model)]
    for i, u in enumerate(urls):
        if save_dir:
            try:
                p = download(u, save_dir, "kie_%s_%d" % (task_id, i))
                lines.append("Скачано: %s  (источник: %s)" % (p, u))
            except Exception as e:
                lines.append("URL: %s  (скачать не удалось: %s)" % (u, e))
        else:
            lines.append("URL: %s" % u)
    return "\n".join(lines)


def tool_get_task(args):
    if not api_key():
        return "Ошибка: KIE_API_KEY не задан."
    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return "Ошибка: параметр task_id обязателен."
    data = get_record(task_id)
    state = (data.get("state") or "").lower()
    out = ["task_id=%s, state=%s, progress=%s" % (task_id, state, data.get("progress"))]
    if state == "success":
        for u in extract_urls(data):
            save_dir = args.get("save_dir") or os.environ.get("KIE_SAVE_DIR") or ""
            if save_dir:
                try:
                    out.append("Скачано: %s" % download(u, save_dir, "kie_%s" % task_id))
                except Exception as e:
                    out.append("URL: %s (скачать не удалось: %s)" % (u, e))
            else:
                out.append("URL: %s" % u)
    elif state == "fail":
        out.append("Ошибка: %s %s" % (data.get("failCode"), data.get("failMsg")))
    return "\n".join(out)


TOOLS = [
    {
        "name": "kie_generate_image",
        "description": ("Сгенерировать изображение через kie.ai (unified Jobs API). Создаёт задачу, "
                        "ждёт результат и возвращает URL(ы). Если задан save_dir — скачивает файл локально и "
                        "возвращает путь. Модель по умолчанию nano-banana-2; другие id — на https://kie.ai/market."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Текстовое описание изображения."},
                "model": {"type": "string", "description": "ID модели kie.ai, напр. nano-banana-2, flux-2-pro-text-to-image.", "default": "nano-banana-2"},
                "aspect_ratio": {"type": "string", "description": "Соотношение сторон: 1:1, 16:9, 9:16, 4:3, 3:4 и т.д.", "default": "1:1"},
                "resolution": {"type": "string", "description": "1K, 2K или 4K.", "default": "1K"},
                "output_format": {"type": "string", "description": "png или jpg.", "default": "png"},
                "image_input": {"type": "array", "items": {"type": "string"}, "description": "Опционально: URL(ы) исходных изображений для image-to-image."},
                "save_dir": {"type": "string", "description": "Опционально: папка для скачивания результата (абсолютный путь)."},
                "timeout_sec": {"type": "number", "description": "Сколько секунд ждать готовности (по умолчанию 180).", "default": 180}
            },
            "required": ["prompt"]
        }
    },
    {
        "name": "kie_get_task",
        "description": "Проверить статус/результат ранее созданной задачи kie.ai по task_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Идентификатор задачи (taskId)."},
                "save_dir": {"type": "string", "description": "Опционально: папка для скачивания при готовности."}
            },
            "required": ["task_id"]
        }
    }
]


def dispatch_tool(name, args):
    if name == "kie_generate_image":
        return tool_generate_image(args)
    if name == "kie_get_task":
        return tool_get_task(args)
    raise RuntimeError("Unknown tool: %s" % name)


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def build_response(msg):
    """Process one JSON-RPC message and return a response dict, or None for
    notifications/responses that require no reply. Transport-agnostic: both the
    stdio loop and the HTTP transport (kie_http_server.py) call this."""
    method = msg.get("method")
    mid = msg.get("id")

    # notifications (no id) — no response
    if method == "notifications/initialized" or (method and mid is None):
        return None

    if method == "initialize":
        proto = (msg.get("params") or {}).get("protocolVersion") or DEFAULT_PROTOCOL
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": proto,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}
        }}

    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            text = dispatch_tool(name, args)
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": text}]
            }}
        except Exception as e:
            log("tool error:", repr(e))
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "Ошибка: %s" % e}],
                "isError": True
            }}

    # unknown method with id → error
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found: %s" % method}}
    return None


def process_request_body(raw):
    """Given a raw HTTP request body (bytes or str) holding a single JSON-RPC
    message or a batch array, return (http_status, response_obj). response_obj is
    None when the client sent only notifications/responses (send HTTP 202 then).
    Shared by the standalone HTTP server and the Vercel serverless adapter."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    try:
        msg = json.loads(raw) if raw and raw.strip() else {}
    except Exception as e:
        return 400, {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "Parse error: %s" % e}}
    if isinstance(msg, list):
        responses = [r for r in (build_response(m) for m in msg) if r is not None]
        return (202, None) if not responses else (200, responses)
    resp = build_response(msg)
    return (202, None) if resp is None else (200, resp)


def handle(msg):
    """stdio transport: process a message and write any response to stdout."""
    resp = build_response(msg)
    if resp is not None:
        send(resp)


def main():
    log("starting; key set:" , bool(api_key()))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as e:
            log("bad json:", e)
            continue
        try:
            handle(msg)
        except Exception as e:
            log("handle error:", repr(e))


if __name__ == "__main__":
    main()
