# arb-shadow monitor: a long-running, READ-ONLY measurement worker.
#
# Build context is the REPOSITORY ROOT, not this directory:
#   docker compose -f docker/docker-compose.yml build
#
# Two properties this image deliberately keeps:
#
#   IT CANNOT TRADE. Nothing here installs a signer, a wallet or a private key,
#   and the application imports none. That is the whole premise of the project
#   and the image should make it inspectable rather than assumed.
#
#   IT RUNS AS A NON-ROOT USER with no write access to its own code. The only
#   writable path is /data, so a bug cannot rewrite the measurement code that
#   produced the measurements.

FROM python:3.11-slim AS base

# PYTHONDONTWRITEBYTECODE: a read-only code directory would make .pyc writes
# fail, and stale __pycache__ has already caused one wrong result in this
# project's history (a mutation-test run reported a pass that was not real).
# PYTHONUNBUFFERED: without it, logs sit in a pipe buffer and `docker logs`
# shows nothing for minutes, which looks exactly like a stalled monitor.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, as their own layer: the requirements file changes far
# less often than the source, so edits to the monitor do not re-resolve pip.
COPY requirements.txt /app/requirements.txt
# The optional `pipca` secret is for building behind a TLS-inspecting proxy,
# which re-signs pypi.org with a private CA that the build container does not
# trust -- pip then fails with CERTIFICATE_VERIFY_FAILED and "No matching
# distribution found", which reads like a bad pin rather than a trust problem.
# Pass your CA when you need it and it is used for this layer only:
#
#   docker build --secret id=pipca,src=/path/to/ca-bundle.crt ...
#
# A secret mount is not a layer, so the certificate never ends up in the
# image. required=false means a normal build ignores it entirely. Disabling
# verification would be the other way to make this work, and is not an option.
RUN --mount=type=secret,id=pipca,target=/tmp/pip-ca.crt,required=false \
    sh -eux -c 'if [ -s /tmp/pip-ca.crt ]; then \
                  export PIP_CERT=/tmp/pip-ca.crt; \
                  export REQUESTS_CA_BUNDLE=/tmp/pip-ca.crt; \
                  export SSL_CERT_FILE=/tmp/pip-ca.crt; \
                fi; \
                pip install -r /app/requirements.txt' \
 && python -c "from eth_utils import keccak; keccak(text='backend check')"
#  ^ Fails the BUILD if no keccak backend resolved. Without this the image
#    builds clean and dies at the first venue connect() instead, which reads
#    as a dead chain rather than a packaging mistake.

COPY . /app

# A fixed UID so a bind-mounted ./data has predictable ownership on Linux.
# 10001 is above the usual host-user range, so it will not silently collide
# with a real account.
# The user is `arbshadow`, NOT `shadow`: Debian already ships a system group
# called `shadow` (gid 42, the group that owns /etc/shadow), so `useradd
# shadow` fails with exit 9 trying to create a group that exists. An explicit
# group avoids depending on useradd's default-group behaviour either way.
RUN groupadd --gid 10001 arbshadow \
 && useradd --uid 10001 --gid 10001 --create-home \
            --shell /usr/sbin/nologin arbshadow \
 && mkdir -p /data \
 && chown -R arbshadow:arbshadow /data \
 && chmod +x /app/docker/entrypoint.sh

# /data is the ONLY writable path: the sqlite database, the JSONL event log and
# the status snapshot. Everything else, including /app, stays owned by root and
# is read-only to the process.
VOLUME ["/data"]
USER arbshadow

# No EXPOSE and no ports. This worker is outbound-only -- it POSTs to a webhook
# you own and writes files. Nothing connects in to it.

# Liveness is measured, not assumed. The monitor rewrites /data/status.json on
# every heartbeat, so a stale file means the loop stopped even though the
# process is still alive -- which is this project's single most expensive
# failure mode (a 9.5-hour silent outage on a sibling system). start-period
# covers venue connect(), which does real on-chain validation and is not fast.
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
  CMD ["python", "/app/docker/healthcheck.py"]

ENTRYPOINT ["/app/docker/entrypoint.sh"]
