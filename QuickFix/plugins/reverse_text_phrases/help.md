# reverse_text_phrases

Reverses the order of phrases in a plain text file.

## Input

- Type: `text/plain`
- Encoding: UTF-8
- Max size: 10 MB

## Output

- Type: `text/plain`
- Suffix: `_reversed`
- Example: `document.txt` → `document_reversed.txt`

## Requirements

- `lua5.4` must be installed on the system

---

## Examples

### Basic — single line

**Input:**
```
Hello world. How are you? I am fine.
```

**Output:**
```
I am fine. How are you? Hello world.
```

---

### Mixed delimiters and decimal numbers

**Input (`test.txt`):**
```
Are you ready? Let us go! The adventure begins. Pi is 3.14 approximately. It is irrational.
```

**Output (`test_reversed.txt`):**
```
It is irrational. Pi is 3.14 approximately. The adventure begins. Let us go! Are you ready?
```

Demonstrates that `.`, `?`, and `!` all work as phrase delimiters, and that
decimal numbers like `3.14` are not incorrectly split — a delimiter requires
punctuation **followed by a space**, so mid-number dots are ignored.

---

### Multiple lines

**Input:**
```
The sun rises in the east. Birds begin to sing. A new day starts.
Coffee brews in the kitchen. The smell fills the room. Morning is peaceful.
```

**Output:**
```
Morning is peaceful. The smell fills the room. Coffee brews in the kitchen. A new day starts. Birds begin to sing. The sun rises in the east.
```

All lines are treated as a single stream — line breaks are normalised to
spaces before splitting. The output is written as a single line.

---

## Splitting Rules

Phrases are split on `.`, `?`, or `!` **followed by at least one space**.
End-of-file punctuation (no trailing space) also terminates the last phrase.

| Input pattern | Split? | Reason |
|---|---|---|
| `Hello world. How` | ✓ | `.` followed by space |
| `Are you ready? Yes` | ✓ | `?` followed by space |
| `Stop! Now` | ✓ | `!` followed by space |
| `Pi is 3.14 today` | ✗ | digit before `.` — no split |
| `I am not sure...` | ✓ once | `...` treated as single terminator |
| `Hello world.` (EOF) | ✓ | `.` at end of file |

---

## Known Limitations

### Abbreviations are split incorrectly

Dot-terminated abbreviations followed by a space trigger an unintended split
because the splitter operates purely on punctuation patterns without a word
list.

**Example:**
```
Dr. Smith arrived early. The meeting started.
```
**Actual output (incorrect):**
```
The meeting started. Smith arrived early. Dr.
```
**Expected output (not yet supported):**
```
The meeting started. Dr. Smith arrived early.
```

Affected abbreviations include `Dr.`, `Mr.`, `Ms.`, `Sr.`, `Jr.`, `Prof.`,
`St.`, and any other word ending in a dot followed by a capitalised word.

This is a known limitation of the current implementation. A future version
may introduce an abbreviation allowlist to handle these cases correctly.

---

## Notes

- The original file is never modified — the plugin always writes to a copy.
- Empty files and whitespace-only files are passed through unchanged.
- Invalid UTF-8 input is rejected with exit code `2` and a JSONL error event.
