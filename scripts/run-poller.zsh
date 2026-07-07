#!/bin/zsh
set -euo pipefail
cd /Users/so1omon/homelab/plaud-poller
exec /usr/bin/python3 -m plaud_poller.poll "$@"
