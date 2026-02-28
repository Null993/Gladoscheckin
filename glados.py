import requests
import os
import sys

# -------------------------------------------------------------------------------------------
# GLADOS 自动签到 - 企业微信机器人版
# -------------------------------------------------------------------------------------------

if __name__ == '__main__':

    # 企业微信机器人 webhook
    WECOM_WEBHOOK = os.environ.get("WECOM_WEBHOOK", "")

    # GLADOS Cookie
    GLADOS_COOKIE = os.environ.get("GLADOS_COOKIE", "")

    # 本地测试可手动填写
    # GLADOS_COOKIE = ""
    # WECOM_WEBHOOK = ""

    if not GLADOS_COOKIE:
        print("未获取到 GLADOS_COOKIE")
        sys.exit(0)

    checkin_url = "https://glados.rocks/api/user/checkin"
    status_url = "https://glados.rocks/api/user/status"

    headers = {
        "cookie": GLADOS_COOKIE,
        "referer": "https://glados.rocks/console/checkin",
        "origin": "https://glados.rocks",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "content-type": "application/json;charset=UTF-8"
    }

    payload = {
        "token": "glados.cloud"
    }

    try:
        # -------------------- 签到 --------------------
        checkin = requests.post(checkin_url, headers=headers, json=payload, timeout=15)
        checkin_json = checkin.json()
    except Exception as e:
        print("签到请求失败:", e)
        sys.exit(1)

    try:
        # -------------------- 获取状态 --------------------
        state = requests.get(status_url, headers=headers, timeout=15)
        state_json = state.json()
    except Exception as e:
        print("状态请求失败:", e)
        sys.exit(1)

    # -------------------- 权限检测 --------------------
    if state_json.get("code") != 0:
        message = state_json.get("message", "未知错误")
        print("Cookie失效或无权限:", message)

        if WECOM_WEBHOOK:
            msg = {
                "msgtype": "text",
                "text": {
                    "content": f"GLADOS签到失败\n原因: {message}"
                }
            }
            requests.post(WECOM_WEBHOOK, json=msg)

        sys.exit(0)

    # -------------------- 正常数据 --------------------
    try:
        left_days = state_json["data"]["leftDays"].split('.')[0]
        email = state_json["data"]["email"]
    except KeyError:
        print("接口结构发生变化:", state_json)
        sys.exit(1)

    # -------------------- 签到结果 --------------------
    if checkin_json.get("code") == 0:
        message = checkin_json.get("message", "签到成功")
    else:
        message = checkin_json.get("message", "未知状态")

    result_text = f"{email} ---- {message} ---- 剩余({left_days})天"

    print(result_text)

    # -------------------- 企业微信推送 --------------------
    if WECOM_WEBHOOK:
        msg = {
            "msgtype": "text",
            "text": {
                "content": f"GLADOS签到通知\n{result_text}"
            }
        }
        requests.post(WECOM_WEBHOOK, json=msg)