FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LF_DATA_DIR=/data \
    LF_IN_CONTAINER=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Run as an unprivileged user. /data holds the database, uploaded files and the hot folder, and MUST be
# persistent storage (a named volume or bind mount). There's deliberately no VOLUME here: an anonymous
# volume would hide a missing mount and silently lose everything on the next redeploy. Instead the app
# shows a warning when /data isn't mounted.
RUN useradd --system --uid 10001 --home /srv lpf && mkdir -p /data && chown lpf /data
USER lpf
EXPOSE 8800

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8800/healthz', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8800", "--proxy-headers", "--forwarded-allow-ips", "*"]
