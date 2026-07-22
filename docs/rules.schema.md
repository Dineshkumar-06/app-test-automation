# rules.schema.md — the rules file contract

`rules.json` is the boundary between Layer 1 (extraction) and Layer 2 (engine). It is
application-specific but schema-stable. The engine reads only this file plus the live page; it
never re-reads the source workbooks.

## Top-level shape

```json
{
  "application": "string  (short id, e.g. nitr_jul26)",
  "page": "string  (e.g. reg_details.php)",
  "as_on_date": "YYYY-MM-DD  (age reference date from age sheet)",
  "fields": [ Field, ... ],
  "category_by_post": { "<post name>": ["SC","ST",...], ... },
  "age": { "<post name>": AgeRule, ... },
  "eligibility": { "<post name>": EligibilityTree, ... }
}
```

## Field object

```json
{
  "sl": "string   (SOW Sl No, e.g. '19.2', '27')",
  "label": "string   (SOW Label Name — the stable resolution key)",
  "section": "string?  (SOW section heading, for disambiguation)",
  "type": "numeric | alpha | alpha_space | alphanumeric | varchar | date | email | checkbox | radio | dropdown | multiselect",
  "max_length": "integer?",
  "mandatory": "true | false | 'conditional'",
  "input_method": "text | dropdown | radio | checkbox | label | multiselect",
  "regex": "string?   (validation pattern, e.g. '^[0-9]{12}$')",
  "charset": "string?  (e.g. 'alpha_space', 'digits', 'alnum_dot_amp_space')",
  "enum": ["value", ...],          // for dropdown/radio: allowed values
  "default": "string?",
  "uppercase": "true|false?",      // e.g. PAN, IFSC auto-uppercase
  "must_equal": "string?",         // label of another field (e.g. Confirm Account No)
  "enabled_when": "string?  (expression, default 'always')",
  "conditional": {
     "enabled_when": "expr?",
     "disabled_when": "expr?",
     "mandatory_when": "expr?",
     "options_when": { "expr": ["opt", ...] }   // dynamic option lists
  },
  "source": { "sheet": "string", "row": "integer" },
  "confidence": "high | low",      // only on LLM-interpreted rules
  "rule_source": "string?          (e.g. 'library:aadhaar' or 'extracted')"
}
```

### Expression grammar (for conditional / enabled_when)

Simple, evaluated by the engine against current field values — NOT by an LLM:

```
expr      := term (('and'|'or') term)*
term      := <label> <op> <value>  |  <label> 'in' [<value>, ...]  |  '(' expr ')'
op        := '=='  |  '!='
<label>   := a field label or a normalised handle (engine maps both)
<value>   := 'quoted string' | number | true | false
```

Examples:
- `marital_status == 'Married'`
- `disability_40_plus == 'NO'`  → (SOW: disable points 4–9)
- `marital_status in ['Unmarried','Divorced','Judicially Separated']`

Keep expressions declarative. The engine has one evaluator; do not encode logic anywhere else.

## AgeRule object

Derived from the age sheet. All bounds expressed as DOB cut-offs (inclusive) so the engine only
compares dates against the DOB dropdown, never recomputes ages.

```json
{
  "min_dob": "YYYY-MM-DD   (latest allowed DOB = youngest candidate)",
  "max_dob_by_category": {
    "UR": "YYYY-MM-DD   (earliest allowed DOB for this category+relaxation)",
    "EWS": "YYYY-MM-DD", "OBC": "...", "SC": "...", "ST": "..."
  },
  "relaxations": {
    "PWD":        { "UR": "YYYY-MM-DD", ... },
    "Departmental": { ... },
    "EXS":        { ... }          // note: EXS adds 'period of service' — see note
  }
}
```

Notes:
- Age sheets sometimes split by **state group** (see the SBI sample: "Vacancy for all category"
  vs "Vacancy only for SC, OBC, EWS, UR" etc.). When present, key AgeRule by post AND state
  group; the extractor must capture which states map to which group.
- EXS relaxation is "N + period of service" — model the base N here and let the engine add the
  candidate's entered period-of-service months at check time.

## EligibilityTree object

AND/OR tree from the eligibility sheet. Leaves are qualification requirements.

```json
{
  "op": "OR",
  "children": [
    { "op": "AND", "children": [
      { "qualification": "Graduation", "stream": ["Science with Geology"], "min_pct": 0, "class": "First Class" }
    ]},
    { "op": "AND", "children": [
      { "qualification": "Graduation", "stream": ["Any"], "min_pct": 0, "class": "Any" },
      { "qualification": "Post Graduation", "stream": ["Geology","Applied Geology"], "min_pct": 50, "class": "Any" }
    ]}
  ]
}
```

Leaf fields: `qualification`, `stream` (list; "Any" allowed), `min_pct` (number; ">0%" → 0 with
`strict:true` optional), `class` ("First Class" | "Any" | grade string), `work_experience?`.

## Provenance and confidence — always populate

- Every field-derived rule MUST carry `source` (sheet + row) so the report can trace a defect to
  a requirement line.
- Every LLM-interpreted rule MUST carry `confidence`. `low` → surfaced for human review before
  the engine runs. `high` and library rules → flow through.

## Worked mini-example (from NITR sample)

Standard field, high confidence, from library:
```json
{ "sl": "19.3", "label": "PAN Card No.", "section": "Basic Details",
  "type": "alphanumeric", "max_length": 10, "mandatory": false,
  "regex": "^[A-Z]{5}[0-9]{4}[A-Z]$", "uppercase": true,
  "source": {"sheet":"Basic Details","row":45}, "confidence":"high",
  "rule_source":"library:pan" }
```

Interpreted conditional, low confidence (needs review):
```json
{ "sl": "9", "label": "Do you intend to use the services of a scribe ?",
  "type": "radio", "enum": ["YES","NO"], "mandatory": "conditional",
  "conditional": { "mandatory_when":
    "disability_type in ['B','LV','MI','SLD','ASD'] or cerebral_palsy=='YES' or dominant_hand_affected=='YES'" },
  "source": {"sheet":"Basic Details","row":24}, "confidence":"low" }
```

The second is `low` because the SOW prose ("Enabled and Mandatory for B/LV OR Mental Illness ...
selected YES in Cerebral palsy in pt.5 or selected YES in Dominant hand in pt.7") required
interpretation. A reviewer confirms the expression once; thereafter it can be promoted to the
library if it recurs.
