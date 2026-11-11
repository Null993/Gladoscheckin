# 绿联 DXP4800 / Intel N100：青龙调用浏览器签到

适用：x86_64 NAS、Docker 部署青龙。浏览器放在独立容器，青龙仍运行仓库里的 `qinglong/glados.py`，Python 依赖仍只有 `requests`。代码由仓库订阅更新，以下只需部署容器和填写环境变量。

此模式让 Chromium 打开官网，在同一浏览器里运行官网 challenge / Turnstile、点击签到并读取实际接口结果；不把获取 `cf_clearance` 当成签到成功，不导出验证令牌给 requests。采用 [cloudflare-bypass-2026](https://github.com/1837620622/cloudflare-bypass-2026) 所介绍的 SeleniumBase UC 与 Xvfb 运行方式；本仓库实现了 GLaDOS 页面流程，没有复制该项目的源码。

**浏览器模式不是保证通过的方案。** Turnstile、设备校验或会话限制仍可能拒绝自动操作。需要人工验证或登录时，任务明确失败并走现有告警；当前容器不提供远程桌面，也不会使用第三方打码服务。不能仅凭离线测试保证你的账号首次签到或奖励恢复。

## 1. 准备部署目录

在 NAS 文件管理器创建一个目录，例如共享文件夹里的 `docker/glados-browser`。将本仓库 **browser 目录内的文件** 放进去，至少包括：

```text
Dockerfile
compose.yaml
requirements.txt
server.py
worker.py
.env.example
```

可以从本次提供的部署压缩包解压，也可从 GitHub 获取 browser 目录。它们是独立容器的构建文件，不需要在青龙容器里改代码。仅在青龙运行订阅不会自动创建浏览器容器。

把 `.env.example` 复制为 `.env`（注意文件名开头的点），填写：

```dotenv
NAS_IP=192.168.1.100
GLADOS_BROWSER_TOKEN=在这里填写自己生成的随机密钥
```

- `NAS_IP` 改成 DXP4800 的实际局域网 IP。不要填公网地址、`0.0.0.0` 或 `127.0.0.1`。
- 密钥至少 32 个 ASCII 字符，无空格。可用密码管理器生成 48～64 位字母数字串。也可在有 Python 的终端执行 `python3 -c "import secrets; print(secrets.token_hex(32))"`。
- 不要把 Cookie 放进 `.env`；Cookie 继续只在青龙环境变量里保存。
- `.env` 已被 Git 和 Docker 构建上下文排除，勿手动上传到 GitHub。

## 2. 在绿联 Docker 中启动

在绿联「Docker」→「项目」新建 Compose 项目，项目路径选择上面的 `docker/glados-browser` 目录，使用其中的 `compose.yaml` 构建并启动。需要选择**从本地目录构建镜像**；不要只粘贴 YAML 而缺少同目录的 Dockerfile/源码。不同 UGOS 版本的按钮名称可能不同。

如果面板不支持构建，开启 NAS SSH，在实际部署目录执行：

```sh
docker compose up -d --build
docker compose logs --tail=30
```

首次构建要下载 Debian/Python 镜像、Chromium 和 Python 包，可能较慢。构建过程不需要 GLaDOS Cookie。容器使用最多 2 核、2 GB 内存，Chrome 仅在签到任务期间运行。

容器日志出现 `GLaDOS browser service 2026-11-11.5 ready on port 8093` 后，在局域网浏览器访问：

```text
http://你的NAS局域网IP:8093/health
```

应看到 `"ok": true`。这只说明调用服务正常，尚未验证 Chrome 或 GLaDOS 签到。8093 只用于 NAS 局域网调用，不要做公网端口映射。Compose 不需要特权模式、host 网络或 Docker socket。

从版本 `2026-11-11.4` 开始，镜像构建会自动运行 `python worker.py --selftest`，实际启动浏览器、执行 JavaScript 并检查 Linux 显示环境；失败时停止构建，输出阶段、异常类型及固定分类，不输出 Cookie 或原始异常内容。此自检不访问 GLaDOS，不证明账号签到可以通过。

## 3. 在青龙新增环境变量

| 名称 | 值 |
| --- | --- |
| `GLADOS_MODE` | `browser` |
| `GLADOS_BROWSER_URL` | `http://你的NAS局域网IP:8093` |
| `GLADOS_BROWSER_TOKEN` | 与容器 `.env` 中完全相同的密钥 |
| `GLADOS_BROWSER_TIMEOUT` | 可选，默认 `180` 秒，可设 60～300 |

保留原来的 `GLADOS_COOKIE`、邮件和企业微信配置，任务命令及定时规则不变：

```sh
task Null993_Gladoscheckin/qinglong/glados.py
```

`GLADOS_USER_AGENT` 如有填写，浏览器模式也会读取；应填写 Cookie 所属浏览器的真实值。**不要继续使用此前通用 Chrome/126 示例作为实际设备信息。** 未配置时浏览器模式使用容器 Chromium 原生 User-Agent。会话仍可能要求设备匹配；Cookie 能查询状态不等于能签到。

青龙与浏览器是两个容器，URL 中的 `127.0.0.1` 会指向青龙自己，必须使用 NAS 局域网 IP。`GLADOS_BROWSER_TOKEN` 会用于调用认证，Cookie 经该内网请求临时送入隔离浏览器，不写入服务日志或持久配置；不要将此接口暴露到不受信任的网络。

## 4. 拉取仓库更新并验证

1. 在青龙「订阅管理」运行 `Null993_Gladoscheckin`，确认拉取成功。
2. 手动运行原签到任务。应显示版本 `2026-11-11.5`、`签到模式: browser`、`使用 NAS 浏览器签到`。
3. 等待任务结束。读取的是官网实际签到 POST 返回，仍需核对官网流水；当天已经签到时只会返回重复签到结果，无法证明明天奖励数额。

多账号继续用换行或 `&` 分隔 Cookie，逐个执行。成功只有 1 分不会发送失败邮件。浏览器服务忙时会拒绝新任务，不排队反复签到；调用超时或结果不明时不会自动重试、不会回退到旧 HTTP 方式，请先核对官网流水。

## 更新与排错

### 自检浏览器环境，不触发签到

先更新青龙脚本和浏览器容器至 `2026-11-11.5`。在青龙临时增加 `GLADOS_BROWSER_SELFTEST=1`，再运行原任务。此时不需要 Cookie，不发送签到或通知，只调用浏览器环境自检。

通过后**删除或禁用 `GLADOS_BROWSER_SELFTEST`** 再执行真正签到，否则定时任务也只会自检。也可在容器终端执行 `python /app/worker.py --selftest`。此命令只适用于 `2026-11-11.4` 及更新版本。

失败日志会包含类似 `stage=start_browser, diagnostic=SessionNotCreatedException, category=chrome_start_failed` 的信息，请反馈这部分。`WorkerExit` 表示子进程没有返回结果，需结合退出码及容器 OOM 状态排查。`/` 在旧版返回 `{"ok": false}` 是未知路径的响应，不代表健康检查失败；新版 `/` 与 `/health` 都显示服务信息。

### MouseInfo 提示缺少 tkinter

这是早期镜像遗漏了 Tcl/Tk 运行库。官方 `python:3.12-slim-bookworm` 的 Python 已编译 Tkinter，但 slim 清理过程不保留 Tk 的运行依赖。仅安装 Debian 的 `python3-tk` 会引入另一套系统 Python，不能据此保证 `/usr/local/bin/python` 可用。

`2026-11-11.5` 的镜像已补装 `libtk8.6` / `libtcl8.6`，并在构建时验证 `import tkinter`。也修复了 MouseInfo 直接退出造成旧日志只显示 `failed` 的问题。

现有容器可在终端临时验证修复：

```sh
apt-get update
apt-get install -y --no-install-recommends libtk8.6 libtcl8.6
python -c "import tkinter; print('Tkinter OK:', tkinter.TkVersion)"
```

正常应输出 `Tkinter OK: 8.6`，然后再执行浏览器启动测试或青龙任务。这只补装容器依赖，不修改签到代码。手动安装的依赖在重建容器后会丢失，长期使用请更新部署文件、保留 `.env` 并重新构建镜像。

- **青龙脚本更新**：手动运行订阅即可。
- **浏览器源码更新**：更新此部署目录中的 browser 文件，保留 `.env`，重新构建启动；只重启容器不会更新镜像内代码。
- **服务无法连接**：先访问 `/health`，检查实际 NAS IP、8093 端口、防火墙、容器状态。青龙只对这个内网调用禁用环境代理，不修改系统代理。
- **密钥不匹配**：核对青龙和 `.env` 的值；修改 `.env` 后重新创建容器。
- **浏览器执行失败**：确认镜像构建完整、内存够用；查看容器日志。服务只输出固定状态，不输出原始异常或账号凭据。
- **官网拒绝设备验证**：需要核对实际 Cookie/User-Agent；本模式不会伪造通过结果。
- **浏览器验证未能自动完成**：到官网完成本次签到；此模式不保证每次通过 Turnstile。

本次开发验证包括离线服务接口测试和本地浏览器模拟页面测试；没有访问真实账号或在 DXP4800 上执行容器构建。NAS 的实际镜像构建和账号签到结果需要部署后验证。
