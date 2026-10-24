import json
import math
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode

import requests

from catalog import fingerprint, youtube_trailer_url


COUNTRY = "CA"
CONFIG_PATH = Path(__file__).resolve().with_name("tmdb.local.json")


class TMDBError(Exception):
    pass


class TMDBRateLimitError(TMDBError):
    def __init__(self, seconds):
        self.retry_after = max(1, math.ceil(seconds))
        super().__init__(f"TMDB limitado (HTTP 429). Espera al menos {self.retry_after} segundos antes de volver a consultar.")


def retry_after_seconds(value):
    if not isinstance(value, str):
        return 60
    value = value.strip()
    if re.fullmatch(r"[0-9]{1,12}", value):
        return max(1, int(value))
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            return 60
        return max(1, math.ceil((date - datetime.now(timezone.utc)).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        return 60


class TMDBRequestGate:
    """Serialize API requests and share pacing/cooldown across all local clients."""

    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self.lock = threading.Lock()
        self.clock = clock
        self.sleep = sleep
        self.next_request = 0
        self.blocked_until = 0

    @contextmanager
    def request(self):
        with self.lock:
            self.wait()
            yield

    def wait(self):
        remaining = self.blocked_until - self.clock()
        if remaining > 0:
            raise TMDBRateLimitError(remaining)
        while self.next_request > self.clock():
            self.sleep(max(0, self.next_request - self.clock()))
        self.next_request = self.clock() + 0.25

    def throttle(self, retry_after):
        delay = retry_after_seconds(retry_after)
        self.blocked_until = max(self.blocked_until, self.clock() + delay)
        raise TMDBRateLimitError(self.blocked_until - self.clock())


REQUEST_GATE = TMDBRequestGate()


def recommended_titles(store, kind, record_id, client_factory=None):
    record = store.detail(kind, record_id)
    if record is None:
        raise TMDBError("El título ya no está en el catálogo.")
    metadata = store.tmdb_cached(kind, record_id, COUNTRY, fingerprint(record["data"]))
    if not metadata or metadata.get("status") != "matched":
        return []
    tmdb_id = metadata.get("id")
    if type(tmdb_id) is not int or tmdb_id <= 0:
        raise TMDBError("La asociación TMDB no tiene un identificador válido.")
    now = time.time()
    ids = store.cached_recommendations(kind, tmdb_id, now - 7 * 86400)
    if ids is None:
        ids = (client_factory or TMDBClient)().recommendations(kind, tmdb_id)
        store.save_recommendations(kind, tmdb_id, ids, now)
    return store.recommended_titles(kind, tmdb_id, ids)


def normalized(value):
    return "".join(
        c for c in unicodedata.normalize("NFKD", value.casefold())
        if c.isalnum() and not unicodedata.combining(c)
    )


def search_terms(data):
    titles = []
    year = str(data.get("year") or "")[:4]
    for field in ("name", "title"):
        value = data.get(field)
        if not isinstance(value, str):
            continue
        match = re.search(r"\((\d{4})\)\s*$", value)
        if not year and match:
            year = match[1]
        value = re.sub(r"\s*\(\d{4}\)\s*$", "", value)
        value = re.sub(r"\s+(?:4K|UHD|FHD|HD|1080p|720p)\s*$", "", value, flags=re.I)
        alternatives = re.findall(r"\(([^()]+)\)", value)
        primary = re.sub(r"\([^()]*\)", "", value).strip()
        for title in [primary, *alternatives]:
            title = " ".join(title.split())
            if title and normalized(title) not in {normalized(t) for t in titles}:
                titles.append(title)
    if not re.fullmatch(r"\d{4}", year):
        for field in ("release_date", "releaseDate"):
            match = re.match(r"(\d{4})-", str(data.get(field) or ""))
            if match:
                year = match[1]
                break
        else:
            year = ""
    return titles[:3], year


class TMDBClient:
    def __init__(self, config_path=CONFIG_PATH):
        try:
            config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TMDBError("No se pudo leer tmdb.local.json. Revisa su formato y permisos.") from exc
        if not isinstance(config, dict):
            raise TMDBError("La configuración de TMDB debe ser un objeto JSON.")
        self.token = config.get("read_access_token", "")
        self.key = config.get("api_key", "")
        if not isinstance(self.token, str) or not isinstance(self.key, str):
            raise TMDBError("Las credenciales de TMDB deben ser texto.")
        self.token, self.key = self.token.strip(), self.key.strip()
        if not self.token and not self.key:
            raise TMDBError("Completa las credenciales en tmdb.local.json.")

    def get(self, path, params=None):
        with REQUEST_GATE.request():
            return self._get(path, params)

    def _get(self, path, params=None):
        params = dict(params or {})
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        else:
            params["api_key"] = self.key
        url = f"https://api.themoviedb.org/3/{path}"
        try:
            response = requests.get(url, params=params, headers=headers, timeout=(10, 25), allow_redirects=False)
        except requests.exceptions.SSLError:
            if sys.platform != "darwin":
                raise TMDBError("Error de certificados TLS de TMDB. Configura un almacén de certificados válido.") from None
            return self._macos_get(url, params, headers)
        except requests.RequestException:
            raise TMDBError("No se pudo conectar con TMDB. Vuelve a intentarlo.") from None
        if response.status_code == 429:
            REQUEST_GATE.throttle(response.headers.get("Retry-After"))
        if response.status_code != 200:
            raise TMDBError(f"TMDB devolvió HTTP {response.status_code}. Revisa las credenciales o inténtalo más tarde.")
        try:
            data = response.json()
        except ValueError:
            raise TMDBError("TMDB devolvió una respuesta JSON inválida.") from None
        if not isinstance(data, dict):
            raise TMDBError("TMDB devolvió un formato inesperado.")
        return data

    def _macos_get(self, url, params, headers):
        # macOS curl uses the system trust store; TLS verification stays enabled.
        settings = "url = " + json.dumps(url + "?" + urlencode(params)) + "\n"
        for key, value in headers.items():
            settings += "header = " + json.dumps(f"{key}: {value}") + "\n"
        try:
            # The TLS fallback is another request and consumes its own paced slot.
            REQUEST_GATE.wait()
            with tempfile.TemporaryDirectory(prefix="tmdb-headers-") as directory:
                header_file = Path(directory) / "headers"
                result = subprocess.run(
                    ["/usr/bin/curl", "--config", "-", "--silent", "--proto", "=https",
                     "--connect-timeout", "10", "--max-time", "25",
                     "--dump-header", str(header_file), "--write-out", "\n%{http_code}"],
                    input=settings, capture_output=True, text=True, timeout=30,
                )
                response_headers = header_file.read_text(encoding="iso-8859-1") if header_file.exists() else ""
        except (OSError, subprocess.TimeoutExpired):
            raise TMDBError("No se pudo consultar TMDB con el almacén TLS de macOS.") from None
        if result.returncode:
            raise TMDBError("Falló la conexión HTTPS a TMDB con el almacén TLS de macOS.")
        body, _, status = result.stdout.rpartition("\n")
        if status == "429":
            values = re.findall(r"^Retry-After:\s*([^\r\n]*)", response_headers, flags=re.I | re.M)
            REQUEST_GATE.throttle(values[-1] if values else None)
        if status != "200":
            raise TMDBError(f"TMDB devolvió HTTP {status if status.isdecimal() else 'inválido'}.")
        try:
            data = json.loads(body)
        except ValueError:
            raise TMDBError("TMDB devolvió una respuesta JSON inválida.") from None
        if not isinstance(data, dict):
            raise TMDBError("TMDB devolvió un formato inesperado.")
        return data

    def recommendations(self, kind, tmdb_id):
        media = "movie" if kind == "movies" else "tv"
        data = self.get(f"{media}/{tmdb_id}/recommendations", {"language": "es-ES", "page": 1})
        results = data.get("results")
        if not isinstance(results, list) or any(
            not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] <= 0
            for item in results
        ):
            raise TMDBError("TMDB devolvió recomendaciones con un formato inesperado.")
        return list(dict.fromkeys(item["id"] for item in results if not item.get("adult")))

    def search(self, kind, data):
        media = "movie" if kind == "movies" else "tv"
        titles, year = search_terms(data)
        candidates = {}
        for title in titles:
            params = {"query": title, "language": "es-ES", "include_adult": "false"}
            if year:
                params["year" if media == "movie" else "first_air_date_year"] = year
            result = self.get(f"search/{media}", params)
            for item in result.get("results", []):
                candidates[item["id"]] = {
                    "id": item["id"], "title": item.get("title") or item.get("name"),
                    "original_title": item.get("original_title") or item.get("original_name"),
                    "date": item.get("release_date") or item.get("first_air_date") or "",
                }
        values = list(candidates.values())
        exact = [
            item for item in values if year and item["date"][:4] == year
            and any(normalized(title) in {normalized(item["title"] or ""), normalized(item["original_title"] or "")} for title in titles)
        ]
        return values, exact[0]["id"] if len(exact) == 1 else None

    def details(self, kind, tmdb_id):
        media = "movie" if kind == "movies" else "tv"
        data = self.get(f"{media}/{tmdb_id}", {
            "language": "es-ES", "append_to_response": "credits,videos,watch/providers,external_ids,keywords",
            "include_video_language": "es,en,null",
        })
        credits = data.get("credits", {})
        providers = data.get("watch/providers", {}).get("results", {}).get(COUNTRY, {})
        videos = data.get("videos", {}).get("results", [])
        trailers = [v for v in videos if v.get("site") == "YouTube" and v.get("type") == "Trailer" and youtube_trailer_url(v.get("key"))]
        latin_countries = {"MX", "AR", "BO", "CL", "CO", "CR", "CU", "DO", "EC",
                           "SV", "GT", "HN", "NI", "PA", "PY", "PE", "PR", "UY", "VE"}
        def trailer_priority(video):
            language = video.get("iso_639_1")
            if language == "es":
                priority = 0 if video.get("iso_3166_1") in latin_countries else 1
            elif language == "en":
                priority = 2
            elif language and language == data.get("original_language"):
                priority = 3
            else:
                priority = 4
            return priority, not video.get("official", False)

        if not any(trailer_priority(video)[0] == 0 for video in trailers):
            response = self.get(f"{media}/{tmdb_id}/videos")
            videos = response.get("results")
            if not isinstance(videos, list):
                raise TMDBError("TMDB devolvió una lista de vídeos inválida.")
            trailers.extend(v for v in videos if isinstance(v, dict) and v.get("site") == "YouTube"
                            and v.get("type") == "Trailer" and youtube_trailer_url(v.get("key")))
        trailers = [v for v in trailers if trailer_priority(v)[0] < 4]
        trailers.sort(key=trailer_priority)
        trailer = trailers[0] if trailers else None
        backdrop = data.get("backdrop_path")
        backdrop_url = None
        if isinstance(backdrop, str) and re.fullmatch(r"/[A-Za-z0-9_-]+\.(?:jpg|png|webp)", backdrop):
            backdrop_url = f"https://image.tmdb.org/t/p/w780{backdrop}"
        keywords = data.get("keywords", {}).get("keywords" if media == "movie" else "results", [])
        keyword_names = [keyword["name"] for keyword in keywords]
        return {
            "status": "matched", "id": tmdb_id, "media": media,
            "title": data.get("title") or data.get("name"),
            "original_title": data.get("original_title") or data.get("original_name"),
            "overview": data.get("overview"),
            "keywords": keyword_names,
            "lgbti_keywords": lgbti_keywords(keyword_names),
            "release_date": data.get("release_date") or data.get("first_air_date"),
            "backdrop_url": backdrop_url,
            "genres": [g["name"] for g in data.get("genres", [])],
            "runtime": data.get("runtime"),
            "directors": [p["name"] for p in credits.get("crew", []) if p.get("job") == "Director"],
            "creators": [p["name"] for p in data.get("created_by", [])],
            "cast": [{"name": p["name"], "character": p.get("character")} for p in credits.get("cast", [])[:10]],
            "original_language": data.get("original_language"),
            "countries": [p["name"] for p in data.get("production_countries", [])],
            "companies": [p["name"] for p in data.get("production_companies", [])],
            "seasons": data.get("number_of_seasons"), "episodes": data.get("number_of_episodes"),
            "rating": data.get("vote_average"), "votes": data.get("vote_count"),
            "imdb_id": data.get("imdb_id") or data.get("external_ids", {}).get("imdb_id"),
            "trailer_url": youtube_trailer_url(trailer["key"]) if trailer else None,
            "trailer_official": trailer.get("official", False) if trailer else False,
            "providers": {key: [p["provider_name"] for p in providers.get(key, [])] for key in ("flatrate", "rent", "buy", "free", "ads")},
        }


def lgbti_keywords(names):
    terms = {
        "lgbt", "lgbtq", "lgbtq+", "lgbtqi", "lgbti", "lgbtqia", "lgbtqia+",
        "gay", "gay interest", "gay theme", "gay relationship", "gay romance",
        "gay couple", "gay protagonist", "gay character",
        "lesbian", "lesbian interest", "lesbian relationship", "lesbian romance",
        "lesbian couple", "lesbian protagonist", "lesbian character",
        "transgender", "transgender rights", "transgender protagonist",
        "transsexual", "bisexual", "bisexuality", "homosexuality", "homosexual",
        "queer", "queer cinema", "intersex", "non-binary", "nonbinary",
        "same sex relationship", "same-sex relationship", "same sex romance",
        "same-sex romance", "same sex marriage", "same-sex marriage",
    }
    return sorted({name for name in names if name.strip().casefold() in terms}, key=str.casefold)


def enrich(store, kind, record_id, client_factory=TMDBClient, refresh=False, selected=None):
    record = store.detail(kind, record_id)
    if record is None:
        raise TMDBError("El registro ya no existe.")
    identity = fingerprint(record["data"])
    cached = store.tmdb_cached(kind, record_id, COUNTRY, identity)
    if selected is not None:
        if not cached or cached["status"] != "candidates" or selected not in {item["id"] for item in cached["candidates"]}:
            raise TMDBError("La selección no pertenece a los candidatos actuales. Vuelve a consultar.")
    elif cached and not refresh:
        return cached
    client = client_factory()
    if selected is None:
        candidates, selected = client.search(kind, record["data"])
        result = {"status": "candidates" if candidates else "missing", "candidates": candidates}
    if selected is not None:
        result = client.details(kind, selected)
    result["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result["country"] = COUNTRY
    store.save_tmdb(kind, record_id, COUNTRY, identity, result)
    return result
