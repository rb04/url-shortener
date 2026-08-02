import string, random, os
from contextlib import contextmanager
from urllib.parse import urlparse
import redis
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
import psycopg2
from psycopg2 import errors, pool

app = FastAPI()

# A pool rather than one shared connection: FastAPI runs these sync endpoints in
# a threadpool, so concurrent requests would otherwise contend over a single
# connection.
POOL = pool.ThreadedConnectionPool(
    1,
    10,
    os.environ.get("DATABASE_URL", "postgresql://user:password@localhost:5432/urlshortener"),
)
CACHE = redis.Redis(host=os.environ.get("REDIS_HOST", "localhost"), port=6379, decode_responses=True)
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
CACHE_TTL = 60 * 60 * 24  # 24 hours
CODE_ATTEMPTS = 5  # retries before giving up on a unique short code


@contextmanager
def db_cursor():
    """Borrow a connection from the pool, commit on success, roll back on error.

    The rollback matters: a failed query leaves its connection in an aborted
    transaction, so without it every later request that reused that connection
    would fail too.
    """
    conn = POOL.getconn()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        POOL.putconn(conn)


# Create table on startup
with db_cursor() as cur:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS urls (
            short_code TEXT PRIMARY KEY,
            original_url TEXT NOT NULL,
            click_count INT DEFAULT 0
        )
    """)

def make_code():
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=7))

def check_url(url: str):
    """Only allow http(s). Anything else could be used as a redirect to a
    javascript: or file: target."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=422, detail="url must be a valid http or https URL")

@app.post("/shorten")
def shorten(url: str):
    check_url(url)

    # Codes are random, so an INSERT can collide with one already stored.
    for _ in range(CODE_ATTEMPTS):
        code = make_code()
        try:
            with db_cursor() as cur:
                cur.execute("INSERT INTO urls (short_code, original_url) VALUES (%s, %s)", (code, url))
            break
        except errors.UniqueViolation:
            continue
    else:
        raise HTTPException(status_code=500, detail="Could not generate a unique short code")

    # Write to cache immediately so first visit is instant
    CACHE.set(code, url, ex=CACHE_TTL)
    return {"short_url": f"{BASE_URL}/{code}"}

@app.get("/stats/{code}")
def stats(code: str):
    with db_cursor() as cur:
        cur.execute("SELECT original_url, click_count FROM urls WHERE short_code = %s", (code,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return {"original_url": row[0], "click_count": row[1]}

@app.get("/{code}")
def redirect(code: str):
    # 1. Check cache first
    url = CACHE.get(code)

    if url:
        # Cache hit — skip the DB read entirely
        with db_cursor() as cur:
            cur.execute("UPDATE urls SET click_count = click_count + 1 WHERE short_code = %s", (code,))
        # 302, not 301: browsers cache permanent redirects and would stop
        # hitting the server, which would freeze click_count.
        return RedirectResponse(url=url, status_code=302)

    # 2. Cache miss — go to DB, and count the click in the same transaction
    with db_cursor() as cur:
        cur.execute("SELECT original_url FROM urls WHERE short_code = %s", (code,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Not found")
        cur.execute("UPDATE urls SET click_count = click_count + 1 WHERE short_code = %s", (code,))

    # 3. Write back to cache so next visit is fast
    CACHE.set(code, row[0], ex=CACHE_TTL)

    return RedirectResponse(url=row[0], status_code=302)
