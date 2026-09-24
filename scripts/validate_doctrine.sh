#!/usr/bin/env bash
# validate_doctrine.sh — Verify foundational doctrine presence across all authoritative documents.
# Exit 0 if all checks pass, exit 1 with specific failure messages otherwise.

set -euo pipefail

FAILURES=0

check() {
    local label="$1"
    shift
    if "$@"; then
        echo "  ✓ $label"
    else
        echo "  ✗ $label"
        FAILURES=$((FAILURES + 1))
    fi
}

echo "=== Doctrine Validation ==="
echo ""

# 1. Constitution
echo "[1] .codebot/constitution.md"
check "contains thesis statement" grep -q 'Context is the source. Code is the artifact.' .codebot/constitution.md
check "references context compiler" grep -q 'context compiler' .codebot/constitution.md
echo ""

# 2. Entrypoint
echo "[2] ENTRYPOINT.md"
check "contains thesis statement" grep -q 'Context is the source. Code is the artifact.' ENTRYPOINT.md
check "contains compiler pipeline (Parse)" grep -q 'Parse' ENTRYPOINT.md
check "contains durability test" grep -q 'killing every agent' ENTRYPOINT.md
echo ""

# 3. ADR-008
echo "[3] docs/adr/008-context-is-source.md"
check "file exists" test -f docs/adr/008-context-is-source.md
check "Status present" grep -q 'Status' docs/adr/008-context-is-source.md
check "Accepted status" grep -q 'Accepted' docs/adr/008-context-is-source.md
check "contains thesis statement" grep -q 'Context is the source' docs/adr/008-context-is-source.md
check "references compiler" grep -q 'compiler' docs/adr/008-context-is-source.md
check "Consequences section" grep -q 'Consequences' docs/adr/008-context-is-source.md
echo ""

# 4. Coding Standards
echo "[4] docs/CODING_STANDARDS.md"
check "references context compiler" grep -q 'context compiler' docs/CODING_STANDARDS.md
check "Durability Test Principle present" grep -q 'Durability Test\|durability test\|killing every agent' docs/CODING_STANDARDS.md
check "references ADR-008" grep -q 'ADR-008\|008-context-is-source' docs/CODING_STANDARDS.md
echo ""

# 5. Active plan preambles
echo "[5] Active plans (.omo/plans/)"
PLANS=(
    ".omo/plans/user-agent-role.md"
    ".omo/plans/module-design-docs.md"
    ".omo/plans/ticket-context-carrier.md"
    ".omo/plans/context-control-plane.md"
)
for plan in "${PLANS[@]}"; do
    check "$plan" grep -q 'Context is the source\|ADR-008\|context compiler' "$plan"
done
echo ""

# Summary
if [ "$FAILURES" -eq 0 ]; then
    echo "=== ALL CHECKS PASSED ==="
    exit 0
else
    echo "=== FAILED: $FAILURES check(s) did not pass ==="
    exit 1
fi
