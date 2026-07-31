import string, random, os
import redis
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
import psycopg2

app = FastAPI()

DB = psycopg2.connect(os.environ.get("DATABASE_URL", "postgresql://user:password@localhost:5432/urlshortener"))
CACHE = redis.Redis(host=os.environ.get("REDIS_HOST", "localhost"), port=6379, decode_responses=True)
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
CACHE_TTL = 60 * 60 * 24  # 24 hours

# Create table on startup
with DB.cursor() as cur:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS urls (
            short_code TEXT PRIMARY KEY,
            original_url TEXT NOT NULL,
            click_count INT DEFAULT 0
        )
    """)
    DB.commit()

def make_code():
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=7))

@app.post("/shorten")
def shorten(url: str):
    code = make_code()
    with DB.cursor() as cur:
        cur.execute("INSERT INTO urls (short_code, original_url) VALUES (%s, %s)", (code, url))
        DB.commit()
    # Write to cache immediately so first visit is instant
    CACHE.setex(code, CACHE_TTL, url)
    return {"short_url": f"{BASE_URL}/{code}"}

@app.get("/stats/{code}")
def stats(code: str):
    with DB.cursor() as cur:
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
        with DB.cursor() as cur:
            cur.execute("UPDATE urls SET click_count = click_count + 1 WHERE short_code = %s", (code,))
            DB.commit()
        # 302, not 301: browsers cache permanent redirects and would stop
        # hitting the server, which would freeze click_count.
        return RedirectResponse(url=url, status_code=302)

    # 2. Cache miss — go to DB
    with DB.cursor() as cur:
        cur.execute("SELECT original_url FROM urls WHERE short_code = %s", (code,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")

    # 3. Write back to cache so next visit is fast
    CACHE.setex(code, CACHE_TTL, row[0])

    with DB.cursor() as cur:
        cur.execute("UPDATE urls SET click_count = click_count + 1 WHERE short_code = %s", (code,))
        DB.commit()

    return RedirectResponse(url=row[0], status_code=302)
