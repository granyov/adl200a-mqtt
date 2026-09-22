#!/usr/bin/env python3
"""Пример MQTT-консьюмера для сервиса adl200a — печатает показания каналов.

  python3 subscribe.py [host] [port] [topic]
  по умолчанию: 127.0.0.1 1883 adl200a/#
"""
import json
import sys

import paho.mqtt.client as mqtt

host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 1883
topic = sys.argv[3] if len(sys.argv) > 3 else "adl200a/#"


def on_message(cl, userdata, msg):
    try:
        data = json.loads(msg.payload)
    except Exception:
        return
    if msg.topic.startswith("adl200a/ch/"):
        v = data.get("values", {})
        print("ch%02d  t01=%-10s t06=%-10s  %s" % (
            data.get("channel", 0), v.get("t01"), v.get("t06"),
            data.get("timestamp", "")))


try:  # paho-mqtt >= 2.0
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
except AttributeError:  # paho-mqtt < 2.0
    c = mqtt.Client()

c.on_message = on_message
c.connect(host, port, 60)
c.subscribe(topic)
print("subscribed to %s on %s:%d — Ctrl+C to stop" % (topic, host, port))
c.loop_forever()
