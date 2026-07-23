"""Tests for the offline-testable parts of validation_extractor.py and
library/: JSON-contaminant stripping, the needs_llm short-circuit, library
matching, and the merge logic. None of these call the LLM — they exercise
call_llm's callers, not call_llm itself, so the suite runs without network
access or NVIDIA_API_KEY.
"""

import pytest

from tool.extractor.library import library_match
from tool.extractor.other_details import load_option_lists, resolve_dropdown
from tool.extractor.validation_extractor import (
    _clean_conditional,
    _extract_json_object,
    _has_condition,
    _parse_llm_json,
    _point_handle,
    apply_extraction,
    apply_library,
    build_field_directory,
    extract_and_merge,
    needs_llm,
)
from tool.models import Conditional, Field, Source


def make_field(**overrides) -> Field:
    defaults = dict(
        sl="1",
        label="Some Field",
        type="varchar",
        max_length=20,
        mandatory=True,
        input_method="text",
        source=Source(sheet="Basic Details", row=3),
    )
    defaults.update(overrides)
    return Field(**defaults)


# ---------------------------------------------------------------------------
# _extract_json_object
# ---------------------------------------------------------------------------


def test_extract_json_object_plain():
    assert _extract_json_object('{"a": 1}') == '{"a": 1}'


def test_extract_json_object_strips_markdown_fence():
    text = '```json\n{"a": 1}\n```'
    assert _extract_json_object(text) == '{"a": 1}'


def test_extract_json_object_strips_leading_and_trailing_prose():
    text = 'Sure, here is the JSON:\n{"a": {"b": 2}}\nHope that helps!'
    assert _extract_json_object(text) == '{"a": {"b": 2}}'


def test_extract_json_object_brace_inside_string_value_not_a_delimiter():
    text = '{"a": "contains } brace", "b": 2}'
    assert _extract_json_object(text) == text


def test_extract_json_object_no_brace_raises():
    with pytest.raises(ValueError):
        _extract_json_object("no json here")


def test_extract_json_object_unbalanced_raises():
    with pytest.raises(ValueError):
        _extract_json_object('{"a": 1')


# ---------------------------------------------------------------------------
# needs_llm
# ---------------------------------------------------------------------------


def test_needs_llm_false_when_nothing_to_interpret():
    assert needs_llm(None, None, None) is False


def test_needs_llm_false_when_values_already_a_clean_enum():
    assert needs_llm("YES or NO", None, ["YES", "NO"]) is False


def test_needs_llm_true_when_validations_present():
    assert needs_llm(None, "Enabled and mandatory if...", None) is True


def test_needs_llm_true_when_values_is_unparsed_prose():
    assert needs_llm("some free text values", None, None) is True


# ---------------------------------------------------------------------------
# library
# ---------------------------------------------------------------------------


def test_library_matches_pan():
    match = library_match("PAN Card No. :")
    assert match["rule_source"] == "library:pan"
    assert match["confidence"] == "high"
    assert match["uppercase"] is True


def test_library_matches_aadhaar_but_not_consent_lookalike():
    assert library_match("Aadhaar Card No. :")["rule_source"] == "library:aadhaar"
    assert library_match("Consent to Aadhaar Verification") is None


def test_library_matches_ifsc_and_pincode():
    assert library_match("IFSC")["rule_source"] == "library:ifsc"
    assert library_match("Pin code")["rule_source"] == "library:pincode"


def test_library_no_match_for_unrelated_label():
    assert library_match("Nationality / Citizenship :") is None


def test_library_matches_personal_fields():
    assert library_match("Gender")["rule_source"] == "library:gender"
    assert library_match("Marital Status :*")["rule_source"] == "library:marital_status"
    assert library_match("Father's Name :")["rule_source"] == "library:person_name"
    assert library_match("Account Holder Name")["rule_source"] == "library:person_name"
    assert library_match("Type of Account")["rule_source"] == "library:account_type"


def test_library_confirm_account_gets_must_equal_and_plain_does_not():
    assert library_match("Bank Account No")["rule_source"] == "library:bank_account_no"
    confirm = library_match("Confirm Bank Account No")
    assert confirm["rule_source"] == "library:confirm_bank_account_no"
    assert confirm["must_equal"] == "Bank Account No"


def test_library_does_not_match_scribe_name():
    assert library_match("Name of the Scribe") is None


def test_apply_library_merges_rule_and_preserves_other_field_attrs():
    field = make_field(label="PAN Card No. :", type="alphanumeric", max_length=10)
    lib_rule = library_match(field.label)
    merged = apply_library(field, lib_rule)
    assert merged.regex == r"^[A-Z]{5}[0-9]{4}[A-Z]$"
    assert merged.uppercase is True
    assert merged.confidence == "high"
    assert merged.rule_source == "library:pan"
    assert merged.sl == field.sl  # untouched
    assert merged.max_length == 10  # untouched


# ---------------------------------------------------------------------------
# apply_extraction
# ---------------------------------------------------------------------------


def test_apply_extraction_merges_conditional_and_confidence():
    field = make_field(label="Do you intend to use the services of a scribe ?", type="alpha")
    extraction = {
        "regex": None,
        "charset": None,
        "enum": ["YES", "NO"],
        "must_equal": None,
        "uppercase": None,
        "conditional": {
            "enabled_when": None,
            "disabled_when": None,
            "mandatory_when": "disability_type in ['B','LV'] or cerebral_palsy == 'YES'",
            "options_when": None,
        },
        "confidence": "low",
        "note": "references other fields by point number",
    }
    merged = apply_extraction(field, extraction)
    assert merged.enum == ["YES", "NO"]
    assert merged.conditional.mandatory_when == "disability_type in ['B','LV'] or cerebral_palsy == 'YES'"
    assert merged.confidence == "low"
    assert merged.rule_source == "extracted"


def test_apply_extraction_keeps_existing_value_when_extraction_is_null():
    field = make_field(enum=["Male", "Female", "Others"])
    extraction = {
        "regex": None, "charset": None, "enum": None, "must_equal": None,
        "uppercase": None, "conditional": None, "confidence": "high", "note": None,
    }
    merged = apply_extraction(field, extraction)
    assert merged.enum == ["Male", "Female", "Others"]  # not clobbered by a null


# ---------------------------------------------------------------------------
# extract_and_merge — library and no-op paths only (no LLM call involved)
# ---------------------------------------------------------------------------


def test_extract_and_merge_library_field_skips_llm_entirely(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("LLM should not be called for a library-matched field")

    monkeypatch.setattr(
        "tool.extractor.validation_extractor.extract_field_validation", boom
    )
    field = make_field(label="IFSC", type="alphanumeric", max_length=11)
    merged, review_queue = extract_and_merge([field], {"1": {"values": None, "validations": None}})
    assert merged[0].rule_source == "library:ifsc"
    assert review_queue == []


def test_extract_and_merge_clean_field_skips_llm_entirely(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("LLM should not be called when there's nothing to interpret")

    monkeypatch.setattr(
        "tool.extractor.validation_extractor.extract_field_validation", boom
    )
    field = make_field(label="Nationality / Citizenship :")
    merged, review_queue = extract_and_merge([field], {"1": {"values": None, "validations": None}})
    assert merged[0].confidence == "high"
    assert merged[0].rule_source == "extracted"
    assert review_queue == []


def test_extract_and_merge_records_low_confidence_in_review_queue(monkeypatch):
    def fake_extract(field, values_raw, validations_raw, field_directory="", self_handle="", prev_handle=""):
        return {
            "regex": None, "charset": None, "enum": None, "must_equal": None,
            "uppercase": None, "conditional": None, "confidence": "low",
            "note": "ambiguous prose",
        }

    monkeypatch.setattr(
        "tool.extractor.validation_extractor.extract_field_validation", fake_extract
    )
    field = make_field(label="Something Ambiguous")
    merged, review_queue = extract_and_merge(
        [field], {"1": {"values": None, "validations": "some ambiguous prose"}}
    )
    assert merged[0].confidence == "low"
    assert len(review_queue) == 1
    assert review_queue[0]["label"] == "Something Ambiguous"
    assert review_queue[0]["reason"] == "low_confidence"
    assert review_queue[0]["note"] == "ambiguous prose"


def test_extract_and_merge_records_parse_error_in_review_queue(monkeypatch):
    def fake_extract(field, values_raw, validations_raw, field_directory="", self_handle="", prev_handle=""):
        return {
            "regex": None, "charset": None, "enum": None, "must_equal": None,
            "uppercase": None, "conditional": None, "confidence": "low",
            "parse_error": "invalid JSON: 'garbage'",
        }

    monkeypatch.setattr(
        "tool.extractor.validation_extractor.extract_field_validation", fake_extract
    )
    field = make_field(label="Something Unparseable")
    merged, review_queue = extract_and_merge(
        [field], {"1": {"values": None, "validations": "some prose"}}
    )
    assert review_queue[0]["reason"] == "parse_error"
    assert "invalid JSON" in review_queue[0]["note"]


# ---------------------------------------------------------------------------
# _clean_conditional / _has_condition
# ---------------------------------------------------------------------------


def test_clean_conditional_collapses_empty_and_all_null():
    assert _clean_conditional(None) is None
    assert _clean_conditional({}) is None
    assert _clean_conditional(
        {"enabled_when": None, "disabled_when": None, "mandatory_when": None, "options_when": None}
    ) is None


def test_clean_conditional_keeps_real_expression():
    cond = {"enabled_when": "x == 'YES'", "disabled_when": None, "mandatory_when": None, "options_when": None}
    assert _clean_conditional(cond) == cond


def test_has_condition():
    assert _has_condition(make_field()) is False
    assert _has_condition(make_field(conditional=Conditional())) is False
    assert _has_condition(make_field(conditional=Conditional(mandatory_when="a == 'b'"))) is True


# ---------------------------------------------------------------------------
# Enforcement: conditional-mandatory must have a condition; dropdown must have enum
# ---------------------------------------------------------------------------


def test_conditional_mandatory_without_condition_forced_low(monkeypatch):
    # LLM (wrongly) returns high with no condition for a conditional field.
    def fake_extract(field, values_raw, validations_raw, field_directory="", self_handle="", prev_handle=""):
        return {
            "regex": None, "charset": None, "enum": ["YES", "NO"], "must_equal": None,
            "uppercase": None, "conditional": None, "confidence": "high", "note": None,
        }

    monkeypatch.setattr(
        "tool.extractor.validation_extractor.extract_field_validation", fake_extract
    )
    field = make_field(label="Conditional Q", mandatory="conditional", input_method="radio")
    merged, review_queue = extract_and_merge(
        [field], {"1": {"values": "YES or NO", "validations": "enabled if something"}}
    )
    assert merged[0].confidence == "low"
    assert any("no extracted condition" in r["note"] for r in review_queue)


def test_dropdown_without_options_forced_low(monkeypatch):
    def fake_extract(field, values_raw, validations_raw, field_directory="", self_handle="", prev_handle=""):
        return {
            "regex": None, "charset": None, "enum": None, "must_equal": None,
            "uppercase": None, "conditional": None, "confidence": "high", "note": None,
        }

    monkeypatch.setattr(
        "tool.extractor.validation_extractor.extract_field_validation", fake_extract
    )
    field = make_field(label="Some Dropdown", mandatory=True, input_method="dropdown")
    merged, review_queue = extract_and_merge(
        [field], {"1": {"values": "refer other details", "validations": None}}, option_lists={}
    )
    assert merged[0].confidence == "low"
    assert any("dropdown field has no options" in r["note"] for r in review_queue)


def test_dropdown_resolved_from_option_lists(monkeypatch):
    def fake_extract(field, values_raw, validations_raw, field_directory="", self_handle="", prev_handle=""):
        return {
            "regex": None, "charset": None, "enum": None, "must_equal": None,
            "uppercase": None, "conditional": None, "confidence": "high", "note": None,
        }

    monkeypatch.setattr(
        "tool.extractor.validation_extractor.extract_field_validation", fake_extract
    )
    field = make_field(label="Post applied for", mandatory=True, input_method="dropdown")
    option_lists = {"Post Names": ["Superintendent", "Office Attendant"]}
    merged, _ = extract_and_merge(
        [field], {"1": {"values": "refer other details", "validations": None}}, option_lists=option_lists
    )
    assert merged[0].enum == ["Superintendent", "Office Attendant"]


# ---------------------------------------------------------------------------
# other_details resolver (against the real NITR sample)
# ---------------------------------------------------------------------------

from pathlib import Path

_NITR_SOW = Path(__file__).parent.parent / "samples" / "nitrjul26" / "Revised_1_SOW.xlsx"


def test_load_option_lists_reads_named_columns():
    lists = load_option_lists(_NITR_SOW)
    assert "Post Names" in lists
    assert len(lists["Post Names"]) == 31
    assert lists["Disability Category"] == ["a", "b", "c", "d", "e"]
    assert "Sr. No." not in lists  # index column excluded


def test_resolve_dropdown_matches_by_alias_and_header():
    lists = load_option_lists(_NITR_SOW)
    assert resolve_dropdown("Post applied for ", lists)[0] == "Post Names"
    assert resolve_dropdown("Disability category ", lists)[0] == "Disability Category"
    assert resolve_dropdown("Type of Disability ", lists)[0] == "Type of Disability"
    assert resolve_dropdown("Father's Name :", lists) is None


def test_centres_sublist_block_parsed_and_resolved():
    lists = load_option_lists(_NITR_SOW)
    assert lists.get("Centres") == ["Raipur", "Bhilai/Durg", "Nagpur"]
    matched, values = resolve_dropdown("Centre of Examination (Preference 1):", lists)
    assert matched == "Centres"
    assert values == ["Raipur", "Bhilai/Durg", "Nagpur"]


# ---------------------------------------------------------------------------
# #3 point handles / field directory
# ---------------------------------------------------------------------------


def test_point_handle_derivation():
    assert _point_handle("9.1") == "point_9_1"
    assert _point_handle("9.10") == "point_9_10"
    assert _point_handle("23") == "point_23"


def test_build_field_directory_lists_all_fields():
    fields = [
        make_field(sl="3", label="Are you a person with benchmark disability of 40% and above ?"),
        make_field(sl="9.1", label="Type of Disability"),
    ]
    directory = build_field_directory(fields)
    assert "point_3: Are you a person with benchmark disability" in directory
    assert "point_9_1: Type of Disability" in directory


# ---------------------------------------------------------------------------
# #2 options_when stripped from LLM output
# ---------------------------------------------------------------------------


def test_parse_llm_json_strips_stray_options_when():
    # Model emits options_when anyway (with the old malformed shape) — it must
    # be dropped, not crash, and the rest of the conditional preserved.
    reply = (
        '{"regex": null, "charset": null, "enum": null, "must_equal": null, '
        '"uppercase": null, "conditional": {"mandatory_when": "point_3 == \'YES\'", '
        '"options_when": {"point_1 == \'X\'": "cat == \'SC\'"}}, "confidence": "high", "note": null}'
    )
    parsed = _parse_llm_json(reply)
    assert "options_when" not in parsed["conditional"]
    assert parsed["conditional"]["mandatory_when"] == "point_3 == 'YES'"


# ---------------------------------------------------------------------------
# #1 date-type dropdown exempt from the enum requirement
# ---------------------------------------------------------------------------


def test_date_dropdown_not_flagged_for_missing_enum(monkeypatch):
    def fake_extract(field, values_raw, validations_raw, field_directory="", self_handle="", prev_handle=""):
        return {
            "regex": None, "charset": None, "enum": None, "must_equal": None,
            "uppercase": None, "conditional": None, "confidence": "high", "note": None,
        }

    monkeypatch.setattr(
        "tool.extractor.validation_extractor.extract_field_validation", fake_extract
    )
    # A non-library date field (Date of Passing) still exercises the exemption.
    field = make_field(label="Date of Passing", type="date", mandatory=True, input_method="dropdown")
    merged, review_queue = extract_and_merge(
        [field], {"1": {"values": "day/month/year", "validations": "pick a date"}}
    )
    assert merged[0].confidence == "high"
    assert review_queue == []


# ---------------------------------------------------------------------------
# constants: State dropdown & DOB
# ---------------------------------------------------------------------------


def test_resolve_constant_options_state():
    from tool.extractor.constants import STATES, resolve_constant_options

    matched, values = resolve_constant_options("State ")
    assert matched == "constant:states"
    assert values == STATES
    # derived GST label is NOT a state picker
    assert resolve_constant_options("State for GST invoicing") is None
    assert resolve_constant_options("Father's Name") is None


def test_state_dropdown_filled_from_constants_but_llm_still_runs(monkeypatch):
    # LLM captures a condition (permanent-address auto-populate); constants
    # fill the enum. Both must survive.
    def fake_extract(field, values_raw, validations_raw, field_directory="", self_handle="", prev_handle=""):
        return {
            "regex": None, "charset": None, "enum": None, "must_equal": None,
            "uppercase": None,
            "conditional": {"disabled_when": "point_35 == true"},
            "confidence": "high", "note": None,
        }

    monkeypatch.setattr(
        "tool.extractor.validation_extractor.extract_field_validation", fake_extract
    )
    from tool.extractor.constants import STATES

    field = make_field(sl="40", label="State", mandatory=True, input_method="dropdown")
    merged, _ = extract_and_merge(
        [field], {"40": {"values": None, "validations": "auto populate if point 35 checked"}}
    )
    assert merged[0].enum == STATES
    assert merged[0].conditional.disabled_when == "point_35 == true"


def test_date_of_birth_is_library_skipped_high(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("DOB should skip the LLM (library standard field)")

    monkeypatch.setattr("tool.extractor.validation_extractor.extract_field_validation", boom)
    field = make_field(sl="21", label="Date of Birth", type="date", mandatory=True, input_method="dropdown")
    merged, review_queue = extract_and_merge(
        [field], {"21": {"values": "day/month/year", "validations": "based on min/max year"}}
    )
    assert merged[0].confidence == "high"
    assert merged[0].rule_source == "library:date_of_birth"
    assert review_queue == []


# ---------------------------------------------------------------------------
# read-only / label fields skip the LLM
# ---------------------------------------------------------------------------


def test_readonly_label_field_skips_llm(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("read-only label field should not hit the LLM")

    monkeypatch.setattr("tool.extractor.validation_extractor.extract_field_validation", boom)
    field = make_field(sl="43", label="State for GST invoicing", input_method="label", mandatory=True)
    merged, review_queue = extract_and_merge(
        [field], {"43": {"values": "Auto Display", "validations": "auto display based on state"}}
    )
    assert merged[0].confidence == "high"
    assert review_queue == []


# ---------------------------------------------------------------------------
# self-referential condition stripped
# ---------------------------------------------------------------------------


def test_self_referential_condition_stripped_and_flagged(monkeypatch):
    def fake_extract(field, values_raw, validations_raw, field_directory="", self_handle="", prev_handle=""):
        return {
            "regex": None, "charset": None, "enum": ["YES", "NO"], "must_equal": None,
            "uppercase": None,
            "conditional": {"disabled_when": "point_3 == 'NO'"},  # self-reference
            "confidence": "high", "note": None,
        }

    monkeypatch.setattr(
        "tool.extractor.validation_extractor.extract_field_validation", fake_extract
    )
    field = make_field(sl="3", label="Are you a person with benchmark disability?",
                       mandatory=True, input_method="radio")
    merged, review_queue = extract_and_merge(
        [field], {"3": {"values": "YES or NO", "validations": "some prose"}}
    )
    assert merged[0].conditional is None  # bogus self-ref removed
    assert merged[0].confidence == "low"
    assert any("self-referential" in r["note"] for r in review_queue)


# ---------------------------------------------------------------------------
# enable-condition-in-prose but not captured -> flagged
# ---------------------------------------------------------------------------


def test_missing_enable_condition_flagged(monkeypatch):
    def fake_extract(field, values_raw, validations_raw, field_directory="", self_handle="", prev_handle=""):
        return {  # model dropped the condition
            "regex": None, "charset": None, "enum": None, "must_equal": None,
            "uppercase": None, "conditional": None, "confidence": "high", "note": None,
        }

    monkeypatch.setattr(
        "tool.extractor.validation_extractor.extract_field_validation", fake_extract
    )
    field = make_field(sl="9.9", label="I undertake to produce UDID...", type="checkbox",
                       mandatory=False, input_method="checkbox")
    merged, review_queue = extract_and_merge(
        [field], {"9.9": {"values": None, "validations": "Enabled but not Mandatory if selected YES in point no 9 OR 9.3"}}
    )
    assert merged[0].confidence == "low"
    assert any("enable condition" in r["note"] for r in review_queue)


def test_enabled_for_all_prose_not_flagged(monkeypatch):
    def fake_extract(field, values_raw, validations_raw, field_directory="", self_handle="", prev_handle=""):
        return {
            "regex": r"^[A-Z]{5}[0-9]{4}[A-Z]$", "charset": None, "enum": None, "must_equal": None,
            "uppercase": True, "conditional": None, "confidence": "high", "note": None,
        }

    monkeypatch.setattr(
        "tool.extractor.validation_extractor.extract_field_validation", fake_extract
    )
    field = make_field(sl="19.3", label="PAN Card No.", type="alphanumeric",
                       mandatory=False, input_method="text")
    merged, review_queue = extract_and_merge(
        [field], {"19.3": {"values": None, "validations": "Enabled for all. Ten characters..."}}
    )
    assert merged[0].confidence == "high"
    assert review_queue == []
