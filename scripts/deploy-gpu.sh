#!/usr/bin/env bash
# Deploy Mnema on GPU-serve (user@mnema-gpu.internal) iz lokal'nogo HEAD cherez git-bundle.
#
# Sposob: git-bundle (a ne push v GitHub i ne rsync dereva) —
#   - na etom hoste delaetsya bundle tekushhey vetki,
#   - na serve `git fetch <bundle>` + `git reset --hard FETCH_HEAD`,
#   - untracked fayly serve (.env, docker-compose.override.yml) ne trayutsya,
#   - zatrem `docker compose build` + `up -d --force-recreate api worker web-bot`
#     s tremya -f (base + gpu + override).
#
# Ispolzovanie:  ./scripts/deploy-gpu.sh
# Peremenkie:    DEST_SSH=user@mnema-gpu.internal  DEST_DIR=/data_local/docker/mnema
#                FORCE_DEPLOY=1  — propustit' blokirovku po aktivnym zapisjam
#                API_CHECK_URL   — URL API dlja proverki (po umolch.: http://HOST:8000)
#
# Pered deplojem skript proveryayet, net li zhivykh zapisey (launching/recording/
# uploading): `up -d --force-recreate web-bot` ubivaet zhivoy brauzer poserede
# vstrechi (job 32, 06.11.2026: 6,2 MB sobrano, vstrecha propala). Pri aktivnykh
# vstrechakh deploy ostanavlivaetsja; FORCE_DEPLOY=1 peremikaet blokirovku.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Real'nyy adres serve (dev-mashina): scripts/deploy-gpu.local.env (untracked, v git ne pishetsya)
[ -f "$REPO_DIR/scripts/deploy-gpu.local.env" ] && . "$REPO_DIR/scripts/deploy-gpu.local.env"

DEST_SSH="${DEST_SSH:-user@mnema-gpu.internal}"
DEST_DIR="${DEST_DIR:-/data_local/docker/mnema}"
API_CHECK_URL="${API_CHECK_URL:-http://$(printf '%s' "$DEST_SSH" | sed 's/.*@//'):8000}"

if ! git -C "$REPO_DIR" diff --quiet -- || ! git -C "$REPO_DIR" diff --cached --quiet --; then
    echo "ERROR: v $REPO_DIR est nekommitnutyje izmenenija otkhyzhdyonnykh faylov." >&2
    echo "Deploy vykladivaet HEAD; snachala sdelajte commit." >&2
    exit 1
fi

# --- blokirovka po aktivnym zapisjam ---------------------------------------
# web-bot perezhivayet `up -d --force-recreate` s umeršim brauzerom: zapis'
# obryvayetsja poserede vstrechi. Sprashivaem API serve o tekushchikh jobakh;
# zapisi v hodke (launching/recording/uploading) — ostanavlivajemsja.
# Proverka idet DO kakih-libo izmeneniy na serve (fetch/reset ešhe nidne) i
# POVTORYAETSA na serve neposredstvenno pered force-recreate (nize v heredoc):
# mezhdu etoy tochkoy i `up -d` lezhat build i alembic —minyuty, za kotorye
# scheduler uspevayet podnjat' vstrechu.
# Neizvestnoye sostojanie (API ne otvechal / garbage / net parserta) blokiruet
# tak zhe, kak aktivnyye zapisi; politika — scripts/deploy-guard-lib.sh,
# edinstvennyy escape-hatch FORCE_DEPLOY=1.
. "$REPO_DIR/scripts/deploy-guard-lib.sh"
JOBS_JSON="$(curl -sf --max-time 10 "$API_CHECK_URL/api/jobs" || true)"
CHECK_SH="$REPO_DIR/scripts/check-active-jobs.sh"
ACTIVE_LIST=""
CHECK_RC=3
if [ -z "$JOBS_JSON" ]; then
    echo "VNI MANIE: API $API_CHECK_URL/api/jobs ne otvechal — sostojanie zapisey NEIZVESTNO." >&2
elif [ ! -f "$CHECK_SH" ]; then
    echo "VNI MANIE: net $CHECK_SH — sostojanie zapisey NEIZVESTNO." >&2
else
    set +e
    ACTIVE_LIST="$(printf '%s' "$JOBS_JSON" | bash "$CHECK_SH" -)"
    CHECK_RC=$?
    set -e
fi
if guard_should_stop "$CHECK_RC" "${FORCE_DEPLOY:-0}"; then
    if [ "$CHECK_RC" = "1" ]; then
        echo "ERROR: idut aktivnyye zapisi — deploy ostanovlen, chtoby ne ubit' zhivyy brauzer web-bot:" >&2
    else
        echo "ERROR: ne udalos' proverit' aktivnyye zapisi (rc=$CHECK_RC) — neizvestnoye" \
             "sostojanie blokiruet deploj tak zhe, kak aktivnyye zapisi." >&2
    fi
    printf '%s\n' "$ACTIVE_LIST" >&2
    echo "Dokanchivajte/otmenyte vstrechi (ili chinite API/parsheg), i povtorite s FORCE_DEPLOY=1." >&2
    exit 1
fi
if [ "$CHECK_RC" != "0" ]; then
    echo "VNI MANIE: rc=$CHECK_RC (aktivnyye/sostojanie ne podtverzhdeny), no FORCE_DEPLOY=1 — prodolzhajem na svoy strakh:" >&2
    printf '%s\n' "$ACTIVE_LIST" >&2
else
    echo "==> Aktivnykh zapisey net — prodolzhajem."
fi

BRANCH="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD)"
COMMIT="$(git -C "$REPO_DIR" rev-parse --short HEAD)"
if [ "$BRANCH" = "HEAD" ]; then
    echo "ERROR: vy v detached HEAD, opredelite vetku." >&2
    exit 1
fi

echo "==> Deploy: vetka $BRANCH @ $COMMIT na $DEST_SSH:$DEST_DIR"

BUNDLE_NAME="mnema-deploy-$(date +%Y%m%d-%H%M%S).bundle"
LOCAL_BUNDLE="/tmp/$BUNDLE_NAME"
trap 'rm -f "$LOCAL_BUNDLE"' EXIT

echo "==> Bundle vetki $BRANCH @ $COMMIT..."
git -C "$REPO_DIR" bundle create "$LOCAL_BUNDLE" "$BRANCH"

echo "==> Kopirujem bundle na servershh..."
scp -q "$LOCAL_BUNDLE" "$DEST_SSH:/tmp/$BUNDLE_NAME"

echo "==> Na serve: fetch bundle + reset --hard + compose up -d --build..."
ssh "$DEST_SSH" bash -s <<EOF
set -euo pipefail
cd "$DEST_DIR"
# Ubirat' bundle s serve pri LYUBOM ishode, a ne tol'ko pri uspehe: ranshe
# `rm -f` stoyal v konce i pri padenii build/alembic/guard-otkaze
# /tmp/mnema-deploy-*.bundle napivalis' na serve (na disk 6 GB/5000 vstrech).
trap 'rm -f "/tmp/$BUNDLE_NAME"' EXIT

git fetch --quiet "/tmp/$BUNDLE_NAME" "refs/heads/$BRANCH"
git reset --hard --quiet FETCH_HEAD
echo "    kommit na serve: \$(git log -1 --oneline)"

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.gpu.yml -f docker-compose.override.yml"
if [ ! -f docker-compose.override.yml ]; then
    echo "    VNI MANIE: net docker-compose.override.yml, bez GPU-override" >&2
    COMPOSE="docker compose -f docker-compose.yml -f docker-compose.gpu.yml"
fi

# esli izmeneny requirements/docker — sobudet zanovo; inache keskh i nothing to do
\$COMPOSE build

# migratsii BDD do zapuska: create_all dobavlyaet tol'ko tablitsy, no ne kolonki
# v sushchestvuyushchikh (esli zabies' shag — Pydantic/SQLAlchemy padayut na
# "no such column" pri permom zhe chtenii Job/VoiceProfile)
echo "    alembic upgrade head..."
# </dev/null: bez nego `compose run` s"edaet ostavshiysya heredoc iz stdin
# (ssh bash -s) — shagi posle nego losevayutsya kak "up: command not found"
\$COMPOSE run --rm --no-deps api python -m alembic upgrade head </dev/null

# --force-recreate: bez nego `up -d` mohet ostavit' sluzhby na STAROM obraze,
# kogda izmeneny tol'ko kod/sloi, a konfiguratsiya compose ne menyalas'
# (proveryalos' na serve: obrazy peresobrany, a konteinery - 2-ndavney datchy).
# redis ne recreationim — on trigger, nego SSOT; persozdanie sbrosit ochered'.

# PVTORNAYA blokirovka: mezhdu lokal'noy proverkoy i etoy tochkoy lezhat
# bundle/scp/build/alembic (minyuty) — scheduler za eto vremya mog podnjat'
# vstrechu (prejoin 2 min). Sprashivaem API snaova, uze na serve; skripty
# berem iz working tree posle `reset --hard`. Politika ta zhe: aktivnyye ili
# neizvestnoye — stop; FORCE_DEPLOY raskryt zdes' lokal'no v tekst heredoc.
. "$DEST_DIR/scripts/deploy-guard-lib.sh"
SRV_JSON="\$(curl -sf --max-time 10 localhost:8000/api/jobs || true)"
SRV_LIST=""
SRV_RC=3
if [ -n "\$SRV_JSON" ]; then
    set +e
    SRV_LIST="\$(printf '%s' "\$SRV_JSON" | bash "\$DEST_DIR/scripts/check-active-jobs.sh" -)"
    SRV_RC=\$?
    set -e
fi
if guard_should_stop "\$SRV_RC" "${FORCE_DEPLOY:-0}"; then
    if [ "\$SRV_RC" = "1" ]; then
        echo "ERROR: zapisi nachalis' vo vremja sborki — ostanavlivaemsja pered force-recreate:" >&2
    else
        echo "ERROR: pered force-recreate ne udalos' proverit' zapisi (rc=\$SRV_RC) — stop." >&2
    fi
    printf '%s\n' "\$SRV_LIST" >&2
    echo "VNI MANIE: repo na serve uzhe obnoven (git reset --hard) i alembic" >&2
    echo "uzhe podnyat — konteinery poka rabotayut na STAROM obraze, eto bezopasno." >&2
    echo "Dokanchivajte zapisi i povtorite deploy: novele reset ne nuzhen, dostatochno" >&2
    echo "sborki i up -d (ili FORCE_DEPLOY=1, esli uvareny, cto zhivykh zapisey net)." >&2
    exit 1
fi

\$COMPOSE up -d --force-recreate api worker web-bot

echo "    proverka zapuska:"
\$COMPOSE ps --format "{{.Name}} {{.Status}}"
sleep 8
echo "    health: \$(curl -sf localhost:8000/health || echo 'NOT READY')"
rm -f "/tmp/$BUNDLE_NAME"
EOF

echo "==> Gotovo."
