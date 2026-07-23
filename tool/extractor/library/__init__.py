"""Hand-written, high-confidence rules for standard fields.

Aadhaar, PAN, IFSC, and Pincode carry identical validation rules across
every application (the SOW marks them grey / "standard" — see CLAUDE.md's
"Minimising human review"). Reviewed once here, they're reused for every
application from then on and never go through the LLM.

A second group covers the mostly-standardized personal/bank fields from the
"Personal Details" section onward (Gender, name fields, bank account,
account type). These carry format rules only. IMPORTANT: because a library
match skips the LLM entirely, the caller must NOT apply the library to a
field that also has conditional enable/mandatory logic — otherwise that
condition is silently dropped. library_match returns the rule regardless;
the caller (extract_and_merge) guards conditional fields.

Matching is by label text, not by sl/position — labels for these fields
are consistent in phrasing across applications even though their sl,
section, and exact type/max_length column values differ. Two caveats worth
review when onboarding a new application: enum sets (Marital Status, Type of
Account) and the must_equal target label can vary between applications.
"""

from __future__ import annotations

import re

# Each entry: (name, label_pattern, rule). label_pattern is matched
# case-insensitively against the field's label. rule keys mirror the
# Field attributes the LLM step would otherwise produce (regex, charset,
# uppercase, must_equal, enum) — sl/label/type/max_length/mandatory/
# input_method/source always come from the parsed workbook row, never
# from the library.
_ENTRIES: list[tuple[str, re.Pattern, dict]] = [
    (
        "aadhaar",
        re.compile(r"aadhaar\s*(card)?\s*(no|number)", re.IGNORECASE),
        {"regex": r"^[0-9]{12}$", "charset": "digits"},
    ),
    (
        "pan",
        re.compile(r"\bpan\s*card\s*(no|number)?\b", re.IGNORECASE),
        {"regex": r"^[A-Z]{5}[0-9]{4}[A-Z]$", "uppercase": True},
    ),
    (
        "ifsc",
        re.compile(r"\bifsc\b", re.IGNORECASE),
        {"regex": r"^[A-Z]{4}0[A-Z0-9]{6}$", "uppercase": True},
    ),
    (
        "pincode",
        re.compile(r"\bpin\s*code\b", re.IGNORECASE),
        {"regex": r"^[0-9]{6}$", "charset": "digits"},
    ),
    # --- Personal / bank standardized fields (format only) ---
    (
        "gender",
        re.compile(r"^\s*gender\b", re.IGNORECASE),
        {"enum": ["Male", "Female", "Others"]},
    ),
    (
        "marital_status",
        re.compile(r"marital\s*status", re.IGNORECASE),
        {"enum": ["Unmarried", "Married", "Widow", "Widower", "Divorced", "Judicially Separated"]},
    ),
    (
        "person_name",
        re.compile(r"(father|mother|spouse|account\s*holder)('?s)?\b.*\bname", re.IGNORECASE),
        {"charset": "alpha_space"},
    ),
    (
        "confirm_bank_account_no",
        re.compile(r"confirm\b.*\baccount\s*(no|number)", re.IGNORECASE),
        {"charset": "digits", "must_equal": "Bank Account No"},
    ),
    (
        "bank_account_no",
        re.compile(r"^(?!.*confirm).*\bbank\s*account\s*(no|number)", re.IGNORECASE),
        {"charset": "digits"},
    ),
    (
        "account_type",
        re.compile(r"type\s*of\s*account", re.IGNORECASE),
        {"enum": ["Savings Account", "Current Account"]},
    ),
    # Date of Birth: standard composite. Day/month are constant (see
    # constants.py, engine-side); year range is age-grid derived (Phase 2).
    # No flat enum — the entry just marks it standard so it skips the LLM.
    # (State is deliberately NOT here: the permanent-address State field
    # carries an auto-populate condition, so it must keep going through the
    # LLM; its options are filled from constants during dropdown resolution.)
    (
        "date_of_birth",
        re.compile(r"date\s*of\s*birth", re.IGNORECASE),
        {},
    ),
]

# Labels that look like a match by keyword but are a different field
# entirely (e.g. a Yes/No consent question, not the number itself).
_EXCLUDE = re.compile(r"consent|verification|declar", re.IGNORECASE)


def library_match(label: str) -> dict | None:
    """Return {rule_source, confidence, **rule} if label is a standard
    field, else None. Callers should prefer this over any LLM-derived
    rule and skip the LLM entirely when it matches.
    """
    if _EXCLUDE.search(label):
        return None
    for name, pattern, rule in _ENTRIES:
        if pattern.search(label):
            return {"rule_source": f"library:{name}", "confidence": "high", **rule}
    return None
