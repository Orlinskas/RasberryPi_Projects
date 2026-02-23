#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
"""
Тест: сначала мигание LED (несколько раз красным, затем зелёным),
после паузы — поворот вправо, затем влево.
"""

import RPi.GPIO as GPIO
import time

# Моторы
IN1, IN2, IN3, IN4 = 20, 21, 19, 26
ENA, ENB = 16, 13

# RGB LED
LED_R = 22
LED_G = 27
LED_B = 24

# Параметры (можно подстроить)
ROTATE_TIME = 3     # секунд на каждый поворот
PAUSE_AFTER = 0.8     # пауза после LED перед поворотами
BLINK_COUNT = 3       # сколько раз мигнуть красным и зелёным
BLINK_ON = 0.15
BLINK_OFF = 0.15
SPEED = 20            # заполнение ШИМ 0–100

pwm_ENA = None
pwm_ENB = None


def setup():
    global pwm_ENA, pwm_ENB
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    # Моторы
    GPIO.setup(ENA, GPIO.OUT, initial=GPIO.HIGH)
    GPIO.setup(ENB, GPIO.OUT, initial=GPIO.HIGH)
    GPIO.setup(IN1, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(IN2, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(IN3, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(IN4, GPIO.OUT, initial=GPIO.LOW)
    pwm_ENA = GPIO.PWM(ENA, 2000)
    pwm_ENB = GPIO.PWM(ENB, 2000)
    pwm_ENA.start(0)
    pwm_ENB.start(0)
    # LED
    GPIO.setup(LED_R, GPIO.OUT)
    GPIO.setup(LED_G, GPIO.OUT)
    GPIO.setup(LED_B, GPIO.OUT)


def stop_motor():
    GPIO.output(IN1, GPIO.LOW)
    GPIO.output(IN2, GPIO.LOW)
    GPIO.output(IN3, GPIO.LOW)
    GPIO.output(IN4, GPIO.LOW)
    if pwm_ENA:
        pwm_ENA.ChangeDutyCycle(0)
    if pwm_ENB:
        pwm_ENB.ChangeDutyCycle(0)


def run_forward(duration):
    GPIO.output(IN1, GPIO.HIGH)
    GPIO.output(IN2, GPIO.LOW)
    GPIO.output(IN3, GPIO.HIGH)
    GPIO.output(IN4, GPIO.LOW)
    pwm_ENA.ChangeDutyCycle(SPEED)
    pwm_ENB.ChangeDutyCycle(SPEED)
    time.sleep(duration)
    stop_motor()


def run_backward(duration):
    GPIO.output(IN1, GPIO.LOW)
    GPIO.output(IN2, GPIO.HIGH)
    GPIO.output(IN3, GPIO.LOW)
    GPIO.output(IN4, GPIO.HIGH)
    pwm_ENA.ChangeDutyCycle(SPEED)
    pwm_ENB.ChangeDutyCycle(SPEED)
    time.sleep(duration)
    stop_motor()


def rotate_right(duration):
    """Поворот вправо: левое колесо вперёд, правое назад."""
    GPIO.output(IN1, GPIO.HIGH)
    GPIO.output(IN2, GPIO.LOW)
    GPIO.output(IN3, GPIO.LOW)
    GPIO.output(IN4, GPIO.HIGH)
    pwm_ENA.ChangeDutyCycle(SPEED)
    pwm_ENB.ChangeDutyCycle(SPEED)
    time.sleep(duration)
    stop_motor()


def rotate_left(duration):
    """Поворот влево: левое колесо назад, правое вперёд."""
    GPIO.output(IN1, GPIO.LOW)
    GPIO.output(IN2, GPIO.HIGH)
    GPIO.output(IN3, GPIO.HIGH)
    GPIO.output(IN4, GPIO.LOW)
    pwm_ENA.ChangeDutyCycle(SPEED)
    pwm_ENB.ChangeDutyCycle(SPEED)
    time.sleep(duration)
    stop_motor()


def led_off():
    GPIO.output(LED_R, GPIO.LOW)
    GPIO.output(LED_G, GPIO.LOW)
    GPIO.output(LED_B, GPIO.LOW)


def blink_red(times):
    for _ in range(times):
        GPIO.output(LED_R, GPIO.HIGH)
        GPIO.output(LED_G, GPIO.LOW)
        GPIO.output(LED_B, GPIO.LOW)
        time.sleep(BLINK_ON)
        led_off()
        time.sleep(BLINK_OFF)


def blink_green(times):
    for _ in range(times):
        GPIO.output(LED_R, GPIO.LOW)
        GPIO.output(LED_G, GPIO.HIGH)
        GPIO.output(LED_B, GPIO.LOW)
        time.sleep(BLINK_ON)
        led_off()
        time.sleep(BLINK_OFF)


def cleanup():
    stop_motor()
    led_off()
    if pwm_ENA:
        pwm_ENA.stop()
    if pwm_ENB:
        pwm_ENB.stop()
    GPIO.cleanup()


def main():
    setup()
    try:
        blink_red(BLINK_COUNT)
        blink_green(BLINK_COUNT)
        time.sleep(PAUSE_AFTER)
        rotate_right(ROTATE_TIME)
        time.sleep(0.3)
        rotate_left(ROTATE_TIME)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()


if __name__ == "__main__":
    main()
