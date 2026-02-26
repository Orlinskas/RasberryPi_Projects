#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Простой тест USB-камеры: сделать снимок и вывести путь к файлу."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Сделать один снимок с USB-камеры")
    parser.add_argument("--camera-index", type=int, default=0, help="Индекс камеры для VideoCapture")
    parser.add_argument("--width", type=int, default=640, help="Ширина кадра")
    parser.add_argument("--height", type=int, default=480, help="Высота кадра")
    parser.add_argument("--fps", type=float, default=30.0, help="Желаемый FPS")
    parser.add_argument("--warmup", type=float, default=0.15, help="Пауза прогрева камеры в секундах")
    parser.add_argument(
        "--output-dir",
        default=str(Path(__file__).with_name("captures")),
        help="Каталог для сохранения снимка",
    )
    parser.add_argument(
        "--filename",
        default="",
        help="Имя файла (например shot.jpg). Если пусто, будет camera_test_<timestamp>.jpg",
    )
    return parser.parse_args()


def make_capture_path(output_dir: Path, filename: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    if filename:
        return output_dir / filename
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return output_dir / f"camera_test_{stamp}.jpg"


def main() -> int:
    args = parse_args()
    output_path = make_capture_path(Path(args.output_dir), args.filename).resolve()
    try:
        import cv2
    except ImportError:
        print("ERROR: OpenCV (cv2) не установлен. Установите python3-opencv.", file=sys.stderr)
        return 10

    cap = cv2.VideoCapture(args.camera_index)
    if not cap.isOpened():
        print(f"ERROR: не удалось открыть камеру index={args.camera_index}", file=sys.stderr)
        return 1

    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(args.width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(args.height))
        cap.set(cv2.CAP_PROP_FPS, float(args.fps))
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc("M", "J", "P", "G"))

        if args.warmup > 0:
            time.sleep(args.warmup)

        ok, frame = cap.read()
        if not ok or frame is None:
            print("ERROR: не удалось получить кадр с камеры", file=sys.stderr)
            return 2

        if not cv2.imwrite(str(output_path), frame):
            print(f"ERROR: не удалось сохранить снимок: {output_path}", file=sys.stderr)
            return 3
    finally:
        cap.release()

    # Важная часть для теста: печатаем путь к снимку.
    print(str(output_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
