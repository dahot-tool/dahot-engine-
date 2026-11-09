#!/bin/sh
# Dahot Tool engine container start.
#
# The bgutil PO-token sidecar is DISABLED (2026-11-09 production verdict:
# tokens were minted and presented from inside this container and YouTube
# still refused real formats — the wall is the datacenter egress, not the
# token — while the provider idled at ~200 MB of the free tier's 256 MB).
# The provider sources stay in pot/ and the engine's POT wiring stays in
# app.py (dormant, POT_PROVIDER_URL env can re-enable it), but the node
# process is NOT started, so the container runs exactly as it did before
# the sidecar existed: just the engine in the foreground.

cd /app || exit 1

exec python app.py
