import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from tmdb import TMDBClient, TMDBError
from platforms import platform_name, ordered_platforms


LOGO_DIR = Path(__file__).resolve().parent / "static" / "providers"


def provider_icons(providers, directory=LOGO_DIR):
    manifest = directory / "manifest.json"
    entries = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
    grouped_entries = {}
    for name in sorted(entries, key=lambda name: name != platform_name(name)):
        filename = entries[name]
        valid = isinstance(filename, str) and re.fullmatch(r"[A-Za-z0-9_-]+\.(?:jpg|png|webp)", filename)
        if valid and (directory / filename).is_file():
            grouped_entries.setdefault(platform_name(name), filename)
    result = {}
    for group, names in providers.items():
        result[group] = []
        for name in ordered_platforms(names):
            filename = grouped_entries.get(name)
            result[group].append({
                "name": name,
                "filename": f"providers/{filename}" if filename else None,
            })
    return result


def download_logo(path, directory):
    if not isinstance(path, str) or not re.fullmatch(r"/[A-Za-z0-9_-]+\.(?:jpg|png|webp)", path):
        raise TMDBError("Ruta de logo de plataforma inválida.")
    filename = path[1:]
    destination = directory / filename
    if destination.is_file() and destination.stat().st_size:
        return filename
    url = "https://image.tmdb.org/t/p/w92" + path
    if sys.platform == "darwin":
        try:
            response = subprocess.run(
                ["/usr/bin/curl", "--fail", "--silent", "--proto", "=https",
                 "--connect-timeout", "10", "--max-time", "30", url],
                capture_output=True, timeout=35,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise TMDBError("No se pudo descargar el logo por HTTPS.") from None
        if response.returncode:
            raise TMDBError(f"Falló la descarga del logo (curl {response.returncode}).")
        content = response.stdout
    else:
        try:
            response = requests.get(url, timeout=(10, 30), allow_redirects=False)
        except requests.RequestException:
            raise TMDBError("No se pudo descargar el logo por HTTPS.") from None
        if response.status_code != 200:
            raise TMDBError(f"Falló la descarga del logo (HTTP {response.status_code}).")
        content = response.content
    if not content or len(content) > 2_000_000 or not (
        content.startswith(b"\xff\xd8\xff") or content.startswith(b"\x89PNG\r\n\x1a\n")
        or (content.startswith(b"RIFF") and content[8:12] == b"WEBP")
    ):
        raise TMDBError("El logo descargado no es una imagen válida.")
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(destination)
    return filename


def sync_logos(client, directory=LOGO_DIR):
    providers = {}
    for media in ("movie", "tv"):
        data = client.get(f"watch/providers/{media}", {"language": "es-ES"})
        for provider in data.get("results", []):
            providers[provider["provider_name"]] = provider.get("logo_path")
    directory.mkdir(parents=True, exist_ok=True)
    paths = sorted({path for path in providers.values() if path})
    downloaded, errors = {}, []

    def fetch(path):
        try:
            return path, download_logo(path, directory), None
        except (TMDBError, OSError) as exc:
            return path, None, str(exc)

    with ThreadPoolExecutor(max_workers=4) as pool:
        for path, filename, error in pool.map(fetch, paths):
            if error:
                errors.append(f"{path}: {error}")
            else:
                downloaded[path] = filename
    entries = {name: downloaded[path] for name, path in providers.items() if path in downloaded}
    manifest = directory / "manifest.json"
    temporary = directory / "manifest.json.tmp"
    temporary.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest)
    return len(entries), len(downloaded), errors


if __name__ == "__main__":
    try:
        names, images, errors = sync_logos(TMDBClient())
    except (TMDBError, OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    print(f"Plataformas con logo local: {names}; imágenes: {images}.")
    for error in errors:
        print(f"Advertencia: {error}", file=sys.stderr)
    sys.exit(1 if errors else 0)
