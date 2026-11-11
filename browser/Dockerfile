FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CHROME_BINARY=/usr/bin/chromium \
    PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=8 PIP_DISABLE_PIP_VERSION_CHECK=1

# ---- 国内网络适配 1/2：Debian apt 源换国内镜像 --------------------------------
# 默认阿里云。若拉取失败可改成 mirrors.tuna.tsinghua.edu.cn 或 mirrors.ustc.edu.cn，
# 通过 compose 的 build.args.APT_MIRROR 传入，不需要改这里。
ARG APT_MIRROR=mirrors.aliyun.com

RUN set -eux; \
    for f in /etc/apt/sources.list \
             /etc/apt/sources.list.d/*.sources \
             /etc/apt/sources.list.d/*.list; do \
        [ -f "$f" ] || continue; \
        sed -i \
            -e "s|http://deb.debian.org|https://${APT_MIRROR}|g" \
            -e "s|https://deb.debian.org|https://${APT_MIRROR}|g" \
            -e "s|http://security.debian.org|https://${APT_MIRROR}|g" \
            -e "s|https://security.debian.org|https://${APT_MIRROR}|g" \
            "$f"; \
    done; \
    echo "---- effective apt sources ----"; \
    cat /etc/apt/sources.list 2>/dev/null || true; \
    cat /etc/apt/sources.list.d/*.sources 2>/dev/null || true

# 缓存 deb 包：上一次构建中断后重来可续用已下载的部分，不用从 0 开始
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    set -eux; \
    apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 update; \
    apt-get install -y --no-install-recommends \
        chromium chromium-driver xvfb xauth fonts-liberation fonts-noto-cjk \
        x11-utils x11-xserver-utils libglib2.0-0 libnss3 libasound2 \
        libtk8.6 libtcl8.6; \
    apt-get install -y --no-install-recommends x11vnc novnc websockify openbox; \
    rm -rf /var/lib/apt/lists/*

RUN python -c "import tkinter; print('Tkinter runtime OK:', tkinter.TkVersion)"

WORKDIR /app
COPY requirements.txt /app/requirements.txt

# ---- 国内网络适配 2/2：pip 走国内源 ------------------------------------------
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_EXTRA_INDEX_URL=

# The environment timeout/retries also apply to pip's isolated-build subprocesses.
RUN --mount=type=cache,target=/root/.cache/pip \
    set -eux; \
    if [ -n "$PIP_EXTRA_INDEX_URL" ]; then \
        python -m pip install --index-url "$PIP_INDEX_URL" --extra-index-url "$PIP_EXTRA_INDEX_URL" -r requirements.txt; \
    else \
        python -m pip install --index-url "$PIP_INDEX_URL" -r requirements.txt; \
    fi; \
    cp /usr/bin/chromedriver /usr/local/lib/python3.12/site-packages/seleniumbase/drivers/uc_driver; \
    chmod 755 /usr/local/lib/python3.12/site-packages/seleniumbase/drivers/uc_driver

COPY server.py worker.py entrypoint.py login.html /app/
RUN command -v Xvfb x11vnc websockify openbox xdpyinfo \
    && test -f /usr/share/novnc/vnc.html
RUN python worker.py --selftest
EXPOSE 8093 8094
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8093/health',timeout=3)"
CMD ["python", "entrypoint.py"]
