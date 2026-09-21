"""Categories as data (see app.models.Category)."""
import re

from config import Config
from .extensions import db
from .models import Category

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def ensure_default_categories():
    """Insert any seed category whose slug is missing (never overwrites or resurrects an edited/archived one)."""
    have = {slug for (slug,) in db.session.query(Category.slug).all()}
    added = False
    for order, slug in enumerate(Config.CATEGORIES):
        if slug not in have:
            db.session.add(Category(slug=slug, label=Config.CATEGORY_LABELS.get(slug, slug), sort_order=(order + 1) * 10))
            added = True
    if added:
        db.session.commit()


def slugify(label):
    return _SLUG_RE.sub("-", label.lower()).strip("-")[:48]


def active_slugs():
    ensure_default_categories()
    return {slug for (slug,) in db.session.query(Category.slug).filter(Category.archived.is_(False)).all()}
