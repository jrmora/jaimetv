"""Single-instance deployment worker, run independently by systemd."""

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import tarfile
from contextlib import closing
from pathlib import Path


def write_state(directory, **values):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    previous = read_state(directory)
    last_success = previous.get("last_success_at")
    if not last_success and previous.get("status") == "completed":
        last_success = previous.get("updated_at")
    if values.get("status") == "completed":
        last_success = time.time()
    temporary = directory / "status.tmp"
    temporary.write_text(json.dumps({**values, "updated_at": time.time(), "last_success_at": last_success}))
    temporary.replace(directory / "status.json")


def read_state(directory):
    path = directory / "status.json"
    if not path.exists():
        return {"status": "idle"}
    return json.loads(path.read_text())


def run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, timeout=900).stdout


def deploy():
    import fcntl

    repository = Path(os.environ["IPTV_UPDATE_REPO"]).resolve()
    target = Path(os.environ["IPTV_UPDATE_TARGET"]).resolve()
    state = Path(os.environ["IPTV_UPDATE_STATE"]).resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state / "deploy.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        write_state(state, status="running", message="Descargando la última versión.")
        try:
            run("git", "fetch", "origin", "main", cwd=repository)
            revision = run("git", "rev-parse", "origin/main", cwd=repository).strip()
            if (state / "revision").exists() and (state / "revision").read_text().strip() == revision:
                write_state(state, status="completed", message="La aplicación ya está actualizada.", revision=revision)
                return
            if run("git", "status", "--porcelain", cwd=repository).strip():
                raise RuntimeError("El repositorio de despliegue tiene cambios locales.")
            run("git", "merge", "--ff-only", "origin/main", cwd=repository)
            with tempfile.TemporaryDirectory(dir=state) as temporary:
                staging = Path(temporary)
                archive = staging / "code.tar"
                run("git", "archive", "--format=tar", "-o", str(archive), revision, cwd=repository)
                with tarfile.open(archive) as bundle:
                    for entry in bundle.getmembers():
                        path = Path(entry.name)
                        if path.is_absolute() or ".." in path.parts or not (entry.isfile() or entry.isdir()):
                            raise RuntimeError("El archivo de despliegue contiene rutas no permitidas.")
                        if any(part.startswith(".") for part in path.parts) and entry.name != ".gitignore":
                            raise RuntimeError("El despliegue contiene archivos ocultos no permitidos.")
                        if path.suffix in {".db", ".key", ".pem"} or entry.name.endswith(".local.json"):
                            raise RuntimeError("El despliegue contiene archivos privados no permitidos.")
                    bundle.extractall(staging / "code", filter="data")
                code = staging / "code"
                write_state(state, status="running", message="Preparando y probando la nueva versión.")
                run("python3", "-m", "venv", str(staging / "venv"))
                python = staging / "venv/bin/python"
                run(str(python), "-m", "pip", "install", "-r", str(code / "requirements.txt"), "gunicorn>=23,<24")
                test_env = {**os.environ, "PYTHONPATH": str(code / "tests")}
                test_env.pop("IPTV_AUTH_PATH", None)
                test_env.pop("IPTV_UPDATE_STATE", None)
                subprocess.run(
                    [str(python), "-m", "unittest", "test_vod_only", "test_tmdb_filters", "test_tmdb_people",
                     "test_movie_versions", "test_title_index", "test_performance", "test_database_concurrency", "test_recommendations", "test_watchlist", "test_trailer_player", "test_admin", "test_auth", "test_updater",
                     "test_web_sync", "test_watched", "test_banned"],
                    cwd=code, env=test_env, check=True, capture_output=True, timeout=900,
                )
                # The existing virtualenv path must stay stable for systemd and its scripts.
                backup = state / ("backup-" + str(time.time_ns()))
                backup.mkdir(mode=0o700)
                (backup / "installed-requirements.txt").write_text(
                    run(str(target / ".venv/bin/python"), "-m", "pip", "freeze")
                )
                files = [Path(name) for name in run(
                    "git", "ls-tree", "-r", "--name-only", revision, cwd=repository
                ).splitlines() if name != "services.json"]
                manifest = state / "manifest.json"
                previous = json.loads(manifest.read_text()) if manifest.exists() else []
                removed = [Path(name) for name in previous if Path(name) not in files]
                if any(path.is_absolute() or ".." in path.parts or path.name.endswith(".local.json")
                       or path.suffix in {".db", ".key", ".pem"} or path.name == "services.json"
                       for path in removed):
                    raise RuntimeError("El manifiesto anterior contiene rutas no permitidas.")
                installed = []
                write_state(state, status="running", message="Instalando la versión y reiniciando el servicio.")
                try:
                    run("sudo", "-n", "/usr/bin/systemctl", "stop", "catalogo-iptv")
                    with closing(sqlite3.connect(target / "catalog.db")) as source:
                        with closing(sqlite3.connect(backup / "catalog.db")) as saved_database:
                            source.backup(saved_database)
                    for private in ("auth.local.json", "config.local.json", "tmdb.local.json", "services.json"):
                        if (target / private).exists():
                            shutil.copy2(target / private, backup / private)
                    for relative in files + removed:
                        destination = target / relative
                        if destination.exists():
                            saved = backup / relative
                            saved.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(destination, saved)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        installed.append(relative)
                        if relative in removed:
                            destination.unlink(missing_ok=True)
                        else:
                            shutil.copy2(code / relative, destination)
                    run(str(target / ".venv/bin/python"), "-m", "pip", "install",
                        "-r", str(target / "requirements.txt"), "gunicorn>=23,<24")
                    run("sudo", "-n", "/usr/bin/systemctl", "start", "catalogo-iptv")
                    verify_health()
                except (OSError, sqlite3.Error, subprocess.SubprocessError, RuntimeError):
                    run("sudo", "-n", "/usr/bin/systemctl", "stop", "catalogo-iptv")
                    for relative in installed:
                        if (backup / relative).exists():
                            shutil.copy2(backup / relative, target / relative)
                        else:
                            (target / relative).unlink(missing_ok=True)
                    if (backup / "catalog.db").exists():
                        with closing(sqlite3.connect(backup / "catalog.db")) as source:
                            with closing(sqlite3.connect(target / "catalog.db")) as restored:
                                source.backup(restored)
                    run(str(target / ".venv/bin/python"), "-m", "pip", "install",
                        "-r", str(backup / "installed-requirements.txt"))
                    run("sudo", "-n", "/usr/bin/systemctl", "restart", "catalogo-iptv")
                    verify_health()
                    raise RuntimeError("La actualización falló. Se restauró el código anterior.") from None
                (state / "revision").write_text(revision)
                manifest.write_text(json.dumps([str(path) for path in files]))
                write_state(state, status="completed", message="Aplicación actualizada.", revision=revision)
        except (OSError, ValueError, sqlite3.Error, tarfile.TarError, subprocess.SubprocessError, RuntimeError) as exc:
            message = str(exc) if isinstance(exc, RuntimeError) else "La actualización falló. Revisa journalctl -u jaimetv-update."
            write_state(state, status="failed", message=message)
            raise RuntimeError(message) from None


def verify_health():
    import urllib.error
    import urllib.request

    for _ in range(30):
        try:
            with urllib.request.urlopen("http://127.0.0.1:5050/login", timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(1)
    raise RuntimeError("La aplicación no respondió después del reinicio.")


if __name__ == "__main__":
    deploy()
