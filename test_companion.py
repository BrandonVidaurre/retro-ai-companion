"""Tests for the parts that decide what the player actually sees.

Rendering is checked by eye with `--shots`; what is worth automating is the
retrieval behaviour, because that is where a silent regression looks exactly
like a working app with nothing in it.

Runs headless: SDL is forced to the dummy driver before companion is imported,
so this works over SSH and in CI with no display attached.
"""

import os
import sqlite3
import urllib.error

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import companion  # noqa: E402


@pytest.fixture(scope="module")
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    companion.seed(c)
    yield c
    c.close()


@pytest.fixture
def brain(conn):
    return companion.Brain(conn, companion.CloudProvider())


# ── the index itself ──────────────────────────────────────────────────

def test_seed_populates_every_table(conn):
    counts = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("systems", "games", "entries")}
    assert counts["systems"] == len(companion.SYSTEMS)
    assert counts["games"] == len(companion.GAMES)
    assert counts["entries"] == sum(len(g[4]) for g in companion.GAMES)


def test_every_system_has_a_searchable_perf_note(conn, brain):
    for _slug, name, _perf, _rating in companion.SYSTEMS:
        hits = brain.search(f"{name} performance")
        assert hits, f"no perf note reachable for {name}"


# ── query handling ────────────────────────────────────────────────────

def test_stopwords_are_stripped_but_meaning_survives(brain):
    assert brain._terms("how do i wall jump") == ["wall", "jump"]


def test_a_query_of_only_stopwords_still_yields_terms(brain):
    # kept-or-raw fallback: better to search junk than to search nothing
    assert brain._terms("what is the") != []


def test_system_shorthand_resolves(brain):
    """'n64' is not the string 'Nintendo 64' — the alias fold is what makes
    the single most-asked question work."""
    hits = brain.search("does n64 work")
    assert hits and "Nintendo 64" in hits[0]["title"]


def test_and_narrows_before_or_widens(brain):
    """Both terms present should beat a result that only matches one."""
    hits = brain.search("wall jump")
    assert hits[0]["title"] == "The wall jump"


def test_or_fallback_rescues_a_query_with_one_odd_word(brain):
    assert brain.search("wall jump qqqzzz") != []


def test_unmatchable_query_returns_empty_not_an_error(brain):
    assert brain.search("qqqzzz wwwxxx") == []


def test_fts_syntax_characters_do_not_raise(brain):
    for hostile in ['"', "*", "AND", "()", "NEAR(", "^", "-"]:
        brain.search(hostile)  # must not raise


# ── display fields ────────────────────────────────────────────────────

def test_shown_system_name_is_clean_not_the_alias_soup(brain):
    """`system` carries 'Nintendo 64 n64 nintendo64 ultra64' so shorthand
    matches; `sysname` is what the player is allowed to see."""
    hit = brain.search("does n64 work")[0]
    assert hit["sysname"] == "Nintendo 64"
    assert "ultra64" in hit["system"]


# ── the offline/cloud contract ────────────────────────────────────────

def test_answers_offline_when_no_cloud_is_configured(brain):
    ans = brain.ask("how do i wall jump")
    assert ans.source == "offline"
    assert "Super Metroid" in ans.text


def test_no_hits_and_no_cloud_explains_itself(brain):
    ans = brain.ask("qqqzzz wwwxxx")
    assert ans.source == "none"
    assert "GROQ_API_KEY" in ans.text


class _StubCloud(companion.CloudProvider):
    name = "stub"

    def __init__(self, reply=None, boom=None):
        self.reply, self.boom = reply, boom
        self.calls = 0

    def available(self):
        return True

    def ask(self, question, context):
        self.calls += 1
        if self.boom:
            raise self.boom
        return self.reply


def test_a_local_hit_never_spends_a_network_call(conn):
    """The whole design claim. A configured key must not change the answer to
    a question the index can already answer — on battery that is a second of
    radio for nothing, and it would make the handheld behave differently
    depending on whether Wi-Fi happened to be up."""
    cloud = _StubCloud(reply="a cloud answer")
    ans = companion.Brain(conn, cloud).ask("how do i wall jump")
    assert ans.source == "offline"
    assert cloud.calls == 0


def test_cloud_answers_are_labelled_as_cloud(conn):
    cloud = _StubCloud(reply="a cloud answer")
    ans = companion.Brain(conn, cloud).ask("qqqzzz wwwxxx")
    assert (ans.source, ans.text) == ("cloud", "a cloud answer")
    assert cloud.calls == 1


def test_cloud_failure_falls_back_to_the_offline_hit(conn, monkeypatch):
    """Reached when the index returned something, but not enough of it to
    stand on its own — GOOD_ENOUGH is the dial. A dead radio must degrade to
    the weak local hit rather than to an error."""
    monkeypatch.setattr(companion.Brain, "GOOD_ENOUGH", 5)
    cloud = _StubCloud(boom=urllib.error.URLError("no route to host"))
    ans = companion.Brain(conn, cloud).ask("how do i wall jump")
    assert ans.source == "offline"
    assert "Cloud unreachable" in ans.text
    assert "Super Metroid" in ans.text


def test_cloud_failure_with_no_offline_hit_says_so(conn):
    cloud = _StubCloud(boom=TimeoutError())
    ans = companion.Brain(conn, cloud).ask("qqqzzz wwwxxx")
    assert ans.source == "none"


# ── the DB is a cache, not state ──────────────────────────────────────

def test_a_stale_database_is_rebuilt_rather_than_half_working(tmp_path, monkeypatch):
    """An old companion.db lacking `sysname` must not degrade into a working
    UI with a permanently empty search."""
    db = tmp_path / "companion.db"
    monkeypatch.setattr(companion, "DB_PATH", str(db))

    old = sqlite3.connect(db)
    old.executescript(
        "CREATE VIRTUAL TABLE entries_fts USING fts5(title, body, game, system);")
    old.commit()
    old.close()

    fresh = companion.open_db()
    assert companion.Brain(fresh, companion.CloudProvider()).search("wall jump")
    fresh.close()
