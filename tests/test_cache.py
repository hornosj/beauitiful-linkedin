from beautiful_linkedin.cache.sqlite_cache import SqliteJsonCache


def test_sqlite_json_cache_round_trip(tmp_path):
    cache = SqliteJsonCache(tmp_path / "cache.sqlite")

    cache.set_json("provider", {"query": "abc"}, {"ok": True})

    assert cache.get_json("provider", {"query": "abc"}) == {"ok": True}
    assert cache.get_json("provider", {"query": "other"}) is None
