# URL Shortener

[![CI](https://github.com/rb04/url-shortener/actions/workflows/ci.yml/badge.svg)](https://github.com/rb04/url-shortener/actions/workflows/ci.yml)

A URL shortening service built with **FastAPI**, **PostgreSQL**, and **Redis**, using the
**cache-aside** pattern so that repeat lookups of a short code skip the database entirely.

## How it works

```
POST /shorten          ──> generate 7-char code ──> INSERT into Postgres ──> write to Redis (TTL 24h)
GET  /{code}           ──> Redis GET ──┬─ hit  ──> 302 redirect (no DB read)
                                       └─ miss ──> Postgres SELECT ──> backfill Redis ──> 302 redirect
GET  /stats/{code}     ──> Postgres SELECT ──> {original_url, click_count}
```

Redis is the read-through cache; Postgres is the source of truth. A code is written to Redis
at creation time, so even the *first* visit is a cache hit. On a cache miss the value is read
from Postgres and backfilled into Redis with a 24-hour TTL.

## Endpoints

| Method | Path            | Description                                  |
| ------ | --------------- | -------------------------------------------- |
| `POST` | `/shorten?url=` | Create a short code for `url`                |
| `GET`  | `/{code}`       | Redirect to the original URL, count the click |
| `GET`  | `/stats/{code}` | Return the original URL and click count      |

Interactive API docs: <http://localhost:8000/docs>

## Requirements

- Python 3.9+
- PostgreSQL and Redis (via Docker, or installed locally)

## Running it

### Option A — Docker Compose (recommended)

Starts Postgres on `5432` and Redis on `6379`, which match the defaults in `main.py`,
so no environment variables are needed.

```bash
docker compose up -d

pip install -r requirements.txt
python3 -m uvicorn main:app --reload
```

### Option B — Local Postgres and Redis

```bash
brew install postgresql@16 redis
brew services start postgresql@16
brew services start redis
```

Then create the role and database the app expects:

```bash
psql -d postgres -c "CREATE ROLE \"user\" WITH LOGIN PASSWORD 'password' CREATEDB;"
psql -d postgres -c "CREATE DATABASE urlshortener OWNER \"user\";"
```

Install dependencies and run:

```bash
pip install -r requirements.txt
python3 -m uvicorn main:app --reload
```

Open <http://localhost:8000/docs>.

## Configuration

All configuration is read from environment variables, with local-friendly defaults
(see `.env.example`):

| Variable       | Default                                                  |
| -------------- | -------------------------------------------------------- |
| `DATABASE_URL` | `postgresql://user:password@localhost:5432/urlshortener`  |
| `REDIS_HOST`   | `localhost`                                              |
| `BASE_URL`     | `http://localhost:8000`                                  |

## Trying it out

```bash
# Create a short link
curl -X POST "http://localhost:8000/shorten?url=https://example.com/hello"
# => {"short_url":"http://localhost:8000/0eO9dHN"}

# Follow it (-i to see the redirect instead of following it)
curl -i "http://localhost:8000/0eO9dHN"
# => HTTP/1.1 302 Found
#    location: https://example.com/hello

# Check the stats
curl "http://localhost:8000/stats/0eO9dHN"
# => {"original_url":"https://example.com/hello","click_count":1}
```

To confirm the cache is actually being used, delete the key and watch it get backfilled
from Postgres on the next request:

```bash
redis-cli del 0eO9dHN
curl -i "http://localhost:8000/0eO9dHN"   # served from Postgres, then re-cached
redis-cli get 0eO9dHN                     # => "https://example.com/hello"
```

## Tests

`main.py` opens its Postgres and Redis connections at import time, so the suite runs as
integration tests against real services. That is deliberate: it means the tests verify the
actual cache-aside behaviour, including what ends up in Redis, rather than asserting against
mocks. Each test creates its own short code, so tests are independent and nothing already in
the database or cache is touched.

With Postgres and Redis running:

```bash
pip install -r requirements-dev.txt
pytest -v
```

CI runs the same suite on Python 3.9 and 3.12 against `postgres:16` and `redis:7` service
containers on every push and pull request — see [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Troubleshooting

**Port 5432 already in use.** Another Postgres (for example one installed from the official
`.dmg`, which runs as a launch daemon) may already own that port. Either stop it, or run your
cluster on a different port and point the app at it:

```bash
pg_ctl -D /opt/homebrew/var/postgresql@16 -o "-p 5433" start
export DATABASE_URL="postgresql://user:password@localhost:5433/urlshortener"
```

**Postgres fails to start with `postmaster became multithreaded during startup`.**
Set a locale before starting it:

```bash
export LC_ALL="en_US.UTF-8"
```

**Redis aborts with `Can't load module ./modules/redisbloom/redisbloom.so`.**
Some Homebrew builds ship a `redis.conf` that references bundled modules by *relative* path.
This app needs no Redis modules, so start Redis without that config file:

```bash
redis-server --port 6379 --dir /opt/homebrew/var/db/redis --daemonize yes
```

## Project layout

```
main.py                     FastAPI app: routes, Postgres + Redis wiring, table bootstrap
tests/test_app.py           Integration tests covering both cache paths
docker-compose.yml          Postgres 16 and Redis 7 for local development
requirements.txt            Runtime dependencies
requirements-dev.txt        Runtime dependencies plus the test tooling
pytest.ini                  Pytest configuration
.env.example                Documented environment variables
.github/workflows/ci.yml    Test matrix run on every push and pull request
```

## Known limitations

Deliberately out of scope for this version, and the natural next steps:

- **One shared database connection.** `main.py` opens a single module-level `psycopg2`
  connection, which is not suited to concurrent requests; a connection pool is the fix.
- **Short codes are not checked for collisions.** Codes are random, so an `INSERT` could in
  principle collide with an existing primary key and fail; a retry loop would handle it.
- **URLs are not validated.** Any string is accepted and later used as a redirect target.
