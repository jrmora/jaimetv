import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class CatalogError(Exception):
    """Error operativo seguro para mostrar sin exponer credenciales."""


ACTIONS = {
    "vod_categories": "get_vod_categories",
    "series_categories": "get_series_categories",
    "movies": "get_vod_streams",
    "series": "get_series",
}
CONFIG_PATH = Path(__file__).resolve().parent / "config.local.json"


class XtreamClient:
    def __init__(self, server_url, username, password):
        if not all(isinstance(value, str) and value.strip() for value in (server_url, username, password)):
            raise CatalogError(
                "Completa server_url, username y password en config.local.json "
                "o configura XTREAM_SERVER_URL, XTREAM_USERNAME y XTREAM_PASSWORD."
            )
        try:
            parts = urlsplit(server_url.strip())
            valid = (
                parts.scheme in {"http", "https"}
                and parts.hostname
                and parts.port != 0
                and not parts.username
                and not parts.password
                and not parts.query
                and not parts.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise CatalogError("XTREAM_SERVER_URL debe ser una URL HTTP(S) sin credenciales ni parámetros.")
        path = parts.path.rstrip("/")
        if path.endswith("/player_api.php"):
            path = path.removesuffix("/player_api.php")
        self.endpoint = urlunsplit(
            (parts.scheme, parts.netloc, path + "/player_api.php", "", "")
        )
        self.credentials = {"username": username, "password": password}
        self.session = requests.Session()
        self.info_session = requests.Session()
        retry = Retry(
            total=2,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

    @classmethod
    def from_env(cls):
        fields = {
            "server_url": "XTREAM_SERVER_URL",
            "username": "XTREAM_USERNAME",
            "password": "XTREAM_PASSWORD",
        }
        config = {}
        if not all(env in os.environ for env in fields.values()):
            try:
                content = CONFIG_PATH.read_text(encoding="utf-8")
            except FileNotFoundError:
                content = None
            except OSError:
                raise CatalogError("No se pudo leer config.local.json. Revisa los permisos.") from None
            except UnicodeError:
                raise CatalogError("config.local.json debe estar codificado en UTF-8.") from None
            if content is not None:
                try:
                    config = json.loads(content)
                except ValueError:
                    raise CatalogError("config.local.json no contiene JSON válido. Revisa comillas y comas.") from None
                if not isinstance(config, dict) or any(
                    key not in fields or not isinstance(value, str)
                    for key, value in config.items()
                ):
                    raise CatalogError(
                        "config.local.json debe ser un objeto con server_url, username y password como texto."
                    )
        return cls(**{
            field: os.environ.get(env, config.get(field))
            for field, env in fields.items()
        })

    def _request(self, action, parameters=None, timeout=120, session=None):
        try:
            with (session or self.session).get(
                self.endpoint,
                params={**self.credentials, "action": action, **(parameters or {})},
                timeout=(10, timeout),
                allow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    hint = {
                        429: " Demasiadas peticiones; espera antes de volver a intentarlo.",
                        503: " Servicio no disponible en el proveedor; espera y vuelve a intentarlo.",
                    }.get(response.status_code, "")
                    raise CatalogError(
                        f"{action}: la API respondió HTTP {response.status_code}.{hint}"
                    )
                payload = response.json()
        except ValueError:
            raise CatalogError(f"{action}: la API no devolvió JSON válido.") from None
        except requests.exceptions.SSLError:
            raise CatalogError(
                f"{action}: fallo TLS. Comprueba el certificado y el protocolo HTTPS del servidor."
            ) from None
        except requests.Timeout:
            raise CatalogError(
                f"{action}: tiempo de espera agotado al conectar o leer la respuesta del servidor."
            ) from None
        except requests.RequestException:
            # Los errores de requests pueden incluir la URL con usuario y contraseña.
            raise CatalogError(f"{action}: fallo de conexión, TLS o tiempo de espera.") from None
        return payload

    def fetch(self, action):
        payload = self._request(action)
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise CatalogError(
                f"{action}: se esperaba una lista de objetos; comprueba el acceso y las credenciales."
            )
        return payload

    def fetch_info(self, kind, record_id):
        actions = {"movies": ("get_vod_info", "vod_id"), "series": ("get_series_info", "series_id")}
        if kind not in actions or not isinstance(record_id, str) or not record_id.isascii() or not record_id.isdecimal():
            raise CatalogError("Identificador de ficha del proveedor inválido.")
        action, parameter = actions[kind]
        # Interactive queries must not inherit the long retries used by full catalog sync.
        payload = self._request(action, {parameter: record_id}, timeout=15, session=self.info_session)
        sections = ("info", "movie_data") if kind == "movies" else ("info", "seasons", "episodes")
        if (not isinstance(payload, dict) or not any(payload.get(key) for key in sections)
                or not isinstance(payload.get("info", {}), dict)
                or (kind == "movies" and not isinstance(payload.get("movie_data", {}), dict))
                or (kind == "series" and not isinstance(payload.get("episodes", {}), (dict, list)))):
            raise CatalogError(f"{action}: el proveedor no devolvió una ficha válida.")
        if kind == "series":
            episodes = payload.get("episodes", {})
            groups = episodes.values() if isinstance(episodes, dict) else [episodes]
            if any(not isinstance(group, list) or any(not isinstance(episode, dict) for episode in group)
                   for group in groups):
                raise CatalogError(f"{action}: el proveedor devolvió episodios con formato inválido.")
        return payload

    def download(self):
        return {table: self.fetch(action) for table, action in ACTIONS.items()}

    def close(self):
        self.session.close()
        self.info_session.close()
