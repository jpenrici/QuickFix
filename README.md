# QuickFix

File manipulation through sandboxed plugins.

QuickFix opens a file, hands it to a plugin, and gives you back the result.
The original is never modified. The plugin does the work — QuickFix just orchestrates.

---

## Requirements

- Linux
- Python 3.11+
- bubblewrap — `sudo apt install bubblewrap`

---

## Getting Started

```bash
# First run — sets up the virtual environment automatically
./run.sh

# Or explicitly
./run.sh --gui        # desktop interface
./run.sh --cli        # interactive terminal
./run.sh --test       # run the test suite
```

`setup.sh` runs automatically on first launch. To skip it on subsequent runs:

```bash
./run.sh --gui --nosetup
./run.sh --cli --nosetup
```

---

## CLI

```bash
# Direct commands
python cli/cli.py list --file report.txt
python cli/cli.py info --plugin reverse_text_phrases
python cli/cli.py run  --file report.txt --plugin reverse_text_phrases
python cli/cli.py run  --file report.txt --plugin reverse_text_phrases --save
python cli/cli.py run  --file report.txt --plugin reverse_text_phrases --save-as out.txt

# Interactive mode (readline — command history, line editing)
python cli/cli.py --menu
```

---

## Writing a Plugin

Each plugin lives in its own directory under `plugins/`:

```
plugins/
└── my_plugin/
    ├── config.json   ← required, validated on load
    ├── help.md       ← shown in GUI and CLI
    ├── main.sh       ← entrypoint (any supported runtime)
    └── tests/
        ├── run_tests.sh
        ├── input/
        └── expected/
```

### config.json — minimal example

```json
{
  "plugin": {
    "name": "my_plugin",
    "version": "1.0.0",
    "description": "Does something useful to a text file.",
    "author": "Your Name",
    "contact": "you@example.com",
    "license": "MIT"
  },
  "execution": {
    "runtime": "bash",
    "entrypoint": "main.sh",
    "timeout_seconds": 30,
    "args_extra": false
  },
  "sandbox": {
    "required": true,
    "engine": "bubblewrap",
    "allow_network": false,
    "allow_new_processes": false,
    "writable_paths": ["OUTPUT_DIR"]
  },
  "input":  { "accepts": ["text/plain"], "max_size_mb": 10, "encoding": "utf-8" },
  "output": { "produces": "text/plain", "filename_suffix": "_processed", "overwrites_input": false },
  "requirements": { "system_binaries": ["bash"], "min_free_disk_mb": 10, "os": "linux" },
  "gui":    { "has_own_window": false, "dialog_tool": null, "extra_input_required": false }
}
```

All fields are required. Wildcards (`*`, `text/*`) in `accepts` are rejected.
`overwrites_input` must be `false`.

### Supported runtimes

`bash` · `python3` · `lua` · `ruby` · `perl` · `binary`

### Plugin contract

The plugin receives two positional arguments:

```
$1  input_file   read-only copy of the original (inside a temp directory)
$2  output_dir   writable directory for results
```

It communicates via JSONL on stdout:

```jsonl
{"event": "start",    "timestamp": "2026-01-01T00:00:00Z"}
{"event": "progress", "percent": 50, "message": "Processing..."}
{"event": "done",     "output_file": "result.txt", "checksum_sha256": "abc123..."}
```

On error:

```jsonl
{"event": "error", "code": "READ_FAIL", "message": "Cannot read input.", "fatal": true}
```

Exit codes: `0` success · `1` generic error · `2` bad input · `3` missing dependency.

The original file is protected with `chmod 444` before execution and its
SHA-256 is verified after. A violation writes a forensic log to
`~/.local/share/quickfix/forensics/` and blocks the output.

### Plugin tests

Tests live inside the plugin directory and run independently of QuickFix:

```bash
bash plugins/my_plugin/tests/run_tests.sh
```

---

## Project Structure

```
QuickFix/
├── run.sh            entry point
├── setup.sh          environment checker and venv setup
├── core/
│   ├── controller.py orchestrates the pipeline
│   ├── loader.py     validates config.json
│   ├── session.py    temp dir, file lock, input copy
│   ├── sandbox.py    bubblewrap / firejail execution
│   └── verifier.py   SHA-256 integrity checks
├── gui/              PySide6 desktop interface
├── cli/              terminal interface
├── plugins/          one directory per plugin
└── tests/            QuickFix core tests (pytest)
```

---

## Sandbox

Plugins with `sandbox.required: true` run inside bubblewrap with:

- empty root filesystem
- read-only bind mounts for system paths and plugin directory
- read-write access only to `output_dir`
- no network
- no new privileges
- killed if QuickFix exits

Plugins with `sandbox.required: false` show a confirmation prompt before
execution. The plugin developer is solely responsible for the outcome.

---

## License

See each plugin's `config.json` for its individual license.
