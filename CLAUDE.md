# CLAUDE.md — Automated Registration-Portal Validation Tool

This file orients you (Claude Code) to the project. Read it fully before writing code.

## What we are building

An automated validation tool that tests multi-page bank/government **exam registration web
applications** against their **requirement documents**, and produces a defect report. It
replaces most manual QA effort while keeping accuracy auditable, because these forms process
real candidate data.

## The one rule that governs the whole design

**The LLM never decides pass/fail.** GenAI is used only to (1) read messy requirement
spreadsheets into a structured rules file, and (2) write the human-readable report. Every
correctness verdict is produced by deterministic Python assertions. If you find yourself asking
an LLM "did this field pass?", stop — that logic belongs in the engine.

## Three layers

1. **Rule extraction (`extractor/`)** — reads the SOW, age, and eligibility workbooks and emits
   `rules.json`. Clean columns (Type, Max Length, Mandatory, Input Method) are parsed
   deterministically with openpyxl/pandas — no LLM. Only the free-text `Validations`/`Values`
   prose goes through the Anthropic API, and each interpreted rule gets a `confidence` tag and a
   `source` (sheet+row).
2. **Execution engine (`engine/`)** — Python + Playwright. Resolves fields by label, then runs
   validation, conditional-logic, cross-document, save/back, and monkey checks. Fully
   deterministic. Emits a structured result set (`results.json`).
3. **Report generation (`report/`)** — turns `results.json` into HTML + xlsx. May use the
   Anthropic API to phrase issues and draft clarification questions, but must not add, remove,
   or change any verdict.

## Input documents (see rules.schema.md for detail)

- **SOW** — one sheet per flow page. Fixed columns: `Sl No, Label Name, Type, Max Length,
  Mandatory ?, Input Method, Values, Default Value, Validations, Help Text, Addl Remarks`.
  Content differs per application; structure does not.
- **Age Criteria** — per-post grid: min/max years and category-wise relaxation as cut-off DOBs
  for a fixed "as-on" date. Category columns: UR/EWS, OBC, SC, ST.
- **Eligibility** — per-post AND/OR trees over qualification, stream, %, class/grade.
- **Supplementary sheets** — optional, requirement-specific (e.g. state-wise district list,
  state-wise medium of paper). Discover dynamically; never hardcode their presence or columns.

The SOW, age, and eligibility workbooks are always present and structurally fixed. Two sample
applications are in `samples/` — they share structure but nothing else, which is the invariant
the whole tool relies on.

## Selector independence — critical

Page `id`/`class`/`name` attributes **change between applications** and must never appear in
rules or assertions. Resolve fields by their stable identifiers instead:

1. `page.get_by_label(label_text)` (Playwright accessibility locator) — handles `<label for>`,
   `aria-label`, wrapping labels.
2. Proximity fallback for bare text-node labels (legacy table forms): find the text, walk to the
   nearest form control in the same container.
3. Section-anchored disambiguation when a label is not unique (e.g. "Address1" under both
   Correspondence and Permanent) — qualify by the SOW section heading.
4. If still unresolved → emit a report finding: `Field <label> (SOW row <n>) not found on page`.
   Never silently skip.

Build a `field_map` (label → resolved locator) once per run. It is the ONLY selector-aware
artifact and is rebuilt every run. Assertions reference labels only.

## Minimising human review

- Deterministic-parse the clean columns → no review needed for those.
- Tag interpreted rules `high`/`low` confidence; only `low` ones surface for review.
- Maintain a **standard-field rule library** (`extractor/library/`): Aadhaar, PAN, IFSC, Pincode
  etc. have identical rules across applications (SOW marks them grey / "standard"). Reviewed once,
  reused thereafter. New applications review only novel fields.

## Tech stack

- Python 3.11+, Playwright (sync API is fine), openpyxl, pandas.
- Anthropic API via the official SDK. Model calls only in `extractor/` and `report/`.
  Constrain prompts to emit JSON only; parse defensively; retry on malformed output.
- CLI entry point: `python -m tool run --url <link> --docs <dir> --page reg_details --out <dir>`.

## Human-gated pages

Basic details, OTP, and photo/signature pages need human interaction (CAPTCHA, OTP, live
capture) and are OUT of scope for automation. Runs start from a **pre-seeded deep link** (e.g.
the `reg_details.php?q=...` link from a manually created entry). Take that link as input.

## Build order (do not skip Phase 0)

- **Phase 0**: workbook parser (deterministic columns) + LLM extractor for `Validations` with
  confidence tags + field resolver. Validate the resolver against the live `reg_details.php`
  before writing any assertion. Agree `rules.json` shape here.
- **Phase 1**: `reg_details.php` full coverage — field-level, conditional, save/back. Basic HTML
  report. Prove exception-only review.
- **Phase 2**: cross-document checks — DOB vs age grid per post+category, category-per-post
  filtering, eligibility gating.
- **Phase 3**: `edu_details.php` reusing the engine unchanged; only new rules.
- **Phase 4+**: remaining pages, monkey generator, polished HTML+xlsx, optional CI.

## Conventions

- Every check result carries: `page, field_label, sow_row, check_type, severity,
  input_used, expected, observed, status (issue|clarification|pass), screenshot?`.
- Assert BOTH directions: invalid input is rejected AND valid input is accepted. A field that
  rejects everything is as much a bug as one that accepts everything.
- Client-side and server-side validation are asserted independently (submit to force server
  round-trip where client validation might mask it).
- No secrets in the repo. Anthropic API key from environment (`ANTHROPIC_API_KEY`).
- Keep the engine dumb and the rules expressive: interpretation lives in `rules.json`, never in
  engine branches.
