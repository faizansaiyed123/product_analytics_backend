"""Role-based permissions (enforced server-side on every protected endpoint)."""

from __future__ import annotations

from enum import IntEnum


class Role(IntEnum):
    viewer = 1
    member = 2
    admin = 3
    owner = 4

    @classmethod
    def parse(cls, value: str) -> Role:
        return cls[value]


ROLE_NAMES = ("owner", "admin", "member", "viewer")

# What each minimum role is needed for (documented in docs/security.md)
READ = Role.viewer  # view analytics, events, users, dashboards, reports
WRITE = Role.member  # create/edit dashboards, reports, cohorts, alerts; export data
MANAGE = Role.admin  # project settings, API keys, team, data management
OWNER = Role.owner  # delete organization, manage owners
