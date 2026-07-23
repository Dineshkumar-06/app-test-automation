"""Application-constant option lists that are identical across registration
portals and live in the app code (PHP arrays), not the requirement workbook.

Like the standard-field library, these are reviewed once and reused. The
state list backs the State dropdowns; the day/month lists back the Date-of-
Birth composite (its YEAR range is not constant — it comes from the age
grid per post+category, a Phase-2 cross-document check).
"""

from __future__ import annotations

import re

# Indian states / UTs, in the app's own order. ('OTHERS' is commented out in
# the source and deliberately excluded here.)
STATES: list[str] = [
    "ANDHRA PRADESH",
    "ARUNACHAL PRADESH",
    "ASSAM",
    "BIHAR",
    "CHHATTISGARH",
    "GOA",
    "GUJARAT",
    "HARYANA",
    "HIMACHAL PRADESH",
    "JAMMU & KASHMIR",
    "JHARKHAND",
    "KARNATAKA",
    "KERALA",
    "MADHYA PRADESH",
    "MAHARASHTRA",
    "MANIPUR",
    "MEGHALAYA",
    "MIZORAM",
    "NAGALAND",
    "ODISHA",
    "PUNJAB",
    "RAJASTHAN",
    "SIKKIM",
    "TAMILNADU",
    "TRIPURA",
    "UTTAR PRADESH",
    "UTTARAKHAND",
    "WEST BENGAL",
    "ANDAMAN & NICOBAR",
    "CHANDIGARH",
    "DADRA & NAGAR HAVELI",
    "DAMAN & DIU",
    "LAKSHADWEEP",
    "DELHI",
    "PUDUCHERRY",
    "TELANGANA",
    "LADAKH",
]

# Date-of-Birth day/month are constant; the engine validates DOB sub-dropdowns
# against these. Year is intentionally absent (age-grid derived, Phase 2).
DOB_DAYS: list[str] = [f"{d:02d}" for d in range(1, 32)]

DOB_MONTHS: list[str] = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]

# State-selection dropdowns get the constant list. Excludes the derived
# "State for GST invoicing" (a read-only label, not a picker).
_STATE_RE = re.compile(r"^\s*state\b(?!.*(?:gst|invoic))", re.IGNORECASE)


def resolve_constant_options(label: str) -> tuple[str, list[str]] | None:
    """If a field label maps to a constant app option list, return
    (source_name, values); else None. Used as a dropdown option source
    after the supplementary sheet, without skipping the LLM (so fields that
    also carry conditions keep them).
    """
    if _STATE_RE.search(label):
        return "constant:states", STATES
    return None
