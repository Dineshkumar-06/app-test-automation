"""Tests against the two real sample applications (samples/).

Counts and spot-checks are pinned to the actual workbook content — if a
sample file changes, these are expected to need updating.
"""

from pathlib import Path

from tool.extractor.workbook_parser import parse_age_criteria, parse_eligibility, parse_sow_sheet

SAMPLES = Path(__file__).parent.parent / "samples"
NITR = SAMPLES / "nitrjul26"
SBI = SAMPLES / "sbiaapr26"


# ---------------------------------------------------------------------------
# SOW field parsing
# ---------------------------------------------------------------------------


def test_nitr_basic_details_counts():
    fields, skipped = parse_sow_sheet(NITR / "Revised_1_SOW.xlsx", "Basic Details")
    assert len(fields) == 65
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "missing_label"
    assert skipped[0]["sl"] == 11


def test_nitr_basic_details_spot_check_pan_field():
    fields, _ = parse_sow_sheet(NITR / "Revised_1_SOW.xlsx", "Basic Details")
    pan = next(f for f in fields if f.sl == "19.3")
    assert pan.label == "PAN Card No. :"
    assert pan.type == "alphanumeric"
    assert pan.max_length == 10
    assert pan.mandatory is False
    assert pan.input_method == "text"
    assert pan.source.sheet == "Basic Details"
    assert pan.source.row == 34


def test_nitr_basic_details_spot_check_conditional_field():
    fields, _ = parse_sow_sheet(NITR / "Revised_1_SOW.xlsx", "Basic Details")
    scribe = next(f for f in fields if f.sl == "9")
    assert scribe.mandatory == "conditional"
    assert scribe.input_method == "radio"


def test_nitr_basic_details_skips_section_and_note_rows():
    # "Basic Details" (merged section header) and "Note: To select multiple
    # disabilities..." (row 4.2) both have a blank Mandatory column and must
    # not appear as fields.
    fields, _ = parse_sow_sheet(NITR / "Revised_1_SOW.xlsx", "Basic Details")
    labels = [f.label for f in fields]
    assert "Basic Details" not in labels
    assert not any(label.startswith("Note:") for label in labels)


def test_nitr_basic_details_handles_subnumbered_sl():
    fields, _ = parse_sow_sheet(NITR / "Revised_1_SOW.xlsx", "Basic Details")
    sls = {f.sl for f in fields}
    assert {"4.1", "4.2", "9.1", "9.10"} <= sls


def test_nitr_qualifiexperlang_counts():
    fields, skipped = parse_sow_sheet(NITR / "Revised_1_SOW.xlsx", "QualifiExperLang")
    assert len(fields) == 47
    assert len(skipped) == 3


def test_sbi_basic_details_counts():
    fields, skipped = parse_sow_sheet(SBI / "Revised_11_SOW.xlsx", "Basic Details")
    assert len(fields) == 65
    assert len(skipped) == 1


def test_sbi_basic_details_spot_check_category_enum():
    fields, _ = parse_sow_sheet(SBI / "Revised_11_SOW.xlsx", "Basic Details")
    category = next(f for f in fields if f.sl == "9.1")
    assert category.label == "Category"
    assert category.enum == ["UR", "EWS", "OBC", "SC", "ST"]
    assert category.mandatory is True


def test_sbi_qualifiexperlang_counts():
    fields, skipped = parse_sow_sheet(SBI / "Revised_11_SOW.xlsx", "QualifiExperLang")
    assert len(fields) == 27
    assert len(skipped) == 1


def test_type_and_input_method_variants_normalize_without_error():
    # Sheets deliberately exercise the messiest spelling variants observed
    # across both workbooks (Alpha Numeric, Checkbox, LAbel, Decimal 5,2...).
    for path, sheets in [
        (NITR / "Revised_1_SOW.xlsx", ["Preview", "Payment"]),
        (SBI / "Revised_11_SOW.xlsx", ["Preview", "Payment"]),
    ]:
        for sheet in sheets:
            fields, _ = parse_sow_sheet(path, sheet)
            assert len(fields) > 0


# ---------------------------------------------------------------------------
# Age criteria parsing
# ---------------------------------------------------------------------------


def test_nitr_age_criteria_post_count():
    age, category_by_post = parse_age_criteria(NITR / "Revised_Age_Criteria.xlsx")
    assert len(age) == 31
    assert age.keys() == category_by_post.keys()


def test_nitr_age_criteria_superintendent_spot_check():
    age, category_by_post = parse_age_criteria(NITR / "Revised_Age_Criteria.xlsx")
    rule = age["Superintendent"]
    assert rule.min_dob.isoformat() == "2008-08-01"
    assert rule.max_dob_by_category["UR/EWS"].isoformat() == "1996-08-02"
    assert rule.max_dob_by_category["ST"].isoformat() == "1991-08-02"
    assert rule.relaxations["EXS"]["UR/EWS"].isoformat() == "1971-08-02"
    assert category_by_post["Superintendent"] == ["UR/EWS", "OBC", "SC", "ST"]


def test_nitr_age_criteria_single_category_sheet():
    # OBC_30 sheet: posts restricted to OBC only, narrower grid layout
    # (fewer leading spacer columns) than the 4-category sheets.
    age, category_by_post = parse_age_criteria(NITR / "Revised_Age_Criteria.xlsx")
    rule = age["Technical Assistant (Chemical Engineering)"]
    assert category_by_post["Technical Assistant (Chemical Engineering)"] == ["OBC"]
    assert rule.max_dob_by_category["OBC"].isoformat() == "1993-08-02"


def test_sbi_age_criteria_splits_by_state_group():
    age, category_by_post = parse_age_criteria(SBI / "Revised_1_Age_Criteria.xlsx")
    assert len(age) == 4
    all_cat_key = "Engagement of Apprentices Under The Apprentices Act, 1961 [Vacancy for all category]"
    assert all_cat_key in age
    assert category_by_post[all_cat_key] == ["UR", "EWS", "OBC", "SC", "ST"]
    # min_dob is shared across all state-group variants of the same post.
    min_dobs = {rule.min_dob for key, rule in age.items() if key.startswith("Engagement")}
    assert len(min_dobs) == 1


def test_sbi_age_criteria_state_group_categories_differ():
    _, category_by_post = parse_age_criteria(SBI / "Revised_1_Age_Criteria.xlsx")
    only_st_key = "Engagement of Apprentices Under The Apprentices Act, 1961 [Vacancy only for ST, EWS, UR]"
    assert set(category_by_post[only_st_key]) == {"UR", "EWS", "ST"}


# ---------------------------------------------------------------------------
# Eligibility parsing
# ---------------------------------------------------------------------------


def test_nitr_eligibility_post_count():
    trees = parse_eligibility(NITR / "Revised_Eligibility.xlsx")
    assert len(trees) == 31


def test_nitr_eligibility_applied_geology_matches_schema_worked_example():
    trees = parse_eligibility(NITR / "Revised_Eligibility.xlsx")
    tree = trees["Technical Assistant (Applied Geology)"]
    assert tree.op == "OR"
    assert len(tree.children) == 2

    first_leaf = tree.children[0].children[0]
    assert first_leaf.qualification == "Graduation"
    assert first_leaf.stream == ["Science with Geology as one subject"]
    assert first_leaf.class_ == "First Class"

    second_group = tree.children[1].children
    assert len(second_group) == 2
    assert second_group[0].class_ == "Any"
    assert second_group[1].qualification == "Post Graduation"
    assert second_group[1].stream == ["Geology", "Applied Geology"]
    assert second_group[1].min_pct == 50.0


def test_nitr_eligibility_descriptive_leaf_preserved():
    # "Do you have knowledge of Computer applications...?" is a plain
    # boolean requirement, not a qualification row — it must survive as a
    # leaf (qualification = the full text) rather than being dropped.
    trees = parse_eligibility(NITR / "Revised_Eligibility.xlsx")
    tree = trees["Superintendent"]
    all_leaves = [leaf for group in tree.children for leaf in group.children]
    assert any("knowledge of Computer applications" in leaf.qualification for leaf in all_leaves)


def test_sbi_eligibility_single_post():
    trees = parse_eligibility(SBI / "Eligibility_Criteria.xlsx")
    assert len(trees) == 1
    tree = next(iter(trees.values()))
    leaf = tree.children[0].children[0]
    assert leaf.qualification == "Graduation"
    assert leaf.class_ == "Any"
    assert leaf.min_pct == 0.0
