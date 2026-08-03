# GLaDOS 自动签到（GitHub Actions / 青龙面板）

本仓库同时保留 GitHub Actions 原版和 NAS 青龙增强版，两套版本彼此隔离：

| 版本 | 脚本 | 定时配置 |
| --- | --- | --- |
| GitHub Actions 原版 | `glados.py` | `.github/workflows/runGladosAction.yml` |
| 青龙增强版 | `qinglong/glados.py` | 在青龙面板中配置 |

原有 GitHub Actions 的脚本和定时配置保持不变。青龙版单独提供网络重试、多账号、可调超时和更完整的失败检测。

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
| `WECOM_WEBHOOK` | 否 | 企业微信群机器人的 Webhook 地址 |
| `GLADOS_TIMEOUT` | 否 | 单次读取超时秒数，默认 `30`，范围 5～120 |
| `GLADOS_RETRIES` | 否 | 首次请求失败后的重试次数，默认 `3`，范围 0～8 |

多账号可把多个 Cookie 放进同一个 `GLADOS_COOKIE`，使用换行或 `&` 分隔。

### 获取 Cookie

1. 登录 GLaDOS，进入签到页。
2. 按 `F12` 打开浏览器开发者工具，切换到「Network / 网络」。
3. 刷新页面，选择发往 `glados.rocks` 的请求。
4. 在「Request Headers / 请求标头」中复制完整的 `Cookie` 值。

Cookie 类似：

```text
koa:sess=...; koa:sess.sig=...
```

Cookie 等同于登录凭证，请只保存在青龙环境变量中，不要写入脚本、日志或提交到 GitHub。

## 四、验证

在青龙「定时任务」中手动运行一次。日志出现以下内容即表示执行成功：

```text
your@email.com ---- Checkin Successfully ---- 剩余 123 天
```

如果当天已经签到，脚本会把“重复签到”视为正常结果。若全部重试后仍失败，任务会以失败状态结束，并在日志及已配置的企业微信中显示原因。

## 重试机制

网络请求失败后，脚本会按 2、4、8 秒逐步等待并重试。默认配置下，原先的单次 15 秒请求已调整为最多 4 次、每次读取等待 30 秒。若 NAS 到 GLaDOS 的网络长期不稳定，可把 `GLADOS_TIMEOUT` 调到 `60`，或将 `GLADOS_RETRIES` 调到 `4`。
