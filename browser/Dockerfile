FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CHROME_BINARY=/usr/bin/chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium chromium-driver xvfb xauth fonts-liberation fonts-noto-cjk \
    x11-utils x11-xserver-utils libglib2.0-0 libnss3 libasound2 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && cp /usr/bin/chromedriver /usr/local/lib/python3.12/site-packages/seleniumbase/drivers/uc_driver \
    && chmod 755 /usr/local/lib/python3.12/site-packages/seleniumbase/drivers/uc_driver
COPY server.py worker.py /app/
EXPOSE 8093
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8093/health',timeout=3)"
CMD ["python", "server.py"]
