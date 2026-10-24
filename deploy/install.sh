#!/bin/sh
# Run as the ordinary login user on the target Linux server.
set -eu
cd "$(dirname "$0")/.."
root=$(pwd)
for tool in podman newuidmap newgidmap; do
  if ! command -v "$tool" >/dev/null; then
    echo "Missing $tool. Install: sudo apt-get install -y podman uidmap slirp4netns fuse-overlayfs" >&2
    exit 1
  fi
done
mkdir -p state "$HOME/.config/systemd/user"
if [ ! -f deploy/runtime.env ]; then
  (umask 077; cp deploy/runtime.env.example deploy/runtime.env)
fi
chmod 600 deploy/runtime.env
if [ ! -f configs/server.toml ]; then
  cp configs/news.toml configs/server.toml
fi
podman build -t localhost/rss-notifier:latest -f Containerfile .
podman run --rm localhost/rss-notifier:latest --check
cat > "$HOME/.config/systemd/user/rss-notifier-model.service" <<UNIT
[Unit]
Description=Local Ollama model for RSS notifier
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/podman run --rm --name rss-notifier-model --network host --env OLLAMA_HOST=127.0.0.1:11434 --env OLLAMA_NUM_PARALLEL=1 --env OLLAMA_MAX_LOADED_MODELS=1 --volume rss-notifier-models:/root/.ollama docker.io/ollama/ollama:latest
ExecStop=/usr/bin/podman stop -t 30 rss-notifier-model
Restart=on-failure
RestartSec=10
TimeoutStartSec=0
TimeoutStopSec=60

[Install]
WantedBy=default.target
UNIT
cat > "$HOME/.config/systemd/user/rss-notifier.service" <<UNIT
[Unit]
Description=RSS consensus summary notifier
After=network-online.target rss-notifier-model.service
Wants=network-online.target rss-notifier-model.service

[Service]
WorkingDirectory=$root
ExecStart=$root/deploy/notifier.sh
ExecStop=/usr/bin/podman stop -t 30 rss-notifier
Restart=on-failure
RestartSec=10
TimeoutStopSec=60

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now rss-notifier-model.service
ready=0
for attempt in $(seq 1 60); do
  if podman exec rss-notifier-model ollama list >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [ "$ready" != 1 ]; then
  echo 'Model server not ready; inspect journalctl --user -u rss-notifier-model' >&2
  exit 1
fi
podman exec rss-notifier-model ollama pull qwen2.5:3b
systemctl --user enable --now rss-notifier.service
systemctl --user status rss-notifier.service --no-pager
loginctl show-user "$USER" -p Linger
