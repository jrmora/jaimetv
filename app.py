import argparse
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, url_for

from catalog import CatalogStore, KINDS, SORT_COLUMNS, CHILDREN_CATEGORIES, PROVIDER_ACCESS, TMDB_STATUSES, MOVIE_ERAS, people_names, non_latin_title, youtube_trailer_url, valid_image, PERFORMANCE_TIMINGS
from xtream import CatalogError, XtreamClient
from tmdb import TMDBClient, TMDBError, TMDBRateLimitError, enrich, fingerprint, recommended_titles
from provider_logos import provider_icons
from tmdb_jobs import TMDBBatch
from platforms import ordered_platforms
from auth import configure_auth
from updater import read_state, write_state


LABELS = {"movies": "Películas", "series": "Series"}
DEFAULT_DB = Path(__file__).resolve().parent / "catalog.db"


def create_app(db_path=None):
    app = Flask(__name__)
    app.logger.setLevel(logging.INFO)
    app.secret_key = secrets.token_hex(32)

    @app.before_request
    def start_performance_measurement():
        g.performance_started = time.perf_counter()
        g.performance_id = secrets.token_hex(8)
        g.performance_timings = {}
        g.performance_token = PERFORMANCE_TIMINGS.set(g.performance_timings)

    configure_auth(app)
    store = CatalogStore(db_path or os.environ.get("IPTV_DB_PATH", DEFAULT_DB))
    app.config["CATALOG_STORE"] = store
    sync_lock = threading.Lock()
    sync_state = {"status": "idle"}
    tmdb_batch = TMDBBatch(store, app.logger)
    app.config["TMDB_BATCH"] = tmdb_batch
    update_directory = os.environ.get("IPTV_UPDATE_STATE")
    app.config["UPDATE_ENABLED"] = bool(update_directory and app.config["AUTH_ENABLED"])
    update_lock = threading.RLock()

    def update_status():
        try:
            return read_state(Path(update_directory))
        except (OSError, ValueError):
            app.logger.error("No se pudo leer el estado de actualización.")
            abort(503, description="No se pudo consultar la actualización.")

    @app.after_request
    def report_performance(response):
        if "performance_started" not in g:
            return response
        elapsed = (time.perf_counter() - g.performance_started) * 1000
        timings = {**g.performance_timings, "total": elapsed}
        response.headers["Server-Timing"] = ", ".join(
            f"{name};dur={duration:.1f}" for name, duration in timings.items()
        )
        response.headers["X-Request-ID"] = g.performance_id
        if request.endpoint != "static":
            log = app.logger.warning if elapsed >= 1000 else app.logger.info
            log("performance request_id=%s endpoint=%s method=%s status=%s %s",
                g.performance_id, request.endpoint, request.method, response.status_code,
                " ".join(f"{name}_ms={duration:.1f}" for name, duration in timings.items()))
        return response

    @app.teardown_request
    def clear_performance_measurement(error):
        token = g.pop("performance_token", None)
        if token is not None:
            PERFORMANCE_TIMINGS.reset(token)

    @app.before_request
    def pause_writes_during_update():
        if app.config["UPDATE_ENABLED"] and request.method == "POST":
            update_lock.acquire()
            g.update_locked = True
        if app.config["UPDATE_ENABLED"] and request.endpoint not in {"application_update", "static", "login"}:
            if update_status()["status"] == "running":
                response = jsonify(error="Actualización en curso. Espera a que termine.")
                response.headers["Retry-After"] = "3"
                return response, 503

    @app.teardown_request
    def release_update_lock(error):
        if g.pop("update_locked", False):
            update_lock.release()

    @app.route("/api/application-update", methods=["GET", "POST"])
    def application_update():
        if not app.config["UPDATE_ENABLED"]:
            abort(404)
        if request.method == "GET":
            return jsonify(update_status())
        check_sync_csrf()
        with update_lock, sync_lock:
            if update_status()["status"] == "running":
                return jsonify(error="Ya hay una actualización en curso."), 409
            if sync_state["status"] == "running" or tmdb_batch.status()["status"] == "running":
                return jsonify(error="Espera a que termine la sincronización o el lote TMDB antes de actualizar."), 409
            directory = Path(update_directory)
            try:
                write_state(directory, status="running", message="Iniciando actualización.")
                (directory / "request").write_text(secrets.token_hex(16))
            except OSError:
                app.logger.error("No se pudo iniciar el servicio de actualización.")
                write_state(directory, status="failed", message="No se pudo iniciar la actualización.")
                return jsonify(error="No se pudo iniciar la actualización. Revisa el servicio de Oracle."), 503
        return jsonify(status="running", message="Actualizando aplicación."), 202

    def check_sync_csrf():
        token = request.form.get("csrf", "")
        expected = session.get("sync_csrf")
        if not expected or not token.isascii() or not secrets.compare_digest(token, expected):
            abort(400, description="La sesión ha caducado. Recarga la página.")

    @app.route("/api/tmdb/batch", methods=["GET", "POST"])
    def tmdb_batch_job():
        if request.method == "GET":
            return jsonify(tmdb_batch.status())
        check_sync_csrf()
        action = request.form.get("action", "start")
        if action == "cancel":
            return jsonify(tmdb_batch.cancel())
        kind = request.form.get("kind")
        limit = request.form.get("limit", "50")
        if action not in {"start", "directory"} or kind not in {"movies", "series"}:
            abort(400, description="Proceso TMDB inválido.")
        if not limit.isascii() or not limit.isdecimal() or len(limit) > 3 or not 1 <= int(limit) <= 500:
            abort(400, description="El lote debe contener entre 1 y 500 títulos.")
        try:
            started = tmdb_batch.start(
                kind, int(limit) if action == "start" else 0,
                app.config.get("TMDB_CLIENT_FACTORY", TMDBClient),
            )
        except TMDBError as exc:
            return jsonify(error=str(exc)), 500
        if not started:
            return jsonify(error="Ya hay un proceso TMDB en curso."), 409
        return jsonify(status="running"), 202

    @app.context_processor
    def sync_controls():
        if "sync_csrf" not in session:
            session["sync_csrf"] = secrets.token_hex(32)
        return {"sync_csrf": session["sync_csrf"]}

    def run_sync():
        try:
            client = app.config.get("XTREAM_CLIENT_FACTORY", XtreamClient.from_env)()
            try:
                totals = store.sync(client)
            finally:
                client.close()
            result = {
                "status": "completed", "totals": totals,
                "changes": store.sync_report, "duplicates": store.duplicates,
            }
        except (CatalogError, sqlite3.Error, OSError) as exc:
            message = str(exc) if isinstance(exc, CatalogError) else "No se pudo guardar el catálogo. Revisa el almacenamiento y los permisos."
            app.logger.warning("Sincronización web fallida: %s", message)
            result = {"status": "failed", "error": message}
        except Exception:
            # Do not expose unexpected exception text, which may contain provider credentials.
            app.logger.error("Error inesperado durante la sincronización web.")
            result = {"status": "failed", "error": "Error inesperado durante la sincronización. Consulta el registro del servidor."}
        with sync_lock:
            sync_state.clear()
            sync_state.update(result)

    @app.route("/api/sync", methods=["GET", "POST"])
    def catalog_sync():
        if request.method == "POST":
            check_sync_csrf()
            with sync_lock:
                if sync_state["status"] == "running":
                    return jsonify(error="Ya hay una sincronización en curso."), 409
                sync_state.clear()
                sync_state.update(status="running")
                try:
                    threading.Thread(target=run_sync, name="catalog-sync", daemon=True).start()
                except RuntimeError:
                    sync_state.update(status="failed", error="No se pudo iniciar la sincronización.")
                    app.logger.error("No se pudo iniciar el hilo de sincronización web.")
                    return jsonify(sync_state), 500
            return jsonify(status="running"), 202
        with sync_lock:
            return jsonify(sync_state)

    def presentation():
        view = request.args.get("view", "table" if request.endpoint == "catalog_api" else "cards")
        if view not in {"table", "cards"}:
            abort(400, description="Vista inválida.")
        return view if request.endpoint == "catalog_api" else "cards"

    def card_provider_icons(result):
        groups = {
            item["id"]: item["matched_services"] for item in result["items"]
            if item.get("matched_services")
        }
        if groups:
            icons = provider_icons(groups)
            for item in result["items"]:
                item["service_icons"] = icons.get(item["id"], [])

    def page_number(key="page"):
        value = request.args.get(key, "1")
        if not value.isascii() or not value.isdecimal() or len(value) > 9 or int(value) < 1:
            abort(400, description="Página inválida.")
        return int(value)

    def page_links(result, endpoint, values, key="page"):
        if "page" not in result:
            return []
        numbers = sorted({1, result["pages"], *range(max(1, result["page"] - 2), min(result["pages"], result["page"] + 2) + 1)})
        links = []
        for label, number in [("Anterior", result["page"] - 1), *[(str(n), n) for n in numbers], ("Siguiente", result["page"] + 1)]:
            links.append({
                "label": label, "current": label == str(result["page"]),
                "url": url_for(endpoint, **{**values, key: number}) if 1 <= number <= result["pages"] else None,
            })
        return links

    def parameters():
        children = request.args.getlist("children")
        children = children[-1] if children else request.cookies.get("children_visibility", "exclude")
        if children not in {"all", "only", "exclude"}:
            abort(400, description="Filtro infantil inválido.")
        foreign = request.args.get("foreign", "0")
        if foreign not in {"0", "1"}:
            abort(400, description="El catálogo debe indicarse con foreign=0 o foreign=1.")
        sort = request.args.get("sort", "desc")
        sort_by = request.args.get("sort_by", "rating")
        if "ordering" in request.args:
            sort_by, separator, sort = request.args["ordering"].rpartition(":")
            if not separator or sort not in {"asc", "desc"}:
                abort(400, description="Ordenamiento inválido.")
        if sort_by not in SORT_COLUMNS:
            abort(400, description="Columna de ordenamiento inválida.")
        q = request.args.get("q", "").strip()
        actor = request.args.get("actor", "").strip()
        director = request.args.get("director", "").strip()
        categories = list(dict.fromkeys(value.strip() for value in request.args.getlist("category") if value.strip()))
        genres = list(dict.fromkeys(value.strip() for value in request.args.getlist("genre") if value.strip()))
        providers = list(dict.fromkeys(value.strip() for value in request.args.getlist("provider") if value.strip()))
        mine = request.args.getlist("my_services")
        mine = mine[-1] if mine else "0"
        access = request.args.get("access", "flatrate")
        if len(providers) > 100 or any(len(value) > 200 for value in providers) or mine not in {"0", "1", "2"} or access not in PROVIDER_ACCESS:
            abort(400, description="Filtro de plataformas inválido (hasta 100 plataformas de 200 caracteres).")
        kind = (request.view_args or {}).get("kind")
        eras = list(dict.fromkeys(request.args.getlist("era")))
        if any(value not in MOVIE_ERAS for value in eras) or (eras and kind not in {None, "movies"}):
            abort(400, description="Época de películas inválida.")
        tmdb_status = request.args.get("tmdb_status", "all")
        if tmdb_status not in TMDB_STATUSES:
            abort(400, description="Estado TMDB inválido.")
        watched = request.args.get("watched", "unseen" if kind == "movies" else "all")
        banned = request.args.get("banned", "hide")
        if banned not in {"hide", "only"} or (kind not in {None, "movies"} and banned != "hide"):
            abort(400, description="Filtro de películas bloqueadas inválido.")
        if watched not in {"all", "seen", "unseen"} or (kind not in {None, "movies"} and watched != "all"):
            abort(400, description="Filtro de películas vistas inválido.")
        if len(categories) > 100 or len(genres) > 100 or any(len(value) > 200 for value in categories + genres):
            abort(400, description="Selecciona hasta 100 categorías y géneros de hasta 200 caracteres.")
        if children == "exclude" and request.endpoint in {"listing", "catalog_api"}:
            kind = request.view_args["kind"]
            if kind in KINDS:
                child_names = {name.casefold() for name in CHILDREN_CATEGORIES[kind]}
                child_ids = {
                    item["category_id"] for item in store.categories(kind)
                    if item["category_name"].casefold() in child_names
                }
                categories = [value for value in categories if value not in child_ids]
        if len(actor) > 200 or len(director) > 200:
            abort(400, description="Actor y director admiten hasta 200 caracteres.")
        if sort not in {"asc", "desc"} or len(q) > 200:
            abort(400, description="Filtros inválidos: orden asc/desc y búsqueda hasta 200 caracteres.")
        values = dict(
            q=q, actor=actor, director=director, category=categories if len(categories) > 1 else (categories[0] if categories else None),
            genre=genres,
            sort=sort, sort_by=sort_by, foreign=int(foreign), children=children,
        )
        # Preserve the API's default response and existing unfiltered URL shapes.
        if providers or mine != "0" or "access" in request.args:
            values.update(provider=ordered_platforms(providers), my_services=int(mine), access=access)
        if kind == "movies" or "watched" in request.args:
            values["watched"] = watched
        if "banned" in request.args:
            values["banned"] = banned
        if "tmdb_status" in request.args:
            values["tmdb_status"] = tmdb_status
        if eras:
            values["era"] = eras
        if "watchlist" in request.args:
            if request.args["watchlist"] not in {"0", "1"}:
                abort(400, description="Filtro de lista inválido.")
            values["watchlist"] = int(request.args["watchlist"])
        return values

    @app.post("/catalog/<kind>/<record_id>/watchlist")
    def title_watchlist(kind, record_id):
        check_sync_csrf()
        value = request.form.get("watchlist")
        if kind not in KINDS:
            abort(404)
        if value not in {"0", "1"}:
            abort(400, description="Estado de lista inválido.")
        if not store.set_watchlisted(kind, record_id, value == "1"):
            abort(404, description="El título no existe en el catálogo local.")
        if request.accept_mimetypes.best == "application/json":
            return jsonify(id=record_id, watchlist=value == "1")
        return redirect(url_for("detail", kind=kind, id=record_id), code=303)

    @app.post("/catalog/movies/<record_id>/banned")
    def movie_banned(record_id):
        check_sync_csrf()
        value = request.form.get("banned")
        if value not in {"0", "1"}:
            abort(400, description="Estado de película bloqueada inválido.")
        filters = parameters()
        if not store.set_movie_banned(record_id, value == "1"):
            abort(404, description="La película no existe en el catálogo local.")
        if request.accept_mimetypes.best == "application/json":
            return jsonify(id=record_id, banned=value == "1")
        return redirect(url_for("detail", kind="movies", id=record_id, **filters), code=303)

    @app.post("/catalog/movies/<record_id>/watched")
    def movie_watched(record_id):
        check_sync_csrf()
        value = request.form.get("watched")
        if value not in {"0", "1"}:
            abort(400, description="Estado de película vista inválido.")
        filters = parameters()
        if not store.set_movie_watched(record_id, value == "1"):
            abort(404, description="La película no existe en el catálogo local.")
        if request.accept_mimetypes.best == "application/json":
            return jsonify(id=record_id, watched=value == "1")
        return redirect(url_for("detail", kind="movies", id=record_id, **filters), code=303)

    @app.get("/")
    def index():
        return redirect(url_for("listing", kind="movies"))

    @app.get("/dashboard")
    def dashboard():
        return redirect(url_for("listing", kind="movies"))

    @app.get("/admin")
    def administration():
        kind = request.args.get("kind", "movies")
        if kind not in {"movies", "series"}:
            abort(400, description="Catálogo TMDB inválido.")
        dates = store.maintenance_dates(kind)
        dates["application"] = None
        if app.config["UPDATE_ENABLED"]:
            state = update_status()
            timestamp = state.get("last_success_at")
            if not timestamp and state.get("status") == "completed":
                timestamp = state.get("updated_at")
            if timestamp:
                dates["application"] = datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")
        return render_template(
            "admin.html", labels=LABELS, active="admin", kind=kind,
            coverages={value: store.tmdb_coverage(value) for value in ("movies", "series")},
            movie_title_count=store.movie_title_count(),
            maintenance_dates=dates,
            maintenance_dates_display={
                key: datetime.fromisoformat(value).astimezone(ZoneInfo("America/Toronto")).strftime("%Y-%m-%d %H:%M:%S %Z")
                for key, value in dates.items() if value
            },
            sync_summary=store.last_sync_summary(),
        )

    @app.get("/movies", defaults={"kind": "movies"})
    @app.get("/series", defaults={"kind": "series"})
    @app.get("/kids/<kind>", endpoint="kids_listing")
    def listing(kind):
        if kind not in KINDS:
            abort(404)
        filters = parameters()
        kids = request.endpoint == "kids_listing"
        if kids:
            filters.update(children="only", foreign=0)
        elif filters["children"] == "only":
            return redirect(url_for("kids_listing", kind=kind, **filters))
        view = presentation()
        result = store.query(kind, **filters, page=page_number())
        card_provider_icons(result)
        filters.update(view=view, page=result.get("page", 1))
        foreign = bool(filters["foreign"])
        categories = []
        if kids:
            categories = [item for item in categories if item["category_name"].casefold() in {
                name.casefold() for name in CHILDREN_CATEGORIES[kind]
            }]
        names = []
        if kind in {"movies", "series"}:
            names = ordered_platforms(
                set(store.provider_names(kind)) | set(store.subscriptions()) | set(filters.get("provider", [])),
            )
        return render_template(
            "catalog.html", kind=kind,
            title=f"Niños · {LABELS[kind]}" if kids else (f"Otros alfabetos · {LABELS[kind]}" if foreign else LABELS[kind]),
            active="kids" if kids else ("foreign" if foreign else kind),
            kids=kids, listing_endpoint="kids_listing" if kids else "listing",
            labels=LABELS, categories=categories, filters=filters, result=result,
            children_category_ids={
                item["category_id"] for item in categories
                if item["category_name"].casefold() in {name.casefold() for name in CHILDREN_CATEGORIES[kind]}
            },
            genres=store.genres(kind), movie_eras=MOVIE_ERAS,
            provider_options=provider_icons({"all": names})["all"],
            subscriptions=ordered_platforms(store.subscriptions()),
            selected_categories=filters["category"] if isinstance(filters["category"], list) else ([filters["category"]] if filters["category"] else []),
            pagination=page_links(result, "kids_listing" if kids else "listing", dict(kind=kind, **filters)),
        )

    @app.get("/catalog/<kind>/detail")
    def detail(kind):
        if kind not in KINDS:
            abort(404)
        record = store.detail(kind, request.args.get("id", ""))
        if record is None:
            abort(404, description="El registro no existe en el catálogo local.")
        filters = parameters()
        filters["view"] = presentation()
        filters["page"] = page_number()
        if kind in {"movies", "series"}:
            session.setdefault("tmdb_csrf", secrets.token_urlsafe(32))
        filters["foreign"] = int(non_latin_title(record["name"]))
        display_data = {
            key: value for key, value in record["data"].items()
            if key not in {"stream_icon", "cover", "backdrop_path"}
            and not (key == "stream_type" and kind in {"movies", "series"})
            and value is not None
            and not (isinstance(value, str) and not value.strip())
            and not (isinstance(value, (list, dict)) and not value)
        }
        name = str(display_data.pop("name", "")).strip()
        title = str(display_data.pop("title", "")).strip()
        if record.get("version_count", 1) <= 1:
            record["name"] = name or title or "Sin título"

        def comparable_title(value):
            without_year = re.sub(r"\s*\(\d{4}\)\s*$", "", value)
            return " ".join(without_year.split()).casefold()

        if name and title and comparable_title(name) != comparable_title(title):
            display_data["alternative_title"] = title
        trailer_url = youtube_trailer_url(display_data.get("youtube_trailer"))
        for key in ("rating", "rating_5based"):
            display_data.pop(key, None)
        premiere = []
        for key in ("year", "release_date", "releaseDate"):
            if key not in display_data:
                continue
            value = str(display_data.pop(key)).strip()
            if key != "year":
                try:
                    value = datetime.strptime(value, "%Y-%m-%d").strftime("%d/%m/%Y")
                except ValueError:
                    pass
            if value not in premiere:
                premiere.append(value)
        if premiere:
            display_data["premiere"] = " · ".join(premiere)
        category_names = {
            str(category["category_id"]): category["category_name"]
            for category in store.categories(kind)
        }

        def category_name(category_id):
            return category_names.get(str(category_id)) or f"Categoría desconocida (ID {category_id})"

        category_links = {}
        if "category_id" in display_data:
            category_id = display_data.pop("category_id")
            display_data["category_name"] = category_name(category_id)
            category_links["category_name"] = [(str(category_id), category_name(category_id))]
        if "category_ids" in display_data:
            ids = display_data.pop("category_ids")
            if not isinstance(ids, list):
                ids = [ids]
            ids = list(dict.fromkeys(str(value) for value in ids))
            display_data["category_names"] = [category_name(value) for value in ids]
            category_links["category_names"] = [(value, category_name(value)) for value in ids]
            if category_links["category_names"] == category_links.get("category_name"):
                del display_data["category_names"]
                del category_links["category_names"]
        for key in ("added", "modified", "last_modified"):
            if key not in display_data:
                continue
            value = display_data[key]
            try:
                if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                    raise ValueError("Timestamp inválido")
                date = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=float(value))
                display_data[key] = date.strftime("%d/%m/%Y")
            except (ValueError, OverflowError):
                display_data[key] = f"Fecha no válida: {value}"
        people = {
            key: people_names(display_data[key])
            for key in ("cast", "director") if key in display_data
        }
        ordered_keys = list(display_data)
        if "genre" in ordered_keys:
            category_keys = [key for key in ("category_name", "category_names") if key in ordered_keys]
            ordered_keys = [key for key in ordered_keys if key not in category_keys]
            position = ordered_keys.index("genre") + 1
            ordered_keys[position:position] = category_keys
        fields = [
            (key, value if isinstance(value := display_data[key], str) else json.dumps(value, ensure_ascii=False, indent=2))
            for key in ordered_keys
            if key not in {"num", "stream_id", "series_id", "container_extension", "added"}
        ]
        image_url = valid_image(record["data"].get("stream_icon")) or valid_image(record["data"].get("cover"))
        backdrops = record["data"].get("backdrop_path", []) if kind == "series" else []
        if isinstance(backdrops, str):
            backdrops = [backdrops]
        if not isinstance(backdrops, list):
            backdrops = []
        backdrop_urls = []
        for candidate in backdrops:
            url = valid_image(candidate)
            if url and url != image_url and url not in backdrop_urls:
                backdrop_urls.append(url)
        response = app.make_response(render_template(
            "detail.html", record=record, fields=fields, trailer_url=trailer_url, people=people, category_links=category_links, kind=kind, image_url=image_url, backdrop_urls=backdrop_urls, filters=filters,
            content_metadata=store.tmdb_cached(kind, request.args.get("id", ""), "CA", fingerprint(record["data"])) if kind in {"movies", "series"} else None,
            back_url=(
                url_for("person_results", name=request.args["person"], view=filters["view"],
                        movies_page=page_number("movies_page"), series_page=page_number("series_page"))
                if request.args.get("person") else
                url_for("kids_listing" if filters["children"] == "only" else "listing", kind=kind, **filters)
            ),
            category_endpoint="kids_listing" if filters["children"] == "only" else "listing",
            labels=LABELS, active="kids" if filters["children"] == "only" else ("foreign" if filters["foreign"] else kind),
            category_foreign=int(non_latin_title(record["name"])),
        ))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/people")
    def person_results():
        name = request.args.get("name", "").strip()
        if not name or len(name) > 200:
            abort(400, description="El nombre debe tener entre 1 y 200 caracteres.")
        results = {}
        view = presentation()
        pages = {f"{kind}_page": page_number(f"{kind}_page") for kind in ("movies", "series")}
        for kind in ("movies", "series"):
            results[kind] = store.query(kind, person=name, sort_by="rating", sort="desc", page=pages.get(f"{kind}_page"))
            pages[f"{kind}_page"] = results[kind]["page"]
        pagination = {
            kind: page_links(result, "person_results", dict(name=name, view=view, **pages), f"{kind}_page")
            for kind, result in results.items()
        }
        return render_template("people.html", name=name, results=results, labels=LABELS,
                               view=view, pagination=pagination, people_pages=pages)

    @app.route("/api/tmdb/<kind>/<record_id>", methods=["GET", "POST"])
    def tmdb_enrichment(kind, record_id):
        if kind not in {"movies", "series"} or store.detail(kind, record_id) is None:
            abort(404, description="La ficha no existe o no admite TMDB.")
        selected = None
        manual_id = None
        if request.method == "POST":
            token = request.form.get("csrf", "")
            expected = session.get("tmdb_csrf")
            if not expected or not token.isascii() or not secrets.compare_digest(token, expected):
                abort(400, description="La sesión ha caducado. Recarga la ficha.")
            if request.form.get("selected"):
                try:
                    selected = int(request.form["selected"])
                except ValueError:
                    abort(400, description="Candidato inválido.")
            if "manual_tmdb" in request.form:
                value = request.form["manual_tmdb"].strip()
                media = "movie" if kind == "movies" else "tv"
                match = re.fullmatch(
                    rf"https://(?:www\.)?themoviedb\.org/{media}/([1-9][0-9]*)(?:-[^/?#\s]+)?/?(?:[?#][^\s]*)?",
                    value,
                )
                identifier = match.group(1) if match else value
                if selected is not None or not re.fullmatch(r"[1-9][0-9]{0,14}", identifier):
                    abort(400, description="Introduce un ID positivo o un enlace TMDB del mismo tipo de ficha.")
                manual_id = int(identifier)
        try:
            data = enrich(
                store, kind, record_id,
                client_factory=app.config.get("TMDB_CLIENT_FACTORY", TMDBClient),
                refresh=request.method == "POST", selected=selected, manual_id=manual_id,
            )
        except TMDBError as exc:
            app.logger.warning("No se pudo enriquecer la ficha con TMDB: %s", exc)
            if isinstance(exc, TMDBRateLimitError):
                response = jsonify(error=str(exc))
                response.headers["Retry-After"] = str(exc.retry_after)
                return response, 429
            return jsonify(error=str(exc)), 502
        trailer = data.get("trailer_url")
        trailer_match = re.fullmatch(r"https://www\.youtube\.com/watch\?v=([A-Za-z0-9_-]{11})", trailer) if isinstance(trailer, str) else None
        return jsonify(status=data.get("status"), content_labels=render_template("content_labels.html", tmdb=data), html=render_template(
            "tmdb.html", tmdb=data, provider_icons=provider_icons(data.get("providers", {})),
            trailer_id=trailer_match.group(1) if trailer_match else None,
        ))

    @app.get("/api/tmdb/<kind>/<record_id>/recommendations")
    def tmdb_recommendations(kind, record_id):
        if kind not in KINDS or store.detail(kind, record_id) is None:
            abort(404, description="La ficha no existe.")
        try:
            items = recommended_titles(
                store, kind, record_id,
                client_factory=app.config.get("TMDB_CLIENT_FACTORY", TMDBClient),
            )
        except TMDBError as exc:
            app.logger.warning("No se pudieron consultar recomendaciones TMDB: %s", exc)
            response = jsonify(error=str(exc))
            if isinstance(exc, TMDBRateLimitError):
                response.headers["Retry-After"] = str(exc.retry_after)
                return response, 429
            return response, 502
        return jsonify(html=render_template("recommendations.html", items=items, kind=kind))

    @app.get("/api/catalog/<kind>")
    def catalog_api(kind):
        if kind not in KINDS:
            abort(404)
        filters = parameters()
        view = presentation()
        result = store.query(kind, **filters, page=page_number() if view == "cards" else None)
        if view == "cards":
            card_provider_icons(result)
            filters.update(view=view, page=result["page"])
            endpoint = "kids_listing" if filters["children"] == "only" else "listing"
            result["html"] = render_template(
                "cards.html", result=result, kind=kind, filters=filters,
                pagination=page_links(result, endpoint, dict(kind=kind, **filters)),
            )
        return jsonify(result)

    @app.errorhandler(CatalogError)
    def catalog_error(error):
        app.logger.warning("No se pudo cargar el catálogo: %s", error)
        if request.path.startswith("/api/"):
            return jsonify(error=str(error)), 500
        return render_template("error.html", message=str(error), code=500, labels=LABELS), 500

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(500)
    def http_error(error):
        message = "Error interno. Consulta el registro del servidor." if error.code == 500 else error.description
        if request.path.startswith("/api/"):
            return jsonify(error=message), error.code
        return render_template("error.html", message=message, code=error.code, labels=LABELS), error.code

    @app.after_request
    def security_headers(response):
        if "catalog_view" in request.cookies:
            response.delete_cookie("catalog_view")
        if request.endpoint in {"listing", "catalog_api"} and "children" in request.args:
            children = request.args.getlist("children")[-1]
            if children in {"all", "exclude"} and response.status_code == 200:
                response.set_cookie("children_visibility", children, max_age=31536000, httponly=True, samesite="Lax")
        if request.endpoint in {"listing", "catalog_api", "kids_listing", "detail", "person_results", "dashboard", "catalog_sync", "tmdb_batch_job"}:
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        image_sources = " http: https:" if request.endpoint in {"detail", "listing", "kids_listing", "person_results"} else ""
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' https://cdn.jsdelivr.net; "
            f"script-src 'self'; img-src 'self' data:{image_sources}; "
            "frame-src https://www.youtube-nocookie.com; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="Explorador local de catálogos Xtream Codes")
    parser.add_argument("--db", default=os.environ.get("IPTV_DB_PATH", str(DEFAULT_DB)), help="Ruta de SQLite")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sync", help="Añadir novedades y actualizar cambios, conservando el catálogo y TMDB")
    commands.add_parser("stats", help="Mostrar totales y categorías más grandes")
    search = commands.add_parser("search", help="Buscar en canales, películas y series")
    search.add_argument("query")
    export = commands.add_parser("export", help="Exportar movies.csv o series.csv")
    export.add_argument("destination", type=Path)
    serve = commands.add_parser("serve", help="Abrir el servidor web local")
    serve.add_argument("--port", type=int, default=5000)
    serve.add_argument("--no-reload", action="store_true", help="Desactivar la recarga automática al editar Python")
    args = parser.parse_args(argv)

    try:
        store = CatalogStore(args.db)
        if args.command == "sync":
            client = XtreamClient.from_env()
            try:
                result = store.sync(client)
            finally:
                client.close()
            print("Sincronización completada:")
            for table, total in result.items():
                changes = store.sync_report[table]
                print(f"  {table}: {total} guardados; {changes['new']} nuevos, "
                      f"{changes['updated']} actualizados, {changes['unchanged']} sin cambios, "
                      f"{changes['excluded']} excluidos, {changes['removed_excluded']} retirados por exclusión, "
                      f"{changes['unavailable']} ya no ofrecidos por el proveedor, {changes['restored']} de nuevo disponibles")
            for table, extra in store.duplicates.items():
                print(f"Aviso: {table} tenía {extra} IDs repetidos; se conservó el último de cada uno.")
        elif args.command == "stats":
            stats = store.stats()
            for kind, total in stats["totals"].items():
                print(f"{LABELS[kind]}: {total}")
            print(f"Última sincronización (UTC): {stats['synced_at'] or 'Nunca'}")
            print("\nCategorías más grandes:")
            if stats["largest"]:
                frame = pd.DataFrame(stats["largest"])
                frame["kind"] = frame["kind"].map(LABELS)
                print(frame.rename(columns={"kind": "Tipo", "category": "Categoría", "total": "Total"}).to_string(index=False))
            else:
                print("Sin datos. Ejecuta: python app.py sync")
        elif args.command == "search":
            if not args.query.strip():
                raise CatalogError("Introduce un texto de búsqueda no vacío.")
            for kind in KINDS:
                result = store.query(kind, q=args.query.strip())
                print(f"\n{LABELS[kind]} ({result['total']}):")
                if not result["total"]:
                    print("Sin resultados.")
                for item in result["items"]:
                    print(f"  {item['id']} | {item['name']} | {item['category_name']}")
        elif args.command == "export":
            kind = args.destination.stem.lower()
            if kind not in {"movies", "series"} or args.destination.suffix.lower() != ".csv":
                raise CatalogError("El destino debe llamarse movies.csv o series.csv (puede incluir una carpeta).")
            total = store.export(kind, args.destination)
            print(f"Exportados {total} registros a {args.destination}.")
        elif args.command == "serve":
            if not 1 <= args.port <= 65535:
                raise CatalogError("El puerto debe estar entre 1 y 65535.")
            local_app = create_app(args.db)
            local_app.config["TEMPLATES_AUTO_RELOAD"] = True
            local_app.run(
                host="127.0.0.1", port=args.port, debug=False, use_reloader=not args.no_reload
            )
    except (CatalogError, sqlite3.Error, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
