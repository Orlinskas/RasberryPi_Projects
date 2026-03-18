#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Author:   Vlad Orlinskas
# Site:     https://prometeriy.com
# Project:  robot_prome_v1 — LLM-driven autonomous robot experiment
# License:  Free for any use
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import logging
import socket
import statistics
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from pathlib import Path
from typing import Any, Deque, Optional, Protocol, Tuple

from settings import (
    CAMERA_BACKEND,
    CAMERA_FPS,
    CAMERA_HEIGHT,
    CAMERA_INDEX,
    CAMERA_WARMUP_S,
    CAMERA_WIDTH,
    CAPTURE_KEEP_LAST,
    ECHO_PIN,
    FRONT_SERVO_PIN,
    GPIO_LOCK,
    PROXIMITY_SERVO_CENTER_DEG,
    PROXIMITY_SERVO_DEVIATION_DEG,
    PROXIMITY_SERVO_SETTLE_S,
    STREAM_DEFAULT_PORT,
    STREAM_FPS,
    STREAM_JPEG_QUALITY,
    TRIG_PIN,
    ULTRASONIC_INTER_MEASURE_DELAY_S,
    ULTRASONIC_MAX_CM,
    ULTRASONIC_MIN_CM,
    ULTRASONIC_OUTLIER_RATIO,
    ULTRASONIC_SAMPLES_PER_READ,
    ULTRASONIC_TIMEOUT_S,
    VISION_EXTRA_DELAY_S,
    VISION_POLL_WAIT_S,
    CameraState,
    ProximityState,
    RobotState,
    VisionConfig,
    atomic_write_json,
    get_effective_duration_ms,
    read_json,
)

LOGGER = logging.getLogger("vision")

try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None

try:
    import cv2
except ImportError:
    cv2 = None

try:
    from picamera2 import Picamera2
except ImportError:
    Picamera2 = None


class FrameBuffer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: Optional[bytes] = None

    def put(self, jpeg_bytes: bytes) -> None:
        with self._lock:
            self._frame = bytes(jpeg_bytes)

    def get(self) -> Optional[bytes]:
        with self._lock:
            return self._frame if self._frame is not None else None


class StreamCapture:
    """Continuously captures frames in background for low-latency MJPEG stream."""

    def __init__(
        self,
        frame_buffer: FrameBuffer,
        camera_index: int = CAMERA_INDEX,
        width: int = CAMERA_WIDTH,
        height: int = CAMERA_HEIGHT,
        capture_fps: float = CAMERA_FPS,
        stream_fps: float = STREAM_FPS,
        jpeg_quality: int = STREAM_JPEG_QUALITY,
    ) -> None:
        self._frame_buffer = frame_buffer
        self._camera_index = camera_index
        self._width = width
        self._height = height
        self._capture_fps = capture_fps
        self._stream_fps = stream_fps
        self._jpeg_quality = max(50, min(95, jpeg_quality))
        self._interval = 1.0 / max(1.0, stream_fps)
        self._cap: Optional[Any] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._raw_lock = threading.Lock()
        self._last_raw: Optional[Any] = None
        self._encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self._jpeg_quality] if cv2 else []

    def _capture_loop(self) -> None:
        if cv2 is None or not self._cap:
            return
        while not self._stop.is_set():
            ok, frame = self._cap.read()
            if ok and frame is not None:
                with self._raw_lock:
                    self._last_raw = frame.copy()
                _, jpeg = cv2.imencode(".jpg", frame, self._encode_params)
                if jpeg is not None:
                    self._frame_buffer.put(jpeg.tobytes())
            self._stop.wait(self._interval)

    def start(self) -> bool:
        if cv2 is None:
            return False
        self._cap = cv2.VideoCapture(self._camera_index)
        if not self._cap.isOpened():
            self._cap.release()
            self._cap = None
            return False
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self._width))
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self._height))
        self._cap.set(cv2.CAP_PROP_FPS, float(self._capture_fps))
        self._cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc("M", "J", "P", "G"))
        time.sleep(CAMERA_WARMUP_S)
        self._stop.clear()
        self._thread = threading.Thread(target=self._capture_loop, name="stream-capture", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._cap:
            self._cap.release()
            self._cap = None

    def get_latest_raw(self) -> Optional[Any]:
        with self._raw_lock:
            return self._last_raw.copy() if self._last_raw is not None else None


class ProximitySensor(Protocol):
    def read_distance_cm(self) -> float:
        ...


class CameraDetector(Protocol):
    def read_image_path(self, state_id: str) -> Optional[str]:
        ...

    def close(self) -> None:
        ...


_SERVO_PWM_FREQ_HZ = 50

class UltrasonicProximitySensor:
    def __init__(self) -> None:
        self._initialized = False
        self._servo_initialized = False
        self._servo_pwm: Optional[Any] = None
        self._history: Deque[float] = deque(maxlen=5)
        self._last_read_time: float = 0.0

    def _init_gpio_once(self) -> None:
        if self._initialized:
            return
        if GPIO is None:
            raise RuntimeError("RPi.GPIO is unavailable")
        with GPIO_LOCK:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            GPIO.setup(ECHO_PIN, GPIO.IN)
            GPIO.setup(TRIG_PIN, GPIO.OUT)
            GPIO.output(TRIG_PIN, GPIO.LOW)
        time.sleep(0.05)
        self._initialized = True

    def _init_servo_once(self) -> None:
        if self._servo_initialized or PROXIMITY_SERVO_DEVIATION_DEG <= 0 or GPIO is None:
            return
        self._init_gpio_once()
        with GPIO_LOCK:
            GPIO.setup(FRONT_SERVO_PIN, GPIO.OUT, initial=GPIO.LOW)
            self._servo_pwm = GPIO.PWM(FRONT_SERVO_PIN, _SERVO_PWM_FREQ_HZ)
            self._servo_pwm.start(0)
        time.sleep(0.1)
        self._servo_initialized = True

    def _set_servo_angle(self, angle_deg: float) -> None:
        if self._servo_pwm is None:
            return
        angle_clamped = max(0.0, min(180.0, angle_deg))
        duty = 2.5 + 10 * angle_clamped / 180
        self._servo_pwm.ChangeDutyCycle(duty)
        time.sleep(PROXIMITY_SERVO_SETTLE_S)

    def _servo_off(self) -> None:
        if self._servo_pwm is not None:
            self._servo_pwm.ChangeDutyCycle(0)

    def _read_once_cm(self) -> Optional[float]:
        deadline = time.monotonic() + ULTRASONIC_TIMEOUT_S
        with GPIO_LOCK:
            GPIO.setup(ECHO_PIN, GPIO.IN)
            GPIO.setup(TRIG_PIN, GPIO.OUT)
            GPIO.output(TRIG_PIN, GPIO.LOW)
            time.sleep(0.000002)
            GPIO.output(TRIG_PIN, GPIO.HIGH)
            time.sleep(0.00001)
            GPIO.output(TRIG_PIN, GPIO.LOW)

            while GPIO.input(ECHO_PIN) == 0:
                if time.monotonic() > deadline:
                    return None
            pulse_start = time.monotonic()

            while GPIO.input(ECHO_PIN) == 1:
                if time.monotonic() > deadline:
                    return None
            pulse_end = time.monotonic()

        distance_cm = (pulse_end - pulse_start) * 34300.0 / 2.0
        if ULTRASONIC_MIN_CM <= distance_cm <= ULTRASONIC_MAX_CM:
            return float(distance_cm)
        return None

    def _filter_outliers(self, samples: list[float]) -> list[float]:
        if len(samples) < 2:
            return samples
        med = statistics.median(samples)
        threshold = max(5.0, med * ULTRASONIC_OUTLIER_RATIO)
        return [s for s in samples if abs(s - med) <= threshold]

    def _read_single_position_cm(self) -> float:
        samples: list[float] = []
        for _ in range(ULTRASONIC_SAMPLES_PER_READ):
            d = self._read_once_cm()
            if d is not None:
                samples.append(d)
            time.sleep(ULTRASONIC_INTER_MEASURE_DELAY_S)
        valid = self._filter_outliers(samples)
        if not valid:
            if self._history:
                return float(statistics.median(self._history))
            raise RuntimeError("No valid ultrasonic echo")
        return float(statistics.median(valid))

    def read_distance_cm(self) -> float:
        self._init_gpio_once()
        now = time.monotonic()
        elapsed = now - self._last_read_time
        if elapsed < ULTRASONIC_INTER_MEASURE_DELAY_S and self._history:
            return float(self._history[-1])

        if PROXIMITY_SERVO_DEVIATION_DEG > 0:
            self._init_servo_once()
            if self._servo_pwm is not None:
                center_deg = PROXIMITY_SERVO_CENTER_DEG
                left_deg = center_deg - PROXIMITY_SERVO_DEVIATION_DEG
                right_deg = center_deg + PROXIMITY_SERVO_DEVIATION_DEG
                readings: list[float] = []
                for angle in (center_deg, left_deg, right_deg):
                    self._set_servo_angle(angle)
                    self._servo_off()
                    try:
                        readings.append(self._read_single_position_cm())
                    except RuntimeError:
                        pass
                self._set_servo_angle(center_deg)
                self._servo_off()
                if readings:
                    result = min(readings)
                    self._last_read_time = time.monotonic()
                    self._history.append(result)
                    return result

        result = self._read_single_position_cm()
        self._last_read_time = time.monotonic()
        self._history.append(result)
        return result


class MockCameraDetector:
    def read_image_path(self, state_id: str) -> Optional[str]:
        _ = state_id
        return None

    def close(self) -> None:
        pass


class Picamera2StreamCapture:
    """Continuously captures frames via Picamera2 for low-latency MJPEG stream."""

    def __init__(
        self,
        frame_buffer: FrameBuffer,
        width: int = CAMERA_WIDTH,
        height: int = CAMERA_HEIGHT,
        capture_fps: float = CAMERA_FPS,
        stream_fps: float = STREAM_FPS,
        jpeg_quality: int = STREAM_JPEG_QUALITY,
    ) -> None:
        self._frame_buffer = frame_buffer
        self._width = width
        self._height = height
        self._capture_fps = capture_fps
        self._stream_fps = stream_fps
        self._jpeg_quality = max(50, min(95, jpeg_quality))
        self._interval = 1.0 / max(1.0, stream_fps)
        self._camera: Optional[Any] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._raw_lock = threading.Lock()
        self._last_raw: Optional[Any] = None
        self._encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self._jpeg_quality] if cv2 else []

    def _capture_loop(self) -> None:
        if cv2 is None or self._camera is None:
            return
        while not self._stop.is_set():
            try:
                frame_rgb = self._camera.capture_array()
                frame = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
            except Exception as exc:
                LOGGER.warning("Picamera2 stream frame capture failed: %s", exc)
                self._stop.wait(self._interval)
                continue
            with self._raw_lock:
                self._last_raw = frame.copy()
            _, jpeg = cv2.imencode(".jpg", frame, self._encode_params)
            if jpeg is not None:
                self._frame_buffer.put(jpeg.tobytes())
            self._stop.wait(self._interval)

    def start(self) -> bool:
        if Picamera2 is None or cv2 is None:
            return False
        try:
            self._camera = Picamera2()
            cfg = self._camera.create_video_configuration(
                main={"size": (self._width, self._height), "format": "RGB888"}
            )
            self._camera.configure(cfg)
            self._camera.start()
            frame_us = int(1_000_000 / max(1.0, self._capture_fps))
            try:
                self._camera.set_controls({"FrameDurationLimits": (frame_us, frame_us)})
            except Exception:
                pass
            time.sleep(CAMERA_WARMUP_S)
        except Exception:
            if self._camera is not None:
                try:
                    self._camera.stop()
                except Exception:
                    pass
                try:
                    self._camera.close()
                except Exception:
                    pass
            self._camera = None
            return False

        self._stop.clear()
        self._thread = threading.Thread(target=self._capture_loop, name="stream-capture", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._camera is not None:
            try:
                self._camera.stop()
            except Exception:
                pass
            try:
                self._camera.close()
            except Exception:
                pass
            self._camera = None

    def get_latest_raw(self) -> Optional[Any]:
        with self._raw_lock:
            return self._last_raw.copy() if self._last_raw is not None else None


class Picamera2CameraDetector:
    def __init__(
        self,
        capture_dir: Path,
        width: int = CAMERA_WIDTH,
        height: int = CAMERA_HEIGHT,
        fps: float = CAMERA_FPS,
        keep_last: int = CAPTURE_KEEP_LAST,
        frame_buffer: Optional[FrameBuffer] = None,
    ) -> None:
        self._capture_dir = capture_dir
        self._frame_buffer = frame_buffer
        self._width = width
        self._height = height
        self._fps = fps
        self._keep_last = max(1, int(keep_last))
        self._camera: Optional[Any] = None
        self._stream_capture: Optional[Picamera2StreamCapture] = None
        self._stream_capture_failed = False
        self._open_warning_logged = False

    def _capture_from_camera(self) -> Optional[Any]:
        if cv2 is None or self._camera is None:
            return None
        try:
            frame_rgb = self._camera.capture_array()
            return cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
        except Exception as exc:
            LOGGER.warning("Picamera2 frame read failed: %s", exc)
            return None

    def _ensure_open(self) -> bool:
        if Picamera2 is None:
            if not self._open_warning_logged:
                LOGGER.warning("Picamera2 unavailable, camera disabled")
                self._open_warning_logged = True
            return False
        if cv2 is None:
            if not self._open_warning_logged:
                LOGGER.warning("OpenCV unavailable, camera disabled")
                self._open_warning_logged = True
            return False

        if self._frame_buffer is not None and self._stream_capture is None and not self._stream_capture_failed:
            self._stream_capture = Picamera2StreamCapture(
                frame_buffer=self._frame_buffer,
                width=self._width,
                height=self._height,
                capture_fps=self._fps,
                stream_fps=STREAM_FPS,
                jpeg_quality=STREAM_JPEG_QUALITY,
            )
            if not self._stream_capture.start():
                LOGGER.warning("Picamera2 stream capture failed, falling back to on-demand capture")
                self._stream_capture = None
                self._stream_capture_failed = True
            else:
                LOGGER.info("Picamera2 stream capture started at %.0f fps", STREAM_FPS)

        if self._stream_capture is not None:
            return True

        if self._camera is not None:
            return True

        self.close()
        try:
            self._camera = Picamera2()
            cfg = self._camera.create_video_configuration(
                main={"size": (self._width, self._height), "format": "RGB888"}
            )
            self._camera.configure(cfg)
            self._camera.start()
            frame_us = int(1_000_000 / max(1.0, self._fps))
            try:
                self._camera.set_controls({"FrameDurationLimits": (frame_us, frame_us)})
            except Exception:
                pass
            time.sleep(CAMERA_WARMUP_S)
        except Exception as exc:
            if self._camera is not None:
                try:
                    self._camera.stop()
                except Exception:
                    pass
                try:
                    self._camera.close()
                except Exception:
                    pass
            self._camera = None
            if not self._open_warning_logged:
                LOGGER.warning("Picamera2 open failed: %s", exc)
                self._open_warning_logged = True
            return False

        self._open_warning_logged = False
        return True

    def read_image_path(self, state_id: str) -> Optional[str]:
        if not self._ensure_open():
            return None

        frame = None
        if self._stream_capture is not None:
            for _ in range(int(STREAM_FPS * 1.5)):
                frame = self._stream_capture.get_latest_raw()
                if frame is not None:
                    break
                time.sleep(1.0 / max(1.0, STREAM_FPS))
        else:
            frame = self._capture_from_camera()

        if frame is None:
            LOGGER.warning("No frame available from Picamera2")
            return None

        if self._frame_buffer is not None and self._stream_capture is None and cv2 is not None:
            _, jpeg = cv2.imencode(".jpg", frame)
            if jpeg is not None:
                self._frame_buffer.put(jpeg.tobytes())

        self._capture_dir.mkdir(parents=True, exist_ok=True)
        image_path = self._capture_dir / f"{state_id}.jpg"
        if cv2 is None or not cv2.imwrite(str(image_path), frame):
            LOGGER.warning("Frame save failed: %s", image_path)
            return None

        _prune_capture_images(self._capture_dir, keep_last=self._keep_last)
        return str(image_path.resolve())

    def start_stream_if_enabled(self) -> None:
        if self._frame_buffer is not None:
            self._ensure_open()

    def close(self) -> None:
        if self._stream_capture is not None:
            self._stream_capture.stop()
            self._stream_capture = None
        if self._camera is not None:
            try:
                self._camera.stop()
            except Exception:
                pass
            try:
                self._camera.close()
            except Exception:
                pass
            self._camera = None


class OpenCVCameraDetector:
    def __init__(
        self,
        capture_dir: Path,
        camera_index: int = CAMERA_INDEX,
        width: int = CAMERA_WIDTH,
        height: int = CAMERA_HEIGHT,
        fps: float = CAMERA_FPS,
        keep_last: int = CAPTURE_KEEP_LAST,
        frame_buffer: Optional[FrameBuffer] = None,
    ) -> None:
        self._capture_dir = capture_dir
        self._frame_buffer = frame_buffer
        self._camera_index = camera_index
        self._width = width
        self._height = height
        self._fps = fps
        self._keep_last = max(1, int(keep_last))
        self._cap = None
        self._stream_capture: Optional[StreamCapture] = None
        self._stream_capture_failed = False
        self._open_warning_logged = False

    def _ensure_open(self) -> bool:
        if cv2 is None:
            if not self._open_warning_logged:
                LOGGER.warning("OpenCV unavailable, camera disabled")
                self._open_warning_logged = True
            return False

        if self._frame_buffer is not None and self._stream_capture is None and not self._stream_capture_failed:
            self._stream_capture = StreamCapture(
                frame_buffer=self._frame_buffer,
                camera_index=self._camera_index,
                width=self._width,
                height=self._height,
                capture_fps=CAMERA_FPS,
                stream_fps=STREAM_FPS,
                jpeg_quality=STREAM_JPEG_QUALITY,
            )
            if not self._stream_capture.start():
                LOGGER.warning(
                    "Stream capture failed (camera index=%s), falling back to on-demand capture",
                    self._camera_index,
                )
                self._stream_capture = None
                self._stream_capture_failed = True
            else:
                LOGGER.info("Stream capture started at %.0f fps", STREAM_FPS)

        if self._stream_capture is not None:
            return True

        if self._cap is not None and self._cap.isOpened():
            return True

        self.close()
        cap = cv2.VideoCapture(self._camera_index)
        if not cap.isOpened():
            cap.release()
            if not self._open_warning_logged:
                LOGGER.warning("OpenCV camera open failed index=%s", self._camera_index)
                self._open_warning_logged = True
            return False

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self._width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self._height))
        cap.set(cv2.CAP_PROP_FPS, float(self._fps))
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc("M", "J", "P", "G"))
        time.sleep(CAMERA_WARMUP_S)
        self._cap = cap
        self._open_warning_logged = False
        return True

    def read_image_path(self, state_id: str) -> Optional[str]:
        if not self._ensure_open():
            return None

        if self._stream_capture is not None:
            for _ in range(int(STREAM_FPS * 1.5)):
                frame = self._stream_capture.get_latest_raw()
                if frame is not None:
                    break
                time.sleep(1.0 / max(1.0, STREAM_FPS))
        else:
            assert self._cap is not None
            ok, frame = self._cap.read()
            if not ok or frame is None:
                LOGGER.warning("OpenCV camera frame read failed")
                return None

        if frame is None:
            LOGGER.warning("No frame available from stream capture")
            return None

        if self._frame_buffer is not None and self._stream_capture is None:
            _, jpeg = cv2.imencode(".jpg", frame)
            if jpeg is not None:
                self._frame_buffer.put(jpeg.tobytes())

        self._capture_dir.mkdir(parents=True, exist_ok=True)
        image_path = self._capture_dir / f"{state_id}.jpg"
        if not cv2.imwrite(str(image_path), frame):
            LOGGER.warning("Frame save failed: %s", image_path)
            return None

        _prune_capture_images(self._capture_dir, keep_last=self._keep_last)
        return str(image_path.resolve())

    def start_stream_if_enabled(self) -> None:
        """Start continuous capture for stream (call early so browser sees video ASAP)."""
        if self._frame_buffer is not None:
            self._ensure_open()

    def close(self) -> None:
        if self._stream_capture is not None:
            self._stream_capture.stop()
            self._stream_capture = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _make_stream_handler(frame_buffer: FrameBuffer) -> type:
    class MJPEGStreamHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            LOGGER.debug("Stream %s", args[0] if args else "")

        def do_GET(self) -> None:
            if self.path == "/" or self.path == "":
                self._serve_index()
            elif self.path == "/stream":
                self._serve_mjpeg()
            else:
                self.send_error(404)

        def _serve_index(self) -> None:
            html = (
                b"<!DOCTYPE html><html><head><meta charset='utf-8'>"
                b"<title>Robot Camera</title>"
                b"<style>"
                b"html,body{margin:0;padding:0;width:100%;height:100%;overflow:hidden;background:#111}"
                b"img{position:fixed;top:0;left:0;width:100vw;height:100vh;object-fit:cover;display:block}"
                b"</style></head><body>"
                b"<img src='/stream' alt='Camera stream'>"
                b"</body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def _serve_mjpeg(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            while True:
                frame = frame_buffer.get()
                if frame:
                    try:
                        part = (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n"
                            b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                        )
                        self.wfile.write(part)
                        self.wfile.write(frame)
                        self.wfile.write(b"\r\n")
                    except (BrokenPipeError, ConnectionResetError):
                        break
                time.sleep(1.0 / max(1.0, STREAM_FPS))

    return MJPEGStreamHandler


class _ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def run_stream_server(
    port: int,
    frame_buffer: FrameBuffer,
    stop_event: threading.Event,
) -> None:
    handler = _make_stream_handler(frame_buffer)
    server = _ThreadedHTTPServer(("0.0.0.0", port), handler)

    def serve() -> None:
        def shutdown_on_stop() -> None:
            stop_event.wait()
            server.shutdown()

        t = threading.Thread(target=shutdown_on_stop, daemon=True)
        t.start()
        server.serve_forever()

    thread = threading.Thread(target=serve, name="camera-stream", daemon=True)
    thread.start()
    LOGGER.info(
        "Video stream: http://%s:%d  (or http://127.0.0.1:%d)",
        _get_local_ip(),
        port,
        port,
    )


def _resolve_camera_backend() -> str:
    backend = (CAMERA_BACKEND or "auto").strip().lower()
    if backend in {"opencv", "picamera2"}:
        return backend
    if backend != "auto":
        LOGGER.warning("Unknown CAMERA_BACKEND=%r; using auto", backend)
    if Picamera2 is not None:
        return "picamera2"
    return "opencv"


def build_sensors(
    config: VisionConfig,
    frame_buffer: Optional[FrameBuffer] = None,
) -> Tuple[ProximitySensor, CameraDetector]:
    camera_backend = _resolve_camera_backend()
    LOGGER.info("Camera backend selected: %s", camera_backend)
    if camera_backend == "picamera2" and Picamera2 is not None:
        camera = Picamera2CameraDetector(
            capture_dir=config.capture_dir,
            keep_last=config.capture_keep_last,
            frame_buffer=frame_buffer,
        )
    elif cv2 is not None:
        camera = OpenCVCameraDetector(
            capture_dir=config.capture_dir,
            keep_last=config.capture_keep_last,
            frame_buffer=frame_buffer,
        )
    else:
        LOGGER.error("No compatible camera backend available, using MockCameraDetector")
        camera = MockCameraDetector()
    return UltrasonicProximitySensor(), camera


def _clear_capture_images(capture_dir: Path) -> None:
    if not capture_dir.exists():
        capture_dir.mkdir(parents=True, exist_ok=True)
        return
    deleted = 0
    for path in capture_dir.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
            continue
        try:
            path.unlink()
            deleted += 1
        except OSError as exc:
            LOGGER.warning("Delete failed %s: %s", path, exc)
    if deleted:
        LOGGER.info("Cleaned captures: %s files removed", deleted)


def _wait_for_command_duration(
    command_path: Path,
    last_processed_command_id: str,
    stop_event: threading.Event,
) -> Optional[str]:
    while not stop_event.is_set():
        raw = read_json(command_path)
        if not isinstance(raw, dict):
            stop_event.wait(VISION_POLL_WAIT_S)
            continue
        command_id = str(raw.get("command_id", ""))
        if not command_id:
            stop_event.wait(VISION_POLL_WAIT_S)
            continue
        if command_id == last_processed_command_id:
            stop_event.wait(VISION_POLL_WAIT_S)
            continue
        action = str(raw.get("action", "LIGHT_OFF"))
        duration_ms = get_effective_duration_ms(action)
        duration_s = duration_ms / 1000.0 + VISION_EXTRA_DELAY_S
        LOGGER.info("Vision: cmd %s (%s), wait %.2fs", command_id, action, duration_s)
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline and not stop_event.is_set():
            stop_event.wait(min(VISION_POLL_WAIT_S, max(0, deadline - time.monotonic())))
        return command_id
    return None


def _prune_capture_images(capture_dir: Path, keep_last: int) -> None:
    keep_last = max(1, int(keep_last))
    files = [
        path
        for path in capture_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
    ]
    if len(files) <= keep_last:
        return
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for old_path in files[keep_last:]:
        try:
            old_path.unlink()
        except OSError as exc:
            LOGGER.warning("Delete failed %s: %s", old_path, exc)


def _build_state(state_counter: int, proximity: ProximitySensor, camera: CameraDetector) -> RobotState:
    state_id = f"st_{state_counter:06d}"

    proximity_state = ProximityState()
    camera_state = CameraState()

    try:
        image_path = camera.read_image_path(state_id)
        if image_path is not None:
            camera_state = CameraState(image_path=image_path)
    except Exception as exc:
        LOGGER.error("Camera read error: %s", exc)

    try:
        proximity_state = ProximityState(obstacle_cm=proximity.read_distance_cm())
    except Exception as exc:
        LOGGER.warning("Proximity sensor error: %s", exc)

    return RobotState(
        state_id=state_id,
        sensor=proximity_state,
        camera=camera_state,
    )


def print_stream_instructions(port: int = STREAM_DEFAULT_PORT) -> None:
    ip = _get_local_ip()
    print()
    print("  " + "=" * 56)
    print("  Camera stream — open in browser:")
    print("  http://{}:{}".format(ip, port))
    print("  (local: http://127.0.0.1:{})".format(port))
    print("  " + "=" * 56)
    print()


def run_vision_loop(config: VisionConfig, stop_event: Optional[threading.Event] = None) -> None:
    stop_event = stop_event or threading.Event()
    _clear_capture_images(config.capture_dir)

    frame_buffer: Optional[FrameBuffer] = None
    if config.stream_enabled and cv2 is not None:
        frame_buffer = FrameBuffer()
        run_stream_server(config.stream_port, frame_buffer, stop_event)
        print_stream_instructions(config.stream_port)

    proximity, camera = build_sensors(config, frame_buffer=frame_buffer)
    if hasattr(camera, "start_stream_if_enabled"):
        camera.start_stream_if_enabled()
    counter = 0
    LOGGER.info("Vision started state_path=%s", config.state_path)

    last_processed_command_id = ""
    try:
        while not stop_event.is_set():
            command_id = _wait_for_command_duration(
                config.command_path,
                last_processed_command_id,
                stop_event,
            )
            if command_id is None:
                break
            last_processed_command_id = command_id
            counter += 1
            state = _build_state(counter, proximity, camera)
            previous_state = read_json(config.state_path)
            if isinstance(previous_state, dict):
                state.command = str(previous_state.get("command", "")).strip()
            state_payload = state.to_dict()
            atomic_write_json(config.state_path, state_payload)
            LOGGER.info("STATE written:\n%s", json.dumps(state_payload, ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        camera.close()
        LOGGER.info("Vision stopped")


def parse_args() -> VisionConfig:
    return VisionConfig()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    config = parse_args()
    try:
        run_vision_loop(config)
    except KeyboardInterrupt:
        LOGGER.info("Vision stopped by user")


if __name__ == "__main__":
    main()
