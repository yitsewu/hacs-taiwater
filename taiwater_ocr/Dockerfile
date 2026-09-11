FROM python:3.12-slim

ARG BUILD_VERSION=dev
ARG BUILD_ARCH=unknown

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --only-binary=:all: --requirement requirements.txt

COPY server.py ./

RUN groupadd --gid 1000 taiwater \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin taiwater

USER 1000:1000
EXPOSE 8080
STOPSIGNAL SIGTERM

CMD ["python", "-I", "/app/server.py"]
