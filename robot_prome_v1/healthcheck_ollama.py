#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка доступности удаленного Ollama и рабочих моделей для robot_prome_v1."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple


DEFAULT_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://192.168.0.10:11434")
DEFAULT_BRAIN_MODEL = os.getenv("OLLAMA_BRAIN_MODEL", "qwen2.5:0.5b")
DEFAULT_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "moondream:latest")
DEFAULT_TIMEOUT_S = float(os.getenv("OLLAMA_TIMEOUT_S", "15"))


def _post_json(base_url: str, path: str, payload: Dict[str, Any], timeout_s: float) -> Tuple[Optional[Dict[str, Any]], float, Optional[str]]:
    started_at = time.perf_counter()
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url=base_url.rstrip("/") + path,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, time.perf_counter() - started_at, str(exc)

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, time.perf_counter() - started_at, f"invalid json: {exc}"
    if not isinstance(decoded, dict):
        return None, time.perf_counter() - started_at, "response is not JSON object"
    return decoded, time.perf_counter() - started_at, None


def _get_json(base_url: str, path: str, timeout_s: float) -> Tuple[Optional[Dict[str, Any]], float, Optional[str]]:
    started_at = time.perf_counter()
    request = urllib.request.Request(url=base_url.rstrip("/") + path, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, time.perf_counter() - started_at, str(exc)

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, time.perf_counter() - started_at, f"invalid json: {exc}"
    if not isinstance(decoded, dict):
        return None, time.perf_counter() - started_at, "response is not JSON object"
    return decoded, time.perf_counter() - started_at, None


def _print_result(ok: bool, name: str, detail: str) -> None:
    prefix = "[PASS]" if ok else "[FAIL]"
    print(f"{prefix} {name}: {detail}")


def _check_tags(base_url: str, timeout_s: float) -> Tuple[bool, Optional[Dict[str, Any]]]:
    payload, elapsed_s, err = _get_json(base_url, "/api/tags", timeout_s)
    if err is not None:
        _print_result(False, "Ollama API /api/tags", f"{err} ({elapsed_s:.3f}s)")
        return False, None
    _print_result(True, "Ollama API /api/tags", f"reachable ({elapsed_s:.3f}s)")
    return True, payload


def _model_exists(tags_payload: Dict[str, Any], model_name: str) -> bool:
    models = tags_payload.get("models")
    if not isinstance(models, list):
        return False
    for model in models:
        if not isinstance(model, dict):
            continue
        if str(model.get("name", "")).strip() == model_name:
            return True
    return False


def _chat_probe(base_url: str, model: str, timeout_s: float, prompt: str) -> bool:
    payload, elapsed_s, err = _post_json(
        base_url,
        "/api/chat",
        {
            "model": model,
            "stream": False,
            "messages": [
                {"role": "system", "content": "You are a concise assistant."},
                {"role": "user", "content": prompt},
            ],
            "options": {"temperature": 0.0, "num_predict": 32},
        },
        timeout_s,
    )
    if err is not None:
        _print_result(False, f"Chat probe {model}", f"{err} ({elapsed_s:.3f}s)")
        return False
    message = payload.get("message") if isinstance(payload, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        _print_result(False, f"Chat probe {model}", f"empty content ({elapsed_s:.3f}s)")
        return False
    _print_result(True, f"Chat probe {model}", f"ok ({elapsed_s:.3f}s)")
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Healthcheck remote Ollama for robot_prome_v1")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Ollama base URL, e.g. http://192.168.1.100:11434")
    parser.add_argument("--brain-model", default=DEFAULT_BRAIN_MODEL, help="Brain model tag (qwen)")
    parser.add_argument("--vision-model", default=DEFAULT_VISION_MODEL, help="Vision model tag (moondream)")
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S, help="HTTP timeout in seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    timeout_s = max(0.1, float(args.timeout_s))

    print("== Ollama healthcheck ==")
    print(f"base_url={args.base_url}")
    print(f"brain_model={args.brain_model}")
    print(f"vision_model={args.vision_model}")
    print(f"timeout_s={timeout_s}")
    print("")

    ok_all = True

    ok_tags, tags_payload = _check_tags(args.base_url, timeout_s)
    ok_all = ok_all and ok_tags
    if not ok_tags or tags_payload is None:
        return 1

    brain_exists = _model_exists(tags_payload, args.brain_model)
    _print_result(brain_exists, f"Model exists {args.brain_model}", "found in /api/tags" if brain_exists else "not found")
    ok_all = ok_all and brain_exists

    vision_exists = _model_exists(tags_payload, args.vision_model)
    _print_result(vision_exists, f"Model exists {args.vision_model}", "found in /api/tags" if vision_exists else "not found")
    ok_all = ok_all and vision_exists

    if brain_exists:
        ok_all = _chat_probe(
            args.base_url,
            args.brain_model,
            timeout_s,
            "Return one short line: brain health ok",
        ) and ok_all
    if vision_exists:
        ok_all = _chat_probe(
            args.base_url,
            args.vision_model,
            timeout_s,
            "Return one short line: vision health ok",
        ) and ok_all

    print("")
    if ok_all:
        print("HEALTHCHECK: OK")
        return 0
    print("HEALTHCHECK: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
