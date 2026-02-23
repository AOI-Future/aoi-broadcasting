FROM python:3.14-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY main.py .

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin streamer && \
    mkdir -p /data/music /data/archive /app/assets && \
    chown -R streamer:streamer /data /app

USER streamer

CMD ["python", "-u", "main.py"]
