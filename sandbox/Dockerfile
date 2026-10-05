# Image the harness runs a target repository's checks in (--sandbox docker).
#
# Build it once, from the harness folder:
#     docker build -t harness-sandbox -f sandbox/Dockerfile .
#
# It holds Python, pytest and the target's packages. The repository itself
# is NOT copied in: each check mounts the disposable copy read-only at /work.
FROM python:3.12-slim

COPY demo/requirements-target.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

# Checks never run as root.
RUN useradd --create-home --uid 1000 runner
USER runner
WORKDIR /work
