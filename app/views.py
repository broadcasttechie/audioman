from flask import Blueprint, redirect, render_template, url_for

from config import Config

ui_bp = Blueprint("ui", __name__)


@ui_bp.get("/")
def index():
    # No review-queue UI yet (PLAN.md section 7) -- settings is the
    # only page that exists so far.
    return redirect(url_for("ui.settings_page"))


@ui_bp.get("/settings")
def settings_page():
    return render_template("settings.html", redirect_uri=Config.GOOGLE_OAUTH_REDIRECT_URI)
