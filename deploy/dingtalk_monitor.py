#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UPS anomaly monitor with DingTalk notifications (PVE host).

Polls NUT (`upsc ups`) every 5 seconds and sends a DingTalk text message
when the UPS state changes: on battery, low battery, FSD, over-temperature,
overload, or loss of NUT data.  While a fault persists a reminder is sent
periodically; recovery is reported as well.

The DingTalk webhook URL and optional sign secret are read from
/etc/ups-dingtalk/dingtalk.env (mode 600, never logged).
"""

import base64
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

CONF_PATH = "/etc/ups-dingtalk/dingtalk.env"
STATE_PATH = "/var/lib/ups-dingtalk/state.json"
UPS_NAME = "ups"

POLL_SECONDS = 5
STARTUP_GRACE = 10
FAIL_THRESHOLD = 3          # consecutive upsc failures before "no data"
SEND_RETRY_SECONDS = 60

TEMP_HIGH = 45.0
TEMP_CLEAR = 43.0
LOAD_HIGH = 90.0
LOAD_CLEAR = 85.0
CHARGE_LOW = 20.0

REPEAT_CRITICAL = 10 * 60
REPEAT_WARNING = 30 * 60
REPEAT_FSD = 5 * 60


def load_conf(path):
    conf = {}
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            conf[key.strip()] = value
    return conf


def send_dingtalk(url, secret, content):
    if secret:
        timestamp = str(int(time.time() * 1000))
        string_to_sign = timestamp + "\n" + secret
        digest = hmac.new(
            secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("ascii"))
        separator = "&" if "?" in url else "?"
        url = url + separator + "timestamp=" + timestamp + "&sign=" + sign

    payload = json.dumps(
        {
            "msgtype": "text",
            "text": {"content": content},
            "at": {"isAtAll": False},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8", "replace")
    except Exception as exc:
        return False, "request error: %s" % exc

    try:
        result = json.loads(body)
    except ValueError:
        return False, "invalid response: %s" % body[:200]

    if result.get("errcode") == 0:
        return True, "ok"
    return False, "errcode=%s errmsg=%s" % (result.get("errcode"), result.get("errmsg"))


def read_ups():
    try:
        proc = subprocess.run(
            ["upsc", UPS_NAME],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)

    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout).strip()[:200]

    data = {}
    for line in proc.stdout.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            data[key.strip()] = value.strip()
    if not data:
        return None, "empty upsc output"
    return data, None


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate(data):
    tokens = set((data.get("ups.status") or "").split())
    online = "OB" not in tokens
    charge = safe_float(data.get("battery.charge"))
    temperature = safe_float(data.get("ups.temperature"))
    load = safe_float(data.get("ups.load"))
    low_battery = ("LB" in tokens) or (
        not online and charge is not None and charge <= CHARGE_LOW
    )
    fsd = "FSD" in tokens
    return online, low_battery, fsd, charge, temperature, load


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_state(state):
    directory = os.path.dirname(STATE_PATH)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_PATH)


def build_message(signature, data, data_ok, host):
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    if not data_ok:
        return "\n".join(
            [
                "【UPS告警】无法读取 UPS 数据",
                "主机: %s" % host,
                "时间: %s" % now,
                "原因: 连续 %d 次执行 upsc %s 失败" % (FAIL_THRESHOLD, UPS_NAME),
                "请检查 nut-server、nut-driver@ups 和 ups-nut-bridge 的运行状态。",
            ]
        )

    parts = set() if signature == "OK" else set(signature.split(","))
    if "FSD" in parts:
        title = "【UPS告警】UPS 已请求关机（FSD）"
    elif "LB" in parts:
        title = "【UPS告警】UPS 电池电量低"
    elif "OB" in parts:
        title = "【UPS告警】市电中断，UPS 转电池供电"
    elif "NOCOMM" in parts:
        title = "【UPS告警】UPS 通信中断（USB 失联）"
    elif "TEMP" in parts:
        title = "【UPS告警】UPS 温度过高"
    elif "LOAD" in parts:
        title = "【UPS告警】UPS 负载过高"
    else:
        title = "【UPS告警·恢复】UPS 供电已恢复正常"

    lines = [
        title,
        "主机: %s" % host,
        "时间: %s" % now,
        "状态: %s" % (data.get("ups.status") or "-"),
        "电量: %s%%  负载: %s%%  温度: %s℃"
        % (
            data.get("battery.charge") or "-",
            data.get("ups.load") or "-",
            data.get("ups.temperature") or "-",
        ),
        "输入: %s V / %s Hz   输出: %s V"
        % (
            data.get("input.voltage") or "-",
            data.get("input.frequency") or "-",
            data.get("output.voltage") or "-",
        ),
    ]

    notes = []
    if "OB" in parts:
        notes.append("市电中断，当前由电池供电。")
    if "LB" in parts:
        notes.append("电池电量低，请尽快保存数据并准备关机。")
    if "FSD" in parts:
        notes.append("主机已进入关机流程。")
    if "NOCOMM" in parts:
        notes.append(
            "UPS 数据已停止更新，当前显示最后一次实测值，电量和续航已冻结。"
        )
    if "TEMP" in parts:
        notes.append("温度超过 %.0f℃。" % TEMP_HIGH)
    if "LOAD" in parts:
        notes.append("负载超过 %.0f%%。" % LOAD_HIGH)
    if not parts:
        notes.append("市电已恢复，UPS 回到在线供电。")
    if notes:
        lines.append("说明: " + " ".join(notes))

    return "\n".join(lines)


def main():
    conf = load_conf(CONF_PATH)
    url = conf.get("DINGTALK_WEBHOOK_URL", "").strip()
    secret = conf.get("DINGTALK_SECRET", "").strip()
    if not url.startswith("https://"):
        raise SystemExit("DINGTALK_WEBHOOK_URL missing or invalid in " + CONF_PATH)

    host = socket.gethostname()
    state = load_state()
    prev_signature = state.get("signature")
    last_sent = float(state.get("last_sent") or 0)
    temp_high = bool(state.get("temp_high", False))
    load_high = bool(state.get("load_high", False))
    data_ok = bool(state.get("data_ok", True))
    failures = 0
    next_attempt = 0.0

    print(
        "monitor started: ups=%s host=%s previous=%s"
        % (UPS_NAME, host, prev_signature),
        flush=True,
    )
    time.sleep(STARTUP_GRACE)

    while True:
        now = time.time()
        data, error = read_ups()
        message_data = data or {}

        if data is None:
            failures += 1
            if failures == FAIL_THRESHOLD:
                print("upsc failed: %s" % error, flush=True)
            if failures < FAIL_THRESHOLD:
                time.sleep(POLL_SECONDS)
                continue
            data_ok = False
            current_signature = "NOCOMM"
            repeat = REPEAT_CRITICAL
        else:
            failures = 0
            data_ok = True
            online, low_battery, fsd, _charge, temperature, load = evaluate(data)

            if temperature is not None:
                if temperature >= TEMP_HIGH:
                    temp_high = True
                elif temperature <= TEMP_CLEAR:
                    temp_high = False
            if load is not None:
                if load >= LOAD_HIGH:
                    load_high = True
                elif load <= LOAD_CLEAR:
                    load_high = False

            parts = []
            if not online:
                parts.append("OB")
            tokens = set((data.get("ups.status") or "").split())
            if "ALARM" in tokens:
                parts.append("NOCOMM")
            if low_battery:
                parts.append("LB")
            if fsd:
                parts.append("FSD")
            if temp_high:
                parts.append("TEMP")
            if load_high:
                parts.append("LOAD")
            current_signature = ",".join(parts) if parts else "OK"
            repeat = (
                REPEAT_FSD
                if fsd
                else (
                    REPEAT_CRITICAL
                    if (low_battery or "NOCOMM" in parts)
                    else REPEAT_WARNING
                )
            )

        abnormal = current_signature != "OK"
        send_now = False
        if prev_signature is None:
            if abnormal:
                send_now = True
            else:
                prev_signature = "OK"
        elif current_signature != prev_signature:
            send_now = True
        elif abnormal and (now - last_sent) >= repeat:
            send_now = True

        if send_now and now >= next_attempt:
            content = build_message(current_signature, message_data, data_ok, host)
            ok, result = send_dingtalk(url, secret, content)
            if ok:
                prev_signature = current_signature
                last_sent = now
                print("sent: %s" % current_signature, flush=True)
            else:
                next_attempt = now + SEND_RETRY_SECONDS
                print("send failed: %s" % result, flush=True)

        snapshot = {
            "signature": prev_signature,
            "last_sent": last_sent,
            "temp_high": temp_high,
            "load_high": load_high,
            "data_ok": data_ok,
        }
        if snapshot != state:
            save_state(snapshot)
            state = snapshot

        time.sleep(POLL_SECONDS)


def run_test():
    conf = load_conf(CONF_PATH)
    url = conf.get("DINGTALK_WEBHOOK_URL", "").strip()
    secret = conf.get("DINGTALK_SECRET", "").strip()
    if not url.startswith("https://"):
        print("FAILED: DINGTALK_WEBHOOK_URL missing or invalid in " + CONF_PATH)
        return 1

    content = "\n".join(
        [
            "【UPS告警】钉钉监控测试",
            "主机: %s" % socket.gethostname(),
            "时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
            "说明: 收到此消息表示 UPS 异常推送配置可用。",
        ]
    )
    ok, result = send_dingtalk(url, secret, content)
    print(("OK: " if ok else "FAILED: ") + result)
    return 0 if ok else 1


if __name__ == "__main__":
    if "--test" in sys.argv[1:]:
        sys.exit(run_test())
    main()
