import hashlib
import json
import math
import re
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pandas as pd

from xtream import ACTIONS, CatalogError
from platforms import platform_name


PERFORMANCE_TIMINGS: ContextVar[dict[str, float] | None] = ContextVar("catalog_performance_timings", default=None)


def record_timing(name: str, started: float) -> None:
    timings = PERFORMANCE_TIMINGS.get()
    if timings is not None:
        timings[name] = timings.get(name, 0) + (time.perf_counter() - started) * 1000


KINDS = {
    "movies": ("movies", "vod_categories", "stream_id"),
    "series": ("series", "series_categories", "series_id"),
}
SORT_COLUMNS = {
    "name": "CASEFOLD(s.name)",
    "category_name": "CASEFOLD(COALESCE(c.category_name, 'Sin categoría'))",
    "year": "CAST(NULLIF(s.year, '') AS INTEGER)",
    "rating": "s.rating",
}
CATEGORY_TABLES = ("vod_categories", "series_categories")


def excluded_category(name):
    name = "".join(c for c in unicodedata.normalize("NFKD", text(name)).casefold() if not unicodedata.combining(c))
    return bool(re.search(r"\b(?:adultos?|adult|xxx|terror|horror)\b", name))


def category_memberships(row):
    secondary = row.get("category_ids", [])
    if not isinstance(secondary, list):
        secondary = [secondary]
    return {text(value) for value in [row.get("category_id"), *secondary] if value is not None}


CHILDREN_CATEGORIES = {
    "movies": {"INFANTIL", "ANIMACION", "CAST. ANIMACION", "ANIME"},
    "series": {"S INFANTIL", "S. Animacion", "S ANIME"},
}

PROVIDER_ACCESS = {"all", "flatrate", "rent", "buy", "free", "ads"}
TMDB_STATUSES = {"all", "matched", "candidates", "missing", "pending"}
MOVIE_ERAS = {
    "recent": ("Recientes", "2015 en adelante"),
    "modern": ("Modernas", "2000–2014"),
    "nostalgia": ("Nostalgia", "1980–1999"),
    "classic": ("Clásicas", "Antes de 1980"),
    "unknown": ("Sin año conocido", ""),
}
SERVICES_PATH = Path(__file__).resolve().with_name("services.json")


def fingerprint(data):
    fields = {key: data.get(key) for key in ("name", "title", "year", "release_date", "releaseDate")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def youtube_trailer_url(value):
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{11}", value.strip()):
        return f"https://www.youtube.com/watch?v={value.strip()}"
    return None


def valid_image(candidate):
    if not isinstance(candidate, str) or not candidate.strip():
        return None
    try:
        parts = urlsplit(candidate.strip())
        valid = parts.scheme in {"http", "https"} and parts.hostname and not parts.username and not parts.password
    except ValueError:
        valid = False
    return candidate.strip() if valid else None


def non_latin_title(value):
    return any(
        character.isalpha() and "LATIN" not in unicodedata.name(character, "")
        for character in unicodedata.normalize("NFKD", text(value))
    )


def people_names(value):
    values = value if isinstance(value, list) else [value]
    names, seen = [], set()
    for item in values:
        if not isinstance(item, str):
            continue
        for part in item.split(","):
            name = part.strip()
            if name and name.casefold() not in seen:
                seen.add(name.casefold())
                names.append(name)
    return names


def effective_people(raw, payload):
    tmdb = json.loads(payload) if payload else {}
    cast = [person["name"] for person in tmdb.get("cast", []) if person.get("name", "").strip()]
    directors = people_names(tmdb.get("directors", []))
    return json.dumps({
        "cast": cast,
        "director": directors,
        "creators": people_names(tmdb.get("creators", [])),
    }, ensure_ascii=False)


def text(value):
    return "" if value is None else str(value)


def identifier(value, context):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise CatalogError(f"{context}: identificador ausente o inválido.")
    return str(value)


def rating(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def release_year(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    value = str(value).strip()
    if re.fullmatch(r"[1-9][0-9]{3}", value):
        return int(value)
    if re.fullmatch(r"[1-9][0-9]{3}-[0-9]{2}-[0-9]{2}", value):
        try:
            return datetime.strptime(value, "%Y-%m-%d").year
        except ValueError:
            return None
    return None


class CatalogStore:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.duplicates = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode != "wal":
                raise sqlite3.OperationalError("No se pudo activar WAL para el catálogo SQLite.")
            for table in CATEGORY_TABLES:
                db.execute(f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        category_id TEXT PRIMARY KEY,
                        category_name TEXT NOT NULL,
                        parent_id TEXT,
                        raw_json TEXT NOT NULL
                    )
                """)
            for table, _, key in KINDS.values():
                db.execute(f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        {key} TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        category_id TEXT,
                        icon TEXT,
                        rating REAL,
                        year TEXT,
                        added TEXT,
                        container_extension TEXT,
                        genre TEXT,
                        release_date TEXT,
                        last_modified TEXT,
                        raw_json TEXT NOT NULL
                    )
                """)
                db.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_category ON {table}(category_id)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS sync_metadata (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    synced_at TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS maintenance_updates (
                    operation TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS tmdb_metadata (
                    kind TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    country TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (kind, record_id, country)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS tmdb_recommendations (
                    kind TEXT NOT NULL,
                    tmdb_id INTEGER NOT NULL,
                    checked_at REAL NOT NULL,
                    ids TEXT NOT NULL,
                    PRIMARY KEY (kind, tmdb_id)
                )
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_tmdb_recommendation_id
                ON tmdb_metadata(kind, country, json_extract(payload, '$.id'))
                WHERE json_extract(payload, '$.status')='matched'
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS sync_excluded_categories (
                    category_table TEXT NOT NULL,
                    category_id TEXT NOT NULL,
                    PRIMARY KEY (category_table, category_id)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS tmdb_provider_directory (
                    country TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    PRIMARY KEY (country, kind, name)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS watched_movies (
                    record_id TEXT PRIMARY KEY,
                    watched_at TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS banned_movies (
                    record_id TEXT PRIMARY KEY,
                    banned_at TEXT NOT NULL
                )
            """)

            db.execute("""
                CREATE TABLE IF NOT EXISTS watchlist (
                    kind TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    saved_at TEXT NOT NULL,
                    PRIMARY KEY (kind, record_id)
                )
            """)

    def set_watchlisted(self, kind, record_id, enabled):
        if kind not in KINDS or not isinstance(enabled, bool):
            raise ValueError("Estado de lista inválido.")
        table, _, key = KINDS[kind]
        with self.connect() as db:
            if db.execute(f"SELECT 1 FROM {table} WHERE {key}=?", (record_id,)).fetchone() is None:
                return False
            if enabled:
                db.execute("INSERT OR IGNORE INTO watchlist VALUES (?, ?, ?)",
                           (kind, record_id, datetime.now(timezone.utc).isoformat(timespec="seconds")))
            else:
                db.execute("DELETE FROM watchlist WHERE kind=? AND record_id=?", (kind, record_id))
        return True

    def set_movie_watched(self, record_id, watched):
        if not isinstance(watched, bool):
            raise ValueError("El estado de vista debe ser booleano.")
        return self._set_movie_state("watched_movies", record_id, watched)

    def set_movie_banned(self, record_id, banned):
        if not isinstance(banned, bool):
            raise ValueError("El estado de bloqueo debe ser booleano.")
        return self._set_movie_state("banned_movies", record_id, banned)

    def _set_movie_state(self, table, record_id, enabled):
        with self.connect() as db:
            if db.execute("SELECT 1 FROM movies WHERE stream_id=?", (record_id,)).fetchone() is None:
                return False
            if enabled:
                db.execute(
                    f"INSERT OR IGNORE INTO {table} VALUES (?, ?)",
                    (record_id, datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
            else:
                db.execute(f"DELETE FROM {table} WHERE record_id=?", (record_id,))
        return True

    def subscriptions(self):
        try:
            config = json.loads(SERVICES_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CatalogError("No se pudo leer services.json. Revisa su formato y permisos.") from exc
        names = config.get("services") if isinstance(config, dict) else None
        if not isinstance(names, list) or len(names) > 100 or any(
            not isinstance(name, str) or not name.strip() or len(name) > 200 for name in names
        ):
            raise CatalogError("services.json debe contener una lista services con nombres de plataformas válidos.")
        return sorted({platform_name(name) for name in names})

    def save_provider_directory(self, directories, country="CA"):
        with self.connect() as db:
            for kind, names in directories.items():
                if kind not in {"movies", "series"}:
                    raise ValueError("Tipo de catálogo inválido.")
                db.execute("DELETE FROM tmdb_provider_directory WHERE country=? AND kind=?", (country, kind))
                db.executemany("INSERT INTO tmdb_provider_directory VALUES (?, ?, ?)",
                               [(country, kind, name) for name in sorted(set(names))])
                db.execute(
                    "INSERT OR REPLACE INTO maintenance_updates VALUES (?, ?)",
                    (f"platforms:{country}:{kind}", datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )

    def maintenance_dates(self, kind, country="CA"):
        with self.connect() as db:
            synced = db.execute("SELECT synced_at FROM sync_metadata WHERE id=1").fetchone()
            platforms = db.execute(
                "SELECT updated_at FROM maintenance_updates WHERE operation=?",
                (f"platforms:{country}:{kind}",),
            ).fetchone()
            tmdb = db.execute(
                "SELECT MAX(json_extract(payload, '$.checked_at')) FROM tmdb_metadata WHERE kind=? AND country=?",
                (kind, country),
            ).fetchone()
        return {
            "catalog": synced[0] if synced else None,
            "platforms": platforms[0] if platforms else None,
            "tmdb": tmdb[0],
        }

    def provider_names(self, kind, country="CA"):
        table, _, key = KINDS[kind]
        with self.connect() as db:
            names = {row[0] for row in db.execute(
                "SELECT name FROM tmdb_provider_directory WHERE country=? AND kind=?", (country, kind),
            )}
            names.update(row[0] for row in db.execute(f"""
                SELECT DISTINCT provider.value
                FROM {table} s JOIN tmdb_metadata m ON m.kind=? AND m.record_id=s.{key}
                    AND m.country=? AND m.fingerprint=TITLE_FINGERPRINT(s.raw_json),
                    json_each(m.payload, '$.providers') access,
                    json_each(access.value) provider
                WHERE json_extract(m.payload, '$.status')='matched'
            """, (kind, country)))
        return sorted({platform_name(name) for name in names}, key=str.casefold)

    def tmdb_coverage(self, kind, country="CA"):
        table, _, key = KINDS[kind]
        with self.connect() as db:
            row = db.execute(f"""
                SELECT COUNT(*) AS total, COUNT(m.record_id) AS checked,
                    COALESCE(SUM(json_extract(m.payload, '$.status')='matched'), 0) AS matched,
                    COALESCE(SUM(json_extract(m.payload, '$.status')='candidates'), 0) AS candidates,
                    COALESCE(SUM(json_extract(m.payload, '$.status')='missing'), 0) AS missing
                FROM {table} s LEFT JOIN tmdb_metadata m ON m.kind=? AND m.record_id=s.{key}
                    AND m.country=? AND m.fingerprint=TITLE_FINGERPRINT(s.raw_json)
            """, (kind, country)).fetchone()
        return {**dict(row), "pending": row["total"] - row["checked"]}

    def tmdb_pending(self, kind, limit, country="CA"):
        table, _, key = KINDS[kind]
        with self.connect() as db:
            return [row[0] for row in db.execute(f"""
                SELECT s.{key} FROM {table} s LEFT JOIN tmdb_metadata m
                    ON m.kind=? AND m.record_id=s.{key} AND m.country=?
                    AND m.fingerprint=TITLE_FINGERPRINT(s.raw_json)
                WHERE m.record_id IS NULL ORDER BY CASEFOLD(s.name), s.{key} LIMIT ?
            """, (kind, country, limit))]

    def tmdb_cached(self, kind, record_id, country, fingerprint):
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM tmdb_metadata WHERE kind=? AND record_id=? AND country=? AND fingerprint=?",
                (kind, record_id, country, fingerprint),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def save_tmdb(self, kind, record_id, country, fingerprint, payload):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO tmdb_metadata VALUES (?, ?, ?, ?, ?)",
                (kind, record_id, country, fingerprint, json.dumps(payload, ensure_ascii=False)),
            )

    def cached_recommendations(self, kind, tmdb_id, since):
        with self.connect() as db:
            row = db.execute(
                "SELECT ids FROM tmdb_recommendations WHERE kind=? AND tmdb_id=? AND checked_at>=?",
                (kind, tmdb_id, since),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def save_recommendations(self, kind, tmdb_id, ids, checked_at):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO tmdb_recommendations VALUES (?, ?, ?, ?)",
                (kind, tmdb_id, checked_at, json.dumps(ids)),
            )

    def recommended_titles(self, kind, tmdb_id, ids):
        table, _, key = KINDS[kind]
        # CROSS JOIN keeps the small ID lists ahead of costly identity validation.
        banned = """AND json_extract(m.payload, '$.id') NOT IN (
            SELECT json_extract(blocked.payload, '$.id') FROM banned_movies b
            CROSS JOIN tmdb_metadata blocked
            CROSS JOIN movies original
            WHERE blocked.record_id=b.record_id
                AND blocked.kind='movies' AND blocked.country='CA'
                AND original.stream_id=b.record_id
                AND blocked.fingerprint=TITLE_FINGERPRINT(original.raw_json)
                AND json_extract(blocked.payload, '$.status')='matched'
                AND json_type(blocked.payload, '$.id')='integer'
        )""" if kind == "movies" else ""
        with self.connect() as db:
            rows = db.execute(f"""
                SELECT s.{key} AS id, s.name, s.raw_json, m.payload,
                    EXISTS (SELECT 1 FROM watchlist saved WHERE saved.kind=m.kind
                        AND saved.record_id=s.{key}) AS watchlisted,
                    json_extract(m.payload, '$.id') AS tmdb_id
                FROM json_each(?) recommended
                CROSS JOIN tmdb_metadata m
                JOIN {table} s ON s.{key}=m.record_id
                WHERE m.kind=? AND m.country='CA'
                    AND json_extract(m.payload, '$.status')='matched'
                    AND json_extract(m.payload, '$.id')=recommended.value
                    AND m.fingerprint=TITLE_FINGERPRINT(s.raw_json)
                    AND json_extract(m.payload, '$.id')!=? {banned}
                ORDER BY CAST(recommended.key AS INTEGER), CASEFOLD(s.name), s.{key}
            """, (json.dumps(ids), kind, tmdb_id)).fetchall()
        items, seen = [], set()
        for row in rows:
            if row["tmdb_id"] in seen:
                continue
            seen.add(row["tmdb_id"])
            raw, metadata = json.loads(row["raw_json"]), json.loads(row["payload"])
            items.append({
                "id": row["id"], "name": row["name"],
                "watchlisted": bool(row["watchlisted"]),
                "poster_url": valid_image(raw.get("stream_icon")) or valid_image(raw.get("cover")),
                "year": release_year(metadata.get("release_date")),
            })
            if len(items) == 6:
                break
        return items

    @contextmanager
    def connect(self):
        started = time.perf_counter()
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.create_function("NON_LATIN_TITLE", 1, non_latin_title, deterministic=True)
        db.create_function("CASEFOLD", 1, lambda value: text(value).casefold(), deterministic=True)
        db.create_function("TITLE_FINGERPRINT", 1, lambda raw: fingerprint(json.loads(raw)), deterministic=True)
        db.create_function("PLATFORM_NAME", 1, platform_name, deterministic=True)
        db.create_function("RELEASE_YEAR", 1, release_year, deterministic=True)
        db.create_function("PEOPLE_METADATA", 2, effective_people, deterministic=True)
        db.create_function(
            "HAS_GENRE", 2,
            lambda value, genre: genre.casefold() in {name.casefold() for name in people_names(value)},
            deterministic=True,
        )
        db.create_function(
            "HAS_PERSON", 2,
            lambda raw, name: name.casefold() in {
                person.casefold()
                for field in ("cast", "director", "creators")
                for person in people_names(json.loads(raw).get(field))
            }, deterministic=True,
        )
        committing = None
        try:
            with db:
                try:
                    yield db
                finally:
                    committing = time.perf_counter()
        finally:
            if committing is not None:
                record_timing("commit", committing)
            db.close()
            record_timing("sqlite", started)

    def replace(self, snapshot):
        return self._write_snapshot(snapshot)

    def _write_snapshot(self, snapshot, incremental=False):
        if not set(ACTIONS).issubset(snapshot) or set(snapshot) - set(ACTIONS) - {"live_categories", "live_streams"}:
            raise CatalogError("La descarga no contiene las cuatro colecciones de películas y series requeridas.")
        # Una única transacción conserva el catálogo anterior si falla cualquier colección.
        duplicates = {}
        report = {table: dict(new=0, updated=0, unchanged=0, excluded=0, removed_excluded=0) for table in ACTIONS}
        with self.connect() as db:
            blocked = {}
            if incremental:
                # Resolve exclusions against both saved and incoming categories.
                for table in CATEGORY_TABLES:
                    if not isinstance(snapshot[table], list):
                        raise CatalogError(f"{table}: formato de colección inválido.")
                    blocked[table] = {
                        row["category_id"] for row in db.execute(f"SELECT category_id, category_name FROM {table}")
                        if excluded_category(row["category_name"])
                    }
                    blocked[table].update(row[0] for row in db.execute(
                        "SELECT category_id FROM sync_excluded_categories WHERE category_table=?", (table,),
                    ))
                    for row in snapshot[table]:
                        if not isinstance(row, dict):
                            raise CatalogError(f"{table}: registro inválido.")
                        row_id = identifier(row.get("category_id"), table)
                        if excluded_category(row.get("category_name")):
                            blocked[table].add(row_id)
                    db.executemany(
                        "INSERT OR IGNORE INTO sync_excluded_categories VALUES (?, ?)",
                        [(table, row_id) for row_id in blocked[table]],
                    )
                category_tables = {table: categories for table, categories, _ in KINDS.values()}
                for table in ACTIONS:
                    key = "category_id" if table in CATEGORY_TABLES else "series_id" if table == "series" else "stream_id"
                    ids = blocked[table if table in CATEGORY_TABLES else category_tables[table]]
                    for row in db.execute(f"SELECT {key}, raw_json FROM {table}").fetchall():
                        data = json.loads(row["raw_json"])
                        forbidden = row[key] in ids if table in CATEGORY_TABLES else bool(category_memberships(data) & ids)
                        if forbidden:
                            db.execute(f"DELETE FROM {table} WHERE {key}=?", (row[key],))
                            report[table]["removed_excluded"] += 1
            for table in ACTIONS:
                if not incremental:
                    db.execute(f"DELETE FROM {table}")
                rows = snapshot[table]
                if not isinstance(rows, list):
                    raise CatalogError(f"{table}: formato de colección inválido.")
                seen = set()
                for row in rows:
                    if not isinstance(row, dict):
                        raise CatalogError(f"{table}: registro inválido.")
                    key_name = (
                        "category_id" if table in CATEGORY_TABLES
                        else "series_id" if table == "series" else "stream_id"
                    )
                    row_id = identifier(row.get(key_name), table)
                    # Algunos proveedores repiten IDs: se conserva el último registro.
                    if row_id in seen:
                        duplicates[table] = duplicates.get(table, 0) + 1
                    seen.add(row_id)
                    raw = json.dumps(row, ensure_ascii=False, allow_nan=False)
                    if incremental:
                        ids = blocked[table if table in CATEGORY_TABLES else category_tables[table]]
                        forbidden = row_id in ids if table in CATEGORY_TABLES else bool(category_memberships(row) & ids)
                        if forbidden:
                            report[table]["excluded"] += 1
                            if table not in CATEGORY_TABLES:
                                removed = db.execute(f"DELETE FROM {table} WHERE {key_name}=?", (row_id,)).rowcount
                                report[table]["removed_excluded"] += removed
                            continue
                        old = db.execute(f"SELECT raw_json FROM {table} WHERE {key_name}=?", (row_id,)).fetchone()
                        if old is not None and json.loads(old[0]) == row:
                            report[table]["unchanged"] += 1
                            continue
                        report[table]["updated" if old else "new"] += 1
                    if table in CATEGORY_TABLES:
                        db.execute(
                            f"INSERT OR REPLACE INTO {table} VALUES (?, ?, ?, ?)",
                            (
                                identifier(row.get("category_id"), table),
                                text(row.get("category_name")),
                                text(row.get("parent_id")),
                                raw,
                            ),
                        )
                    else:
                        key = "series_id" if table == "series" else "stream_id"
                        db.execute(
                            f"INSERT OR REPLACE INTO {table} VALUES ({','.join('?' for _ in range(12))})",
                            (
                                identifier(row.get(key), table),
                                text(row.get("name")),
                                text(row.get("category_id")),
                                text(row.get("cover") if table == "series" else row.get("stream_icon")),
                                rating(row.get("rating")),
                                text(row.get("year")),
                                text(row.get("added")),
                                text(row.get("container_extension")),
                                text(row.get("genre")),
                                text(row.get("releaseDate") or row.get("release_date")),
                                text(row.get("last_modified")),
                                raw,
                            ),
                        )
            db.execute(
                "INSERT OR REPLACE INTO sync_metadata VALUES (1, ?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
            )
            if incremental:
                db.execute("DROP TABLE IF EXISTS live_streams")
                db.execute("DROP TABLE IF EXISTS live_categories")
                db.execute("DELETE FROM sync_excluded_categories WHERE category_table='live_categories'")
        if incremental:
            self.sync_report = report
        return duplicates

    def sync(self, client):
        snapshot = client.download()
        try:
            self.duplicates = self._write_snapshot(snapshot, incremental=True)
        except (sqlite3.IntegrityError, TypeError, ValueError) as exc:
            raise CatalogError(
                "Catálogo inválido (datos no serializables); no se aplicaron cambios."
            ) from exc
        with self.connect() as db:
            return {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ACTIONS}

    def categories(self, kind):
        _, table, _ = KINDS[kind]
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                f"SELECT category_id, category_name FROM {table} ORDER BY CASEFOLD(category_name), category_id"
            )]

    def genres(self, kind):
        table, _, key = KINDS[kind]
        with self.connect() as db:
            rows = db.execute(f"""
                SELECT DISTINCT genre.value FROM {table} s
                JOIN tmdb_metadata m ON m.kind=? AND m.record_id=s.{key}
                    AND m.country='CA' AND m.fingerprint=TITLE_FINGERPRINT(s.raw_json)
                JOIN json_each(m.payload, '$.genres') genre
                WHERE json_extract(m.payload, '$.status')='matched'
            """, (kind,)).fetchall()
        names = {}
        for row in rows:
            for name in people_names(row[0]):
                names.setdefault(name.casefold(), name)
        return sorted(names.values(), key=str.casefold)

    def query(self, kind, q="", category=None, sort="asc", sort_by="name", actor="", director="", person="", foreign=None, children="all", page=None, genre=None, provider=None, my_services: int = 0, access="flatrate", watched="all", banned="hide", tmdb_status="all", era=None, watchlist=False):
        table, categories, key = KINDS[kind]
        if page is not None and (isinstance(page, bool) or not isinstance(page, int) or page < 1):
            raise ValueError("Página inválida.")
        if children not in {"all", "only", "exclude"}:
            raise ValueError("Filtro infantil inválido.")
        if sort_by not in SORT_COLUMNS or sort not in {"asc", "desc"}:
            raise ValueError("Orden inválido.")
        if access not in PROVIDER_ACCESS:
            raise ValueError("Tipo de acceso inválido.")
        if my_services not in {0, 1, 2}:
            raise ValueError("Filtro de mis servicios inválido.")
        if watched not in {"all", "seen", "unseen"} or (kind != "movies" and watched != "all"):
            raise ValueError("Filtro de películas vistas inválido.")
        if banned not in {"hide", "only"} or (kind != "movies" and banned != "hide"):
            raise ValueError("Filtro de películas bloqueadas inválido.")
        if tmdb_status not in TMDB_STATUSES:
            raise ValueError("Estado TMDB inválido.")
        eras = era if isinstance(era, list) else ([era] if era else [])
        if any(value not in MOVIE_ERAS for value in eras) or (eras and kind != "movies"):
            raise ValueError("Época de películas inválida.")
        valid_rating = """CASE WHEN
            json_type(score.payload, '$.rating') IN ('integer', 'real')
            AND json_extract(score.payload, '$.rating') BETWEEN 0 AND 10
            AND json_type(score.payload, '$.votes') IN ('integer', 'real')
            AND json_extract(score.payload, '$.votes') > 0
            THEN json_extract(score.payload, '$.rating') END"""
        materialize_metadata = not watchlist and (sort_by == "rating" or any((q, actor, director, person)))
        tmdb_rating = "score.rating" if materialize_metadata else valid_rating
        votes = "score.votes" if materialize_metadata else f"CASE WHEN ({valid_rating}) IS NOT NULL THEN json_extract(score.payload, '$.votes') END"
        order = (
            f"(({tmdb_rating}) * ({votes}) + 100.0 * ?) / (({votes}) + 100.0)"
            if sort_by == "rating" else SORT_COLUMNS[sort_by]
        )
        year = "score.year" if materialize_metadata else "RELEASE_YEAR(json_extract(score.payload, '$.release_date'))"
        genre_expression = "(SELECT group_concat(value, ', ') FROM json_each(score.payload, '$.genres'))"
        if sort_by == "year":
            order = year
        if sort_by == "category_name":
            order = f"CASEFOLD({genre_expression})"
        secondary_order = (
            f", ({year}) IS NULL, {year} DESC"
            if sort_by == "rating" else ""
        )
        conditions, params = [], []
        watchlist_expression = f"EXISTS (SELECT 1 FROM watchlist saved WHERE saved.kind='{kind}' AND saved.record_id=s.{key})"
        if watchlist:
            conditions.append(watchlist_expression)
        if eras:
            era_conditions = {
                "recent": f"{year} >= 2015",
                "modern": f"{year} BETWEEN 2000 AND 2014",
                "nostalgia": f"{year} BETWEEN 1980 AND 1999",
                "classic": f"{year} < 1980",
                "unknown": f"{year} IS NULL",
            }
            conditions.append("(" + " OR ".join(era_conditions[value] for value in dict.fromkeys(eras)) + ")")
        if tmdb_status != "all":
            status_condition = "" if tmdb_status == "pending" else "AND json_extract(m.payload, '$.status')=?"
            cached = f"""EXISTS (
                SELECT 1 FROM tmdb_metadata m
                WHERE m.kind=? AND m.record_id=s.{key} AND m.country='CA'
                    AND m.fingerprint=TITLE_FINGERPRINT(s.raw_json)
                    {status_condition}
            )"""
            conditions.append(f"NOT {cached}" if tmdb_status == "pending" else cached)
            params.append(kind)
            if tmdb_status != "pending":
                params.append(tmdb_status)
        banned_expression = "EXISTS (SELECT 1 FROM banned_movies b WHERE b.record_id=s.stream_id)"
        if kind == "movies":
            conditions.append(banned_expression if banned == "only" else f"NOT {banned_expression}")
        watched_expression = "EXISTS (SELECT 1 FROM watched_movies w WHERE w.record_id=s.stream_id)"
        if watched != "all":
            conditions.append(watched_expression if watched == "seen" else f"NOT {watched_expression}")
        people_metadata = "COALESCE(score.people, '{}')" if materialize_metadata else "PEOPLE_METADATA(NULL, score.payload)"
        if q:
            escaped = q.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            expressions = (
                "s.name", "json_extract(score.payload, '$.title')",
                "json_extract(score.payload, '$.original_title')",
                f"json_extract({people_metadata}, '$.cast')",
                f"json_extract({people_metadata}, '$.director')",
                f"json_extract({people_metadata}, '$.creators')",
            )
            conditions.append("(" + " OR ".join(f"CASEFOLD({expression}) LIKE ? ESCAPE '\\'" for expression in expressions) + ")")
            params.extend([f"%{escaped}%"] * len(expressions))
        for value, expression in (
            (actor, f"json_extract({people_metadata}, '$.cast')"),
            (director, f"json_extract({people_metadata}, '$.director')"),
        ):
            if value:
                escaped = value.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                conditions.append(f"CASEFOLD({expression}) LIKE ? ESCAPE '\\'")
                params.append(f"%{escaped}%")
        if children != "all":
            membership = f"""EXISTS (
                SELECT 1 FROM json_each(score.payload, '$.genres')
                WHERE CASEFOLD(value) IN ('familia', 'animación', 'family', 'animation')
            )"""
            conditions.insert(0, membership if children == "only" else f"NOT {membership}")
        if foreign is not None and children != "only" and kind in {"movies", "series"}:
            conditions.append("NON_LATIN_TITLE(s.name) = ?")
            params.append(int(bool(foreign)))
        if person:
            conditions.append(f"HAS_PERSON({people_metadata}, ?) = 1")
            params.append(person)
        selected_genres = genre if isinstance(genre, list) else ([genre] if genre else [])
        if selected_genres:
            conditions.append("(" + " OR ".join(f"HAS_GENRE({genre_expression}, ?) = 1" for _ in selected_genres) + ")")
            params.extend(selected_genres)
        selected_providers = provider if isinstance(provider, list) else ([provider] if provider else [])
        selected_providers = sorted({platform_name(name) for name in selected_providers})
        if selected_providers or my_services == 1:
            if kind not in {"movies", "series"}:
                raise ValueError("Los canales no admiten filtros de plataformas TMDB.")
            if my_services == 1:
                services = set(self.subscriptions())
                selected_providers = sorted(services.intersection(selected_providers) if selected_providers else services)
            if not selected_providers:
                conditions.append("0")
            else:
                conditions.append(f"""EXISTS (
                    SELECT 1 FROM tmdb_metadata m,
                        json_each(m.payload, '$.providers') availability,
                        json_each(availability.value) provider
                    WHERE m.kind=? AND m.record_id=s.{key} AND m.country='CA'
                        AND m.fingerprint=TITLE_FINGERPRINT(s.raw_json)
                        AND json_extract(m.payload, '$.status')='matched'
                        AND (?='all' OR availability.key=?)
                        AND PLATFORM_NAME(provider.value) IN ({','.join('?' for _ in selected_providers)})
                )""")
                params.extend((kind, access, access, *selected_providers))
        if my_services == 2:
            if kind not in {"movies", "series"}:
                raise ValueError("Los canales no admiten filtros de plataformas TMDB.")
            services = self.subscriptions()
            conditions.append(f"""EXISTS (
                SELECT 1 FROM tmdb_metadata m
                WHERE m.kind=? AND m.record_id=s.{key} AND m.country='CA'
                    AND m.fingerprint=TITLE_FINGERPRINT(s.raw_json)
                    AND json_extract(m.payload, '$.status')='matched'
                    AND NOT EXISTS (
                        SELECT 1 FROM json_each(m.payload, '$.providers') availability,
                            json_each(availability.value) provider
                        WHERE (?='all' OR availability.key=?)
                            AND PLATFORM_NAME(provider.value) IN ({','.join('?' for _ in services) or 'NULL'})
                    )
            )""")
            params.extend((kind, access, access, *services))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        source = f"{table} s LEFT JOIN {categories} c ON c.category_id = s.category_id"
        if materialize_metadata:
            source += f" LEFT JOIN query_metadata score ON score.record_id=s.{key}"
        else:
            source += f""" LEFT JOIN tmdb_metadata score
                ON score.kind='{kind}' AND score.record_id=s.{key} AND score.country='CA'
                AND score.fingerprint=TITLE_FINGERPRINT(s.raw_json)
                AND json_extract(score.payload, '$.status')='matched'"""
        watched_projection = f", {watched_expression} AS watched, {banned_expression} AS banned" if kind == "movies" else ""
        with self.connect() as db:
            if materialize_metadata:
                started = time.perf_counter()
                db.execute("""CREATE TEMP TABLE query_metadata (
                    record_id TEXT PRIMARY KEY, payload TEXT, rating,
                    votes, year INTEGER, people TEXT
                )""")
                people = "PEOPLE_METADATA(NULL, score.payload)" if q or actor or director or person else "NULL"
                if children != "all":
                    people_condition = membership if children == "only" else f"NOT {membership}"
                    people = f"CASE WHEN {people_condition} THEN {people} END"
                db.execute(f"""INSERT INTO query_metadata
                    SELECT score.record_id, score.payload, {valid_rating},
                        CASE WHEN ({valid_rating}) IS NOT NULL THEN json_extract(score.payload, '$.votes') END,
                        RELEASE_YEAR(json_extract(score.payload, '$.release_date')), {people}
                    FROM tmdb_metadata score JOIN {table} s ON s.{key}=score.record_id
                    WHERE score.kind=? AND score.country='CA'
                        AND score.fingerprint=TITLE_FINGERPRINT(s.raw_json)
                        AND json_extract(score.payload, '$.status')='matched'
                """, (kind,))
                record_timing("prepare_metadata", started)
            total = None
            limit = ""
            provider_projection = ""
            projection_params = []
            if my_services == 1 and selected_providers:
                provider_projection = f""", (
                    SELECT json_group_array(DISTINCT PLATFORM_NAME(provider.value))
                    FROM tmdb_metadata m, json_each(m.payload, '$.providers') availability,
                        json_each(availability.value) provider
                    WHERE m.kind=? AND m.record_id=s.{key} AND m.country='CA'
                        AND m.fingerprint=TITLE_FINGERPRINT(s.raw_json)
                        AND json_extract(m.payload, '$.status')='matched'
                        AND (?='all' OR availability.key=?)
                        AND PLATFORM_NAME(provider.value) IN ({','.join('?' for _ in selected_providers)})
                ) AS matched_services"""
                projection_params.extend((kind, access, access, *selected_providers))
            query_params = list(params)
            if sort_by == "rating":
                started = time.perf_counter()
                mean = db.execute(f"SELECT AVG({tmdb_rating}) FROM {source}").fetchone()[0]
                record_timing("rating_mean", started)
                query_params.extend((mean, mean))
            if page is not None:
                started = time.perf_counter()
                total = db.execute(f"SELECT COUNT(*) FROM {source}{where}", params).fetchone()[0]
                record_timing("count", started)
                pages = max(1, (total + 49) // 50)
                page = min(page, pages)
                limit = "LIMIT ? OFFSET ?"
                query_params.extend((50, (page - 1) * 50))
            started = time.perf_counter()
            rows = db.execute(
                f"""SELECT s.{key} AS id, s.name, s.category_id, {tmdb_rating} AS rating,
                           {votes} AS votes, {year} AS year, {watchlist_expression} AS watchlisted,
                           {genre_expression} AS genre, COALESCE(c.category_name, 'Sin categoría') AS category_name,
                           json_extract(s.raw_json, '$.youtube_trailer') AS youtube_trailer,
                           json_extract(s.raw_json, '$.stream_icon') AS preview_icon,
                           json_extract(s.raw_json, '$.cover') AS preview_cover
                           {watched_projection}
                           {provider_projection}
                    FROM {source}{where}
                    ORDER BY ({order}) IS NULL, {order} {sort}{secondary_order}, CASEFOLD(s.name), s.{key}
                    {limit}
                    """,
                projection_params + query_params,
            ).fetchall()
            record_timing("select_rows", started)
        items = []
        for row in rows:
            item = dict(row)
            item["watchlisted"] = bool(item["watchlisted"])
            if kind == "movies":
                item["watched"] = bool(item["watched"])
                item["banned"] = bool(item["banned"])
            if my_services == 1:
                item["matched_services"] = sorted(json.loads(item.pop("matched_services", "[]")), key=str.casefold)
            item["trailer_url"] = youtube_trailer_url(item.pop("youtube_trailer"))
            icon, cover = item.pop("preview_icon"), item.pop("preview_cover")
            item["poster_url"] = valid_image(icon) or valid_image(cover)
            item["preview_url"] = item["poster_url"] if kind in {"movies", "series"} else None
            items.append(item)
        result = {
            "items": items,
            "total": len(rows) if total is None else total,
        }
        if page is not None:
            result.update(page=page, pages=pages, per_page=50)
        return result

    def detail(self, kind, record_id):
        table, _, key = KINDS[kind]
        with self.connect() as db:
            row = db.execute(
                f"SELECT name, raw_json FROM {table} WHERE {key} = ?", (record_id,)
            ).fetchone()
            watched = kind == "movies" and db.execute(
                "SELECT 1 FROM watched_movies WHERE record_id=?", (record_id,),
            ).fetchone() is not None
            banned = kind == "movies" and db.execute(
                "SELECT 1 FROM banned_movies WHERE record_id=?", (record_id,),
            ).fetchone() is not None
            watchlisted = db.execute(
                "SELECT 1 FROM watchlist WHERE kind=? AND record_id=?", (kind, record_id),
            ).fetchone() is not None
        if row is None:
            return None
        result = {"name": row["name"], "data": json.loads(row["raw_json"]), "watchlisted": watchlisted}
        if kind == "movies":
            result["watched"] = watched
            result["banned"] = banned
        return result

    def stats(self):
        totals, largest = {}, []
        with self.connect() as db:
            for kind, (table, categories, _) in KINDS.items():
                totals[kind] = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                rows = db.execute(f"""
                    SELECT c.category_id, COALESCE(c.category_name, 'Sin categoría') AS category, COUNT(*) AS total
                    FROM {table} s LEFT JOIN {categories} c ON c.category_id = s.category_id
                    GROUP BY c.category_id, c.category_name
                    ORDER BY total DESC, CASEFOLD(category) LIMIT 10
                """).fetchall()
                largest.extend({"kind": kind, **dict(row)} for row in rows)
            metadata = db.execute("SELECT synced_at FROM sync_metadata WHERE id = 1").fetchone()
        return {
            "totals": totals,
            "largest": sorted(largest, key=lambda item: (-item["total"], item["kind"], item["category"]))[:10],
            "synced_at": metadata[0] if metadata else None,
        }

    def export(self, kind, destination):
        table, _, key = KINDS[kind]
        with self.connect() as db:
            records = [json.loads(row[0]) for row in db.execute(
                f"SELECT raw_json FROM {table} ORDER BY CASEFOLD(name), {key}"
            )]
        frame = pd.json_normalize(records) if records else pd.DataFrame(columns=[key, "name", "category_id"])
        frame.to_csv(destination, index=False, encoding="utf-8-sig")
        return len(frame)
