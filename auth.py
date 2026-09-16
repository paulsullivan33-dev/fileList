import json
import os
import secrets
import time
from functools import wraps
from urllib.parse import urljoin, urlparse

from flask import abort, redirect, render_template_string, request, session, url_for
from werkzeug.security import check_password_hash
from styles import APP_TITLE

from utils import safe_dir_index, user_can_access_directory


USERS_FILE = "users.json"

LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ app_title }}</title>
  <style>
    body { font-family: system-ui, sans-serif; background: #f3f4f6; margin: 0; }
    main { width: min(360px, calc(100% - 40px)); margin: 12vh auto; background: white;
           padding: 28px; border-radius: 10px; box-shadow: 0 3px 14px #0002; }
    label { display: block; margin-top: 14px; font-weight: 600; }
    input { box-sizing: border-box; width: 100%; padding: 10px; margin-top: 5px;
            border: 1px solid #bbb; border-radius: 5px; font: inherit; }
    button { width: 100%; margin-top: 20px; padding: 10px; border: 0; border-radius: 5px;
             background: #267a3d; color: white; font: inherit; font-weight: 600; cursor: pointer; }
    .error { color: #a40000; margin-top: 14px; }
  </style>
</head>
<body>
  <main>
    <h1>{{ app_title }}</h1>
    <h2>Sign in</h2>
    <form method="post">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <label for="username">Username</label>
      <input id="username" name="username" autocomplete="username" required autofocus>
      <label for="password">Password</label>
      <input id="password" name="password" type="password" autocomplete="current-password" required>
      <button type="submit">Sign in</button>
    </form>
    {% if error %}<p class="error">{{ error }}</p>{% endif %}
  </main>
</body>
</html>"""


def _users_path(app):
    return os.path.join(app.root_path, USERS_FILE)


def load_users(app):
    """Load the JSON array of configured users without exposing passwords."""
    try:
        with open(_users_path(app), "r", encoding="utf-8") as users_file:
            users = json.load(users_file)
    except (OSError, json.JSONDecodeError):
        return []
    return users if isinstance(users, list) else []


def _find_user(app, username):
    for user in load_users(app):
        if isinstance(user, dict) and user.get("username") == username:
            return user
    return None


def _is_safe_next(target):
    if not isinstance(target, str) or not target.startswith("/") or target.startswith("//"):
        return False
    host_url = urlparse(request.host_url)
    redirect_url = urlparse(urljoin(request.host_url, target))
    return redirect_url.scheme in ("http", "https") and redirect_url.netloc == host_url.netloc


def _setting(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("login", next=request.full_path))
        return view(*args, **kwargs)
    return wrapped


def register(app, directories):
    app.jinja_env.globals["app_title"] = APP_TITLE
    # A persistent key keeps existing sessions valid across normal restarts. Set
    # FLASK_SECRET_KEY in production to manage it outside this folder.
    secret_path = os.path.join(app.root_path, ".flask_session_key")
    secret_key = os.environ.get("FLASK_SECRET_KEY")
    if not secret_key:
        try:
            with open(secret_path, "r", encoding="utf-8") as secret_file:
                secret_key = secret_file.read().strip()
        except OSError:
            secret_key = secrets.token_urlsafe(32)
            with open(secret_path, "w", encoding="utf-8") as secret_file:
                secret_file.write(secret_key)
    app.secret_key = secret_key
    login_attempts = {}
    login_max_attempts = _setting("LOGIN_MAX_ATTEMPTS", 5)
    login_window_seconds = _setting("LOGIN_WINDOW_SECONDS", 300)
    login_lock_seconds = _setting("LOGIN_LOCK_SECONDS", 300)

    def login_key():
        return request.remote_addr or "unknown"

    def login_locked(key, now):
        state = login_attempts.get(key)
        return bool(state and state["locked_until"] > now)

    def record_failed_login(key, now):
        state = login_attempts.get(key)
        if state is None or now - state["first_failure"] >= login_window_seconds:
            state = {"failures": 0, "first_failure": now, "locked_until": 0}
        state["failures"] += 1
        if state["failures"] >= login_max_attempts:
            state["locked_until"] = now + login_lock_seconds
            state["failures"] = 0
            state["first_failure"] = now
        login_attempts[key] = state

    @app.before_request
    def require_login():
        if request.endpoint in {"login", "logout", "static"}:
            return None
        if "username" not in session:
            return redirect(url_for("login", next=request.full_path))
        request_data = request.get_json(silent=True) if request.is_json else {}
        for key in ("dir", "src_dir", "dest_dir"):
            value = request.values.get(key)
            if value is None and request_data:
                value = request_data.get(key)
            if value is None:
                continue
            directory_index = safe_dir_index(value, directories)
            if directory_index is None or not user_can_access_directory(
                directories[directory_index], session.get("groups", [])
            ):
                abort(403)
        return None

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            key = login_key()
            now = time.monotonic()
            if login_locked(key, now):
                return render_template_string(
                    LOGIN_PAGE, error="Too many sign-in attempts. Please try again later."
                ), 429
            user = _find_user(app, username)
            password_hash = user.get("password") if user else ""
            try:
                password_ok = isinstance(password_hash, str) and check_password_hash(
                    password_hash, password
                )
            except (TypeError, ValueError):
                password_ok = False
            if password_ok:
                login_attempts.pop(key, None)
                session.clear()
                session["username"] = username
                groups = user.get("groups", "")
                session["groups"] = [group.strip() for group in groups.split(",") if group.strip()]
                target = request.args.get("next") or request.form.get("next")
                return redirect(target if _is_safe_next(target) else url_for("index"))
            record_failed_login(key, now)
            return render_template_string(LOGIN_PAGE, error="Invalid username or password."), 401
        if "username" in session:
            return redirect(url_for("index"))
        return render_template_string(LOGIN_PAGE, error=None)

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))
