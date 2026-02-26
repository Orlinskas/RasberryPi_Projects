#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тестовый MJPEG-видеострим с USB-камеры для просмотра на компьютере."""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from http import server
from socketserver import ThreadingMixIn
from typing import Optional


class FrameStore:
    """Потокобезопасное хранилище последнего JPEG-кадра."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: Optional[bytes] = None
        self._last_frame_ts: float = 0.0
        self._frames_total: int = 0
        self._last_error: str = ""

    def set(self, frame: bytes) -> None:
        with self._lock:
            self._frame = frame
            self._last_frame_ts = time.time()
            self._frames_total += 1

    def get(self) -> Optional[bytes]:
        with self._lock:
            return self._frame

    def set_error(self, message: str) -> None:
        with self._lock:
            self._last_error = message

    def snapshot(self) -> tuple[float, int, str]:
        with self._lock:
            return self._last_frame_ts, self._frames_total, self._last_error


class StreamHandler(server.BaseHTTPRequestHandler):
    """HTTP-обработчик: страница предпросмотра и MJPEG поток."""

    frame_store: FrameStore = FrameStore()
    stop_event = threading.Event()

    def log_message(self, format: str, *args) -> None:
        # Убираем шум стандартного HTTP-лога.
        return None

    def _send_index(self) -> None:
        last_frame_ts, frames_total, last_error = self.frame_store.snapshot()
        if last_frame_ts > 0:
            last_age_s = max(0.0, time.time() - last_frame_ts)
            status = f"OK, frames={frames_total}, last_frame_age={last_age_s:.2f}s"
        else:
            status = "No frames yet"
        if last_error:
            status = f"{status}; last_error={last_error}"
        body = (
            "<html><head><title>Camera Stream</title></head>"
            "<body><h2>USB Camera Stream</h2>"
            f"<p>Status: {status}</p>"
            "<p><a href='/health'>health</a></p>"
            "<img src='/stream.mjpg' style='max-width:100%;height:auto;'/>"
            "</body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_stream(self) -> None:
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        try:
            while not self.stop_event.is_set():
                frame = self.frame_store.get()
                if frame is None:
                    time.sleep(0.03)
                    continue
                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii"))
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                time.sleep(0.001)
        except (BrokenPipeError, ConnectionResetError):
            return None

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._send_index()
            return
        if self.path == "/stream.mjpg":
            self._send_stream()
            return
        if self.path == "/health":
            last_frame_ts, frames_total, last_error = self.frame_store.snapshot()
            payload = (
                "{\n"
                f'  "frames_total": {frames_total},\n'
                f'  "last_frame_ts": {last_frame_ts:.6f},\n'
                f'  "has_frame": {"true" if last_frame_ts > 0 else "false"},\n'
                f'  "last_error": "{last_error.replace(chr(34), chr(92) + chr(34))}"\n'
                "}\n"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(404)


class ThreadedHTTPServer(ThreadingMixIn, server.HTTPServer):
    """Многопоточный HTTP сервер."""

    daemon_threads = True
    allow_reuse_address = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Тестовый видеострим с USB-камеры")
    parser.add_argument("--host", default="0.0.0.0", help="Хост HTTP сервера")
    parser.add_argument("--port", type=int, default=8080, help="Порт HTTP сервера")
    parser.add_argument("--camera-index", type=int, default=0, help="Индекс камеры")
    parser.add_argument("--width", type=int, default=640, help="Ширина кадра")
    parser.add_argument("--height", type=int, default=480, help="Высота кадра")
    parser.add_argument("--fps", type=float, default=20.0, help="Ограничение FPS потока")
    parser.add_argument("--jpeg-quality", type=int, default=80, help="Качество JPEG (1..100)")
    parser.add_argument("--warmup", type=float, default=0.2, help="Пауза прогрева камеры")
    parser.add_argument(
        "--backend",
        choices=["auto", "v4l2", "gstreamer"],
        default="v4l2",
        help="Backend OpenCV для VideoCapture",
    )
    return parser.parse_args()


def _open_camera(cv2, args: argparse.Namespace):
    if args.backend == "auto":
        cap = cv2.VideoCapture(args.camera_index)
        if cap.isOpened():
            return cap, "auto"
        cap.release()
        return None, "auto"

    backend = cv2.CAP_V4L2 if args.backend == "v4l2" else cv2.CAP_GSTREAMER
    cap = cv2.VideoCapture(args.camera_index, backend)
    if cap.isOpened():
        return cap, args.backend
    cap.release()
    return None, args.backend


def capture_loop(args: argparse.Namespace, frame_store: FrameStore, stop_event: threading.Event) -> None:
    try:
        import cv2
    except ImportError:
        print("ERROR: OpenCV (cv2) не установлен. Установите python3-opencv.", file=sys.stderr)
        frame_store.set_error("cv2_missing")
        stop_event.set()
        return

    cap, backend_name = _open_camera(cv2, args)
    if cap is None:
        message = f"camera_open_failed(index={args.camera_index}, backend={args.backend})"
        print(f"ERROR: не удалось открыть камеру index={args.camera_index} backend={args.backend}", file=sys.stderr)
        frame_store.set_error(message)
        stop_event.set()
        return
    print(f"Camera opened: index={args.camera_index} backend={backend_name}")

    frame_interval = 1.0 / max(1.0, float(args.fps))
    quality = max(1, min(100, int(args.jpeg_quality)))
    read_failures = 0

    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(args.width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(args.height))
        cap.set(cv2.CAP_PROP_FPS, float(args.fps))
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc("M", "J", "P", "G"))
        if args.warmup > 0:
            time.sleep(float(args.warmup))

        while not stop_event.is_set():
            started = time.monotonic()
            ok, frame = cap.read()
            if ok and frame is not None:
                read_failures = 0
                enc_ok, encoded = cv2.imencode(
                    ".jpg",
                    frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), quality],
                )
                if enc_ok:
                    frame_store.set(encoded.tobytes())
                    frame_store.set_error("")
                else:
                    frame_store.set_error("jpeg_encode_failed")
            else:
                read_failures += 1
                if read_failures % 20 == 0:
                    frame_store.set_error(f"camera_read_failed_x{read_failures}")
                    print(f"WARN: camera read failed x{read_failures}", file=sys.stderr)
            elapsed = time.monotonic() - started
            remaining = frame_interval - elapsed
            if remaining > 0:
                stop_event.wait(remaining)
    finally:
        cap.release()


def main() -> int:
    args = parse_args()
    stop_event = threading.Event()
    frame_store = FrameStore()

    StreamHandler.frame_store = frame_store
    StreamHandler.stop_event = stop_event

    server_instance = ThreadedHTTPServer((args.host, args.port), StreamHandler)
    capture_thread = threading.Thread(
        target=capture_loop,
        args=(args, frame_store, stop_event),
        name="camera-capture",
        daemon=True,
    )
    capture_thread.start()

    def _shutdown(*_unused: object) -> None:
        stop_event.set()
        server_instance.shutdown()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print(f"Stream URL: http://{args.host}:{args.port}/")
    print(f"MJPEG URL:  http://{args.host}:{args.port}/stream.mjpg")
    print("Press Ctrl+C to stop.")

    try:
        server_instance.serve_forever()
    finally:
        stop_event.set()
        server_instance.server_close()
        capture_thread.join(timeout=2.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
