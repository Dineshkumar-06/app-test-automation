"""Data models for rules.json — the contract between extractor and engine.

Mirrors docs/rules.schema.md field-for-field. The engine reads only these
structures (plus the live page); it never re-reads source workbooks. See
CLAUDE.md: interpretation lives here and in rules.json, never in engine
branches.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Union

from pydantic import BaseModel

FieldType = Literal[
    "numeric",
    "alpha",
    "alpha_space",
    "alphanumeric",
    "varchar",
    "date",
    "email",
    "checkbox",
    "radio",
    "dropdown",
    "multiselect",
]

InputMethod = Literal["text", "dropdown", "radio", "checkbox", "label", "multiselect"]

Mandatory = Union[bool, Literal["conditional"]]

Confidence = Literal["high", "low"]

CheckStatus = Literal["issue", "clarification", "pass"]


class Source(BaseModel):
    """Provenance pointer back to the requirement workbook.

    Every field-derived rule MUST carry this so the report can trace a
    defect to a requirement line.
    """

    sheet: str
    row: int


class Conditional(BaseModel):
    """Expression-based conditional behaviour for a field.

    Expressions are evaluated by the engine's single evaluator against
    current field values — never by an LLM. Grammar is documented in
    docs/rules.schema.md under "Expression grammar".
    """

    enabled_when: str | None = None
    disabled_when: str | None = None
    mandatory_when: str | None = None
    options_when: dict[str, list[str]] | None = None


class Field(BaseModel):
    """A single SOW row, resolved to a stable label — never a selector."""

    sl: str
    label: str
    section: str | None = None
    type: FieldType
    max_length: int | None = None
    mandatory: Mandatory
    input_method: InputMethod
    regex: str | None = None
    charset: str | None = None
    enum: list[str] | None = None
    default: str | None = None
    uppercase: bool | None = None
    must_equal: str | None = None
    enabled_when: str = "always"
    conditional: Conditional | None = None
    source: Source
    confidence: Confidence | None = None
    rule_source: str | None = None


class AgeRule(BaseModel):
    """Per-post age eligibility, expressed as DOB cut-offs.

    All bounds are DOB cut-offs (inclusive) so the engine only compares
    dates against the DOB dropdown and never recomputes ages. EXS
    relaxation models the base N here; the engine adds the candidate's
    entered period-of-service months at check time.
    """

    min_dob: date
    max_dob_by_category: dict[str, date]
    relaxations: dict[str, dict[str, date]] | None = None


class EligibilityLeaf(BaseModel):
    """A single qualification requirement — a leaf of an EligibilityTree."""

    qualification: str
    stream: list[str]
    min_pct: float
    class_: str
    strict: bool | None = None
    work_experience: str | None = None


class EligibilityTree(BaseModel):
    """AND/OR tree over qualification leaves, from the eligibility sheet."""

    op: Literal["AND", "OR"]
    children: list[Union["EligibilityTree", EligibilityLeaf]]


class RulesFile(BaseModel):
    """Top-level shape of rules.json — the boundary artifact itself."""

    application: str
    page: str
    as_on_date: date
    fields: list[Field]
    category_by_post: dict[str, list[str]]
    age: dict[str, AgeRule]
    eligibility: dict[str, EligibilityTree]


class CheckResult(BaseModel):
    """One engine-produced verdict. Every field carries this shape.

    Per CLAUDE.md conventions: the LLM never decides pass/fail — this is
    populated only by deterministic Python assertions in engine/.
    """

    page: str
    field_label: str
    sow_row: str
    check_type: str
    severity: str
    input_used: str | None = None
    expected: str
    observed: str
    status: CheckStatus
    screenshot: str | None = None
