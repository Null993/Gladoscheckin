"""
cron: 30 9 * * *
new Env('GLaDOS 青龙签到');
"""

from __future__ import annotations

import os
import smtplib
import ssl
import sys
import time
import traceback
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError as exc:
    requests = None  # type: ignore[assignment]
    REQUESTS_IMPORT_ERROR: Optional[Exception] = exc
else:
    REQUESTS_IMPORT_ERROR = None


CHECKIN_URL = "https://glados.rocks/api/user/checkin"
STATUS_URL = "https://glados.rocks/api/user/status"
DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 3


def env_int(variable: str, default: int, minimum: int, maximum: int) -> int:
    """读取整数环境变量，并限制在合理范围内。"""
    raw_value = os.environ.get(variable, "").strip()
    if not raw_value:
        return default
    try:
        return max(minimum, min(int(raw_value), maximum))
    except ValueError:
        print(f"环境变量 {variable}={raw_value!r} 不是整数，将使用默认值 {default}")
        return default


def split_recipients(raw_recipients: str) -> List[str]:
    """将逗号、分号或换行分隔的收件地址转换为列表。"""
    normalized = raw_recipients.replace(";", ",").replace("\r", "\n")
    normalized = normalized.replace("\n", ",")
    return [address.strip() for address in normalized.split(",") if address.strip()]


def send_failure_email(subject: str, content: str) -> bool:
    """通过 SMTP 发送失败告警；未配置邮件时仅记录日志。"""
    host = os.environ.get("EMAIL_SMTP_HOST", "").strip()
    user = os.environ.get("EMAIL_SMTP_USER", "").strip()
    password = os.environ.get("EMAIL_SMTP_PASSWORD", "").strip()
    sender = os.environ.get("EMAIL_FROM", "").strip() or user
    recipients = split_recipients(os.environ.get("EMAIL_TO", ""))
    security = os.environ.get("EMAIL_SMTP_SECURITY", "ssl").strip().lower()
    default_port = 465 if security == "ssl" else 587
    port = env_int("EMAIL_SMTP_PORT", default_port, 1, 65535)

    configured_values = (host, user, password, sender, recipients)
    if not any(configured_values):
        print("未配置邮件告警，跳过失败邮件")
        return False

    missing = []
    if not host:
        missing.append("EMAIL_SMTP_HOST")
    if not user:
        missing.append("EMAIL_SMTP_USER")
    if not password:
        missing.append("EMAIL_SMTP_PASSWORD")
    if not sender:
        missing.append("EMAIL_FROM")
    if not recipients:
        missing.append("EMAIL_TO")
    if missing:
        print(f"邮件告警配置不完整，缺少: {', '.join(missing)}")
        return False
    if security not in ("ssl", "starttls", "none"):
        print("EMAIL_SMTP_SECURITY 仅支持 ssl、starttls 或 none")
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(content)

    try:
        tls_context = ssl.create_default_context()
        if security == "ssl":
            smtp_client = smtplib.SMTP_SSL(
                host, port, timeout=30, context=tls_context
            )
        else:
            smtp_client = smtplib.SMTP(host, port, timeout=30)

        with smtp_client as smtp:
            if security == "starttls":
                smtp.ehlo()
                smtp.starttls(context=tls_context)
                smtp.ehlo()
            smtp.login(user, password)
            smtp.send_message(message)
        print(f"失败告警邮件已发送至: {', '.join(recipients)}")
        return True
    except (OSError, smtplib.SMTPException) as exc:
        print(f"失败告警邮件发送失败: {exc}")
        return False


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
    if requests is None:
        raise RuntimeError(f"requests 依赖导入失败: {REQUESTS_IMPORT_ERROR}")

    raw_cookies = os.environ.get("GLADOS_COOKIE", "").strip()
    webhook = os.environ.get("WECOM_WEBHOOK", "").strip()
    timeout = env_int("GLADOS_TIMEOUT", DEFAULT_TIMEOUT, 5, 120)
    retries = env_int("GLADOS_RETRIES", DEFAULT_RETRIES, 0, 8)

    cookies = split_cookies(raw_cookies)
    if not cookies:
        failure = "未获取到 GLADOS_COOKIE，请先在青龙面板中添加该环境变量"
        print(failure)
        send_failure_email("GLaDOS 签到失败", failure)
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
    if failures:
        send_failure_email(
            "GLaDOS 签到失败",
            "以下账号签到失败：\n\n" + "\n".join(failures),
        )
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception:
        error_detail = traceback.format_exc()
        print(f"脚本运行异常:\n{error_detail}")
        send_failure_email("GLaDOS 签到脚本运行异常", error_detail)
        exit_code = 1
    sys.exit(exit_code)
