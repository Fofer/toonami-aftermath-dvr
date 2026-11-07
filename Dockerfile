FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    TA_DATA_DIR=/data \
    TA_CHANNELS_FILE=/app/channels.json \
    TA_PORT=8787

RUN adduser -D -u 10001 app && mkdir /data && chown app /data
WORKDIR /app
COPY src/toonami_epg.py channels.json ./

USER app
VOLUME /data
EXPOSE 8787

HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/healthz'%os.environ.get('TA_PORT','8787'),timeout=4)"

CMD ["python", "toonami_epg.py", "serve"]
