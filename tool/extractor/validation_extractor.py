"""LLM-assisted extraction of the SOW's free-text Validations/Values prose
into structured rule attributes: regex, charset, enum, must_equal,
uppercase, and conditional expressions.

Per CLAUDE.md's one governing rule, this module never decides pass/fail —
it only turns prose into structured data, every LLM-derived rule carries
`confidence`, and correctness verdicts stay entirely in engine/. No SDK is
imported here; every call goes through tool.extractor.llm.call_llm so the
provider/model stays swappable in exactly one place.
"""

from __future__ import annotations

import json
import re

from tool.extractor.constants import resolve_constant_options
from tool.extractor.library import library_match
from tool.extractor.llm import call_llm
from tool.extractor.other_details import resolve_dropdown
from tool.models import Conditional, Field

_SYSTEM_PROMPT = """\
You are extracting deterministic validation rules from ONE row of a bank/government exam \
registration requirement spreadsheet. You are given that field's Type, Max Length, Mandatory, \
Input Method, its two free-text columns (Values and Validations), and a FIELD DIRECTORY listing \
the other fields on the same page with their point handles.

Output a single JSON object and nothing else — no markdown code fences, no commentary, no \
preamble, no explanation. Output must start with `{` and end with `}`.

JSON shape (every key optional/nullable — omit or null when not applicable):
{
  "regex": string or null,          // e.g. "^[A-Z]{5}[0-9]{4}[A-Z]$"
  "charset": string or null,        // e.g. "digits", "alpha_space", "alnum"
  "enum": array of strings or null, // allowed values, only if Values lists a closed set inline
  "must_equal": string or null,     // point handle of another field this must equal (e.g. a Confirm field)
  "uppercase": true or false or null,
  "conditional": {
    "enabled_when": string or null,
    "disabled_when": string or null,
    "mandatory_when": string or null
  } or null,
  "confidence": "high" or "low",
  "note": string or null            // only when confidence is "low": why
}

Conditional expression grammar (for enabled_when / disabled_when / mandatory_when):
  expr   := term (('and'|'or') term)*
  term   := <field> <op> <value>  |  <field> 'in' [<value>, ...]  |  '(' expr ')'
  op     := '==' | '!='
  <field> := a point handle taken from the FIELD DIRECTORY (e.g. point_9_1). NEVER invent a name.
  <value> := 'quoted string' | number | true | false

Resolving references: the Validations prose refers to other fields either by point number \
("point no. 9.1", "pt.5", "point no.- 4 to 9", "previous point") or by quoting/paraphrasing \
their label ("if selected YES for 'Are you a person with benchmark disability...'"). In BOTH \
cases, find that field in the FIELD DIRECTORY and use its point handle. "previous point" / \
"previous pt" means the immediately preceding field — its handle is given to you explicitly \
above as "The immediately preceding field is ...". A range like "point no.- 4 to 9" means each \
of those points.

NEVER reference THIS FIELD's own handle in its own condition — a field is not enabled/disabled \
by its own value. If the trigger is another field, use that other field's handle.

Confidence: because you have the directory, resolving a point-number or quoted-label reference \
to a handle is the NORMAL case and is "high" when the mapping is clear. Also "high" when there \
is no special validation at all (every key null). Mark "low" only when: the prose is genuinely \
ambiguous, you cannot tell WHICH directory field a reference means, the trigger values are \
unclear, or the logic is too complex to capture faithfully in the grammar.

CRITICAL — conditional behaviour applies even when Mandatory is No/false. A field may be \
conditionally ENABLED (shown) without being mandatory. If the Validations prose says the field \
is "Enabled ... if/when <X>" or "Enabled but not Mandatory if <X>" (anything other than \
"Enabled for all"), you MUST set enabled_when accordingly — regardless of the Mandatory value. \
And if Mandatory is "conditional", you MUST capture the condition via enabled_when / \
disabled_when / mandatory_when. Never return a null/empty conditional when the prose states one.

Dropdown option lists: when Values gives an inline list of choices, extract them into `enum`. \
When Values/Validations say the options live elsewhere ("Refer other details", "List given in \
other details"), set `enum` to null — do NOT invent options; the pipeline resolves those from \
the supplementary sheet separately.

Dynamic option lists: if the SET of allowed options depends on another field's value (e.g. \
"display categories based on the post selected", "values as per point no 4"), do NOT try to \
encode that mapping. Set enum to null, describe the dependency in `note`, and mark "low".

Example 1 (clean, high confidence):
Input: label="PAN Card No.", type=alphanumeric, max_length=10, mandatory=false, values=null, \
validations="Enabled for all. Maximum length should be 10 in AlphaNumeric only. First five \
letters (uppercase), then four numerals, then one letter (uppercase)."
Output: {"regex": "^[A-Z]{5}[0-9]{4}[A-Z]$", "charset": null, "enum": null, "must_equal": null, \
"uppercase": true, "conditional": null, "confidence": "high", "note": null}

Example 2 (resolves point/label references via the directory — high confidence):
Directory includes: point_3: Are you a person with benchmark disability of 40% and above ? ; \
point_5: Are you suffering from cerebral palsy ... ; point_7: Whether your dominant (Writing) \
hand is affected ? ; point_4_1: Type of Disability
Input: label="Do you intend to use the services of a scribe ?", mandatory=conditional, \
values="YES or NO", validations="Enabled and Mandatory for B/LV OR Mental Illness (MI)/ SLD or \
ASD, selected YES in Cerebral palsy in pt.5 or selected YES in Dominant hand in pt.7."
Output: {"regex": null, "charset": null, "enum": ["YES", "NO"], "must_equal": null, \
"uppercase": null, "conditional": {"enabled_when": null, "disabled_when": null, \
"mandatory_when": "point_4_1 in ['B','LV','MI','SLD','ASD'] or point_5 == 'YES' or point_7 == \
'YES'"}, "confidence": "high", "note": null}
"""

_RETRY_MESSAGE = "\n\nYour previous reply was not valid JSON. Return only the JSON object."


def _point_handle(sl: str) -> str:
    """Canonical reference token for a field, derived from its SOW Sl No:
    '9.1' -> 'point_9_1'. Deterministic and reversible, so the engine can
    map an expression's handle back to the field it names.
    """
    return "point_" + re.sub(r"[^0-9a-zA-Z]+", "_", str(sl).strip())


def build_field_directory(fields: list[Field]) -> str:
    """One 'point_<sl>: <label>' line per field, given to the LLM so its
    conditional expressions reference real fields by a stable handle
    instead of invented, run-to-run-varying names.
    """
    return "\n".join(f"{_point_handle(f.sl)}: {f.label.strip()[:70]}" for f in fields)


def _build_user_prompt(
    field: Field,
    values_raw: str | None,
    validations_raw: str | None,
    field_directory: str,
    self_handle: str = "",
    prev_handle: str = "",
) -> str:
    this_field = f"THIS FIELD's handle is {self_handle}."
    if prev_handle:
        this_field += (
            f" The immediately preceding field is {prev_handle} — resolve "
            f'"previous point"/"previous pt"/"the previous point" to {prev_handle}.'
        )
    return (
        "FIELD DIRECTORY (other fields on this page; use these handles in expressions):\n"
        f"{field_directory}\n\n"
        f"{this_field}\n\n"
        "THIS FIELD:\n"
        f'label={field.label!r}, type={field.type}, max_length={field.max_length}, '
        f'mandatory={field.mandatory!r}, input_method={field.input_method}\n'
        f"values={values_raw!r}\n"
        f"validations={validations_raw!r}"
    )


def _extract_json_object(text: str) -> str:
    """Strip common LLM contaminants and return the outermost {...} substring.

    Handles: leading/trailing whitespace, ```json/``` fences, and prose
    before/after the JSON object. Brace-matches rather than using the
    first "{" and last "}" naively, so nested objects/arrays don't
    truncate the match early.
    """
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    start = text.find("{")
    if start == -1:
        raise ValueError("no '{' found in LLM reply")

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError("unbalanced braces in LLM reply")


def _parse_llm_json(raw_reply: str) -> dict:
    """Parse the reply into a dict and validate its "conditional" sub-shape.

    Valid JSON syntax isn't enough — the model must also produce a
    correctly-shaped conditional. We no longer ask the model for
    options_when (its dynamic-option mapping was the recurring shape
    failure), so any stray options_when it emits anyway is dropped here
    before validation rather than crashing the run. Validating the
    Conditional inside the caller's JSON try/except means a genuine shape
    error still triggers the retry-once-then-degrade path.
    """
    candidate = _extract_json_object(raw_reply)
    parsed = json.loads(candidate)
    cond = parsed.get("conditional")
    if cond:
        cond.pop("options_when", None)  # not part of the LLM's job anymore
        Conditional(**cond)  # raises ValidationError (a ValueError) if malformed
    return parsed


def _clean_conditional(cond: dict | None) -> dict | None:
    """Return the conditional dict only if it carries at least one real
    expression; an all-null / empty conditional collapses to None so it
    isn't emitted as a meaningless `"conditional": {}`.
    """
    if not cond:
        return None
    if any(v for v in cond.values()):
        return cond
    return None


def extract_field_validation(
    field: Field,
    values_raw: str | None,
    validations_raw: str | None,
    field_directory: str = "",
    self_handle: str = "",
    prev_handle: str = "",
) -> dict:
    """Call the LLM for one field's free-text prose and return extracted
    attributes: regex, charset, enum, must_equal, uppercase, conditional
    (as a dict, or None), confidence, and — only on failure — parse_error.

    `field_directory` is the page's point-handle listing, so conditional
    expressions reference other fields by stable handles;
    `self_handle`/`prev_handle` let the model resolve "previous point"
    references and avoid self-references (see build_field_directory).

    Retries once with a corrective message on malformed JSON. If that also
    fails — or the call itself errors or times out — does not raise:
    returns confidence="low" with a parse_error note so the field lands in
    the human review queue instead of crashing the whole batch run.
    """
    user_prompt = _build_user_prompt(
        field, values_raw, validations_raw, field_directory, self_handle, prev_handle
    )
    label = f"{field.sl}:{field.label[:40]}"

    def _failure(note: str) -> dict:
        return {
            "regex": None,
            "charset": None,
            "enum": None,
            "must_equal": None,
            "uppercase": None,
            "conditional": None,
            "confidence": "low",
            "parse_error": note,
        }

    try:
        reply = call_llm(_SYSTEM_PROMPT, user_prompt, label=label)
    except Exception as exc:  # network/timeout/provider error — never crash the batch
        return _failure(f"LLM call failed: {exc}")

    try:
        parsed = _parse_llm_json(reply)
    except (ValueError, json.JSONDecodeError):
        try:
            retry_reply = call_llm(_SYSTEM_PROMPT, user_prompt + _RETRY_MESSAGE, label=f"{label} (retry)")
        except Exception as exc:
            return _failure(f"LLM retry call failed: {exc}")
        try:
            parsed = _parse_llm_json(retry_reply)
        except (ValueError, json.JSONDecodeError) as exc:
            return _failure(f"{exc}: {retry_reply[:300]!r}")

    return {
        "regex": parsed.get("regex"),
        "charset": parsed.get("charset"),
        "enum": parsed.get("enum"),
        "must_equal": parsed.get("must_equal"),
        "uppercase": parsed.get("uppercase"),
        "conditional": _clean_conditional(parsed.get("conditional")),
        "confidence": parsed.get("confidence") or "low",
        "note": parsed.get("note"),
    }


def needs_llm(values_raw: str | None, validations_raw: str | None, enum: list[str] | None) -> bool:
    """False when there's nothing but already-clean columnar data to add —
    e.g. no Validations prose at all, and Values was either empty or a
    simple list the deterministic parser already turned into `enum`.
    Skips a wasted call for fields with genuinely nothing to interpret.
    """
    if validations_raw and str(validations_raw).strip():
        return True
    if values_raw and str(values_raw).strip() and enum is None:
        return True
    return False


def apply_extraction(field: Field, extraction: dict) -> Field:
    """Return a copy of `field` with LLM-derived attributes merged in."""
    conditional = None
    if extraction.get("conditional"):
        conditional = Conditional(**extraction["conditional"])

    updates = {
        "regex": extraction.get("regex") or field.regex,
        "charset": extraction.get("charset") or field.charset,
        "enum": extraction.get("enum") or field.enum,
        "must_equal": extraction.get("must_equal") or field.must_equal,
        "uppercase": extraction.get("uppercase") if extraction.get("uppercase") is not None else field.uppercase,
        "conditional": conditional or field.conditional,
        "confidence": extraction.get("confidence", "high"),
        "rule_source": "extracted",
    }
    return field.model_copy(update=updates)


def apply_library(field: Field, lib_rule: dict) -> Field:
    """Return a copy of `field` with a library rule's attributes merged in."""
    updates = {k: v for k, v in lib_rule.items() if k not in ("rule_source", "confidence")}
    updates["confidence"] = lib_rule["confidence"]
    updates["rule_source"] = lib_rule["rule_source"]
    return field.model_copy(update=updates)


def _condition_exprs(field: Field) -> list[str]:
    """The non-empty conditional expression strings on a field."""
    c = field.conditional
    if not c:
        return []
    return [e for e in (c.enabled_when, c.disabled_when, c.mandatory_when) if e]


def _has_condition(field: Field) -> bool:
    """True if the field carries at least one real conditional expression."""
    c = field.conditional
    return bool(c and (c.enabled_when or c.disabled_when or c.mandatory_when or c.options_when))


def _strip_self_reference(field: Field, self_handle: str) -> tuple[Field, bool]:
    """A field's condition must never reference its OWN handle (the model
    sometimes hallucinates e.g. point_3 == 'NO' on field 3). Such a clause
    is meaningless, so drop the whole conditional and report it.
    """
    pattern = re.compile(rf"\b{re.escape(self_handle)}\b")
    if any(pattern.search(expr) for expr in _condition_exprs(field)):
        return field.model_copy(update={"conditional": None}), True
    return field, False


# Prose that states a conditional-enable ("Enabled ... if/when ...") — but NOT
# the always-on "Enabled for all". Used to catch fields that are conditionally
# enabled even though Mandatory is No/false, where the model sometimes drops
# the condition entirely.
_ENABLE_WORD_RE = re.compile(r"\benabl\w+", re.IGNORECASE)
_ENABLE_TRIGGER_RE = re.compile(r"\b(if|when|only)\b", re.IGNORECASE)
_ENABLE_FOR_ALL_RE = re.compile(r"\benabl\w+\s+for\s+all\b", re.IGNORECASE)


def _prose_implies_enable_condition(validations_raw: str | None) -> bool:
    if not validations_raw:
        return False
    text = str(validations_raw)
    if _ENABLE_FOR_ALL_RE.search(text):
        return False
    return bool(_ENABLE_WORD_RE.search(text) and _ENABLE_TRIGGER_RE.search(text))


def _enforce_field_rules(field: Field, validations_raw: str | None) -> list[str]:
    """Return reasons this field must be flagged for review (empty if none).

    Invariants from the requirement author:
      - Mandatory == "conditional" ⇒ there MUST be a condition (the
        Validations column always states one for conditional fields).
      - A value-list dropdown ⇒ its option list MUST be known. Date
        dropdowns are exempt: they're day/month/year spinners, not a flat
        enum, so a missing enum there is expected, not a defect.
      - If the prose describes a conditional-enable ("Enabled ... if/when …")
        the field MUST carry a condition even when Mandatory is No — the
        model otherwise sometimes drops it on non-mandatory fields.
    """
    reasons: list[str] = []
    if field.mandatory == "conditional" and not _has_condition(field):
        reasons.append("conditional-mandatory field has no extracted condition")
    if field.input_method == "dropdown" and field.type != "date" and not field.enum:
        reasons.append("dropdown field has no options resolved")
    if _prose_implies_enable_condition(validations_raw) and not _has_condition(field):
        reasons.append("prose describes an enable condition but none was extracted")
    return reasons


def _assemble_field(
    field: Field,
    extraction: dict | None,
    option_lists: dict[str, list[str]],
    self_handle: str = "",
    validations_raw: str | None = None,
) -> tuple[Field, dict | None]:
    """Apply an extraction (or none), resolve a dropdown's options, strip
    self-referential conditions, and enforce invariants for one field.
    Returns (merged_field, review_item).
    """
    if extraction is not None:
        merged_field = apply_extraction(field, extraction)
    else:
        merged_field = field.model_copy(update={"confidence": "high", "rule_source": "extracted"})
        extraction = {}

    # Resolve dropdown options: supplementary sheet first, then app constants
    # (e.g. the State list). Both leave the LLM's conditional intact.
    enum_source = None
    if merged_field.input_method == "dropdown" and not merged_field.enum:
        resolved = resolve_dropdown(merged_field.label, option_lists)
        if resolved is None:
            resolved = resolve_constant_options(merged_field.label)
        if resolved is not None:
            enum_source, values = resolved
            merged_field = merged_field.model_copy(update={"enum": values})

    merged_field, had_self_ref = _strip_self_reference(merged_field, self_handle)

    reasons = _enforce_field_rules(merged_field, validations_raw)
    if had_self_ref:
        reasons.append(f"self-referential condition (referenced its own {self_handle}) removed")
    if reasons and merged_field.confidence != "low":
        merged_field = merged_field.model_copy(update={"confidence": "low"})

    review_item = None
    if merged_field.confidence == "low":
        note_parts: list[str] = []
        if extraction.get("parse_error"):
            note_parts.append(extraction["parse_error"])
        elif extraction.get("note"):
            note_parts.append(extraction["note"])
        note_parts.extend(reasons)
        if enum_source:
            note_parts.append(f"dropdown options taken from '{enum_source}' (verify)")
        review_item = {
            "sl": field.sl,
            "label": field.label,
            "source": field.source.model_dump(),
            "reason": "parse_error" if extraction.get("parse_error") else "low_confidence",
            "note": "; ".join(note_parts) or None,
        }
    return merged_field, review_item


def extract_and_merge(
    fields: list[Field],
    prose_by_sl: dict[str, dict],
    option_lists: dict[str, list[str]] | None = None,
    *,
    max_workers: int = 1,
    progress=None,
) -> tuple[list[Field], list[dict]]:
    """Merge library rules, LLM-extracted validations, and supplementary-sheet
    dropdown options into `fields`.

    Per field: (1) standard-field library match (skips the LLM), else (2) LLM
    extraction of Validations/Values prose when there's prose worth
    interpreting, (3) dropdown options resolved from the supplementary sheet
    if still empty, (4) enforcement — conditional-mandatory fields must carry
    a condition and dropdowns must carry options, else forced to low.

    Only the independent LLM calls (step 2) are parallelised: with
    max_workers > 1 they run in a thread pool (they're I/O-bound network
    calls). Assembly stays sequential and in field order, so output is
    deterministic regardless of worker count. `progress(done, total, field)`
    is called as each LLM extraction completes.

    Returns (merged_fields, review_queue) — review_queue lists every field
    that came back confidence="low", for human review before the engine runs.
    """
    option_lists = option_lists or {}
    # Directory covers every field on the page so expressions can reference
    # any other point by a stable handle, regardless of processing order.
    field_directory = build_field_directory(fields)
    self_handle = {f.sl: _point_handle(f.sl) for f in fields}
    # Immediately-preceding field's handle, for resolving "previous point".
    prev_handle = {
        fields[i].sl: (_point_handle(fields[i - 1].sl) if i > 0 else "")
        for i in range(len(fields))
    }

    def _uses_library(field: Field) -> bool:
        lib = library_match(field.label)
        return lib is not None and field.mandatory != "conditional"

    # Phase 1 — classify each field: library / read-only / clean / needs LLM.
    # Read-only "label" fields (auto-display, e.g. "State for GST invoicing")
    # have no user input to validate, so they skip the LLM like clean fields.
    llm_jobs: dict[str, tuple[Field, str | None, str | None]] = {}
    for field in fields:
        if _uses_library(field) or field.input_method == "label":
            continue
        prose = prose_by_sl.get(field.sl, {})
        values_raw, validations_raw = prose.get("values"), prose.get("validations")
        if needs_llm(values_raw, validations_raw, field.enum):
            llm_jobs[field.sl] = (field, values_raw, validations_raw)

    # Phase 2 — run the LLM extractions (optionally concurrent).
    extractions: dict[str, dict] = {}
    total = len(llm_jobs)

    def _run(f, v, val):
        return extract_field_validation(
            f, v, val, field_directory, self_handle[f.sl], prev_handle[f.sl]
        )

    if total:
        if max_workers > 1:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_run, f, v, val): f for (f, v, val) in llm_jobs.values()}
                for done, fut in enumerate(as_completed(futures), 1):
                    f = futures[fut]
                    extractions[f.sl] = fut.result()
                    if progress:
                        progress(done, total, f)
        else:
            for done, (f, v, val) in enumerate(llm_jobs.values(), 1):
                extractions[f.sl] = _run(f, v, val)
                if progress:
                    progress(done, total, f)

    # Phase 3 — assemble in field order (deterministic).
    merged: list[Field] = []
    review_queue: list[dict] = []
    for field in fields:
        if _uses_library(field):
            merged.append(apply_library(field, library_match(field.label)))
            continue
        merged_field, review_item = _assemble_field(
            field,
            extractions.get(field.sl),
            option_lists,
            self_handle[field.sl],
            prose_by_sl.get(field.sl, {}).get("validations"),
        )
        merged.append(merged_field)
        if review_item is not None:
            review_queue.append(review_item)

    return merged, review_queue
