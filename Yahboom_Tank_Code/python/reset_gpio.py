#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
"""
Сброс всех GPIO пинов Yahboom Tank в безопасное состояние.
Запускайте после прерывания программы (Ctrl+C), чтобы моторы, LED и пины не оставались в одном состоянии.

На Raspberry Pi 5 пины 22, 24, 27 (RGB LED) через rpi-lgpio часто дают "GPIO not allocated".
Поэтому сброс делается через libgpiod (gpioset). Запускайте с sudo: sudo python3 reset_gpio.py
"""

import os
import subprocess
import sys

# Все пины BCM, которые используются как выходы в проекте Yahboom Tank
OUTPUT_PINS = [
    1,   # TrigPin (ультразвук)
    2,   # OutfirePin
    8,   # Buzzer / Key
    9,   # ServoUpDownPin
    11,  # ServoLeftRightPin
    13,  # ENB (мотор)
    16,  # ENA (мотор)
    19,  # IN3 (мотор)
    20,  # IN1 (мотор)
    21,  # IN2 (мотор)
    22,  # LED_R
    23,  # ServoPin / FrontServoPin
    24,  # LED_B
    26,  # IN4 (мотор)
    27,  # LED_G
]


def detect_gpio_chip():
    """Определить чип с 40-pin заголовком: на Pi 5 это gpiochip4 (rp1)."""
    try:
        out = subprocess.run(
            ["gpiodetect"], capture_output=True, text=True, timeout=5
        )
        if out.returncode != 0:
            return None
        # Ищем rp1 (Pi 5) или pinctrl-bcm2835 (Pi 4 и старше)
        for line in out.stdout.splitlines():
            if "gpiochip" in line.lower() and ("rp1" in line or "pinctrl" in line or "bcm2" in line):
                parts = line.strip().split()
                for p in parts:
                    if p.startswith("gpiochip"):
                        return p
        # По умолчанию Pi 5
        return "gpiochip4"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "gpiochip4"


def reset_via_gpioset(hold_seconds=2):
    """
    Сброс пинов через gpioset (libgpiod).
    Сначала пробуем все пины разом; при "Device or resource busy" — по одному,
    чтобы сбросить хотя бы те, что не заняты.
    """
    chip = detect_gpio_chip()
    hold_seconds = max(1, hold_seconds)

    def run_gpioset(pins):
        if not pins:
            return True
        args = ["gpioset", "-m", "time", "-s", str(hold_seconds), chip]
        for pin in pins:
            args.append("{}={}".format(pin, 0))
        r = subprocess.run(args, timeout=hold_seconds + 2, capture_output=True, text=True)
        return r.returncode == 0, (r.stderr or "").strip()

    try:
        ok, err = run_gpioset(OUTPUT_PINS)
        if ok:
            return True, []
        # Часть пинов занята (например 22, 24, 27 после rpi-lgpio). Сбрасываем по одному.
        if "busy" not in err.lower() and "resource" not in err.lower():
            print("gpioset: {}".format(err or "ошибка"))
            return False, list(OUTPUT_PINS)
        reset_ok = []
        busy_pins = []
        for pin in OUTPUT_PINS:
            ok, _ = run_gpioset([pin])
            if ok:
                reset_ok.append(pin)
            else:
                busy_pins.append(pin)
        return len(busy_pins) == 0, busy_pins
    except FileNotFoundError:
        print("Утилита gpioset не найдена. Установите: sudo apt install gpiod")
        return False, list(OUTPUT_PINS)
    except subprocess.TimeoutExpired:
        return True, []


def reset_via_rpi_gpio():
    """Сброс через RPi.GPIO (для пинов, которые доступны без sudo)."""
    try:
        import RPi.GPIO as GPIO
    except ImportError:
        return [], list(OUTPUT_PINS)
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    ok, failed = [], []
    for pin in OUTPUT_PINS:
        try:
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, GPIO.LOW)
            ok.append(pin)
        except Exception:
            failed.append(pin)
    try:
        GPIO.cleanup()
    except Exception:
        pass
    return ok, failed


def main():
    # Без root пины 22, 24, 27 на Pi 5 часто недоступны (rpi-lgpio "GPIO not allocated").
    # Перезапускаем скрипт с sudo, чтобы gpioset мог забрать и сбросить пины.
    if os.geteuid() != 0:
        try:
            os.execvp("sudo", ["sudo", sys.executable] + sys.argv)
        except OSError:
            pass
        print("Запуск без sudo. Рекомендуется: sudo python3 reset_gpio.py")
        print()

    # Сброс через gpioset (при "Device or resource busy" сбрасываем пины по одному)
    success, busy_pins = reset_via_gpioset(hold_seconds=2)
    if success:
        print("GPIO сброшены через gpioset: все выходы переведены в LOW на 2 с и освобождены.")
        return
    # Часть пинов сброшена, часть занята — сообщаем
    if busy_pins and len(busy_pins) < len(OUTPUT_PINS):
        print("Сброшены все пины, кроме занятых: {}.".format(busy_pins))
        print("Эти пины держит другой процесс (часто — предыдущий скрипт с rpi-lgpio).")
        print("Закройте все программы, использующие GPIO, или перезагрузите Pi: sudo reboot")
        return

    # Если gpioset не сработал — пробуем RPi.GPIO
    ok, failed = reset_via_rpi_gpio()
    if failed:
        print("Не удалось сбросить пины: {}.".format(failed))
        print("Установите gpiod: sudo apt install gpiod")
        print("Затем запустите: sudo python3 reset_gpio.py")
    else:
        print("GPIO сброшены (RPi.GPIO): все выходы переведены в LOW и освобождены.")


if __name__ == "__main__":
    main()
