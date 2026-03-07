#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Author:   Vlad Orlinskas
# Site:     https://prometeriy.com
# Project:  robot_prome_v1 — LLM-driven autonomous robot experiment
# License:  Free for any use
# ---------------------------------------------------------------------------

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Optional, Union

from settings import read_json

LOGGER = logging.getLogger("voice")

VOICE_LANG = "ru"
VOICE_MAX_LENGTH = 300
VOICE_TIMEOUT_S = 30.0
_ESPEAK_WARNED = False


def _sanitize_phrase(text: str) -> str:
    if not text or not isinstance(text, str):
        return ""
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", text.strip())
    return s[:VOICE_MAX_LENGTH].strip()


def play_phrase(text: str) -> None:
    global _ESPEAK_WARNED
    phrase = _sanitize_phrase(text)
    if not phrase:
        return
    lang = (os.environ.get("VOICE_LANG") or VOICE_LANG or "").strip()
    cmd = ["espeak", "-a", "200", phrase]
    if lang:
        cmd = ["espeak", "-v", lang, "-a", "200", phrase]
    try:
        subprocess.run(
            cmd,
            timeout=VOICE_TIMEOUT_S,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        if not _ESPEAK_WARNED:
            LOGGER.warning("espeak not found; install with: apt install espeak or espeak-ng")
            _ESPEAK_WARNED = True
    except subprocess.TimeoutExpired:
        LOGGER.warning("espeak timed out for phrase (truncated)")
    except OSError as exc:
        LOGGER.warning("espeak failed: %s", exc)


def run_voice_loop(
    command_path: Union[Path, str],
    poll_interval_s: float = 0.05,
    stop_event: Optional[threading.Event] = None,
) -> None:
    stop_event = stop_event or threading.Event()
    command_path = Path(command_path)
    last_command_id = ""

    LOGGER.info("Voice started command_path=%s", command_path)
    while not stop_event.is_set():
        raw = read_json(command_path)
        if not isinstance(raw, dict):
            stop_event.wait(poll_interval_s)
            continue
        command_id = str(raw.get("command_id", ""))
        if command_id != last_command_id:
            last_command_id = command_id
            voice_raw = raw.get("voice")
            voice = (str(voice_raw).strip() if voice_raw is not None else "") or None
            if voice:
                LOGGER.info("Voice playing: %s", voice[:80] + ("..." if len(voice) > 80 else ""))
                play_phrase(voice)
        stop_event.wait(poll_interval_s)
    LOGGER.info("Voice stopped")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Voice module: play phrases via espeak")
    parser.add_argument(
        "--test",
        nargs="?",
        const="Привет, я робот, тест звука",
        default=None,
        metavar="PHRASE",
        help="Test run: play a phrase and exit. With no argument uses default phrase. Running 'voice.py' with no options is test mode.",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run voice loop (watch command.json) instead of test mode.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if args.loop:
        from settings import COMMAND_PATH
        run_voice_loop(COMMAND_PATH, stop_event=threading.Event())
        return
    phrase = args.test if args.test is not None else "Привет, я робот, тест звука"
    LOGGER.info("Test mode: playing phrase")
    play_phrase(phrase)


if __name__ == "__main__":
    main()
