#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Модуль feelings: переносит текущую команду в state.feelings."""

from __future__ import annotations

import argparse
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from shared import FeelingsState, RobotCommand, RobotState, atomic_write_json, now_ts, read_json

LOGGER = logging.getLogger("feelings")


@dataclass
class FeelingsConfig:
    state_path: Path = Path(__file__).with_name("state.json")
    command_path: Path = Path(__file__).with_name("command.json")
    poll_interval_s: float = 0.05


def _build_feelings(command: RobotCommand) -> FeelingsState:
    return FeelingsState(
        command_id=command.command_id,
        action=command.action,
        speed=command.params.speed,
        duration_ms=command.params.duration_ms,
        reason=command.reason,
        updated_at=now_ts(),
    )


def run_feelings_loop(config: FeelingsConfig, stop_event: Optional[threading.Event] = None) -> None:
    stop_event = stop_event or threading.Event()
    LOGGER.info("Feelings запущен. state=%s command=%s", config.state_path, config.command_path)
    last_command_id = ""

    while not stop_event.is_set():
        raw_command = read_json(config.command_path)
        raw_state = read_json(config.state_path)
        if not isinstance(raw_command, dict) or not isinstance(raw_state, dict):
            stop_event.wait(config.poll_interval_s)
            continue

        command = RobotCommand.from_dict(raw_command)
        if not command.command_id or command.command_id == last_command_id:
            stop_event.wait(config.poll_interval_s)
            continue

        state = RobotState.from_dict(raw_state)
        state.feelings = _build_feelings(command)
        atomic_write_json(config.state_path, state.to_dict())
        last_command_id = command.command_id
        LOGGER.debug("Feelings updated by command_id=%s", command.command_id)
        stop_event.wait(config.poll_interval_s)

    LOGGER.info("Feelings остановлен")


def parse_args() -> FeelingsConfig:
    parser = argparse.ArgumentParser(description="Feelings module")
    parser.add_argument("--state-path", default=str(Path(__file__).with_name("state.json")))
    parser.add_argument("--command-path", default=str(Path(__file__).with_name("command.json")))
    parser.add_argument("--poll", type=float, default=0.05)
    args = parser.parse_args()
    return FeelingsConfig(
        state_path=Path(args.state_path),
        command_path=Path(args.command_path),
        poll_interval_s=max(0.02, float(args.poll)),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    config = parse_args()
    try:
        run_feelings_loop(config)
    except KeyboardInterrupt:
        LOGGER.info("Feelings остановлен пользователем")


if __name__ == "__main__":
    main()
