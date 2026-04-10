#!/usr/bin/env bash
# =============================================================================
# dummy_plugin/main.sh
# =============================================================================
# Stub plugin used exclusively for QuickFix pipeline testing.
# Does not process file content — only validates the JSONL contract.
#
# Emits the minimum valid JSONL sequence and exits 0.
#
# Args:
#   $1 - input_file  (read-only copy inside session tempdir)
#   $2 - output_dir  (writable directory for output files)
# =============================================================================

set -euo pipefail

INPUT_FILE="${1:?input_file argument required}"
OUTPUT_DIR="${2:?output_dir argument required}"
OUTPUT_FILE="${OUTPUT_DIR}/dummy_output.txt"

echo '{"event": "start", "timestamp": "'"$(date --iso-8601=seconds)"'"}'

echo '{"event": "progress", "percent": 50, "message": "Copying input to output (stub)"}'

cp "${INPUT_FILE}" "${OUTPUT_FILE}"

CHECKSUM="$(sha256sum "${OUTPUT_FILE}" | cut -d' ' -f1)"

echo '{"event": "done", "output_file": "dummy_output.txt", "checksum_sha256": "'"${CHECKSUM}"'"}'

exit 0
