# ADR 006: UI Component Rendering Architecture

## Status
Accepted

## Context

Ticket CB-B562A4705747EAEE51EC1DAE0E240867 identified that UI rendering logic was being considered for `api_runner.py`, which violates the "Thin router, fat service" architectural invariant (Constitution §4). `api_runner.py` is strictly an LLM API transport/dispatch layer and must not contain presentation logic.

Additionally, `project.yaml` lacked a defined bounded context for UI concerns, leaving no architectural home for such functionality.

## Decision

**Create a dedicated `codebot/ui_components.py` module** to house all UI component rendering logic, and register it as a distinct backend component in `.codebot/project.yaml`.

Specifically:

1. **New Module**: `codebot/ui_components.py` contains pure, stdlib-only functions (`render_button`, `render_card`, `render_layout`) and a `DESIGN_TOKENS` schema dictionary.
2. **Architectural Registration**: `.codebot/project.yaml` is updated to include `ui_components` under `architecture.components`, defining its path and purpose.
3. **Boundary Preservation**: `api_runner.py` remains untouched regarding UI logic. It may import or call these functions if needed for agent tools, but the implementation resides strictly in `ui_components.py`.
4. **Design Token Schema**: A formal `DESIGN_TOKENS` dictionary is defined in `ui_components.py` using a nested structure for colors, spacing, typography, border radius, and shadows.

## Tradeoffs

| Dimension | Dedicated Module (Chosen) | In `api_tools.py` | In `api_runner.py` |
|---|---|---|---|
| **Separation of Concerns** | High - UI logic isolated | Medium - Mixed with tool dispatch | Low - Violates thin-router invariant |
| **Discoverability** | High - Explicit component | Medium - Buried in tools | Low - Unexpected location |
| **Testability** | High - Independent module | Medium - Coupled to tools | Low - Coupled to runner |
| **Policy Compliance** | Compliant - Defined in project.yaml | Compliant - Existing backend | Non-compliant - Violates §4 |

## Consequences

1. **`codebot/ui_components.py` created** with stub render functions and design tokens.
2. **`.codebot/project.yaml` updated** to register `ui_components` as a backend component.
3. **Tests added** in `tests/test_ui_components.py` to verify module existence, token schema, and function signatures.
4. **Future UI work** has a clear bounded context. Any new UI components should be added to this module.
5. **Security**: All render functions use `html.escape()` to sanitize inputs, preventing XSS in generated HTML strings.

## References

- Ticket CB-B562A4705747EAEE51EC1DAE0E240867
- Constitution §4: Thin router, fat service
- `codebot/ui_components.py`
- `.codebot/project.yaml`
