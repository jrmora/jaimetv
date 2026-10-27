"""Safe, display-only metadata from individual Xtream copies."""

import hashlib
import json
import re
from urllib.parse import quote, quote_plus


FIELDS = (
    ("duration", "Duración del archivo"), ("duration_secs", "Duración (segundos)"),
    ("container_extension", "Formato"), ("size", "Tamaño declarado"),
    ("bitrate", "Tasa de bits"), ("season", "Temporada"),
    ("season_number", "Temporada"), ("episode_count", "Episodios"),
)
TRACK_FIELDS = (
    ("codec_name", "Códec"), ("codec_long_name", "Descripción del códec"), ("codec", "Códec"),
    ("profile", "Perfil"), ("width", "Ancho"), ("height", "Alto"),
    ("r_frame_rate", "Fotogramas por segundo"), ("avg_frame_rate", "Fotogramas por segundo (media)"),
    ("sample_rate", "Frecuencia de muestreo"), ("channels", "Canales"),
    ("channel_layout", "Distribución de canales"), ("bit_rate", "Tasa de bits"),
)
LANGUAGES = {
    "es": "Español", "spa": "Español", "en": "Inglés", "eng": "Inglés",
    "fr": "Francés", "fra": "Francés", "fre": "Francés",
    "pt": "Portugués", "por": "Portugués", "it": "Italiano", "ita": "Italiano",
    "de": "Alemán", "deu": "Alemán", "ger": "Alemán", "ja": "Japonés", "jpn": "Japonés",
}


def provider_fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def redact_provider_text(value, credentials=()):
    value = re.sub(r"(?:[a-z][a-z0-9+.-]*://|www\.)\S+", "[enlace omitido]", value, flags=re.I)
    secrets = {
        encoded for secret in credentials if isinstance(secret, str) and secret
        for encoded in (secret, quote(secret, safe=""), quote_plus(secret))
    }
    for secret in sorted(secrets, key=len, reverse=True):
        value = value.replace(secret, "[dato privado]")
    return value


def provider_diagnostic(payload, credentials=()):
    credentials = tuple(credentials)

    def redact(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                sensitive = re.search(
                    r"user_?name|password|passwd|secret|token|authorization|cookie|api_?key|"
                    r"direct_source|stream_url|playback|file_?path|server_url",
                    key, re.I,
                )
                result[redact_provider_text(key, credentials)] = "[dato privado]" if sensitive else redact(item)
            return result
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, str):
            return redact_provider_text(value, credentials)
        if isinstance(value, (int, float)) and str(value) in credentials:
            return "[dato privado]"
        return value

    return redact(payload)


def normalize_provider_info(payload, kind, credentials=()):
    def safe(value):
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return ""
        return redact_provider_text(str(value).strip(), credentials)[:2000]

    def rows(*sources):
        result = []
        seen = set()
        for source in sources:
            if not isinstance(source, dict):
                continue
            for key, label in FIELDS:
                value = safe(source.get(key))
                if value and label not in seen:
                    result.append({"label": label, "value": value})
                    seen.add(label)
        return technical_fields(result)

    def tracks(info, media):
        found = []
        value = info.get(media)
        if isinstance(value, dict):
            found.append(value)
        elif isinstance(value, list):
            found.extend(track for track in value if isinstance(track, dict))
        stream_keys = (f"{media}_streams", "streams")
        if media == "subtitle":
            stream_keys += ("subtitles",)
        for key in stream_keys:
            streams = info.get(key)
            if isinstance(streams, list):
                found.extend(track for track in streams if isinstance(track, dict)
                             and (key != "streams" or track.get("codec_type") == media))
        result = []
        for track in found:
            tags = track.get("tags")
            tags = tags if isinstance(tags, dict) else {}
            language = safe(tags.get("language")) or safe(track.get("language")) or safe(track.get("lang"))
            code = language.lower().replace("_", "-").split("-")[0]
            if code in {"und", "unknown", "undefined", "null"}:
                language = ""
            elif code in LANGUAGES:
                language = f"{LANGUAGES[code]} ({language})"
            fields = []
            if media in {"audio", "subtitle"}:
                fields.append({"label": "Idioma declarado", "value": language or "Idioma no informado"})
            index = safe(track.get("index"))
            if index:
                fields.append({"label": "Índice de pista", "value": index})
            title = safe(tags.get("title")) or safe(track.get("title"))
            if title:
                fields.append({"label": "Nombre de pista", "value": title})
            fields.extend({"label": label, "value": value} for key, label in TRACK_FIELDS
                          if (value := safe(track.get(key))))
            if fields and fields not in result:
                result.append(fields)
        return result

    def technical(info):
        if not isinstance(info, dict):
            info = {}
        return {"fields": rows(info), "audio": tracks(info, "audio"),
                "video": tracks(info, "video"), "subtitles": tracks(info, "subtitle")}

    info = payload.get("info", {})
    result = technical(info)
    result["fields"] = rows(info, payload.get("movie_data", {}))
    result["episodes"] = []
    if kind == "series":
        episodes = payload.get("episodes", {})
        groups = episodes.items() if isinstance(episodes, dict) else [("", episodes)]
        for season, entries in groups:
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                episode = technical(entry.get("info"))
                episode.update(
                    season=safe(entry.get("season")) or safe(season) or "Sin temporada",
                    number=safe(entry.get("episode_num")) or "Sin número",
                    title=safe(entry.get("title")) or "Sin título",
                    fields=rows(entry.get("info"), entry),
                )
                result["episodes"].append(episode)
        seasons = payload.get("seasons")
        result["seasons"] = [
            {"number": safe(season.get("season_number")) or safe(season.get("season")) or "Sin número",
             "fields": rows(season)}
            for season in seasons if isinstance(season, dict)
        ] if isinstance(seasons, list) else []
    return result


def technical_fields(fields):
    labels = {label for _, label in FIELDS}
    aliases = {
        "Duración": "Duración del archivo",
        "Tasa de bits declarada": "Tasa de bits",
        "Tasa de bits (unidad no informada)": "Tasa de bits",
    }
    result = [
        {"label": aliases.get(field["label"], field["label"]), "value": field["value"]}
        for field in fields if aliases.get(field["label"], field["label"]) in labels
    ]
    if any(field["label"] == "Duración del archivo" for field in result):
        result = [field for field in result if field["label"] != "Duración (segundos)"]
    return result


def provider_presentation(data, kind):
    # Older saved results can be adapted without another request to the provider.
    if isinstance(data.get("diagnostic"), dict):
        result = normalize_provider_info(data["diagnostic"], kind)
        result["diagnostic"] = data["diagnostic"]
        return result
    result = {**data, "fields": technical_fields(data["fields"])}
    result["episodes"] = [{**episode, "fields": technical_fields(episode["fields"])}
                          for episode in data.get("episodes", [])]
    if kind == "series":
        result["seasons"] = [{**season, "fields": technical_fields(season["fields"])}
                             for season in data.get("seasons", [])]
    return result
