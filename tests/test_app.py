"""Integration tests for the URL shortener.

main.py opens its Postgres and Redis connections at import time, so these
tests run against real services rather than mocks. That also means they
exercise the cache-aside behaviour for real: the assertions below check what
is actually in Redis, not just what the API returned.

Each test creates its own short code, so tests are independent and nothing
existing in the database or cache is modified.
"""

from concurrent.futures import ThreadPoolExecutor

import psycopg2
import pytest
from fastapi.testclient import TestClient

import main
from main import CACHE, app

EXAMPLE_URL = "https://example.com/some/page"


@pytest.fixture
def client():
    # follow_redirects=False so we can assert on the 302 itself.
    with TestClient(app, follow_redirects=False) as c:
        yield c


def shorten(client, url=EXAMPLE_URL):
    """Create a short link and return just its code."""
    response = client.post("/shorten", params={"url": url})
    assert response.status_code == 200
    return response.json()["short_url"].rsplit("/", 1)[1]


def test_shorten_returns_a_short_url(client):
    response = client.post("/shorten", params={"url": EXAMPLE_URL})

    assert response.status_code == 200
    short_url = response.json()["short_url"]
    assert short_url.startswith("http://localhost:8000/")
    assert len(short_url.rsplit("/", 1)[1]) == 7


def test_shorten_caches_the_url_immediately(client):
    """The code is written to Redis on creation, so the first visit is a hit."""
    code = shorten(client)

    assert CACHE.get(code) == EXAMPLE_URL
    assert 0 < CACHE.ttl(code) <= 60 * 60 * 24


def test_new_link_starts_with_no_clicks(client):
    code = shorten(client)

    response = client.get(f"/stats/{code}")

    assert response.status_code == 200
    assert response.json() == {"original_url": EXAMPLE_URL, "click_count": 0}


def test_redirect_points_at_the_original_url(client):
    code = shorten(client)

    response = client.get(f"/{code}")

    # 302, not 301: browsers cache permanent redirects and would stop
    # reaching the server, which would freeze click_count.
    assert response.status_code == 302
    assert response.headers["location"] == EXAMPLE_URL


def test_each_visit_counts_a_click(client):
    code = shorten(client)

    client.get(f"/{code}")
    client.get(f"/{code}")
    client.get(f"/{code}")

    assert client.get(f"/stats/{code}").json()["click_count"] == 3


def test_redirect_falls_back_to_postgres_and_refills_the_cache(client):
    """The cache-miss path: evict the key, then confirm the redirect still
    works and that Redis was backfilled from Postgres."""
    code = shorten(client)
    CACHE.delete(code)
    assert CACHE.get(code) is None

    response = client.get(f"/{code}")

    assert response.status_code == 302
    assert response.headers["location"] == EXAMPLE_URL
    assert CACHE.get(code) == EXAMPLE_URL


def test_clicks_are_counted_on_a_cache_miss_too(client):
    code = shorten(client)
    CACHE.delete(code)

    client.get(f"/{code}")

    assert client.get(f"/stats/{code}").json()["click_count"] == 1


def test_unknown_code_is_not_found(client):
    assert client.get("/stats/nosuchcode").status_code == 404
    assert client.get("/nosuchcode").status_code == 404


def test_two_links_get_different_codes(client):
    assert shorten(client, "https://example.com/one") != shorten(
        client, "https://example.com/two"
    )


def test_docs_are_served(client):
    """The catch-all GET /{code} route must not shadow the API docs."""
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


# --- URL validation ---------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "not a url",
        "example.com",  # no scheme
        "https://",  # no host
        "",
    ],
)
def test_bad_urls_are_rejected(client, url):
    assert client.post("/shorten", params={"url": url}).status_code == 422


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://example.com",
        "https://example.com/path?query=1#frag",
        "https://sub.example.co.uk:8443/deep/path",
    ],
)
def test_good_urls_are_accepted(client, url):
    assert client.post("/shorten", params={"url": url}).status_code == 200


def test_the_stored_url_is_not_rewritten(client):
    """Validation must not normalise the URL — the redirect has to be exact."""
    url = "https://example.com/path?b=2&a=1"
    code = shorten(client, url)

    assert client.get(f"/{code}").headers["location"] == url


# --- Short code collisions --------------------------------------------------


def test_shorten_retries_when_a_code_collides(client, monkeypatch):
    """Force make_code to hand back an already-used code, then a free one.

    The replacement code is generated rather than hardcoded, so re-running the
    suite against the same database cannot collide with a leftover row.
    """
    taken = shorten(client)
    replacement = main.make_code()
    monkeypatch.setattr(main, "make_code", iter([taken, replacement]).__next__)

    assert shorten(client) == replacement


def test_shorten_gives_up_after_too_many_collisions(client, monkeypatch):
    taken = shorten(client)
    monkeypatch.setattr(main, "make_code", lambda: taken)

    assert client.post("/shorten", params={"url": EXAMPLE_URL}).status_code == 500


# --- Connection pooling -----------------------------------------------------


def test_a_failed_query_does_not_break_later_requests(client):
    """Regression test for the single shared connection.

    A query that errors used to leave the connection in an aborted transaction,
    so every later request failed until the app restarted. db_cursor now rolls
    back before returning the connection to the pool.
    """
    with pytest.raises(psycopg2.Error):
        with main.db_cursor() as cur:
            cur.execute("SELECT * FROM a_table_that_does_not_exist")

    code = shorten(client)
    assert client.get(f"/stats/{code}").status_code == 200


def test_concurrent_requests_all_succeed(client):
    """Sync endpoints run in FastAPI's threadpool, so these really do overlap."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda _: shorten(client), range(24)))

    assert len(set(codes)) == 24
