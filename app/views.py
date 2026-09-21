from flask import Blueprint, redirect, render_template, url_for

from config import Config

ui_bp = Blueprint("ui", __name__)


@ui_bp.get("/")
def index():
    return render_template("home.html", active_tab="home")


@ui_bp.get("/map")
def map_page():
    return render_template("map.html", active_tab="map")


@ui_bp.get("/manage")
def manage():
    return render_template("manage.html", active_tab="manage")


@ui_bp.get("/reclaim")
def reclaim():
    return render_template("reclaim.html", active_tab="settings")


@ui_bp.get("/review")
def review_queue():
    return render_template("review_queue.html", active_tab="review")


@ui_bp.get("/review/<resource_id>")
def resource_detail(resource_id):
    return render_template("resource_detail.html", active_tab="review", resource_id=resource_id)


@ui_bp.get("/library")
def library():
    return render_template("library.html", active_tab="library")


@ui_bp.get("/settings")
def settings_page():
    return render_template(
        "settings.html", active_tab="settings", redirect_uri=Config.GOOGLE_OAUTH_REDIRECT_URI,
    )
