#!/usr/bin/env bash
# =============================================================================
# reverse_text_phrases/tests/run_tests.sh
# =============================================================================
# Runs plugin tests independently of QuickFix.
# Calls main.lua directly and compares output against expected fixtures.
#
# Usage (from project root or plugin directory):
#   bash plugins/reverse_text_phrases/tests/run_tests.sh
#
# Exit codes:
#   0 - all tests passed
#   1 - one or more tests failed
# =============================================================================

set -euo pipefail

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TESTS_DIR="${PLUGIN_DIR}/tests"
INPUT_DIR="${TESTS_DIR}/input"
EXPECTED_DIR="${TESTS_DIR}/expected"
LUA="lua5.4"

PASSED=0
FAILED=0

# -----------------------------------------------------------------------------
# Colors
# -----------------------------------------------------------------------------

if [[ -t 1 ]] && command -v tput &>/dev/null && tput colors &>/dev/null 2>&1; then
    CLR_OK="\033[0;32m"   # green — success
    CLR_FAIL="\033[0;31m" # red   — failure
    CLR_INFO="\033[0;34m" # blue  — informational
    CLR_RESET="\033[0m"
else
    CLR_OK="" CLR_FAIL="" CLR_INFO="" CLR_RESET=""
fi

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

_pass() {
    echo -e "  ${CLR_OK}[PASS]${CLR_RESET} $*"
    ((PASSED++)) || true
}
_fail() {
    echo -e "  ${CLR_FAIL}[FAIL]${CLR_RESET} $*" >&2
    ((FAILED++)) || true
}
_info() { echo -e "${CLR_INFO}[reverse_text_phrases]${CLR_RESET} $*"; }

# -----------------------------------------------------------------------------
# Guard: lua5.4 must be installed
# -----------------------------------------------------------------------------

if ! command -v "${LUA}" &>/dev/null; then
    echo "ERROR: ${LUA} not found. Install with: sudo apt install lua5.4" >&2
    exit 1
fi

# -----------------------------------------------------------------------------
# Run a single test case
# Args: input_file expected_file
# -----------------------------------------------------------------------------

_run_test() {
    local input_file="$1"
    local expected_file="$2"
    local test_name
    test_name="$(basename "${input_file}")"

    # Create isolated output directory
    local tmpout
    tmpout="$(mktemp -d)"
    local stem
    stem="$(basename "${input_file}" .txt)"
    local output_file="${tmpout}/${stem}_reversed.txt"

    # Run plugin — capture JSONL events
    local jsonl_output
    if ! jsonl_output="$("${LUA}" "${PLUGIN_DIR}/main.lua" \
        "${input_file}" "${tmpout}" 2>/dev/null)"; then
        _fail "${test_name}: plugin exited with non-zero code"
        rm -rf "${tmpout}"
        return
    fi

    # Verify JSONL contract: must contain start and done events
    if ! echo "${jsonl_output}" | grep -q '"event": "start"'; then
        _fail "${test_name}: missing 'start' event in JSONL output"
        rm -rf "${tmpout}"
        return
    fi

    if ! echo "${jsonl_output}" | grep -q '"event": "done"'; then
        _fail "${test_name}: missing 'done' event in JSONL output"
        rm -rf "${tmpout}"
        return
    fi

    # Verify output file was created
    if [[ ! -f "${output_file}" ]]; then
        _fail "${test_name}: output file not created: ${output_file}"
        rm -rf "${tmpout}"
        return
    fi

    # Verify checksum declared in done event matches actual file
    local declared_checksum
    declared_checksum="$(echo "${jsonl_output}" |
        grep '"event": "done"' |
        grep -o '"checksum_sha256": "[^"]*"' |
        grep -o '[a-f0-9]\{64\}')"

    local actual_checksum
    actual_checksum="$(sha256sum "${output_file}" | cut -d' ' -f1)"

    if [[ "${declared_checksum}" != "${actual_checksum}" ]]; then
        _fail "${test_name}: checksum mismatch — plugin declared ${declared_checksum}, actual ${actual_checksum}"
        rm -rf "${tmpout}"
        return
    fi

    # Compare output against expected
    if [[ ! -f "${expected_file}" ]]; then
        _fail "${test_name}: expected file not found: ${expected_file}"
        rm -rf "${tmpout}"
        return
    fi

    if diff -q "${output_file}" "${expected_file}" >/dev/null 2>&1; then
        _pass "${test_name}"
    else
        _fail "${test_name}: output differs from expected"
        echo "    Expected: $(cat "${expected_file}")"
        echo "    Got:      $(cat "${output_file}")"
    fi

    rm -rf "${tmpout}"
}

# -----------------------------------------------------------------------------
# Additional behaviour tests (no expected file needed)
# -----------------------------------------------------------------------------

_test_invalid_utf8() {
    local test_name="invalid_utf8_rejected"
    local tmpinput
    tmpinput="$(mktemp --suffix=.txt)"
    local tmpout
    tmpout="$(mktemp -d)"

    # Write invalid UTF-8 bytes
    printf '\xFF\xFE invalid bytes' >"${tmpinput}"

    local jsonl_output exit_code
    set +e
    jsonl_output="$("${LUA}" "${PLUGIN_DIR}/main.lua" "${tmpinput}" "${tmpout}" 2>/dev/null)"
    exit_code=$?
    set -e

    rm -f "${tmpinput}"
    rm -rf "${tmpout}"

    if [[ ${exit_code} -ne 0 ]] && echo "${jsonl_output}" | grep -q '"event": "error"'; then
        _pass "${test_name}"
    else
        _fail "${test_name}: expected error event and non-zero exit for invalid UTF-8"
    fi
}

_test_missing_args() {
    local test_name="missing_args_rejected"
    local exit_code

    set +e
    "${LUA}" "${PLUGIN_DIR}/main.lua" >/dev/null 2>&1
    exit_code=$?
    set -e

    if [[ ${exit_code} -ne 0 ]]; then
        _pass "${test_name}"
    else
        _fail "${test_name}: expected non-zero exit when args are missing"
    fi
}

_test_jsonl_no_extra_output() {
    local test_name="stdout_is_valid_jsonl"
    local input_file="${INPUT_DIR}/simple.txt"
    local tmpout
    tmpout="$(mktemp -d)"

    local jsonl_output
    jsonl_output="$("${LUA}" "${PLUGIN_DIR}/main.lua" "${input_file}" "${tmpout}" 2>/dev/null)"
    rm -rf "${tmpout}"

    local invalid_lines=0
    while IFS= read -r line; do
        [[ -z "${line}" ]] && continue
        if ! echo "${line}" | python3 -c "import json,sys; json.load(sys.stdin)" >/dev/null 2>&1; then
            ((invalid_lines++)) || true
        fi
    done <<<"${jsonl_output}"

    if [[ ${invalid_lines} -eq 0 ]]; then
        _pass "${test_name}"
    else
        _fail "${test_name}: ${invalid_lines} non-JSON line(s) in stdout"
    fi
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

_info "Running tests..."
echo

# Fixture-based tests
for input_file in "${INPUT_DIR}"/*.txt; do
    stem="$(basename "${input_file}" .txt)"
    expected_file="${EXPECTED_DIR}/${stem}_reversed.txt"
    _run_test "${input_file}" "${expected_file}"
done

# Behaviour tests
_test_invalid_utf8
_test_missing_args
_test_jsonl_no_extra_output

echo
_info "Results: ${CLR_OK}${PASSED} passed${CLR_RESET}, $([[ ${FAILED} -gt 0 ]] && echo -e "${CLR_FAIL}${FAILED} failed${CLR_RESET}" || echo "0 failed")"

[[ ${FAILED} -eq 0 ]]
