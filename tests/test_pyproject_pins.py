"""Tests for pyproject.toml dependency pinning policy.

Acceptance Criteria for CB-5613691-A778:
- pyproject.toml uses == pins for all dependencies
- no >= or ~= operators in requires lists
"""
import pytest
from pathlib import Path
import tomllib

PROJECT_ROOT = Path(__file__).parent.parent
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"


def load_pyproject():
    """Load and parse pyproject.toml."""
    with open(PYPROJECT_PATH, "rb") as f:
        return tomllib.load(f)


def extract_requires(data):
    """Extract all requires lists from build-system and optional-dependencies."""
    requires = []
    # Build system requires
    build_system = data.get("build-system", {})
    if "requires" in build_system:
        requires.extend(build_system["requires"])
    
    # Optional dependencies
    optional_deps = data.get("project", {}).get("optional-dependencies", {})
    for deps_list in optional_deps.values():
        requires.extend(deps_list)
    
    # Main dependencies
    main_deps = data.get("project", {}).get("dependencies", [])
    requires.extend(main_deps)
    
    return requires


class TestDependencyPinning:
    def test_pyproject_exists(self):
        assert PYPROJECT_PATH.exists(), "pyproject.toml not found"

    def test_all_deps_use_exact_pins(self):
        """Verify all dependencies use == operator for exact pinning."""
        data = load_pyproject()
        requires = extract_requires(data)
        
        assert len(requires) > 0, "No dependencies found to verify"
        
        for req in requires:
            # Extract package name and version spec
            # Format: "package==version" or "package>=version"
            assert "==" in req, f"Dependency '{req}' does not use exact pin (==). Use 'package==version' format."
            
    def test_no_range_operators(self):
        """Verify no >=, <=, ~=, or < operators are used."""
        data = load_pyproject()
        requires = extract_requires(data)
        
        forbidden_operators = [">=", "<=", "~=", "<", ">"]
        
        for req in requires:
            for op in forbidden_operators:
                # Check if operator is present but not part of ==
                if op in req and "!=" not in req:  # != is allowed for exclusions if needed, but rare
                    # Specifically check for standalone operators
                    if op == "<" and "<=" not in req:
                         assert False, f"Dependency '{req}' contains forbidden operator '<'. Use exact pins only."
                    elif op == ">" and ">=" not in req and "!=" not in req:
                         assert False, f"Dependency '{req}' contains forbidden operator '>'. Use exact pins only."
                    elif op in [">=", "<=", "~="]:
                        assert False, f"Dependency '{req}' contains forbidden operator '{op}'. Use exact pins only."

    def test_setuptools_pinned(self):
        """Verify setuptools is pinned to exact version."""
        data = load_pyproject()
        build_requires = data.get("build-system", {}).get("requires", [])
        
        setuptools_reqs = [r for r in build_requires if "setuptools" in r]
        assert len(setuptools_reqs) > 0, "setuptools not found in build-system requires"
        
        for req in setuptools_reqs:
            assert "==" in req, f"setuptools requirement '{req}' is not exactly pinned"
            # Verify it's in the 68.x series as per previous constraints
            assert "==68." in req, f"setuptools should be pinned to 68.x series, got: {req}"

    def test_pytest_pinned(self):
        """Verify pytest is pinned to exact version."""
        data = load_pyproject()
        dev_deps = data.get("project", {}).get("optional-dependencies", {}).get("dev", [])
        
        pytest_reqs = [r for r in dev_deps if "pytest" in r]
        assert len(pytest_reqs) > 0, "pytest not found in dev optional-dependencies"
        
        for req in pytest_reqs:
            assert "==" in req, f"pytest requirement '{req}' is not exactly pinned"
            # Verify it's in the 7.x series as per previous constraints
            assert "==7." in req, f"pytest should be pinned to 7.x series, got: {req}"
