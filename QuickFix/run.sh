#!/usr/bin/env bash
# =============================================================================
# QuickFix - Entry Point
# =============================================================================
# Usage:
#   ./run.sh [--gui]  - launches GUI (default)
#   ./run.sh  --cli   - launches CLI
#   ./run.sh  --test  - launches Tests
#   ./run.sh  --help  - shows this help
#
# Environment overrides (export before calling):
#   QUICKFIX_VENV=/custom/path   Override default venv location
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
readonly APP_NAME="QuickFix"
readonly MIN_PYTHON_VERSION="3.11"
readonly SETUP_SCRIPT="${SCRIPT_DIR}/setup.sh"
readonly CORE_DIR="${SCRIPT_DIR}/core"
readonly GUI_DIR="${SCRIPT_DIR}/gui"
readonly CLI_DIR="${SCRIPT_DIR}/cli"
readonly TESTS_DIR="${SCRIPT_DIR}/tests"

# Venv location — must match the value used in setup.sh.
# Can be overridden via environment variable before calling.
# Override example: QUICKFIX_VENV="${HOME}/.venv" ./run.sh
readonly VENV_DIR="${QUICKFIX_VENV:-${SCRIPT_DIR}/.venv}"
readonly VENV_READY_MARKER="${VENV_DIR}/.quickfix_ready"

# -----------------------------------------------------------------------------
# Colors (same detection logic as setup.sh)
# -----------------------------------------------------------------------------
_setup_colors() {
    if [[ -t 1 ]] && command -v tput &>/dev/null && tput colors &>/dev/null 2>&1; then
        CLR_OK="\033[0;32m"
        CLR_WARN="\033[0;33m"
        CLR_FAIL="\033[0;31m"
        CLR_INFO="\033[0;34m"
        CLR_RESET="\033[0m"
    else
        CLR_OK=""
        CLR_WARN=""
        CLR_FAIL=""
        CLR_INFO=""
        CLR_RESET=""
    fi
}

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
_info() { echo -e "${CLR_INFO}[${APP_NAME}]${CLR_RESET} $*"; }
_warn() { echo -e "  ${CLR_WARN}[WARN]${CLR_RESET} $*" >&2; }
_error() {
    echo -e "  ${CLR_FAIL}[ERROR]${CLR_RESET} $*" >&2
    exit 1
}

# -----------------------------------------------------------------------------
# Guard: refuse to run as root
# -----------------------------------------------------------------------------
_check_not_root() {
    if [[ "${EUID}" -eq 0 ]]; then
        _error "Running as root is not allowed. Please run as a regular user."
    fi
}

# -----------------------------------------------------------------------------
# Guard: core modules exist
# -----------------------------------------------------------------------------
_check_core() {
    local required_modules=(
        "${CORE_DIR}/controller.py"
        "${CORE_DIR}/loader.py"
        "${CORE_DIR}/session.py"
        "${CORE_DIR}/sandbox.py"
        "${CORE_DIR}/verifier.py"
    )

    for module in "${required_modules[@]}"; do
        if [[ ! -f "${module}" ]]; then
            _error "Core module not found: ${module}"
        fi
    done

    _info "Core modules ${CLR_OK}OK${CLR_RESET}"
}

# -----------------------------------------------------------------------------
# Run setup (always — setup.sh is idempotent and fast on repeat runs)
# -----------------------------------------------------------------------------
_run_setup() {
    if [[ ! -x "${SETUP_SCRIPT}" ]]; then
        _error "setup.sh not found or not executable: ${SETUP_SCRIPT}"
    fi

    _info "Running setup checks..."
    bash "${SETUP_SCRIPT}" || _error "Setup failed. Aborting."
}

# -----------------------------------------------------------------------------
# Activate venv
# Depends on setup.sh having written the VENV_READY_MARKER on success.
# -----------------------------------------------------------------------------
_activate_venv() {
    if [[ ! -f "${VENV_READY_MARKER}" ]]; then
        _error "Venv not ready. Run ./setup.sh to prepare the environment."
    fi

    if [[ ! -f "${VENV_DIR}/bin/activate" ]]; then
        _error "Venv activation script not found at ${VENV_DIR}/bin/activate"
    fi

    # shellcheck source=/dev/null
    source "${VENV_DIR}/bin/activate"
    _info "Venv active: ${VENV_DIR}"
}

# -----------------------------------------------------------------------------
# Launch GUI
# -----------------------------------------------------------------------------
_launch_gui() {
    _info "Launching GUI..."
    python "${GUI_DIR}/window.py"
}

# -----------------------------------------------------------------------------
# Launch CLI
# -----------------------------------------------------------------------------
_launch_cli() {
    _info "Launching CLI..."
    local cmd
    cmd=("${@}")
    cmd=("${cmd[@]:1}") # delete the --cli command
    _info "command: ${cmd[@]}"
    python "${CLI_DIR}/cli.py" "${cmd[@]}"
}

# -----------------------------------------------------------------------------
# Launch Tests
# -----------------------------------------------------------------------------
_launch_tests() {
    _info "Launching Tests..."

    if [[ ! -d "${TESTS_DIR}" ]]; then
        _error "Tests directory not found: ${TESTS_DIR}"
    fi

    # Tests use only dummy_plugin (fixtures/) — never real plugins/
    # No sandbox engine required — dummy_plugin runs with unsafe_override
    local tests=(
        "test_loader.py"
        "test_verifier.py"
        "test_session.py"
        "test_sandbox.py"
        "test_controller.py"
    )

    local passed=0
    local failed=0

    for py_test in "${tests[@]}"; do
        local test_path="${TESTS_DIR}/${py_test}"
        if [[ ! -f "${test_path}" ]]; then
            _warn "Test file not found, skipping: ${py_test}"
            continue
        fi

        _info "Running: ${py_test}"
        if python -m pytest "${test_path}" -v; then
            ((passed++)) || true
        else
            ((failed++)) || true
        fi
    done

    echo
    if ((failed > 0)); then
        _info "Tests: ${CLR_OK}${passed} passed${CLR_RESET}, ${CLR_FAIL}${failed} failed${CLR_RESET}"
        exit 1
    else
        _info "Tests: ${CLR_OK}${passed} passed${CLR_RESET}, 0 failed"
    fi
}

# -----------------------------------------------------------------------------
# Help
# -----------------------------------------------------------------------------
_show_help() {
    cat <<EOF

${APP_NAME} — File manipulation through sandboxed plugins

Usage:
  ./run.sh [--gui]  - launches GUI (default)
  ./run.sh  --cli   - launches CLI
  ./run.sh  --test  - launches Tests
  ./run.sh  --help  - shows this help

Environment:
  QUICKFIX_VENV      Override venv location (default: ./.venv)

Requirements:
  - Linux only
  - Python ${MIN_PYTHON_VERSION}+
  - PySide6 (installed automatically in venv)
  - bubblewrap (recommended for sandbox isolation)

EOF
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
main() {
    _setup_colors

    local mode="gui"

    case "${1:-}" in
    --cli) mode="cli" ;;
    --gui) mode="gui" ;;
    --test) mode="test" ;;
    --help)
        _show_help
        exit 0
        ;;
    "") mode="gui" ;;
    *) _error "Unknown option: '${1}'. Use --help for usage." ;;
    esac

    _info "Checking permissions..."
    _check_not_root # Aborts immediately if root — non-negotiable

    if [[ "${mode}" == "test" ]]; then
        # Tests only need basic env — skip plugin checks (plugins may not be ready)
        _info "Checking environment..."
        bash "${SETUP_SCRIPT}" --basic || _error "Basic setup failed. Aborting."
        _activate_venv
        _launch_tests
        return
    fi

    bash "${SETUP_SCRIPT}" --full || _error "Setup failed. Aborting."
    _activate_venv
    _check_core

    case "${mode}" in
    gui) _launch_gui ;;
    cli) _launch_cli "${@}" ;;
    esac
}

main "${@}"
