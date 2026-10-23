import threading

from tmdb import COUNTRY, TMDBClient, TMDBError, enrich


class TMDBBatch:
    """One cancellable, sequential TMDB job for the local server."""

    def __init__(self, store, logger):
        self.store = store
        self.logger = logger
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.state = {"status": "idle"}

    def status(self):
        with self.lock:
            return dict(self.state)

    def cancel(self):
        with self.lock:
            if self.state["status"] == "running":
                self.cancelled.set()
            return dict(self.state)

    def start(self, kind, limit, client_factory=TMDBClient):
        with self.lock:
            if self.state["status"] == "running":
                return False
            self.cancelled.clear()
            self.state = {
                "status": "running", "kind": kind, "phase": "platforms",
                "total": 0, "processed": 0, "matched": 0, "candidates": 0, "missing": 0,
            }
            try:
                threading.Thread(
                    target=self.run, args=(kind, limit, client_factory),
                    name="tmdb-batch", daemon=True,
                ).start()
            except RuntimeError:
                self.state.update(status="failed", error="No se pudo iniciar el proceso de TMDB.")
                self.logger.error("No se pudo iniciar el hilo TMDB.")
                raise TMDBError(self.state["error"]) from None
        return True

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def run(self, kind, limit, client_factory):
        try:
            client = client_factory()
            directories = {}
            for catalog_kind, media in (("movies", "movie"), ("series", "tv")):
                if self.cancelled.is_set():
                    self.update(status="cancelled")
                    return
                response = client.get(f"watch/providers/{media}", {"language": "es-ES", "watch_region": COUNTRY})
                entries = response.get("results")
                if not isinstance(entries, list) or any(
                    not isinstance(item, dict) or not isinstance(item.get("provider_name"), str)
                    or not item["provider_name"].strip() or len(item["provider_name"]) > 200
                    for item in entries
                ):
                    raise TMDBError("TMDB devolvió un directorio de plataformas inválido.")
                directories[catalog_kind] = [item["provider_name"] for item in entries]
            if self.cancelled.is_set():
                self.update(status="cancelled")
                return
            self.store.save_provider_directory(directories, COUNTRY)
            pending = self.store.tmdb_pending(kind, limit, COUNTRY)
            self.update(phase="titles", total=len(pending))
            for record_id in pending:
                if self.cancelled.is_set():
                    break
                # Reuse enrich's conservative matching and fingerprint validation.
                result = enrich(self.store, kind, record_id, client_factory=lambda: client)
                with self.lock:
                    self.state["processed"] += 1
                    self.state[result["status"]] += 1
                if self.cancelled.wait(0.25):
                    break
            self.update(status="cancelled" if self.cancelled.is_set() else "completed")
        except TMDBError as exc:
            self.logger.warning("Proceso TMDB detenido: %s", exc)
            self.update(status="failed", error=str(exc))
        except Exception:
            # Suppress exception details that could include credential-bearing URLs.
            self.logger.error("Error inesperado durante el proceso por lotes de TMDB.")
            self.update(status="failed", error="No se pudo completar el lote TMDB. Los resultados ya guardados se conservan.")
