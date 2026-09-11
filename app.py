import os
from flask import Flask
from flask_wtf.csrf import CSRFProtect

import config_loader
import auth
import browser
import actions
import uploads


def create_app():
    app = Flask(__name__)
    DIRECTORIES = config_loader.load_directories()
    auth.register(app, DIRECTORIES)
    CSRFProtect(app)

    for root, _ in DIRECTORIES:
        os.makedirs(root, exist_ok=True)

    browser.register(app, DIRECTORIES)
    actions.register(app, DIRECTORIES)
    uploads.register(app, DIRECTORIES)

    app.config["MAX_CONTENT_LENGTH"] = uploads.MAX_REQUEST_SIZE

    @app.after_request
    def set_security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    return app


app = create_app()


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8081")),
        debug=debug,
    )
