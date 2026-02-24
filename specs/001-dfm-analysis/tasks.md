# Tasks: DFM Analysis Integration

**Input**: Design documents from `/specs/001-dfm-analysis/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Tests**: Required per spec (5 test cases defined in spec.md)

**Organization**: Tasks grouped by user story for independent implementation.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2, US3)
- Include exact file paths in descriptions

---

## Phase 1: Setup

**Purpose**: Verify project environment and existing test baseline

- [X] T001 Verify Poetry environment and dependencies are installed
- [X] T002 Run existing test suite to establish baseline: `poetry run pytest tests/ -v`
- [X] T003 [P] Run mypy type checking to verify baseline: `poetry run mypy src --strict`

---

## Phase 2: Foundational (Shared DFM Module)

**Purpose**: Create shared DfmReport model and DfmAnalyzer class skeleton - required by all user stories

**⚠️ CRITICAL**: All user stories depend on this phase

- [X] T004 Create `src/core/dfm.py` with `DfmReport` Pydantic model (fields: `thin_wall_count`, `thin_wall_regions`, `overhang_face_count`, `overhang_area_cm2` with CamelCase aliases)
- [X] T005 Create `DfmAnalyzer` class skeleton in `src/core/dfm.py` with empty `detect_thin_walls()`, `detect_overhangs()`, `analyze()` methods
- [X] T006 Add `dfm_report: DfmReport | None = None` field to `GeometryMetrics` in `src/core/geometry.py`
- [X] T007 Import `DfmReport` from `src.core.dfm` in `src/core/geometry.py`

**Checkpoint**: Foundation ready - DfmReport model exists, GeometryMetrics extended

---

## Phase 3: User Story 1 - Thin Wall Detection (Priority: P1) 🎯 MVP

**Goal**: Detect thin wall regions via ray-casting and report count + coordinates

**Independent Test**: Upload thin plate (50x50x0.5mm) → verify `dfmReport.thinWallCount > 0`

### Tests for User Story 1

- [X] T008 [P] [US1] Create test file `tests/test_dfm.py` with thin wall positive test case (Box 50x50x0.5mm, min_thickness=0.8mm, assert count > 0, verify >90% of thin regions detected per SC-001)
- [X] T009 [P] [US1] Add thin wall negative test case in `tests/test_dfm.py` (Box 10x10x10mm, assert count == 0)
- [X] T010 Run thin wall tests to verify they FAIL: `poetry run pytest tests/test_dfm.py::test_thin_wall_* -v`

### Implementation for User Story 1

- [X] T011 [US1] Implement `detect_thin_walls(mesh, min_thickness_mm=0.8)` in `src/core/dfm.py` using trimesh sample + ray-casting per research.md pattern
- [X] T012 [US1] Run thin wall tests to verify they PASS: `poetry run pytest tests/test_dfm.py::test_thin_wall_* -v`
- [X] T013 [US1] Run mypy type check: `poetry run mypy src --strict`

**Checkpoint**: Thin wall detection works independently

---

## Phase 4: User Story 2 - Overhang Detection (Priority: P1)

**Goal**: Detect overhang faces via normal angle computation and report count + area

**Independent Test**: Upload model with downward faces → verify `dfmReport.overhangFaceCount > 0`

### Tests for User Story 2

- [X] T014 [P] [US2] Add overhang positive test case in `tests/test_dfm.py` (Box with downward faces, assert count > 0)
- [X] T015 [P] [US2] Add overhang negative test case in `tests/test_dfm.py` (all faces horizontal/upward, assert count == 0)
- [X] T016 Run overhang tests to verify they FAIL: `poetry run pytest tests/test_dfm.py::test_overhang_* -v`

### Implementation for User Story 2

- [X] T017 [US2] Implement `detect_overhangs(mesh, critical_angle_deg=45.0)` in `src/core/dfm.py` using numpy dot product per research.md pattern
- [X] T018 [US2] Run overhang tests to verify they PASS: `poetry run pytest tests/test_dfm.py::test_overhang_* -v`
- [X] T019 [US2] Run mypy type check: `poetry run mypy src --strict`

**Checkpoint**: Overhang detection works independently

---

## Phase 5: User Story 3 - Graceful Degradation (Priority: P2)

**Goal**: DFM analysis returns zeros on degenerate mesh, never fails geometry analysis

**Independent Test**: Upload degenerate/empty mesh → verify DFM report with zeros, geometry succeeds

### Tests for User Story 3

- [X] T020 [P] [US3] Add degenerate mesh test case in `tests/test_dfm.py` (empty mesh returns zero-filled DfmReport)
- [X] T021 [P] [US3] Add exception handling test case in `tests/test_dfm.py` (mock exception in DFM, verify zero report returned)
- [X] T022 Run degradation tests to verify they FAIL: `poetry run pytest tests/test_dfm.py::test_degenerate* -v`

### Implementation for User Story 3

- [X] T023 [US3] Implement `analyze(mesh)` method in `src/core/dfm.py` with try/except wrapping per research.md pattern
- [X] T024 [US3] Run degradation tests to verify they PASS: `poetry run pytest tests/test_dfm.py::test_degenerate* -v`
- [X] T025 [US3] Run mypy type check: `poetry run mypy src --strict`

**Checkpoint**: Graceful degradation works independently

---

## Phase 6: Integration

**Purpose**: Integrate DfmAnalyzer into geometry pipeline, verify end-to-end

- [X] T026 Integrate DfmAnalyzer in `GeometryProcessor.analyze_bytes()` in `src/core/geometry.py` (call DfmAnalyzer.analyze() after metrics construction, wrap in try/except, update metrics with dfm_report per FR-007)
- [X] T027 Add performance benchmark test in `tests/test_dfm.py` for SC-003 (verify DFM analysis completes within 5 seconds for 100k triangle mesh)
- [X] T028 Run all DFM tests: `poetry run pytest tests/test_dfm.py -v`
- [X] T029 Run all existing geometry tests to verify no regression: `poetry run pytest tests/test_geometry.py -v`
- [X] T030 Run full test suite: `poetry run pytest tests/ -v`
- [X] T031 Run mypy type check: `poetry run mypy src --strict`
- [X] T032 Run ruff lint and format: `poetry run ruff check . && poetry run ruff format .`

---

## Phase 7: Polish

**Purpose**: Final validation and cleanup

- [X] T033 [P] Verify JSON serialization produces correct CamelCase aliases in DfmReport
- [X] T034 [P] Verify FileAnalyzedEvent message structure matches contracts/file-analyzed-event.md
- [X] T035 Run quickstart.md validation steps
- [X] T036 Final type check and lint: `poetry run mypy src --strict && poetry run ruff check .`

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies - baseline verification
- **Foundational (Phase 2)**: No dependencies - creates shared module
- **User Stories (Phase 3-5)**: All depend on Foundational (Phase 2)
  - US1, US2, US3 can proceed in parallel after Phase 2
- **Integration (Phase 6)**: Depends on US1 + US2 + US3 completion
- **Polish (Phase 7)**: Depends on Integration completion

### User Story Dependencies

- **US1 (Thin Wall)**: No story dependencies - independently testable
- **US2 (Overhang)**: No story dependencies - independently testable
- **US3 (Degradation)**: No story dependencies - independently testable

### Parallel Opportunities

- T008, T009 can run in parallel (different test cases)
- T014, T015 can run in parallel (different test cases)
- T020, T021 can run in parallel (different test cases)
- T033, T034 can run in parallel (different verification tasks)
- All user story phases (3-5) can run in parallel after Phase 2

---

## Parallel Example: All User Stories After Foundation

```bash
# After Phase 2 completes, launch all test tasks in parallel:
Task: T008 - Thin wall positive test
Task: T009 - Thin wall negative test
Task: T014 - Overhang positive test
Task: T015 - Overhang negative test
Task: T020 - Degenerate mesh test
Task: T021 - Exception handling test
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (verify baseline)
2. Complete Phase 2: Foundational (DfmReport + skeleton)
3. Complete Phase 3: User Story 1 (thin walls)
4. **STOP and VALIDATE**: Test thin wall detection independently
5. Skip to Phase 6 integration if only thin walls needed

### Full Feature Delivery

1. Setup + Foundational → Foundation ready
2. US1 → Test thin walls → Works independently
3. US2 → Test overhangs → Works independently
4. US3 → Test degradation → Works independently
5. Integration → All features in pipeline
6. Polish → Production ready

---

## Notes

- Tests MUST fail before implementation (TDD approach per AGENTS.md)
- Type hints required (mypy strict mode)
- No bare `except:` - catch specific exceptions
- DFM failures must not affect geometry analysis success rate
- All existing tests must pass unchanged (SC-005)
