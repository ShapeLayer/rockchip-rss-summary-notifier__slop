#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
exec podman run --rm --name rss-notifier --network host \
  --cap-drop all --security-opt no-new-privileges --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --env-file "$PWD/deploy/runtime.env" \
  -v "$PWD/configs:/app/configs:ro" \
  -v "$PWD/state:/app/state:rw" \
  localhost/rss-notifier:latest --config /app/configs/server.toml
