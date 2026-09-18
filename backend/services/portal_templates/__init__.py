from __future__ import annotations

from backend.services.portal_templates.base import PortalTemplate
from backend.services.portal_templates.cutshort import CutshortTemplate
from backend.services.portal_templates.indeed import IndeedTemplate
from backend.services.portal_templates.instahyre import InstahyreTemplate
from backend.services.portal_templates.linkedin import LinkedInTemplate
from backend.services.portal_templates.naukri import NaukriTemplate
from backend.services.portal_templates.web import WebTemplate
from backend.services.portal_templates.wellfound import WellfoundTemplate

_REGISTRY: dict[str, PortalTemplate] = {
    "linkedin": LinkedInTemplate(),
    "naukri": NaukriTemplate(),
    "indeed": IndeedTemplate(),
    "wellfound": WellfoundTemplate(),
    "cutshort": CutshortTemplate(),
    "instahyre": InstahyreTemplate(),
    "web": WebTemplate(),
}


def get_template(portal_name: str) -> PortalTemplate:
    """Return the PortalTemplate for the given portal name (case-insensitive)."""
    key = portal_name.lower().strip()
    if key not in _REGISTRY:
        supported = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"Unknown portal '{portal_name}'. Supported: {supported}")
    return _REGISTRY[key]


def list_portals() -> list[str]:
    return sorted(_REGISTRY.keys())


__all__ = [
    "PortalTemplate",
    "get_template",
    "list_portals",
    "LinkedInTemplate",
    "NaukriTemplate",
    "IndeedTemplate",
    "WellfoundTemplate",
    "CutshortTemplate",
    "InstahyreTemplate",
    "WebTemplate",
]
