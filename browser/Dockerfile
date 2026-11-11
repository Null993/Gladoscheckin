FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CHROME_BINARY=/usr/bin/chromium \
    PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=8 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium chromium-driver xvfb xauth fonts-liberation fonts-noto-cjk \
    x11-utils x11-xserver-utils libglib2.0-0 libnss3 libasound2 \
    libtk8.6 libtcl8.6 \
    && rm -rf /var/lib/apt/lists/*
RUN python -c "import tkinter; print('Tkinter runtime OK:', tkinter.TkVersion)"
WORKDIR /app
COPY requirements.txt /app/requirements.txt
ARG PIP_INDEX_URL=https://pypi.org/simple
# The environment timeout/retries also apply to pip's isolated-build subprocesses.
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --index-url "$PIP_INDEX_URL" -r requirements.txt \
    && cp /usr/bin/chromedriver /usr/local/lib/python3.12/site-packages/seleniumbase/drivers/uc_driver \
    && chmod 755 /usr/local/lib/python3.12/site-packages/seleniumbase/drivers/uc_driver
COPY server.py worker.py /app/
RUN python worker.py --selftest
EXPOSE 8093
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8093/health',timeout=3)"
CMD ["python", "server.py"]
