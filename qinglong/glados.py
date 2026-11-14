"""
cron: 30 9 * * *
new Env('GLaDOS 青龙签到');
"""

from __future__ import annotations

import os
import hashlib
import math
import re
import smtplib
import ssl
import sys
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from urllib.parse import urlsplit
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
CHALLENGE_URL = "https://glados.rocks/api/user/checkin/challenge"
CHALLENGE_DELAY = 2.7
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 3
DEFAULT_SERVICE_RETRIES = 3
DEFAULT_SERVICE_RETRY_DELAY = 30
SCRIPT_VERSION = "2026-11-14.7"


class RetryableRequestError(RuntimeError):
    """网络或响应错误；签到重试必须重新获取 challenge。"""


def browser_user_agent() -> str:
    value = os.environ.get("GLADOS_USER_AGENT", "").strip()
    if not value:
        return DEFAULT_USER_AGENT
    if value.lower().startswith("user-agent:"):
        value = value[len("user-agent:"):].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("GLADOS_USER_AGENT 无效，请复制浏览器的单行 User-Agent 值")
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        raise ValueError("GLADOS_USER_AGENT 包含不支持的字符，请重新复制") from None
    return value


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


def send_failure_email(subject: str, content: str, image_data: Optional[bytes] = None) -> bool:
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
    if image_data:
        message.add_attachment(image_data, maintype="image", subtype="png", filename="verification.png")

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
        print(f"邮件已发送至: {', '.join(recipients)}")
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
    last_error = "未知错误"

    for attempt in range(1, attempts + 1):
        try:
            response = session.request(
                method, url, timeout=(10, timeout), allow_redirects=False, **kwargs
            )
            if 300 <= response.status_code < 400:
                raise RuntimeError(f"{description}发生重定向，已停止以避免跨域提交凭据")
            if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
                raise RuntimeError(
                    f"{description}被服务器拒绝（HTTP {response.status_code}），不再重试"
                )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("接口返回的不是 JSON 对象")
            return data
        except (requests.RequestException, ValueError) as exc:
            # requests 的异常可能包含 Cookie/请求头，不输出原始异常内容。
            if isinstance(exc, requests.exceptions.InvalidHeader):
                raise RuntimeError("请求头格式无效，请检查 Cookie 和 User-Agent") from None
            last_error = type(exc).__name__
            if attempt == attempts:
                break
            wait_seconds = min(2 ** attempt, 30)
            print(
                f"{description}失败（第 {attempt}/{attempts} 次）: {last_error}；"
                f"{wait_seconds} 秒后重试"
            )
            time.sleep(wait_seconds)

    raise RetryableRequestError(
        f"{description}在 {attempts} 次尝试后仍失败: {last_error}"
    ) from None


def split_cookies(raw_cookies: str) -> List[str]:
    """支持一个变量中用换行或 & 分隔多个青龙账号。"""
    normalized = raw_cookies.replace("\r", "\n").replace("&", "\n")
    return [cookie.strip() for cookie in normalized.split("\n") if cookie.strip()]


def is_already_checked_in(message: str) -> bool:
    normalized = message.lower()
    return any(
        marker in normalized
        for marker in (
            "checkin repeats", "already checked in", "already checked-in",
            "today's observation logged", "已签到", "重复签到", "今日已记录",
        )
    )


def is_auth_failure(result: Dict[str, Any]) -> bool:
    message = str(result.get("message", "")).lower()
    return (
        result.get("code") == -2 or result.get("reason") == "device-mismatch"
        or any(marker in message for marker in (
            "没有权限", "permission", "unauthorized", "automated check-in detected",
        ))
    )


def is_successful_checkin(result: Dict[str, Any]) -> bool:
    if is_auth_failure(result):
        return False
    message = str(result.get("message", ""))
    return (
        result.get("code") == 0 or is_already_checked_in(message)
        or re.search(r"checkin!\s*got\s+\d+\s+points?\b", message, re.I) is not None
    )


def verified_checkin(
    session: requests.Session,
    headers: Dict[str, str],
    account_no: int,
    timeout: int,
) -> Dict[str, Any]:
    """遵循官网一次性 challenge 协议；失败时禁止退回 token-only 请求。"""
    challenge = request_json(
        session, "GET", CHALLENGE_URL, f"账号 {account_no} 获取签到验证",
        headers=headers, retries=0, timeout=timeout,
    )
    received_at = time.monotonic()
    nonce = challenge.get("nonce")
    ttl = challenge.get("ttl", 300000)  # 官网 TTL 单位为毫秒。
    if (
        challenge.get("code") != 0
        or not isinstance(nonce, str) or not 0 < len(nonce) <= 4096
        or type(ttl) not in (int, float)
        or (type(ttl) is float and not math.isfinite(ttl))
        or ttl <= CHALLENGE_DELAY * 1000
    ):
        raise RuntimeError("签到 challenge 不可用或格式无效；已停止，未提交旧版签到")
    # 当前官网会在需要时返回 Turnstile 配置；令牌由浏览器组件生成，
    # 不能用固定字符串、旧令牌或伪造 off 状态替代。
    if challenge.get("turnstile"):
        raise RuntimeError(
            "本次签到要求 Cloudflare Turnstile 浏览器验证，纯 requests 脚本无法完成；"
            "已停止提交，请在官网完成签到。更换 User-Agent 不能替代此验证"
        )
    print(f"账号 {account_no} challenge 未要求 Turnstile 浏览器验证", flush=True)
    proof = hashlib.sha256((nonce + ".glados-observe-v1").encode("utf-8")).hexdigest()
    print(f"账号 {account_no} 已获取签到验证，等待 {CHALLENGE_DELAY} 秒", flush=True)
    time.sleep(CHALLENGE_DELAY)
    if (time.monotonic() - received_at) * 1000 >= ttl:
        raise RetryableRequestError("签到 challenge 已过期，未提交签到")
    # POST 失败不可重放同一 nonce；外层网络重试重新获取验证。
    return request_json(
        session, "POST", CHECKIN_URL, f"账号 {account_no} 签到请求",
        headers=headers, json={
            "token": "glados.rocks", "nonce": nonce, "proof": proof, "tsState": "off",
        },
        retries=0, timeout=timeout,
    )


def is_temporary_service_error(message: str) -> bool:
    """识别接口已响应、但服务端明确要求稍后重试的临时错误。"""
    normalized = message.lower()
    return any(
        marker in normalized
        for marker in (
            "submarine cable data not ready",
            "try again in a moment",
            "try again later",
            "temporarily unavailable",
            "service unavailable",
            "server busy",
            "稍后再试",
            "稍后重试",
            "服务繁忙",
            "服务暂不可用",
        )
    )


def checkin_with_service_retry(
    session: requests.Session,
    headers: Dict[str, str],
    account_no: int,
    retries: int,
    timeout: int,
    service_retries: int,
    service_retry_delay: int,
) -> Dict[str, Any]:
    """在网络重试之外，对 GLaDOS 的临时业务错误进行较长间隔重试。"""
    network_failures = 0
    service_failures = 0
    while True:
        try:
            result = verified_checkin(session, headers, account_no, timeout)
        except RetryableRequestError:
            if network_failures >= retries:
                raise
            network_failures += 1
            wait_seconds = min(2 ** network_failures, 30)
            print(
                f"账号 {account_no} 签到验证/提交失败；{wait_seconds} 秒后重新获取验证 "
                f"（网络重试 {network_failures}/{retries}）", flush=True,
            )
            time.sleep(wait_seconds)
            continue
        message = str(result.get("message", "未知状态"))
        if is_successful_checkin(result) or is_auth_failure(result):
            return result
        if not is_temporary_service_error(message) or service_failures >= service_retries:
            return result

        wait_seconds = min(service_retry_delay * (2 ** service_failures), 300)
        service_failures += 1
        print(
            f"账号 {account_no} 签到服务暂不可用（重试 {service_failures}/{service_retries}）: "
            f"{message}；{wait_seconds} 秒后重新获取验证", flush=True,
        )
        time.sleep(wait_seconds)


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


def checkin_account(
    cookie: str,
    account_no: int,
    retries: int,
    timeout: int,
    service_retries: int,
    service_retry_delay: int,
) -> str:
    if os.environ.get("GLADOS_MODE", "http").strip().lower() == "browser":
        return browser_checkin_account(cookie, account_no)
    headers = {
        "cookie": cookie,
        "referer": "https://glados.rocks/console/checkin",
        "origin": "https://glados.rocks",
        "user-agent": browser_user_agent(),
        "content-type": "application/json;charset=UTF-8",
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }

    with requests.Session() as session:
        checkin = checkin_with_service_retry(
            session,
            headers,
            account_no,
            retries,
            timeout,
            service_retries,
            service_retry_delay,
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
        raise RuntimeError("状态接口结构发生变化，缺少 leftDays")

    email = data.get("email", f"账号 {account_no}")
    left_days = str(data["leftDays"]).split(".", 1)[0]
    message = str(checkin.get("message", "未知状态"))
    successful = is_successful_checkin(checkin)
    if not successful:
        if "automated check-in detected" in message.lower():
            raise RuntimeError(
                "服务端拒绝自动签到（Automated check-in detected）；"
                "challenge 已提交，但服务器仍未接受当前请求。"
                "若重新登录且使用实际 User-Agent 后仍失败，请使用官网签到；"
                "脚本不会通过重复请求绕过验证"
            )
        raise RuntimeError(f"签到接口返回失败: {message}")

    return f"{email} ---- {message} ---- 剩余 {left_days} 天"


def notify_verification(base_url: str, token: str, job_id: str, account_no: int, remaining: int) -> None:
    public_url = os.environ.get("GLADOS_BROWSER_PUBLIC_URL", "").strip().rstrip("/")
    public = urlsplit(public_url)
    if public_url and (public.scheme != "https" or not public.hostname or public.username or public.password
                       or public.query or public.fragment or public.path):
        public_url = ""
    desktop_path = "/desktop/vnc.html?autoconnect=true&resize=scale&path=desktop/websockify"
    links = f"局域网管理页：{base_url}/\n局域网验证界面：{base_url}{desktop_path}\n"
    if public_url:
        links += f"外网管理页：{public_url}/\n外网验证界面：{public_url}{desktop_path}\n"
    else:
        links += "外网入口尚未配置，请在青龙填写 GLADOS_BROWSER_PUBLIC_URL（绿联转发到 8093 的 HTTPS 地址）。\n"
    content = (
        f"GLaDOS 账号 {account_no} 需要人工验证\n自动尝试尚未完成，当前浏览器继续保留，"
        f"剩余约 {remaining} 秒。完成人机验证后脚本会自动继续签到。\n"
        + links + "只需在当前远程浏览器完成验证，不要启动新的登录或签到任务。\n"
        "管理页用户名 glados、密码为原调用密钥；远程桌面使用 VNC 密码。链接本身不含凭据。"
    )
    print(content, flush=True)
    image_data = None
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(base_url + "/verification.png", headers={
                "Authorization": "Bearer " + token, "X-Glados-Job": job_id,
            }, timeout=(3, 5), allow_redirects=False)
            if response.status_code == 200 and len(response.content) <= 1000000 and response.content.startswith(b"\x89PNG\r\n\x1a\n"):
                image_data = response.content
    except requests.RequestException:
        pass
    send_wecom(os.environ.get("WECOM_WEBHOOK", "").strip(), content, 0, 10)
    send_failure_email("GLaDOS 等待人工验证", content, image_data)


def browser_post_with_progress(base_url: str, token: str, payload: Dict[str, Any], account_no: int, selftest: bool):
    def post():
        with requests.Session() as session:
            session.trust_env = False
            return session.post(
                base_url + ("/selftest" if selftest else "/checkin"),
                headers={"Authorization": "Bearer " + token}, json=payload,
                timeout=(10, payload["timeout"] + 20), allow_redirects=False,
            )
    # Exactly one POST. Monitoring is read-only and never replays a timed-out job.
    with ThreadPoolExecutor(max_workers=1) as executor, requests.Session() as monitor:
        monitor.trust_env = False
        future = executor.submit(post)
        notified = False
        while True:
            try:
                return future.result(timeout=2)
            except FutureTimeout:
                if selftest or notified:
                    continue
                try:
                    response = monitor.get(base_url + "/task/status", headers={"Authorization": "Bearer " + token},
                                           timeout=(3, 5), allow_redirects=False)
                    status = response.json() if response.status_code == 200 else {}
                    if (isinstance(status, dict) and status.get("job_id") == payload["job_id"]
                            and status.get("manual_verification") is True):
                        notified = True
                        notify_verification(base_url, token, payload["job_id"], account_no, int(status.get("remaining_seconds", 0)))
                except (requests.RequestException, ValueError, TypeError):
                    pass


def browser_service_request(cookie: str, account_no: int, selftest: bool = False) -> Dict[str, Any]:
    session_mode = os.environ.get("GLADOS_BROWSER_SESSION", "cookie").strip().lower()
    if session_mode not in ("cookie", "profile"):
        raise RuntimeError("GLADOS_BROWSER_SESSION 仅支持 cookie 或 profile")
    profile_mode = session_mode == "profile" and not selftest
    base_url = os.environ.get("GLADOS_BROWSER_URL", "").strip().rstrip("/")
    token = os.environ.get("GLADOS_BROWSER_TOKEN", "").strip()
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError("请配置 GLADOS_BROWSER_URL，例如 http://NAS局域网IP:8093")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise RuntimeError("GLADOS_BROWSER_URL 只能包含协议、主机和端口")
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise RuntimeError("GLADOS_BROWSER_TOKEN 至少需要 32 个无空格 ASCII 字符，并与浏览器容器一致")
    timeout = env_int("GLADOS_BROWSER_TIMEOUT", 420 if profile_mode else 180, 60, 600)
    manual_wait = 0 if selftest else env_int("GLADOS_MANUAL_WAIT", 300, 0, 540)
    total_timeout = timeout + manual_wait + (30 if manual_wait else 0)
    print(
        f"浏览器环境自检（最长 {timeout} 秒，不访问 GLaDOS）" if selftest else
        f"账号 {account_no} 使用 NAS 浏览器签到（总上限 {total_timeout} 秒，含人工等待 {manual_wait} 秒）", flush=True,
    )
    try:
        # 此请求会触发真实签到，超时后状态未知，禁止自动重放或回退到 HTTP 模式。
        response = browser_post_with_progress(base_url, token, {
                    "cookie": "" if profile_mode else cookie, "timeout": total_timeout,
                    "manual_wait": manual_wait, "job_id": uuid.uuid4().hex,
                    "session_mode": "profile" if profile_mode else "cookie",
                    "profile_id": account_no if profile_mode else 1,
                    "user_agent": browser_user_agent() if not profile_mode and os.environ.get("GLADOS_USER_AGENT", "").strip() else None,
                }, account_no, selftest)
        if response.status_code == 401:
            raise RuntimeError("浏览器服务密钥不匹配，请核对两边 GLADOS_BROWSER_TOKEN")
        if response.status_code == 409:
            raise RuntimeError("浏览器服务正在执行另一项签到，请等待原任务结束")
        if response.status_code != 200:
            raise RuntimeError(f"浏览器服务返回 HTTP {response.status_code}，未重试")
        result = response.json()
    except requests.Timeout:
        raise RuntimeError("浏览器服务调用超时，签到状态未知；请先核对官网流水，不会自动重试") from None
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"浏览器服务连接或响应异常（{type(exc).__name__}），未自动重试") from None
    if not isinstance(result, dict):
        raise RuntimeError("浏览器服务响应格式异常")
    if result.get("ok") is not True:
        # 只接受服务端固定错误分类，不回显可能含凭据的页面或异常内容。
        reasons = {
            "login_required": "浏览器会话未登录，请更新 Cookie；必要时需在浏览器环境重新登录",
            "verification_required": "浏览器验证未能自动完成，请在官网签到；不保证 Turnstile 自动通过",
            "page_changed": "未找到官网签到按钮，页面结构可能已变更",
            "timeout": "浏览器任务超时，签到状态未知，请先核对官网流水",
            "browser_error": "浏览器执行失败，请查看容器是否正常及 Chrome 是否安装完整",
            "invalid_cookie": "Cookie 格式无效，请复制完整 Cookie 值",
            "profile_not_ready": "NAS 浏览器会话未保存，请先在 8093 登录页完成对应账号登录并保存",
            "profile_origin_mismatch": "保存会话的域名与 GLADOS_ORIGIN 不一致，请保持原域名或在当前域名重新登录保存",
            "button_not_found": "未找到官网签到按钮，请核对登录状态或页面结构",
            "button_disabled": "官网签到按钮处于禁用状态，请核对页面上的实际提示",
            "checkin_unknown": "已点击签到，但未收到明确结果；请先核对官网流水，不会自动重放",
        }
        message = reasons.get(result.get("error"), "浏览器服务执行失败")
        details = []
        for key in ("stage", "diagnostic", "category"):
            value = result.get(key)
            if isinstance(value, str) and value.isascii() and value.replace("_", "").isalnum() and len(value) <= 64:
                details.append(key + "=" + value)
        if type(result.get("exit_code")) is int:
            details.append("exit_code=" + str(result["exit_code"]))
        filename, line = result.get("diagnostic_file"), result.get("diagnostic_line")
        if isinstance(filename, str) and filename.isascii() and filename.replace("_", "").replace(".", "").isalnum() and len(filename) <= 64 and type(line) is int:
            details.append("location=" + filename + ":" + str(line))
        if details:
            message += " [" + ", ".join(details) + "]"
        raise RuntimeError(message)
    return result


def browser_checkin_account(cookie: str, account_no: int) -> str:
    result = browser_service_request(cookie, account_no)
    checkin, state = result.get("checkin"), result.get("state")
    if not isinstance(checkin, dict) or not isinstance(state, dict):
        raise RuntimeError("浏览器服务缺少签到或状态结果")
    if not is_successful_checkin(checkin):
        if checkin.get("reason") == "device-mismatch":
            devices = ("Windows", "Linux", "Mac OS", "macOS", "Android", "iOS")
            logged = checkin.get("loginDevice")
            current = checkin.get("currentDevice")
            raise RuntimeError(
                f"签到设备不匹配：登录设备={logged if logged in devices else '未知'}，"
                f"当前浏览器={current if current in devices else '未知'}。"
                "请在 NAS 的 8093 登录页用同一 Linux 浏览器重新登录并保存，使用 profile 模式"
            )
        if is_auth_failure(checkin):
            raise RuntimeError("官网拒绝浏览器会话认证/设备验证，请在官网核对登录状态")
        raise RuntimeError(f"浏览器签到失败: {checkin.get('message', '未知错误')}")
    if state.get("code") != 0 or not isinstance(state.get("data"), dict) or "leftDays" not in state["data"]:
        raise RuntimeError("浏览器签到已有响应，但账号状态查询失败，请先核对官网流水")
    data = state["data"]
    return (
        f"{data.get('email', f'账号 {account_no}')} ---- "
        f"{checkin.get('message', '签到成功')} ---- 剩余 {str(data['leftDays']).split('.', 1)[0]} 天"
    )


def main() -> int:
    if requests is None:
        raise RuntimeError(f"requests 依赖导入失败: {REQUESTS_IMPORT_ERROR}")

    print(f"GLaDOS 青龙签到版本 {SCRIPT_VERSION}", flush=True)
    mode = os.environ.get("GLADOS_MODE", "http").strip().lower()
    if mode not in ("http", "browser"):
        raise RuntimeError("GLADOS_MODE 仅支持 http 或 browser")
    print(f"签到模式: {mode}", flush=True)
    profile_mode = mode == "browser" and os.environ.get("GLADOS_BROWSER_SESSION", "cookie").strip().lower() == "profile"
    print(
        "User-Agent: " + (
            "profile 模式使用 NAS 浏览器原生身份，忽略自定义值" if profile_mode else
            "已读取 GLADOS_USER_AGENT" if os.environ.get("GLADOS_USER_AGENT", "").strip()
            else ("未配置，使用浏览器原生值" if mode == "browser" else "未配置，使用默认值")
        ), flush=True,
    )

    if mode == "browser" and os.environ.get("GLADOS_BROWSER_SELFTEST", "").strip() == "1":
        try:
            result = browser_service_request("", 0, selftest=True)
            if result.get("selftest") is not True:
                raise RuntimeError("服务响应不是自检结果，请更新浏览器容器")
            print("浏览器自检通过：Chromium、驱动、JavaScript 和 Linux 显示环境可用；未执行签到")
            return 0
        except RuntimeError as exc:
            print(f"浏览器自检失败: {exc}")
            return 1

    raw_cookies = os.environ.get("GLADOS_COOKIE", "").strip()
    webhook = os.environ.get("WECOM_WEBHOOK", "").strip()
    timeout = env_int("GLADOS_TIMEOUT", DEFAULT_TIMEOUT, 5, 120)
    retries = env_int("GLADOS_RETRIES", DEFAULT_RETRIES, 0, 8)
    service_retries = env_int(
        "GLADOS_SERVICE_RETRIES", DEFAULT_SERVICE_RETRIES, 0, 8
    )
    service_retry_delay = env_int(
        "GLADOS_SERVICE_RETRY_DELAY", DEFAULT_SERVICE_RETRY_DELAY, 5, 600
    )

    cookies = split_cookies(raw_cookies)
    if not cookies and not profile_mode:
        failure = "未获取到 GLADOS_COOKIE，请先在青龙面板中添加该环境变量"
        print(failure)
        send_failure_email("GLaDOS 签到失败", failure)
        return 1

    if profile_mode:
        raw_profiles = os.environ.get("GLADOS_BROWSER_PROFILES", "1").strip()
        parts = re.split(r"[,;\s]+", raw_profiles)
        if not parts or any(not part.isascii() or not part.isdigit() or not 1 <= int(part) <= 20 for part in parts):
            raise RuntimeError("GLADOS_BROWSER_PROFILES 请填写 1～20 的账号编号，多个用逗号分隔")
        accounts = [(profile_id, "") for profile_id in dict.fromkeys(int(part) for part in parts)]
    else:
        accounts = list(enumerate(cookies, start=1))

    results: List[str] = []
    failures: List[str] = []
    for account_no, cookie in accounts:
        try:
            result = checkin_account(
                cookie,
                account_no,
                retries,
                timeout,
                service_retries,
                service_retry_delay,
            )
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
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    try:
        exit_code = main()
    except Exception:
        error_detail = traceback.format_exc()
        print(f"脚本运行异常:\n{error_detail}")
        send_failure_email("GLaDOS 签到脚本运行异常", error_detail)
        exit_code = 1
    sys.exit(exit_code)
