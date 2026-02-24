# Quickstart: DFM Analysis Integration

**Feature**: 001-dfm-analysis
**Date**: 2026-02-21

## Prerequisites

- Python 3.10+
- Poetry installed
- RabbitMQ running locally (or via Docker)
- Test STL/OBJ/STEP file available

## Development Setup

```bash
# Install dependencies
poetry install

# Activate virtual environment
poetry shell

# Run type checking
poetry run mypy src

# Run linting
poetry run ruff check .
poetry run ruff format --check .
```

## Running Tests

```bash
# Run DFM tests only
poetry run pytest tests/test_dfm.py -v

# Run all tests
poetry run pytest tests/ -v

# Run with coverage
poetry run pytest --cov=src
```

## Implementation Order

### Phase 1: Core DFM Module

1. Create `src/core/dfm.py`:
   - `DfmReport` Pydantic model
   - `DfmAnalyzer` class with `detect_thin_walls()`, `detect_overhangs()`, `analyze()`

2. Create `tests/test_dfm.py`:
   - Thin wall positive/negative cases
   - Overhang detection cases
   - Degenerate mesh handling

3. Run tests until all pass:
   ```bash
   poetry run pytest tests/test_dfm.py -v
   ```

### Phase 2: Integration

1. Modify `src/core/geometry.py`:
   - Add `dfm_report` field to `GeometryMetrics`
   - Call `DfmAnalyzer.analyze()` in `analyze_bytes()`

2. Modify `src/core/schemas.py`:
   - Import `DfmReport`
   - Field automatically flows through `GeometryMetrics`

3. Run full test suite:
   ```bash
   poetry run pytest tests/ -v
   ```

### Phase 3: Verification

1. Run type checking:
   ```bash
   poetry run mypy src
   ```

2. Run linting:
   ```bash
   poetry run ruff check .
   poetry run ruff format .
   ```

3. Start service locally:
   ```bash
   AMQP_URL=amqp://guest:guest@localhost:5672/ poetry run python -m src.main
   ```

4. Publish test event to RabbitMQ and verify `dfmReport` in response.

## Key Files

| File                    | Action      | Description                              |
| ----------------------- | ----------- | ---------------------------------------- |
| `src/core/dfm.py`       | CREATE      | DFM analyzer implementation              |
| `src/core/geometry.py`  | MODIFY      | Integrate DFM into geometry pipeline     |
| `src/core/schemas.py`   | MODIFY      | Add DfmReport to message schema          |
| `tests/test_dfm.py`     | CREATE      | Unit tests for DFM analysis              |

## Acceptance Criteria

- [ ] All 5 DFM test cases pass
- [ ] Existing geometry tests unchanged
- [ ] Type checking passes (`mypy src`)
- [ ] Linting passes (`ruff check .`)
- [ ] `dfmReport` appears in `FileAnalyzedEvent` JSON output
