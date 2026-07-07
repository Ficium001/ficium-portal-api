# =============================================================
# ficium-portal-api — Institution role constants
#
# ficium-auth issues `user_role` claims sourced from
# auth_portal.auth_users.role, which currently holds (live values):
#   "admin"               — first user of a self-registered institution
#   "institution_admin"   — invited/seeded institution admin (e.g. bank staff)
#   "institution_checker" — institution maker-checker approver, non-admin
#   "institution_member"  — standard institution portal user, non-admin
#   "super_admin"         — Ficium's own internal platform staff
#
# This is a free-text column with no DB-level enum, so this tuple is the
# single source of truth for "counts as an institution admin" across the
# API. Do not redefine this check locally in individual routers — every
# admin-gated endpoint must import and use this constant, or a role
# ficium-auth actually issues (like "institution_admin") can silently be
# excluded from every admin check at once, as happened previously.
# =============================================================

INSTITUTION_ADMIN_ROLES: tuple[str, ...] = ("admin", "institution_admin", "super_admin")
