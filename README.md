# GLaDOS 自动签到（GitHub Actions / 青龙面板）

**绿联 DXP4800 / N100 / Docker 青龙遇到 Turnstile 验证：**新增独立浏览器模式，配置及部署步骤见 [browser/README.md](browser/README.md)。通过青龙环境变量 `GLADOS_MODE=browser` 启用，需要先部署浏览器容器；未配置时仍使用原 HTTP 模式。

**Windows 登录、NAS 签到返回 `device-mismatch`：**版本 `2026-11-11.6` 支持在 NAS 的 8093 登录页打开远程 Linux 浏览器并保存会话，青龙用 `GLADOS_BROWSER_SESSION=profile` 复用，不再跨系统复制 Cookie。部署时保留 `.env`，新增 8 位 `GLADOS_VNC_PASSWORD`，详细步骤见上述说明。

本仓库同时保留 GitHub Actions 原版和 NAS 青龙增强版，两套版本彼此隔离：

| 版本 | 脚本 | 定时配置 |
| --- | --- | --- |
| GitHub Actions 原版 | `glados.py` | `.github/workflows/runGladosAction.yml` |
| 青龙增强版 | `qinglong/glados.py` | 在青龙面板中配置 |

青龙版提供官网一次性 challenge 验证、网络重试、多账号、可调超时和失败检测。此次更新针对 NAS / 青龙的 `qinglong/glados.py`；根目录 GitHub Actions 原版仍使用旧流程，不包含本次验证适配。

### 2026-11 签到验证更新

参考 [issue #28](https://github.com/lankerr/2026-glados-checkin/issues/28)、[PR #31](https://github.com/lankerr/2026-glados-checkin/pull/31) 和 [PR #33](https://github.com/lankerr/2026-glados-checkin/pull/33)（核对版本 `8649193124403fb0195c66a6e4be8b7d2103b96b`）：官网已增加一次性 challenge。旧脚本只提交 token，缺少这一流程。感谢原项目维护者及 chengchang1987-cloud 的排查。

青龙版现在先在 `glados.rocks` 获取 challenge，等待 2.7 秒后在同一域名提交 nonce、SHA-256 proof 和域名 token。验证不可用或格式异常时停止；验证过期、网络异常或服务临时错误需要重试时，重新获取验证，不复用旧 nonce，也不回退到仅提交 token 的旧流程。Python 依赖仍只有 `requests`，但无法处理官网要求的浏览器 Turnstile 验证，详见下文。

**2026-11-11 补充核对官网前端：**当前签到流程还读取 challenge 返回的 `turnstile` 配置，通过 Cloudflare Turnstile 获取令牌，并提交 `tsState` 及验证令牌。SHA-256 算法未变。11 月 6 日参考版本没有覆盖这部分，不能据此前修复保证当前所有账号自动签到可用。

版本 `2026-11-11.2` 在 challenge 未要求 Turnstile 时按官网提交 `tsState: off`；如果返回有效的 Turnstile 配置，停止提交并提示到官网完成签到，不伪造状态或复用其他请求的验证令牌。如果服务端仍返回 `Automated check-in detected. Please sign in again to continue.`，仅能确定当前请求被拒绝，无法仅凭该文字断定是 Cookie、请求头、网络或其他校验造成。浏览器手动成功也不代表纯 HTTP 脚本可用；换 User-Agent 不能替代浏览器验证。

启动日志会显示版本号、运行模式及是否读取 `GLADOS_USER_AGENT`，不输出 Cookie、nonce、proof 或令牌。HTTP 模式无法处理 Turnstile；浏览器模式的部署和限制见 [独立浏览器模式](browser/README.md)。

已有青龙用户更新步骤：

1. 将本次代码推送到订阅使用的仓库后，在青龙「订阅管理」手动运行一次 `Null993_Gladoscheckin` 订阅，拉取新版 `qinglong/glados.py`，无需直接修改 NAS 上的代码。
2. 保留原有 `GLADOS_COOKIE`、任务命令、定时规则和通知配置。建议增加 `GLADOS_USER_AGENT`，值为获取 Cookie 时所用浏览器的完整 User-Agent。
3. 下次运行时检查日志是否出现 `已获取签到验证，等待 2.7 秒`，并核对首次签到的返回分数和官网积分流水。

若今天已经签到，再次执行通常只会返回“今日已记录”，不能验证下次奖励；此前已入账的 1 分不会由脚本补发。issue 维护者反馈更新后一个账号连续两天获得 12 分，但奖励仍由服务端决定，不能保证固定分数。返回成功且只有 1 分时仍按成功处理，在日志和已配置的企业微信通知中显示原始结果，不触发失败邮件。

## GitHub Actions 原版

继续使用原版时，在仓库的 `Settings` → `Secrets and variables` → `Actions` 中配置：

- `GLADOS_COOKIE`：GLaDOS 完整 Cookie（必填）
- `WECOM_WEBHOOK`：企业微信群机器人 Webhook（可选）

工作流仍按原计划在北京时间每天 09:30 自动运行，也可在 Actions 页面手动触发。

## 青龙面板增强版

脚本对网络超时、连接异常、HTTP 5xx 和异常响应提供自动重试。默认每次请求最多尝试 4 次，读取超时为 30 秒，能显著降低偶发网络波动造成的签到失败。

## 一、添加青龙订阅

进入青龙面板的「订阅管理」，新建订阅：

| 配置项 | 内容 |
| --- | --- |
| 名称 | `GLaDOS 签到` |
| 类型 | `公开仓库` |
| 链接 | `https://github.com/Null993/Gladoscheckin.git` |
| 定时类型 | `crontab` |
| 定时规则 | `0 2 * * *`（每天 02:00 拉取代码） |
| 白名单 | `qinglong/glados.py` |

保存后手动运行一次订阅。如果你的青龙版本没有自动生成任务，请在「定时任务」中新建：

```shell
task Null993_Gladoscheckin/qinglong/glados.py
```

建议的签到定时规则为 `30 9 * * *`，即每天北京时间 09:30 执行。你当前订阅日志显示仓库目录为 `/ql/data/repo/Null993_Gladoscheckin`，因此应使用上面的任务命令。如果以后修改了订阅名称或青龙生成了不同目录，请按订阅日志中的实际目录调整，并确保结尾为 `qinglong/glados.py`。

## 二、安装依赖

进入青龙面板的「依赖管理」→「Python3」并添加：

```text
requests
```

部分青龙镜像已经内置，无需重复安装。

## 三、添加环境变量

在「环境变量」中添加：

| 变量名 | 必填 | 说明 |
| --- | --- | --- |
| `GLADOS_COOKIE` | 是 | GLaDOS 网站的完整 Cookie |
| `GLADOS_USER_AGENT` | 否，建议配置 | 获取 Cookie 的浏览器 User-Agent；未填则使用内置值。会话若校验设备，应与浏览器保持一致 |
| `WECOM_WEBHOOK` | 否 | 企业微信群机器人的 Webhook 地址 |
| `GLADOS_TIMEOUT` | 否 | 单次读取超时秒数，默认 `30`，范围 5～120 |
| `GLADOS_RETRIES` | 否 | 首次请求失败后的重试次数，默认 `3`，范围 0～8 |
| `GLADOS_SERVICE_RETRIES` | 否 | 服务端提示“稍后重试”后的额外重试次数，默认 `3` |
| `GLADOS_SERVICE_RETRY_DELAY` | 否 | 服务端临时错误的首次等待秒数，默认 `30`，之后递增 |
| `EMAIL_SMTP_HOST` | 否 | 失败邮件的 SMTP 服务器，例如 `smtp.qq.com` |
| `EMAIL_SMTP_PORT` | 否 | SMTP 端口，SSL 默认 `465`，STARTTLS 通常为 `587` |
| `EMAIL_SMTP_USER` | 否 | SMTP 登录账号，通常是发件邮箱地址 |
| `EMAIL_SMTP_PASSWORD` | 否 | 邮箱服务商生成的 SMTP 授权码，不是网页登录密码 |
| `EMAIL_TO` | 否 | 收件邮箱；多个地址可用逗号、分号或换行分隔 |
| `EMAIL_FROM` | 否 | 发件地址，默认与 `EMAIL_SMTP_USER` 相同 |
| `EMAIL_SMTP_SECURITY` | 否 | `ssl`（默认）、`starttls` 或 `none` |

多账号可把多个 Cookie 放进同一个 `GLADOS_COOKIE`，使用换行或 `&` 分隔。

### 获取 Cookie

1. 登录 GLaDOS，进入签到页。
2. 按 `F12` 打开浏览器开发者工具，切换到「Network / 网络」。
3. 刷新页面，选择发往 `glados.rocks` 的请求。
4. 在「Request Headers / 请求标头」中复制完整的 `Cookie` 值。

Cookie 类似：

```text
gld:sess=...; gld:sess.sig=...
```

Cookie 等同于登录凭证，请只保存在青龙环境变量中，不要写入脚本、日志或提交到 GitHub。

新版会话常见名称为 `gld:sess` / `gld:sess.sig`，请复制当前登录浏览器的完整值，不要手动把名称改为旧版 `koa:sess`。同一请求的 `User-Agent` 值可用于 `GLADOS_USER_AGENT`，也可在浏览器控制台执行 `navigator.userAgent` 获取，粘贴时不带外层引号。多账号共用此 User-Agent，建议在同一种浏览器环境中获取各账号 Cookie。

### 在青龙添加 User-Agent

进入青龙「环境变量」→「新建变量」，名称填写 `GLADOS_USER_AGENT`，值填写完整的单行 User-Agent，保存并启用。不要把 `GLADOS_USER_AGENT=` 或 `User-Agent:` 一起填进值里。

以下是可直接填写的完整示例（也是脚本内置默认值）：

```text
Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36
```

这个示例不是从你的浏览器读取的，不能保证与现有 Cookie 的设备信息匹配。若要保持一致，推荐在获取 Cookie 的浏览器里执行 `navigator.userAgent`，用实际结果替换示例。只需在青龙保存环境变量，订阅拉取仓库不会覆盖该变量。

### 失败邮件告警

只要配置了邮件变量，账号签到失败、Cookie 缺失或未捕获的脚本运行异常都会触发邮件；签到成功时不会发送邮件。发送邮件只需要 SMTP，POP3/IMAP 是收取邮件的协议，无需配置。

以 QQ 邮箱为例：

```text
EMAIL_SMTP_HOST=smtp.qq.com
EMAIL_SMTP_PORT=465
EMAIL_SMTP_USER=你的QQ邮箱
EMAIL_SMTP_PASSWORD=QQ邮箱生成的SMTP授权码
EMAIL_TO=接收告警的邮箱
EMAIL_SMTP_SECURITY=ssl
```

163 邮箱可使用 `smtp.163.com:465`；其他邮箱请采用服务商提供的 SMTP 地址、端口和应用专用密码/授权码。所有邮件变量必须作为青龙环境变量保存，切勿提交到仓库。

### 青龙任务名称异常

青龙版脚本内置了 `GLaDOS 青龙签到` 任务名称和每天 09:30 的定时元数据，并避开了会被旧版青龙误解析为 `str, default` 的字段。如果错误任务已经存在，请删除该任务后重新运行一次订阅，或直接把任务名称手动改为 `GLaDOS 青龙签到`。

## 四、验证

在青龙「定时任务」中手动运行一次。日志出现以下内容即表示执行成功：

```text
your@email.com ---- Checkin Successfully ---- 剩余 123 天
```

如果当天已经签到，脚本会把“重复签到”视为正常结果。若全部重试后仍失败，任务会以失败状态结束，并在日志及已配置的企业微信中显示原因。

新版首次签到可能返回 `Checkin! Got 12 Points`，重复签到可能返回 `Today's observation logged`。challenge 获取成功的日志只表示获取了验证，最终仍以签到接口结果及官网流水为准。HTTP 401/403 或设备校验失败时，请重新获取 Cookie 并核对 User-Agent；不会反复提交请求。

## 重试机制

网络请求失败后，脚本会按 2、4、8 秒逐步等待并重试。默认配置下，签到流程最多进行 3 次额外网络重试；每次重新获取 challenge 并等待 2.7 秒后再提交。状态查询独立最多尝试 4 次，每次读取等待 30 秒。

如果接口正常返回、但提示 `Submarine cable data not ready. Please try again in a moment.` 等服务端临时错误，脚本会另外按 30、60、120 秒等待后再次发起签到；这组重试不会被前面的网络超时或 502 消耗。Cookie 失效等非临时错误不会反复重试。

每次业务重试同样使用全新 challenge。验证缺失、结构无效或请求发生重定向时明确失败，不发送旧版签到请求，也不会转到其他域名重放凭据。

若 NAS 到 GLaDOS 的网络长期不稳定，可把 `GLADOS_TIMEOUT` 调到 `60`、`GLADOS_RETRIES` 调到 `4`；若 GLaDOS 服务本身经常短时不可用，可把 `GLADOS_SERVICE_RETRIES` 调到 `5`。

## 青龙随机延迟

青龙的 `RandomDelay` 是全局随机延迟配置，不是脚本自身行为。例如任务日志显示随机延迟 2639 秒，09:30 的任务实际要到 10:14 左右才开始。若不需要该行为，请在青龙配置文件中将 `RandomDelay` 的值置空（或按所用青龙版本设为 `0`），保存后重启相关调度服务；也可以保留它来错峰运行。

## 开发验证

安装 `qinglong/requirements.txt` 后，运行 `python -m unittest discover -s tests -v`。测试模拟响应及等待，覆盖验证计算、过期/异常验证、提交失败后获取新验证、临时业务重试、认证失败、重复签到、多账号和 1 分成功不发失败邮件，不访问真实账号。
