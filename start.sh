#!/bin/sh
# Dahot Tool engine container start.
#
# Two processes, one container:
#   1. The bgutil PO-token provider (bgutil-ytdlp-pot-provider 2.0.2) as a
#      sidecar on LOOPBACK ONLY, 127.0.0.1:4416 — yt-dlp's plugin in the
#      engine reaches it via POT_PROVIDER_URL (default in app.py is this
#      same loopback address). It is never exposed outside the container:
#      the provider has no authentication, so it must not be reachable
#      from the network (upstream 2.0.x binds localhost by default for
#      the same reason — see GHSA-qpv9-8xfj-xx9m; do NOT pass --host
#      0.0.0.0 here).
#   2. The engine itself (`python app.py`, which runs uvicorn on $PORT,
#      7860 by default) in the foreground, exactly as before.
#
# The provider is started at most once (this script is the container's
# single entrypoint) and it is strictly best-effort: if node or the
# provider build is missing, or the provider later dies, the engine
# keeps serving every platform — yt-dlp just gets no PO tokens and
# YouTube behaves as it did before the sidecar existed. Nothing here
# uses `set -e`, and the provider's failure can never take the shell
# (and therefore the container's foreground process) down with it.

cd /app || exit 1

if [ -f pot/build/main.js ] && command -v node >/dev/null 2>&1; then
    node pot/build/main.js --host 127.0.0.1 --port 4416 &
else
    echo "[pot] provider build or node missing; starting engine without sidecar" >&2
fi

exec python app.py
