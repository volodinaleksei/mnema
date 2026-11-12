"""Testy skripta check-active-jobs.sh (blokirovka deploy pri aktivnykh zapisjah).

Job 32 (06.11.2026): deploy `--force-recreate web-bot` uryval zhivoy brauzer
poserede zapisi. Skript proveryayet otvet GET /api/jobs i signalit ob aktivnykh
vstrechakh koda vykhoda: 0 — net, 1 — yest', 2 — vvod ne chitaetsya.
"""
import os
import subprocess
from pathlib import Path

os.environ.setdefault("ZOOM_PROFILE_DIR", "/tmp/test-zp")
os.environ.setdefault("SMTP_HOST", "localhost")
os.environ.setdefault("NOTIFY_EMAIL_TO", "ops@example.ru")
os.environ.setdefault("DATA_DIR", "/tmp/test-data")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/test-mnema.sqlite")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")

CHECK_SH = Path(__file__).resolve().parent.parent / "scripts" / "check-active-jobs.sh"


def run_check(stdin: str):
    return subprocess.run(
        ["bash", str(CHECK_SH), "-"],
        input=stdin, capture_output=True, text=True,
    )


def job(jid, status, topic="Vstrecha"):
    return ('{"id": %d, "status": "%s", "topic": "%s", "start_at": "2026-11-06T09:30:00"}'
            % (jid, status, topic))


def test_no_active_returns_zero():
    res = run_check("[%s,%s,%s]" % (job(1, "done"), job(2, "scheduled"), job(3, "cancelled")))
    assert res.returncode == 0
    assert res.stdout.strip() == ""


def test_recording_returns_one_and_lists_it():
    res = run_check("[%s,%s]" % (job(32, "recording", "Ohrana truda"), job(33, "done")))
    assert res.returncode == 1
    assert "#32 recording" in res.stdout
    assert "Ohrana truda" in res.stdout


def test_launching_counts_as_active():
    res = run_check("[%s]" % job(5, "launching"))
    assert res.returncode == 1
    assert "#5 launching" in res.stdout


def test_uploading_counts_as_active():
    res = run_check("[%s]" % job(6, "uploading"))
    assert res.returncode == 1


def test_processing_counts_as_active():
    # P1-2: deploy force-recreate'it worker — uryvayet ASR-na-GPU posered konveyera
    res = run_check("[%s,%s,%s]" % (job(7, "processing"), job(8, "awaiting_review"), job(9, "awaiting_llm")))
    assert res.returncode == 1
    assert "#7 processing" in res.stdout


def test_empty_list_returns_zero():
    assert run_check("[]").returncode == 0


def test_garbage_input_returns_two():
    res = run_check("<html>502 Bad Gateway</html>")
    assert res.returncode == 2


def test_json_object_instead_of_list_returns_two():
    res = run_check('{"detail": "Method Not Allowed"}')
    assert res.returncode == 2


def test_non_dict_items_are_ignored():
    res = run_check('[1, null, "recording", %s]' % job(10, "done"))
    assert res.returncode == 0


# --- politika blokirovki deploy-guard-lib.sh -------------------------------

GUARD_LIB = Path(__file__).resolve().parent.parent / "scripts" / "deploy-guard-lib.sh"


def should_stop(rc: str, force: str) -> bool:
    res = subprocess.run(
        ["bash", "-c",
         '. "%s"; guard_should_stop "%s" "%s"; echo $?' % (GUARD_LIB, rc, force)],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout.strip().endswith("0")


def test_guard_no_active_allows_deploy():
    assert should_stop("0", "0") is False
    assert should_stop("0", "1") is False


def test_guard_active_blocks_deploy():
    assert should_stop("1", "0") is True


def test_guard_active_allowed_with_force():
    assert should_stop("1", "1") is False


def test_guard_unknown_state_blocks_deploy():
    # neizvestnoye sostojanie (net otveta / garbage / net pythona) — stop
    for rc in ("2", "3", "127", "x"):
        assert should_stop(rc, "0") is True, rc


def test_guard_unknown_state_allowed_only_with_force():
    for rc in ("2", "3", "127"):
        assert should_stop(rc, "1") is False, rc
