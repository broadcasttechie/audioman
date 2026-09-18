from flask import Blueprint, redirect, render_template, url_for

from config import Config

ui_bp = Blueprint("ui", __name__)


@ui_bp.get("/")
def index():
    return redirect(url_for("ui.review_queue"))


@ui_bp.get("/review")
def review_queue():
    return render_template("review_queue.html", active_tab="review")


@ui_bp.get("/library")
def library():
    return render_template("library.html", active_tab="library")


@ui_bp.get("/settings")
def settings_page():
    return render_template(
        "settings.html", active_tab="settings", redirect_uri=Config.GOOGLE_OAUTH_REDIRECT_URI,
    )
