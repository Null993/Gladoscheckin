import requests,json,os
import math
import sys


# -------------------------------------------------------------------------------------------
# GLADOS 自动签到 稳定增强版
# -------------------------------------------------------------------------------------------

if __name__ == '__main__':

    # PushPlus Token
    PUSH_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")

    # 推荐从环境变量读取
    GLADOS_COOKIE = os.environ.get("GLADOS_COOKIE", "")

    # 如果本地测试，可以取消下面注释填写
    GLADOS_COOKIE = ""

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

    sendContent = ""

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

        if PUSH_TOKEN:
            requests.get(
                f"http://www.pushplus.plus/send?token={PUSH_TOKEN}&title=GLADOS签到失败&content=Cookie失效"
            )
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

    sendContent += result_text

    # -------------------- 推送 --------------------
    if PUSH_TOKEN:
        requests.get(
            f"http://www.pushplus.plus/send?token={PUSH_TOKEN}&title=GLADOS签到通知&content={sendContent}"
        )