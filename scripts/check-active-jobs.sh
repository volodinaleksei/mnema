#!/usr/bin/env bash
# Proverka aktivnykh zapisey (zhivykh vstrech web-bot) po otvetu GET /api/jobs.
#
# Vkhod: JSON-spisok jobov — filim argumentom ili so stdin ("-").
# Vykhod: 0 — aktivnykh net; 1 — yest' aktivnyye (pechatayet ih v stdout);
#         2 — vvod ne chitaetsya / ne spiskom (API ne otvechal).
#
# Aktivnyye statusy: launching, recording, uploading — v nikh web-bot derzhit
# zhivoy brauzer, a `compose up -d --force-recreate web-bot` ubyet ego poserede
# vstrechi (sluchay job 32: 06.11.2026 — 6,2 MB zapisi ostalis' bez protokola,
# vstrecha propala).
# P1-2: + processing — `--force-recreate worker` uryvaet ASR/Diarizatsiyu na GPU
# posered konveyera; job zavisal v processing bez vosstanovleniya.
set -euo pipefail

src="${1:--}"
if [ "$src" = "-" ]; then
    json="$(cat)"
else
    json="$(cat "$src")"
fi

PY='import json, sys

try:
    jobs = json.load(sys.stdin)
except Exception:
    sys.exit(2)
if not isinstance(jobs, list):
    sys.exit(2)

ACTIVE = {"launching", "recording", "uploading", "processing"}
bad = [j for j in jobs if isinstance(j, dict) and j.get("status") in ACTIVE]
for j in bad:
    print("  #{} {}: {} (start {})".format(
        j.get("id"), j.get("status"), j.get("topic") or "", j.get("start_at") or "?"))
sys.exit(1 if bad else 0)'

printf '%s' "$json" | python3 -c "$PY"
