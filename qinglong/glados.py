"""GLaDOS 自动签到脚本，适用于青龙面板及普通定时任务。"""

import os
import sys
import time
from typing import Any, Dict, List, Optional

import requests


CHECKIN_URL = "https://glados.rocks/api/user/checkin"
STATUS_URL = "https://glados.rocks/api/user/status"
DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 3


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """读取整数环境变量，并限制在合理范围内。"""
    raw_value = os.environ.get(name, "").strip()
    if not raw_value:
        return default
    try:
        return max(minimum, min(int(raw_value), maximum))
    except ValueError:
        print(f"环境变量 {name}={raw_value!r} 不是整数，将使用默认值 {default}")
        return default


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    description: str,
    *,
    retries: int,
    timeout: int,
    **kwargs: Any,
) -> Dict[str, Any]:
    """发起请求；遇到超时、网络异常、5xx 或非法 JSON 时指数退避重试。"""
    attempts = retries + 1
    last_error: Optional[Exception] = None

    for attempt in range(1, attempts + 1):
        try:
            response = session.request(method, url, timeout=(10, timeout), **kwargs)
            if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
                raise RuntimeError(
                    f"{description}被服务器拒绝（HTTP {response.status_code}），不再重试"
                )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError(f"接口返回的不是 JSON 对象: {data!r}")
            return data
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt == attempts:
                break
            wait_seconds = min(2 ** attempt, 30)
            print(
                f"{description}失败（第 {attempt}/{attempts} 次）: {exc}；"
                f"{wait_seconds} 秒后重试"
            )
            time.sleep(wait_seconds)

    raise RuntimeError(f"{description}在 {attempts} 次尝试后仍失败: {last_error}")


def split_cookies(raw_cookies: str) -> List[str]:
    """支持一个变量中用换行或 & 分隔多个青龙账号。"""
    normalized = raw_cookies.replace("\r", "\n").replace("&", "\n")
    return [cookie.strip() for cookie in normalized.split("\n") if cookie.strip()]


def is_already_checked_in(message: str) -> bool:
    normalized = message.lower()
    return any(
        marker in normalized
        for marker in ("repeat", "already", "tomorrow", "已签到", "重复签到")
    )


def send_wecom(webhook: str, content: str, retries: int, timeout: int) -> None:
    if not webhook:
        return
    attempts = min(retries + 1, 3)
    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(
                webhook,
                json={"msgtype": "text", "text": {"content": content}},
                timeout=(10, timeout),
            )
            response.raise_for_status()
            result = response.json()
            if result.get("errcode", 0) != 0:
                raise RuntimeError(result.get("errmsg", "企业微信接口返回错误"))
            return
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            if attempt < attempts:
                wait_seconds = 2 ** attempt
                print(f"企业微信通知发送失败: {exc}；{wait_seconds} 秒后重试")
                time.sleep(wait_seconds)
            else:
                # 通知失败不应覆盖签到本身的执行结果。
                print(f"企业微信通知在 {attempts} 次尝试后仍失败: {exc}")


def checkin_account(cookie: str, account_no: int, retries: int, timeout: int) -> str:
    headers = {
        "cookie": cookie,
        "referer": "https://glados.rocks/console/checkin",
        "origin": "https://glados.rocks",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
        ),
        "content-type": "application/json;charset=UTF-8",
    }

    with requests.Session() as session:
        checkin = request_json(
            session,
            "POST",
            CHECKIN_URL,
            f"账号 {account_no} 签到请求",
            headers=headers,
            json={"token": "glados.cloud"},
            retries=retries,
            timeout=timeout,
        )
        state = request_json(
            session,
            "GET",
            STATUS_URL,
            f"账号 {account_no} 状态请求",
            headers=headers,
            retries=retries,
            timeout=timeout,
        )

    if state.get("code") != 0:
        raise RuntimeError(f"Cookie 失效或无权限: {state.get('message', '未知错误')}")

    data = state.get("data")
    if not isinstance(data, dict) or "leftDays" not in data:
        raise RuntimeError(f"状态接口结构发生变化: {state}")

    email = data.get("email", f"账号 {account_no}")
    left_days = str(data["leftDays"]).split(".", 1)[0]
    message = str(checkin.get("message", "未知状态"))
    successful = checkin.get("code") == 0 or is_already_checked_in(message)
    if not successful:
        raise RuntimeError(f"签到接口返回失败: {message}")

    return f"{email} ---- {message} ---- 剩余 {left_days} 天"


def main() -> int:
    raw_cookies = os.environ.get("GLADOS_COOKIE", "").strip()
    webhook = os.environ.get("WECOM_WEBHOOK", "").strip()
    timeout = env_int("GLADOS_TIMEOUT", DEFAULT_TIMEOUT, 5, 120)
    retries = env_int("GLADOS_RETRIES", DEFAULT_RETRIES, 0, 8)

    cookies = split_cookies(raw_cookies)
    if not cookies:
        print("未获取到 GLADOS_COOKIE，请先在青龙面板中添加该环境变量")
        return 1

    results: List[str] = []
    failures: List[str] = []
    for account_no, cookie in enumerate(cookies, start=1):
        try:
            result = checkin_account(cookie, account_no, retries, timeout)
            print(result)
            results.append(result)
        except Exception as exc:  # 保证多账号中一个失败时仍继续执行其他账号。
            failure = f"账号 {account_no} ---- 签到失败: {exc}"
            print(failure)
            failures.append(failure)

    lines = results + failures
    send_wecom(webhook, "GLADOS 签到通知\n" + "\n".join(lines), retries, timeout)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
