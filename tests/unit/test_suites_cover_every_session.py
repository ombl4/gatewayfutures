"""Every active session belongs to at least one suite, and every suite entry resolves."""

from gf.config import ROOT
from gf.sessions.schema import load_all
from gf.sessions.taxonomy import load_suites, session_file, sessions_in_suite


def test_every_active_session_is_in_a_suite_and_every_entry_resolves():
    folder = ROOT / "sessions"
    suites = load_suites(folder)
    listed = {name for files in suites.values() for name in files}
    active = {session_file(s) for s in load_all(folder, include_retired=False)}
    assert active - listed == set(), f"sessions in no suite: {sorted(active - listed)}"
    for name, files in suites.items():
        resolved = {session_file(s) for s in sessions_in_suite(folder, name)}
        assert resolved == set(files), f"suite {name}: unresolved {sorted(set(files) - resolved)}"
    assert set(suites) >= {
        "smoke",
        "regression",
        "core",
        "edge",
        "personas",
        "adversarial",
        "faults",
        "denial",
    }
