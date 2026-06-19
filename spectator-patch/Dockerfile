# Somtum dual-bancho: patched osu-server-spectator.
#
# Derives a corrected image from the upstream googuteam build by binary-patching
# the compiled friend-presence SQL string in osu.Server.Spectator.dll. See
# patch_dll.py for the why (Friend->FOLLOW + priv=1 -> priv&1=1). No .NET SDK or
# GitHub-Packages PAT required — the edit is length-preserving.
#
# Build (from g0v0-server/spectator-patch/):
#   podman build -t g0v0-spectator-somtum:relfix .
ARG BASE=ghcr.io/googuteam/osu-server-spectator:v2

FROM ${BASE} AS base

FROM python:3.12-slim AS patch
COPY --from=base /app/osu.Server.Spectator.dll /work/osu.Server.Spectator.dll
COPY patch_dll.py /work/patch_dll.py
RUN python3 /work/patch_dll.py /work/osu.Server.Spectator.dll

FROM ${BASE}
COPY --from=patch /work/osu.Server.Spectator.dll /app/osu.Server.Spectator.dll
