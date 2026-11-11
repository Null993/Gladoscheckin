# DXP4800 / N100：在 NAS 登录，再由青龙签到

版本 `2026-11-11.6`。针对 `device-mismatch / loginDevice=Windows / currentDevice=Linux`，新增 NAS 原生浏览器登录和持久会话。首次在 NAS 的 Linux Chromium 中登录，后续青龙复用同一个浏览器配置，不再注入 Windows Cookie。

保留 WorkBuddy 验证过的域名配置、页面内按钮点击、Turnstile 资源检测、阶段日志和国内构建源。采用 [cloudflare-bypass-2026](https://github.com/1837620622/cloudflare-bypass-2026) 介绍的 SeleniumBase UC 运行方式。HTTP 200、捕获到响应或取得 cf_clearance 都不等于签到成功，最终判断实际签到接口和积分流水。

## 已部署用户升级

1. 更新部署目录中的本包文件，**保留现有 `.env`**。
2. `.env` 新增 `GLADOS_VNC_PASSWORD`，必须恰好 8 位英文字母/数字；不要复用原有的长调用密钥。保持已使用的 `GLADOS_ORIGIN=https://glados.one`。
3. 用同目录的 `compose.yaml` 重新构建启动。只拉取青龙订阅不会更新浏览器镜像。
4. 打开 NAS 的 8093 登录页，完成下述首次登录和保存。
5. 青龙新增 `GLADOS_BROWSER_SESSION=profile`、`GLADOS_BROWSER_PROFILES=1`，运行仓库订阅更新脚本，再执行原签到任务。

本版增加 8094 远程桌面端口和 `glados-browser-data` 数据卷。此前没有数据卷的部署没有可迁移会话，需要首次登录一次。以后更新容器时保留该卷，**不要删除卷或执行 `docker compose down -v`**。

## 部署文件及 .env

将 browser 目录内所有构建文件放到 NAS，例如 `/volume1/docker/glados-browser`：

```text
Dockerfile
compose.yaml
requirements.txt
server.py
worker.py
entrypoint.py
login.html
.dockerignore
.env.example
```

新用户复制 `.env.example` 为 `.env`；已有用户只在原文件补充缺少的值：

```dotenv
NAS_IP=192.168.0.119
GLADOS_BROWSER_TOKEN=自己的至少32位随机调用密钥
GLADOS_VNC_PASSWORD=自己的8位字母数字密码
GLADOS_ORIGIN=https://glados.one
```

中文是占位说明，请替换。调用密钥至少 32 个无空格 ASCII 字符，推荐生成 48～64 位字母数字。VNC 协议仅使用前 8 字节，本版要求恰好 8 位，避免悄悄截断长密钥。Cookie、GLaDOS 密码和验证码不写入 `.env`。

域名支持 `glados.one`、`glados.rocks`、`glados.cloud` 的 HTTPS 地址。保存会话后保持域名不变；换域名需要重新登录。当前证据指向设备不匹配，不要求迁移域名。

绿联 Docker 项目选择此目录的 Compose 文件构建启动，或在 NAS SSH 进入该目录执行：

```sh
docker compose up -d --build
docker compose logs --tail=40
```

镜像包含 Chromium、驱动、Tcl/Tk、Xvfb、Openbox、x11vnc 和 noVNC。启动时共享 X 显示屏。构建会运行无账号自检，但不登录或签到。最多使用 2 核、2 GB 内存，任务逐个执行。

默认保留 WorkBuddy 的阿里云 Debian 源和清华 PyPI 源，可通过 `.env` 的 `APT_MIRROR`、`PIP_INDEX_URL`、`PIP_EXTRA_INDEX_URL` 调整。pip 超时 120 秒、网络重试 8 次，BuildKit 保留下载缓存。Chrome 如需要代理，请显式设置 `GLADOS_BROWSER_PROXY`，不要假设它读取 HTTP_PROXY；地址必须从容器可达，不能用代表容器自己的 127.0.0.1。

## 首次在 NAS 浏览器登录

1. 打开 `http://192.168.0.119:8093/`（按实际 IP 修改）。认证框用户名填 **glados**，密码填 **GLADOS_BROWSER_TOKEN** 的长密钥。
2. 账号编号用 **1**，点击「启动登录浏览器」。
3. 点击「打开远程浏览器」。noVNC 的连接密码填 **GLADOS_VNC_PASSWORD**。启动期间可能先看到空白桌面；当前 NAS 的 Chrome 冷启动可能仍需约 136 秒，请等待状态更新。
4. 在远程 Chromium 中登录 GLaDOS，自行输入账号、密码/邮箱验证码并完成验证，确认进入账号页。**浏览器实际运行在 NAS 上，观看远程桌面的 Windows 电脑不会改变它的 Linux 身份。**
5. 返回 8093 页，点击「保存登录并关闭浏览器」，等待「登录已保存」。未确认账号状态时不会显示保存成功。

登录操作不会自动签到，不注入 Windows Cookie，也不替你提交登录凭据。保存时正常关闭浏览器并写入配置，避免 UC 直接结束进程使会话未落盘。Chrome 配置保存在卷中的 `/data/profiles/1`，不会导出到青龙或日志。

多账号逐个选择编号 2、3 等登录保存，最多 20 个；各编号目录隔离。登录最长 15 分钟，期间青龙会提示服务忙，完成保存后再运行签到。日常任务运行时不要通过远程桌面操作该浏览器。

8093、8094 仅绑定 NAS 局域网地址，**不要做公网映射**。5900 VNC 只监听容器内回环地址，不映射到 NAS。

## 青龙环境变量

| 名称 | 值 |
| --- | --- |
| `GLADOS_MODE` | `browser` |
| `GLADOS_BROWSER_SESSION` | `profile` |
| `GLADOS_BROWSER_PROFILES` | `1`，多账号例如 `1,2` |
| `GLADOS_BROWSER_URL` | `http://192.168.0.119:8093`，按实际 IP 填写 |
| `GLADOS_BROWSER_TOKEN` | 与容器 .env 的长密钥一致 |
| `GLADOS_BROWSER_TIMEOUT` | 可选，profile 默认 `420` 秒，范围 60～600 |

profile 模式**不需要且忽略 GLADOS_COOKIE / GLADOS_USER_AGENT**，使用 NAS 原生身份，不让桌面变量覆盖 Linux 会话。原变量可停用。邮件及企业微信配置保留，成功只有 1 分不会发失败邮件。

运行仓库订阅更新青龙脚本；任务命令和定时规则继续使用：

```sh
task Null993_Gladoscheckin/qinglong/glados.py
```

日志应显示 `2026-11-11.6`，以及 profile 使用原生身份。重复签到视为正常结果；超时或结果不明不自动重放，请先核对流水。登录失效时到 8093 页选择对应编号重新登录保存。

未配置 profile 时仍使用旧 Cookie 模式以兼容已有部署；Cookie 模式不能通过复制 Windows Cookie 解决本次 Windows/Linux 设备差异，其默认超时仍为 180 秒。

## 自检、更新和限制

- `/health` 无需认证，只说明服务正常，不代表登录或签到成功。
- 青龙临时设置 `GLADOS_BROWSER_SELFTEST=1` 可运行无账号自检；完成后删除或禁用此变量，否则定时任务也只自检。
- 容器终端可用 `python /app/worker.py --selftest`；不要在其他任务运行时同时自检，以免争用桌面。
- 青龙更新运行订阅即可；浏览器更新需覆盖构建文件、保留 `.env` 和数据卷，再重新构建。
- 持久会话等同于登录凭据，备份卷时按凭据保管，不上传到 GitHub。
- `profile_not_ready` 表示编号尚未保存登录；`profile_origin_mismatch` 表示域名不一致。设备不匹配会显示登录和当前平台；进程异常提供阶段及类型，不打印 Cookie、验证码或 nonce/proof。

本地测试覆盖服务认证、独立目录、登录期间不签到，以及真实 Chrome 关闭后重启复用会话。尚未在你的 NAS 构建新镜像或用真实账号签到。此方案解决同一个 Linux 环境内登录和执行的问题，不能保证 Turnstile 或其他服务端校验每次自动通过。约 136 秒的启动耗时原因尚未确认，不把新增启动参数当作已解决。
