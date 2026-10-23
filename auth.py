import json
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path

from flask import abort, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash


def configure_auth(app):
    configured_path = os.environ.get("IPTV_AUTH_PATH")
    path = Path(configured_path) if configured_path else Path(app.root_path) / "auth.local.json"
    if not path.exists() and not configured_path:
        app.config["AUTH_ENABLED"] = False
        return
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(config, dict) or any(
            not isinstance(config.get(key), str) or not config[key].strip()
            for key in ("username", "password_hash", "secret_key")
        ):
            raise ValueError
        if len(config["secret_key"]) < 64:
            raise ValueError
        if not config["password_hash"].startswith(("scrypt:", "pbkdf2:")):
            raise ValueError
        parts = config["password_hash"].split("$")
        if len(parts) != 3 or not parts[1] or not parts[2]:
            raise ValueError
        check_password_hash(config["password_hash"], "configuration-validation")
    except (OSError, ValueError, TypeError):
        raise RuntimeError("No se pudo cargar la configuración de autenticación. Revisa IPTV_AUTH_PATH y sus campos.") from None

    app.secret_key = config["secret_key"]
    app.config.update(
        AUTH_ENABLED=True,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    failures = deque()
    lock = threading.Lock()

    def authenticated():
        issued = session.get("login_time")
        return (
            session.get("login_user") == config["username"]
            and isinstance(issued, (int, float))
            and 0 <= time.time() - issued < 12 * 60 * 60
        )

    @app.before_request
    def require_login():
        if request.endpoint in {"login", "static"} or authenticated():
            return None
        if request.path.startswith("/api/") or request.method != "GET":
            return jsonify(error="Inicia sesión para continuar.", login_url=url_for("login")), 401
        return redirect(url_for("login"))

    @app.context_processor
    def auth_controls():
        return {"auth_logged_in": authenticated()}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        status = 200
        if request.method == "POST":
            token = request.form.get("csrf", "")
            expected = session.get("sync_csrf")
            if not expected or not token.isascii() or not secrets.compare_digest(token, expected):
                abort(400, description="La sesión ha caducado. Recarga la página.")
            with lock:
                now = time.monotonic()
                while failures and now - failures[0] >= 60:
                    failures.popleft()
                if len(failures) >= 10:
                    response = app.make_response((
                        render_template("login.html", error="Demasiados intentos. Espera un minuto."),
                        429,
                    ))
                    response.headers["Retry-After"] = "60"
                    return response
                username = request.form.get("username", "")
                password = request.form.get("password", "")
                valid_password = check_password_hash(config["password_hash"], password) if len(password) <= 1024 else False
                valid_user = secrets.compare_digest(username.encode(), config["username"].encode())
                if valid_user and valid_password:
                    session.clear()
                    session["login_user"] = config["username"]
                    session["login_time"] = time.time()
                    return redirect(url_for("listing", kind="movies"), code=303)
                failures.append(now)
            app.logger.warning("Intento de inicio de sesión rechazado.")
            error = "Usuario o contraseña incorrectos."
            status = 401
        return render_template("login.html", error=error), status

    @app.post("/logout")
    def logout():
        token = request.form.get("csrf", "")
        expected = session.get("sync_csrf")
        if not expected or not token.isascii() or not secrets.compare_digest(token, expected):
            abort(400, description="La sesión ha caducado. Recarga la página.")
        session.clear()
        return redirect(url_for("login"), code=303)
