#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Модуль vision: читает датчики и публикует `protocol/state.json`."""

from __future__ import annotations

import argparse
import logging
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Optional, Protocol, Tuple

from shared import GPIO_LOCK, CameraState, FeelingsState, ProximityState, RobotState, atomic_write_json, now_ts, read_json

LOGGER = logging.getLogger("vision")
STATE_PATH = Path(__file__).with_name("protocol") / "state.json"
CAPTURE_DIR = Path(__file__).with_name("captures")

INTERVAL_S = 5

ECHO_PIN = 0
TRIG_PIN = 1
ULTRASONIC_TIMEOUT_S = 0.03
ULTRASONIC_MIN_CM = 2.0
ULTRASONIC_MAX_CM = 500.0

CAMERA_INDEX = 0
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480
CAMERA_FPS = 30.0
CAMERA_WARMUP_S = 1.0
CAPTURE_KEEP_LAST = 30

try:
    import RPi.GPIO as GPIO
except ImportError:  # pragma: no cover
    GPIO = None

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None


class ProximitySensor(Protocol):
    """Интерфейс датчика расстояния."""

    def read_distance_cm(self) -> float:
        ...


class CameraDetector(Protocol):
    """Интерфейс обработки кадра с камеры."""

    def read_observation(self, state_id: str) -> Optional[Tuple[bool, Optional[float], float]]:
        ...

    def get_last_image_path(self) -> Optional[str]:
        ...

    def close(self) -> None:
        ...


class UltrasonicProximitySensor:
    """Простой датчик HC-SR04: чтение + сглаживание по 3 последним замерам."""

    def __init__(self) -> None:
        self._initialized = False
        self._history: Deque[float] = deque(maxlen=3)

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

    def _read_once_cm(self) -> Optional[float]:
        deadline = time.monotonic() + ULTRASONIC_TIMEOUT_S
        with GPIO_LOCK:
            # Переустанавливаем направления при каждом чтении:
            # controller может делать GPIO.cleanup() при завершении.
            GPIO.setup(ECHO_PIN, GPIO.IN)
            GPIO.setup(TRIG_PIN, GPIO.OUT)

            GPIO.output(TRIG_PIN, GPIO.HIGH)
            time.sleep(0.000015)
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

    def read_distance_cm(self) -> float:
        self._init_gpio_once()
        distance = self._read_once_cm()
        if distance is None:
            if not self._history:
                raise RuntimeError("No valid ultrasonic echo")
            return float(statistics.mean(self._history))
        self._history.append(distance)
        return float(statistics.mean(self._history))


class MockCameraDetector:
    """Заглушка камеры: не подмешивает данные в решения brain."""

    def read_observation(self, state_id: str) -> Optional[Tuple[bool, Optional[float], float]]:
        _ = state_id
        return None

    def get_last_image_path(self) -> Optional[str]:
        return None

    def close(self) -> None:
        return None


class OpenCVCameraDetector:
    """One-shot захват изображения с USB-камеры."""

    def __init__(
        self,
        capture_dir: Path,
        camera_index: int = CAMERA_INDEX,
        width: int = CAMERA_WIDTH,
        height: int = CAMERA_HEIGHT,
        fps: float = CAMERA_FPS,
        keep_last: int = CAPTURE_KEEP_LAST,
    ) -> None:
        self._capture_dir = capture_dir
        self._camera_index = camera_index
        self._width = width
        self._height = height
        self._fps = fps
        self._keep_last = max(1, int(keep_last))
        self._cap = None
        self._last_image_path: Optional[str] = None
        self._open_warning_logged = False

    def _ensure_open(self) -> bool:
        if cv2 is None:
            if not self._open_warning_logged:
                LOGGER.warning("OpenCV (cv2) недоступен, камера отключена")
                self._open_warning_logged = True
            return False

        if self._cap is not None and self._cap.isOpened():
            return True

        self.close()
        cap = cv2.VideoCapture(self._camera_index)
        if not cap.isOpened():
            cap.release()
            if not self._open_warning_logged:
                LOGGER.warning("Не удалось открыть USB-камеру index=%s", self._camera_index)
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

    def read_observation(self, state_id: str) -> Optional[Tuple[bool, Optional[float], float]]:
        self._last_image_path = None
        if not self._ensure_open():
            return None

        assert self._cap is not None  # for type checkers
        ok, frame = self._cap.read()
        if not ok or frame is None:
            LOGGER.warning("Не удалось получить кадр из USB-камеры")
            return None

        self._capture_dir.mkdir(parents=True, exist_ok=True)
        image_path = self._capture_dir / f"{state_id}.jpg"
        if not cv2.imwrite(str(image_path), frame):
            LOGGER.warning("Не удалось сохранить кадр: %s", image_path)
            return None

        _prune_capture_images(self._capture_dir, keep_last=self._keep_last)
        self._last_image_path = str(image_path.resolve())
        return False, None, 1.0

    def get_last_image_path(self) -> Optional[str]:
        return self._last_image_path

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


@dataclass
class VisionConfig:
    """Конфигурация цикла vision (только частота генерации state)."""

    interval_s: float = INTERVAL_S
    capture_dir: Path = CAPTURE_DIR
    capture_keep_last: int = CAPTURE_KEEP_LAST


def build_sensors(config: VisionConfig) -> Tuple[ProximitySensor, CameraDetector]:
    if cv2 is None:
        LOGGER.error("cv2 не найден, используется MockCameraDetector")
        camera: CameraDetector = MockCameraDetector()
    else:
        camera = OpenCVCameraDetector(capture_dir=config.capture_dir, keep_last=config.capture_keep_last)
    return UltrasonicProximitySensor(), camera


def _clear_capture_images(capture_dir: Path) -> None:
    """Очищает каталог снимков перед запуском vision."""
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
        except OSError as exc:  # pragma: no cover
            LOGGER.warning("Не удалось удалить старый снимок %s: %s", path, exc)
    if deleted:
        LOGGER.info("Очищен каталог снимков: удалено %s файлов", deleted)


def _prune_capture_images(capture_dir: Path, keep_last: int) -> None:
    """Хранит только последние keep_last снимков в каталоге."""
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
        except OSError as exc:  # pragma: no cover
            LOGGER.warning("Не удалось удалить старый снимок %s: %s", old_path, exc)


def _build_state(state_counter: int, proximity: ProximitySensor, camera: CameraDetector) -> RobotState:
    """Формирует единый state из всех входов vision."""
    state_id = f"st_{state_counter:06d}"
    ts = now_ts()

    proximity_state = ProximityState(valid=False)
    camera_state = CameraState(valid=False)

    try:
        proximity_state = ProximityState(distance_cm=proximity.read_distance_cm(), valid=True)
    except Exception as exc:  # pragma: no cover
        LOGGER.warning("Ошибка чтения датчика приближения: %s", exc)

    try:
        observation = camera.read_observation(state_id)
        if observation is not None:
            obstacle, target_x, confidence = observation
            camera_state = CameraState(
                obstacle=obstacle,
                target_x=target_x,
                confidence=confidence,
                image_path=camera.get_last_image_path(),
                valid=True,
            )
    except Exception as exc:  # pragma: no cover
        LOGGER.error("Ошибка чтения камеры: %s", exc)

    return RobotState(
        state_id=state_id,
        timestamp=ts,
        sensor=proximity_state,
        camera=camera_state,
    )


def run_vision_loop(config: VisionConfig, stop_event: Optional[threading.Event] = None) -> None:
    """Основной цикл vision: publish state.json по таймеру."""
    stop_event = stop_event or threading.Event()
    _clear_capture_images(config.capture_dir)
    proximity, camera = build_sensors(config)
    counter = 0
    LOGGER.info("Vision запущен. state_path=%s interval=%.2fs", STATE_PATH, config.interval_s)

    try:
        while not stop_event.is_set():
            counter += 1
            state = _build_state(counter, proximity, camera)
            current_state = read_json(STATE_PATH)
            if isinstance(current_state, dict):
                last_command_payload = current_state.get("last_command", current_state.get("feelings", {}))
                if isinstance(last_command_payload, dict):
                    state.last_command = FeelingsState.from_dict(last_command_payload)
            atomic_write_json(STATE_PATH, state.to_dict())
            LOGGER.debug("Опубликован state_id=%s", state.state_id)
            stop_event.wait(config.interval_s)
    finally:
        camera.close()
        LOGGER.info("Vision остановлен")


def parse_args() -> VisionConfig:
    parser = argparse.ArgumentParser(description="Vision module")
    _ = parser.parse_args()
    return VisionConfig()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    config = parse_args()
    try:
        run_vision_loop(config)
    except KeyboardInterrupt:
        LOGGER.info("Vision остановлен пользователем")


if __name__ == "__main__":
    main()
