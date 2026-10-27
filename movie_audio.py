import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import quote

import requests
from urllib3.exceptions import HTTPError

from xtream import CatalogError, XtreamClient


MAX_BYTES = 12 * 1024 * 1024
PROBE_SECONDS = 20
JOB_SECONDS = 45
LANGUAGES = {
    "spa": "ES", "eng": "EN", "fra": "FR", "fre": "FR", "por": "PT",
    "ita": "IT", "deu": "DE", "ger": "DE", "jpn": "JA", "kor": "KO",
    "zho": "ZH", "chi": "ZH", "ara": "AR", "rus": "RU", "hin": "HI",
    "nld": "NL", "dut": "NL", "cat": "CA", "eus": "EU", "baq": "EU",
    "glg": "GL", "ces": "CS", "cze": "CS", "pol": "PL", "tur": "TR",
    "swe": "SV", "dan": "DA", "fin": "FI", "nor": "NO", "ell": "EL",
    "gre": "EL", "heb": "HE", "ukr": "UK", "ron": "RO", "rum": "RO",
    "hun": "HU", "tha": "TH", "vie": "VI", "ind": "ID",
}


def copy_fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def audio_tracks(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("streams"), list):
        raise CatalogError("La inspección no devolvió pistas válidas.")
    languages = []
    unknown = 0
    count = 0
    for stream in payload["streams"]:
        if not isinstance(stream, dict) or stream.get("codec_type") not in {"audio", "video", "subtitle", "data", "attachment", "unknown"}:
            raise CatalogError("La inspección devolvió una pista inválida.")
        if stream.get("codec_type") != "audio":
            continue
        count += 1
        tags = stream.get("tags", {})
        raw = tags.get("language", "") if isinstance(tags, dict) else ""
        language = raw.strip().lower() if isinstance(raw, str) else ""
        if re.fullmatch(r"[a-z]{2,3}(?:[-_][a-z0-9]{2,8})+", language):
            language = re.split(r"[-_]", language)[0]
        code = LANGUAGES.get(language)
        if code is None and re.fullmatch(r"[a-z]{2,3}", language) and language not in {"und", "mul", "zxx", "mis"}:
            code = language.upper()
        if code:
            if code not in languages:
                languages.append(code)
        else:
            unknown += 1
    return {"languages": languages, "unknown": unknown, "tracks": count}


def movie_url(client, record_id, data):
    extension = data.get("container_extension")
    if not re.fullmatch(r"[0-9]{1,20}", record_id) or not isinstance(extension, str) or not re.fullmatch(r"[a-zA-Z0-9]{1,8}", extension):
        raise CatalogError("Esta copia no tiene un identificador o formato válido para comprobar el audio.")
    base = client.endpoint.removesuffix("/player_api.php")
    username = quote(client.credentials["username"], safe="")
    password = quote(client.credentials["password"], safe="")
    return f"{base}/movie/{username}/{password}/{record_id}.{extension}"


def probe_movie(client, record_id, data):
    executable = shutil.which("ffprobe")
    if not executable:
        raise CatalogError("Falta ffprobe en el servidor. Instala FFmpeg en Oracle para comprobar el audio.")
    upstream = movie_url(client, record_id, data)
    deadline = time.monotonic() + PROBE_SECONDS
    state = {"bytes": 0, "requests": 0, "error": None}

    # A local bridge bounds actual network reads and keeps credentials out of process arguments.
    class Bridge(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            if self.path != "/media" or time.monotonic() >= deadline:
                self.send_error(404)
                return
            state["requests"] += 1
            if state["bytes"] >= MAX_BYTES or state["requests"] > 16:
                state["error"] = "Se alcanzó el límite de lectura de la comprobación."
                self.send_error(502)
                return
            range_header = self.headers.get("Range", "")
            if range_header and not re.fullmatch(r"bytes=\d+-\d*", range_header):
                self.send_error(400)
                return
            headers = {"Accept-Encoding": "identity"}
            if range_header:
                headers["Range"] = range_header
            try:
                with requests.get(upstream, headers=headers, stream=True, timeout=(3, 3), allow_redirects=False) as response:
                    if response.status_code not in {200, 206}:
                        state["error"] = (
                            "El proveedor redirige el vídeo; esta comprobación no sigue redirecciones por seguridad."
                            if 300 <= response.status_code < 400 else
                            f"El proveedor respondió HTTP {response.status_code} al consultar el vídeo."
                        )
                        self.send_error(502)
                        return
                    if response.headers.get("Content-Encoding", "identity") != "identity":
                        state["error"] = "El proveedor devolvió un vídeo comprimido no compatible con la comprobación."
                        self.send_error(502)
                        return
                    self.send_response(response.status_code)
                    for header in ("Content-Length", "Content-Range", "Accept-Ranges"):
                        value = response.headers.get(header)
                        if value and "\r" not in value and "\n" not in value:
                            self.send_header(header, value)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    while time.monotonic() < deadline:
                        remaining = MAX_BYTES - state["bytes"]
                        if remaining <= 0:
                            state["error"] = "Se alcanzó el límite de lectura (12 MiB) sin completar la comprobación."
                            break
                        chunk = response.raw.read(min(32 * 1024, remaining))
                        if not chunk:
                            break
                        state["bytes"] += len(chunk)
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                return
            except (requests.RequestException, HTTPError, OSError):
                state["error"] = "No se pudo leer el vídeo del proveedor dentro del tiempo permitido."
            finally:
                self.close_connection = True

    server = HTTPServer(("127.0.0.1", 0), Bridge)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    try:
        worker.start()
    except RuntimeError:
        server.server_close()
        raise CatalogError("No se pudo iniciar el lector de vídeo. Vuelve a intentarlo.") from None
    try:
        try:
            result = subprocess.run(
                [executable, "-v", "error", "-protocol_whitelist", "http,tcp",
                 "-format_whitelist", "mov,matroska,webm,avi,mpegts,flv,aac,mp3,ogg,wav,asf",
                 "-rw_timeout", "3000000", "-probesize", "4000000", "-analyzeduration", "3000000",
                 "-show_entries", "stream=codec_type:stream_tags=language", "-of", "json",
                 f"http://127.0.0.1:{server.server_port}/media"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=PROBE_SECONDS, check=False,
            )
        except subprocess.TimeoutExpired:
            raise CatalogError("La comprobación superó los 20 segundos. Puedes volver a intentarlo.") from None
        except OSError:
            raise CatalogError("No se pudo ejecutar ffprobe en el servidor.") from None
        if state["error"]:
            raise CatalogError(state["error"])
        if result.returncode or len(result.stdout) > 65536:
            raise CatalogError("No se pudieron identificar las pistas de esta copia con los límites de lectura.")
        try:
            return audio_tracks(json.loads(result.stdout))
        except (ValueError, UnicodeError):
            raise CatalogError("ffprobe no devolvió metadatos de audio válidos.") from None
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=4)


class MovieAudioJobs:
    def __init__(self, store, logger):
        self.store = store
        self.logger = logger

    def status(self, record_id):
        with self.store.connect() as db:
            row = db.execute("SELECT raw_json FROM movies WHERE stream_id=?", (record_id,)).fetchone()
            if row is None:
                raise CatalogError("Esta copia ya no existe en el catálogo.")
            identity = copy_fingerprint(json.loads(row["raw_json"]))
            cached = db.execute(
                "SELECT payload, checked_at, fingerprint FROM movie_audio_metadata WHERE record_id=?", (record_id,),
            ).fetchone()
            job = db.execute("SELECT * FROM movie_audio_job WHERE record_id=?", (record_id,)).fetchone()
        metadata = json.loads(cached["payload"]) if cached and cached["fingerprint"] == identity else None
        running = bool(job and job["status"] == "running" and time.time() - job["started_at"] < JOB_SECONDS)
        error = None
        if job and not running:
            if job["fingerprint"] != identity and job["status"] in {"failed", "running"}:
                error = "La copia cambió desde la última comprobación. Vuelve a comprobar el audio."
            elif job["fingerprint"] == identity:
                error = job["error"] if job["status"] == "failed" else (
                    "La comprobación se interrumpió. Vuelve a intentarlo." if job["status"] == "running" else None
                )
        return {
            "metadata": metadata, "checked_at": cached["checked_at"] if metadata else None,
            "running": running, "error": error, "stale": bool(cached and metadata is None),
        }

    def running(self):
        with self.store.connect() as db:
            return db.execute(
                "SELECT 1 FROM movie_audio_job WHERE status='running' AND started_at>?",
                (time.time() - JOB_SECONDS,),
            ).fetchone() is not None

    def start(self, record_id, client_factory=XtreamClient.from_env, probe=probe_movie):
        started = time.time()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT 1 FROM movie_audio_job WHERE status='running' AND started_at>?",
                                (started - JOB_SECONDS,)).fetchone()
            if active:
                return False
            row = db.execute(
                """SELECT raw_json FROM movies WHERE stream_id=? AND NOT EXISTS
                   (SELECT 1 FROM provider_removed WHERE kind='movies' AND record_id=movies.stream_id)""",
                (record_id,),
            ).fetchone()
            if row is None:
                raise CatalogError("Esta copia no está disponible en el proveedor.")
            data = json.loads(row["raw_json"])
            identity = copy_fingerprint(data)
            db.execute(
                "INSERT OR REPLACE INTO movie_audio_job VALUES (?, ?, 'running', ?, NULL)",
                (record_id, identity, started),
            )

        def run():
            error = None
            try:
                client = client_factory()
                try:
                    payload = probe(client, record_id, data)
                finally:
                    client.close()
                with self.store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    current = db.execute(
                        "SELECT 1 FROM movie_audio_job WHERE record_id=? AND started_at=? AND status='running'",
                        (record_id, started),
                    ).fetchone()
                    if current is None:
                        raise CatalogError("La comprobación caducó. Vuelve a intentarlo.")
                    row = db.execute("SELECT raw_json FROM movies WHERE stream_id=?", (record_id,)).fetchone()
                    if row is None or copy_fingerprint(json.loads(row["raw_json"])) != identity:
                        raise CatalogError("La copia cambió durante la comprobación. Vuelve a intentarlo.")
                    db.execute("INSERT OR REPLACE INTO movie_audio_metadata VALUES (?, ?, ?, ?)", (
                        record_id, identity, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        json.dumps(payload),
                    ))
            except (CatalogError, sqlite3.Error, OSError) as exc:
                error = str(exc) if isinstance(exc, CatalogError) else "No se pudo guardar la comprobación de audio."
                self.logger.warning("Comprobación de audio fallida: %s", error)
            except Exception:
                error = "Error inesperado al comprobar el audio. Consulta el registro del servidor."
                self.logger.error("Error inesperado al comprobar el audio.")
            finally:
                try:
                    with self.store.connect() as db:
                        db.execute(
                            "UPDATE movie_audio_job SET status=?, error=? WHERE record_id=? AND started_at=?",
                            ("failed" if error else "completed", error, record_id, started),
                        )
                except sqlite3.Error:
                    self.logger.error("No se pudo guardar el estado de la comprobación de audio.")

        try:
            threading.Thread(target=run, daemon=True).start()
        except RuntimeError:
            with self.store.connect() as db:
                db.execute("UPDATE movie_audio_job SET status='failed', error=? WHERE record_id=? AND started_at=?",
                           ("No se pudo iniciar la comprobación. Vuelve a intentarlo.", record_id, started))
            raise CatalogError("No se pudo iniciar la comprobación. Vuelve a intentarlo.") from None
        return True
