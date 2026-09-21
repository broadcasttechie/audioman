"""
The physical NAS path is always a *rendering* of DB fields — never the
source of truth. That is what makes `refile-all` possible: change the
template or a project/session name, re-render, and see what no longer matches.

Two rules matter more than the template itself:

* Every substituted value is made filesystem-safe (`safe_component`): a session named
  "Night 2 / Act 1" or ".." must not be able to write outside its folder or make a hidden one.
* The ORIGINAL FILENAME is never changed. A Reaper project refers to audio by its relative
  path (measured on the user's real project, PLAN 18.5j), so a renamed audio file breaks it.
  A description found in a filename becomes a title suggestion, not part of the path.
"""
import re

from config import Config

_UNSAFE = re.compile(r'[\x00-\x1f/\\:*?"<>|]')


def safe_component(value, fallback="unnamed"):
    """One path component: no separators, no control or Windows-reserved characters, no leading/
    trailing dots or spaces (a trailing dot or space is silently dropped by SMB/Windows), never
    '.' or '..'. Only the folder names we build go through this, never the original filename."""
    text = _UNSAFE.sub("-", str(value)).strip(" .")
    text = re.sub(r"\s+", " ", text)
    return text or fallback


def render_path(resource, project=None, session=None):
    """
    resource: app.models.Resource
    project:  app.models.Project or None
    session:  app.models.RecordingSession or None
    Returns a NAS-relative path (join with Config.NAS_LIBRARY_ROOT).
    """
    session_part = safe_component(session.name) if session else ""
    if project:
        template = Config.PATH_TEMPLATE_WITH_PROJECT
        fields = {"project": safe_component(project.slug), "session": session_part}
    else:
        template = Config.PATH_TEMPLATE_NO_PROJECT
        when = resource.captured_at
        fields = {
            "category": safe_component(resource.category or "uncategorised"),
            "year": when.year if when else "unknown",
            "month": f"{when.month:02d}" if when else "unknown",
            "session": session_part,
        }
    # The filename is deliberately NOT sanitised or changed: see the module docstring.
    fields["filename"] = resource.filename

    path = template.format(**fields)
    return "/".join(part for part in path.split("/") if part)
