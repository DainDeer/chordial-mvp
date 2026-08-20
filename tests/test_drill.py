"""the drill's own guardrails (scripts/drill.py, sol's #79 round).

the rig is a script, but its safety valve and its quota arithmetic are
pure functions - and both had real findings against them: the scratch
check matched 'drill' anywhere in the url (a password or query param
could authorize a production database), and the cap probe assumed the
cap divides evenly by the batch size (--cap 25 --batch-size 10 falsely
failed a correct server).
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

_spec = importlib.util.spec_from_file_location(
    "drill", REPO / "scripts" / "drill.py")
drill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drill)


def _refused(url):
    with pytest.raises(SystemExit):
        drill._require_scratch(url)


def test_scratch_marker_must_be_in_the_database_name():
    drill._require_scratch("sqlite:////tmp/chordial-drill-x/drill.db")
    drill._require_scratch("sqlite:///chordial-drill.db")
    drill._require_scratch("postgresql+psycopg://localhost/chordial_drill")
    _refused("postgresql://localhost/chordial_prod")


def test_drill_in_password_does_not_authorize_a_database():
    _refused("postgresql://drill:drillpass@prod-host/chordial")


def test_drill_in_query_params_does_not_authorize_a_database():
    _refused("postgresql://ops@prod-host/chordial?application_name=drill")


def test_drill_in_a_directory_does_not_authorize_a_sqlite_file():
    # the throwaway WORKDIR is named chordial-drill-*; only the db file's
    # own name counts, or any file in that directory would qualify
    _refused("sqlite:////tmp/chordial-drill-abc/chordial.db")


def test_unparseable_urls_are_refused_not_excused():
    _refused("not a url at all ://")


def test_cap_probe_expects_the_batch_atomic_landing():
    # the quota rejects a crossing batch whole, so uniform batches land
    # the largest batch-multiple under the cap - sol's repro: 25/10 -> 20
    assert drill._expected_cap_applied(25, 10) == 20
    assert drill._expected_cap_applied(1000, 100) == 1000
    assert drill._expected_cap_applied(99, 100) == 0
