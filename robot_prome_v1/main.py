#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Точка входа: orchestrator, который запускает все модули робота."""

from __future__ import annotations

import argparse
import logging
import threading
import time
from pathlib import Path

from brain import BrainConfig, run_brain_loop
from controller import run_controller_loop
from feelings import FeelingsConfig, run_feelings_loop
from shared import atomic_write_json, read_json, zero_command_payload, zero_state_payload
from vision import VisionConfig, run_vision_loop

LOGGER = logging.getLogger("main")


def monitor_health(
    state_path,
    command_path,
    stop_event: threading.Event,
    check_interval_s: float = 0.5,
) -> None:
    """Пассивный монитор: проверяет наличие и корректность state/command."""
    LOGGER.info("Health monitor запущен")
    while not stop_event.is_set():
        state = read_json(state_path)
        command = read_json(command_path)

        if not isinstance(state, dict):
            LOGGER.warning("state.json отсутствует или поврежден")
        if not isinstance(command, dict):
            LOGGER.warning("command.json отсутствует или поврежден")

        stop_event.wait(check_interval_s)
    LOGGER.info("Health monitor остановлен")


def parse_args():
    parser = argparse.ArgumentParser(description="Robot main orchestrator")
    parser.add_argument("--vision-interval", type=float, default=VisionConfig.interval_s)
    parser.add_argument("--controller-poll", type=float, default=0.05)
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    args = parse_args()

    state_path = Path(__file__).with_name("state.json")
    command_path = Path(__file__).with_name("command.json")

    stop_event = threading.Event()
    vision_config = VisionConfig(interval_s=max(0.1, args.vision_interval))
    brain_config = BrainConfig(state_path=state_path, command_path=command_path)
    feelings_config = FeelingsConfig(state_path=state_path, command_path=command_path)

    threads = [
        threading.Thread(target=run_vision_loop, args=(vision_config, stop_event), name="vision", daemon=True),
        threading.Thread(target=run_brain_loop, args=(brain_config, stop_event), name="brain", daemon=True),
        threading.Thread(
            target=run_controller_loop,
            args=(command_path, max(0.02, args.controller_poll), stop_event),
            name="controller",
            daemon=True,
        ),
        threading.Thread(
            target=run_feelings_loop,
            args=(feelings_config, stop_event),
            name="feelings",
            daemon=True,
        ),
        threading.Thread(
            target=monitor_health,
            args=(state_path, command_path, stop_event),
            name="health-monitor",
            daemon=True,
        ),
    ]

    for thread in threads:
        thread.start()
        LOGGER.info("Запущен поток: %s", thread.name)

    try:
        while True:
            dead = [thread.name for thread in threads if not thread.is_alive()]
            if dead:
                # Любой критический сбой приводит к аварийному завершению оркестратора.
                LOGGER.error("Критические модули остановились: %s. Аварийная остановка.", ", ".join(dead))
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        LOGGER.info("Остановка пользователем")
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=3.0)
        # При завершении системы сбрасываем runtime-файлы в нулевое состояние.
        atomic_write_json(state_path, zero_state_payload())
        atomic_write_json(command_path, zero_command_payload())
        LOGGER.info("state.json и command.json сброшены в нулевое состояние")
        LOGGER.info("Main orchestrator остановлен")


if __name__ == "__main__":
    main()
