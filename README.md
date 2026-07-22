# app-test-automation

Automated validation tool for multi-page bank/government exam registration web applications.
Tests a live application against its requirement workbooks (SOW, age, eligibility) and produces
a defect report. See [CLAUDE.md](CLAUDE.md) for the full design and [docs/rules.schema.md](docs/rules.schema.md)
for the `rules.json` contract between extraction and engine.

## Setup

```
uv sync
uv run playwright install chromium
```

## Layout

- `tool/extractor/` — workbook parsing + LLM-assisted rule extraction → `rules.json`
- `tool/engine/` — Playwright-driven deterministic validation engine → `results.json`
- `tool/report/` — HTML/xlsx report generation
- `tool/models.py` — shared data models (mirrors `docs/rules.schema.md`)
- `samples/` — sample application docs (do not modify)
- `tests/` — test suite
