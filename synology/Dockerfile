# PeekESP agent for Synology - the Container Manager path.
#
#   docker build -t peekesp-synology .
#   docker build --build-arg WITH_SMART=1 -t peekesp-synology .     # adds smartctl, for drive temps
#
# Most people should use install.sh instead: it needs no container, and it is the
# only path that can read drive temperatures and a UPS without extra plumbing. This
# exists for the people who would rather keep a NAS free of anything installed by
# hand, and for DSM boxes that have no usable Python 3.
#
# Same agent file, byte for byte, as install.sh puts on the box.
#
# PeekESP by rhshourav  -  Discord: rhshourav
#   https://github.com/rhshourav/PeekESP   https://rhshourav.github.io/PeekESP/

FROM python:3.12-alpine

LABEL org.opencontainers.image.title="PeekESP agent for Synology" \
      org.opencontainers.image.description="Zero-dependency telemetry agent for Synology DSM 7.x: a LAN widget feed and a relay push, each its own switch." \
      org.opencontainers.image.authors="rhshourav" \
      org.opencontainers.image.source="https://github.com/rhshourav/PeekESP" \
      org.opencontainers.image.url="https://rhshourav.github.io/PeekESP/" \
      org.opencontainers.image.licenses="MIT"

# smartctl is the only thing worth adding, and only if you will also hand the
# container a drive (see docker-compose.yml). Off by default: a container that
# cannot reach a drive has no use for it.
ARG WITH_SMART=0
RUN if [ "$WITH_SMART" = "1" ]; then apk add --no-cache smartmontools; fi

# Settings and the open-incident state live here, and only here. A named volume
# copies this ownership on first use, so the unprivileged user below can write it.
RUN mkdir -p /data && chown 65534:65534 /data

COPY peek-agent-synology.py /app/peek-agent-synology.py

# PEEK_RUN_DIR is tmpfs in the compose file: the status file is rewritten every
# couple of seconds and must never land on a volume.
ENV PEEK_CONFIG_DIR=/data \
    PEEK_RUN_DIR=/tmp \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Nothing the agent does without SMART needs root: /proc, /sys and statvfs are
# world-readable. The compose file switches to root only when drives are mapped.
USER 65534:65534
VOLUME /data

# Container Manager shows this as healthy / unhealthy. It reads the agent's own
# status file: process alive, status fresh, and both enabled outputs working.
HEALTHCHECK --interval=60s --timeout=10s --start-period=150s --retries=3 \
  CMD ["python", "/app/peek-agent-synology.py", "--healthcheck"]

ENTRYPOINT ["python", "/app/peek-agent-synology.py"]
