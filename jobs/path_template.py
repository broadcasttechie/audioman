"""
The physical NAS path is always a *rendering* of DB fields — never the
source of truth. This is what makes `refile-all` possible: change the
template or a project's slug, re-render, move what doesn't match.
"""
from config import Config


def render_path(resource, project=None):
    """
    resource: app.models.Resource
    project: app.models.Project or None
    Returns a NAS-relative path (join with Config.NAS_LIBRARY_ROOT).
    """
    if project:
        template = Config.PATH_TEMPLATE_WITH_PROJECT
        fields = {"project": project.slug, "filename": resource.filename}
    else:
        template = Config.PATH_TEMPLATE_NO_PROJECT
        year = resource.captured_at.year if resource.captured_at else "unknown"
        fields = {
            "category": resource.category,
            "year": year,
            "filename": resource.filename,
        }

    return template.format(**fields)
