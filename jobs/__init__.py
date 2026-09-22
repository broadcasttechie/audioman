"""
Every job function here takes no args, returns a small dict summary,
and is registered under a name used by both the scheduler and the
manual "run now" API endpoint (`/api/jobs/<name>/run`) — same code
path either way, so there's only one implementation to trust.
"""

from app.api import JOB_REGISTRY

from .rclone_jobs import drive_inbox_pull, nas_to_drive_library, library_verify
from .maintenance import refile_all, verify_integrity, find_orphans, retry_failed
from .enrich import enrich_locations, enrich_photos
from .export import process_exports
from .filing import file_resources
from .previews import generate_previews
from .geocode import geocode_locations
from .grouping import suggest_groupings, join_pending_groups

JOB_REGISTRY.update({
    "drive-inbox-pull": drive_inbox_pull,
    "nas-to-drive-library": nas_to_drive_library,
    "library-verify": library_verify,
    "refile-all": refile_all,
    "verify-integrity": verify_integrity,
    "find-orphans": find_orphans,
    "retry-failed": retry_failed,
    "enrich-locations": enrich_locations,
    "enrich-photos": enrich_photos,
    "process-exports": process_exports,
    "file-resources": file_resources,
    "generate-previews": generate_previews,
    "geocode-locations": geocode_locations,
    "suggest-groupings": suggest_groupings,
    "join-groups": join_pending_groups,
})
