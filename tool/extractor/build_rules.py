"""Assembles the full rules.json for one application+page: SOW fields
(clean columns + library/LLM-merged validations), age criteria, and
eligibility — the boundary artifact engine/ reads (see docs/rules.schema.md).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from tool.extractor.other_details import load_option_lists
from tool.extractor.validation_extractor import extract_and_merge
from tool.extractor.workbook_parser import (
    get_age_as_on_date,
    parse_age_criteria,
    parse_eligibility,
    parse_sow_sheet_with_prose,
)
from tool.models import Field, RulesFile


def build_rules(
    *,
    application: str,
    page: str,
    sow_path: str | Path,
    sow_sheet: str,
    age_path: str | Path | None = None,
    eligibility_path: str | Path | None = None,
    field_filter: Callable[[Field], bool] | None = None,
    max_workers: int = 1,
    progress=None,
) -> tuple[RulesFile, list[dict], list[dict]]:
    """Returns (rules, skipped_sow_rows, low_confidence_review_queue).

    `field_filter` optionally restricts which SOW fields are processed (e.g.
    only the scribe section for an incremental run); age/eligibility are
    always parsed in full since they're deterministic and cheap.
    `max_workers` > 1 runs the per-field LLM calls concurrently; `progress`
    is forwarded to extract_and_merge for live per-field reporting.
    """
    fields, skipped, prose = parse_sow_sheet_with_prose(sow_path, sow_sheet)
    if field_filter is not None:
        fields = [f for f in fields if field_filter(f)]
    option_lists = load_option_lists(sow_path)
    merged_fields, review_queue = extract_and_merge(
        fields, prose, option_lists, max_workers=max_workers, progress=progress
    )

    age, category_by_post = ({}, {})
    as_on_date = None
    if age_path is not None:
        age, category_by_post = parse_age_criteria(age_path)
        as_on_date = get_age_as_on_date(age_path)

    eligibility = parse_eligibility(eligibility_path) if eligibility_path is not None else {}

    rules = RulesFile(
        application=application,
        page=page,
        as_on_date=as_on_date,
        fields=merged_fields,
        category_by_post=category_by_post,
        age=age,
        eligibility=eligibility,
    )
    return rules, skipped, review_queue


def _scribe_section(field: Field) -> bool:
    """The 'till scribe section' slice: sl 1 through 9.10 (the disability /
    scribe block). sl values are dotted strings like '9.10'; compare by the
    integer part first, then the decimal part.
    """
    def key(sl: str) -> tuple[int, int]:
        head, _, tail = sl.partition(".")
        try:
            return (int(head), int(tail) if tail else 0)
        except ValueError:
            return (10**9, 0)  # non-numeric sl sorts last (never in scribe block)

    return key(field.sl) <= key("9.10")


def _make_progress(stream):
    """A progress reporter that prints '[done/total] sl label  Xs (avg, eta)'
    to `stream`, estimating remaining time from the running average latency.
    """
    import time

    start = time.perf_counter()

    def progress(done: int, total: int, field) -> None:
        elapsed = time.perf_counter() - start
        avg = elapsed / done
        eta = avg * (total - done)
        bar_len = 24
        filled = int(bar_len * done / total)
        bar = "#" * filled + "-" * (bar_len - filled)
        print(
            f"[{bar}] {done}/{total}  {field.sl}:{field.label[:28]:30}  "
            f"avg {avg:4.0f}s  eta ~{eta/60:4.1f}m",
            file=stream,
            flush=True,
        )

    return progress


if __name__ == "__main__":
    import argparse
    import json
    import sys

    from tool.extractor.llm import usage_summary

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scribe-only",
        action="store_true",
        help="process only the scribe section (sl 1..9.10) for an incremental run",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="number of concurrent LLM calls (default 4; use 1 for strictly sequential)",
    )
    args = parser.parse_args()

    rules, skipped, review_queue = build_rules(
        application="nitr_jul26",
        page="reg_details.php",
        sow_path="samples/nitrjul26/Revised_1_SOW.xlsx",
        sow_sheet="Basic Details",
        age_path="samples/nitrjul26/Revised_Age_Criteria.xlsx",
        eligibility_path="samples/nitrjul26/Revised_Eligibility.xlsx",
        field_filter=_scribe_section if args.scribe_only else None,
        max_workers=args.workers,
        progress=_make_progress(sys.stderr),
    )
    print(
        json.dumps(rules.model_dump(exclude_none=True), default=str, indent=2),
        file=sys.stdout,
    )
    print(f"skipped_rows={len(skipped)} low_confidence={len(review_queue)}", file=sys.stderr)
    print(f"usage={usage_summary()}", file=sys.stderr)
    if review_queue:
        print("--- review queue ---", file=sys.stderr)
        for item in review_queue:
            print(f"  {item['sl']} | {item['label'][:55]} | {item['note']}", file=sys.stderr)
