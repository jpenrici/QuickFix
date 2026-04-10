-- =============================================================================
-- reverse_text_phrases/main.lua
-- =============================================================================
-- Reverses the order of phrases in a plain text file.
-- Emits JSONL progress events to stdout.
-- Stderr is reserved for unexpected crashes only.
--
-- Contract:
--   Args:
--     arg[1] - input_file  (read-only copy inside session tempdir)
--     arg[2] - output_dir  (writable directory for output files)
--
--   Stdout — JSONL events:
--     {"event": "start",    "timestamp": "..."}
--     {"event": "progress", "percent": N, "message": "..."}
--     {"event": "done",     "output_file": "...", "checksum_sha256": "..."}
--     {"event": "error",    "code": "...", "message": "...", "fatal": true}
--
--   Exit codes:
--     0 - success
--     1 - generic error
--     2 - input file unreadable or invalid encoding
-- =============================================================================

-- -----------------------------------------------------------------------------
-- JSONL emitters
-- -----------------------------------------------------------------------------

local function emit(event)
    io.write(event .. "\n")
    io.flush()
end

local function emit_start()
    emit('{"event": "start", "timestamp": "' ..
         os.date("!%Y-%m-%dT%H:%M:%SZ") .. '"}')
end

local function emit_progress(percent, message)
    -- Escape double quotes in message for valid JSON
    local safe_msg = message:gsub('"', '\\"')
    emit('{"event": "progress", "percent": ' .. percent ..
         ', "message": "' .. safe_msg .. '"}')
end

local function emit_done(output_file, checksum)
    emit('{"event": "done", "output_file": "' .. output_file ..
         '", "checksum_sha256": "' .. checksum .. '"}')
end

local function emit_error(code, message, fatal)
    local safe_msg = message:gsub('"', '\\"')
    local fatal_str = fatal and "true" or "false"
    emit('{"event": "error", "code": "' .. code ..
         '", "message": "' .. safe_msg ..
         '", "fatal": ' .. fatal_str .. '}')
end

-- -----------------------------------------------------------------------------
-- SHA-256 via system sha256sum
-- Delegates to the system binary — no external Lua library needed.
-- -----------------------------------------------------------------------------

local function sha256_file(path)
    local handle = io.popen("sha256sum " .. string.format("%q", path))
    if not handle then
        return nil, "Failed to run sha256sum"
    end
    local result = handle:read("*l")
    handle:close()
    if not result then
        return nil, "sha256sum returned no output"
    end
    -- sha256sum output: "<hash>  <filename>"
    local hash = result:match("^(%x+)")
    if not hash or #hash ~= 64 then
        return nil, "Unexpected sha256sum output: " .. tostring(result)
    end
    return hash, nil
end

-- -----------------------------------------------------------------------------
-- Phrase splitter
-- Splits text into sentences on ". ", "? ", "! " or end-of-string.
-- Preserves the delimiter attached to each phrase.
-- Handles multiple lines — the entire file content is treated as a stream.
-- -----------------------------------------------------------------------------

local function split_phrases(text)
    local phrases = {}
    local remaining = text

    -- Normalise line endings
    remaining = remaining:gsub("\r\n", "\n"):gsub("\r", "\n")

    -- Replace newlines with spaces for uniform splitting,
    -- but track whether the original ended with a newline
    local trailing_newline = remaining:sub(-1) == "\n"
    remaining = remaining:gsub("\n", " ")

    -- Trim leading/trailing whitespace
    remaining = remaining:match("^%s*(.-)%s*$")

    if #remaining == 0 then
        return phrases, trailing_newline
    end

    -- Split on sentence-ending punctuation followed by one or more spaces
    -- Pattern: capture everything up to and including [.?!] before spaces
    local pos = 1
    while pos <= #remaining do
        -- Find next sentence terminator: . ? ! followed by space(s) or end
        local term_start, term_end = remaining:find("[%.%?%!]+%s+", pos)

        if term_start then
            -- Phrase includes the punctuation, trim trailing spaces
            local phrase = remaining:sub(pos, term_end):match("^(.-)%s*$")
            if #phrase > 0 then
                table.insert(phrases, phrase)
            end
            pos = term_end + 1
        else
            -- Last phrase — no terminator after it
            local phrase = remaining:sub(pos):match("^(.-)%s*$")
            if #phrase > 0 then
                table.insert(phrases, phrase)
            end
            break
        end
    end

    return phrases, trailing_newline
end

-- -----------------------------------------------------------------------------
-- Main
-- -----------------------------------------------------------------------------

local input_file = arg[1]
local output_dir  = arg[2]

-- Validate arguments
if not input_file or not output_dir then
    io.stderr:write("Usage: lua5.4 main.lua <input_file> <output_dir>\n")
    os.exit(1)
end

emit_start()

-- Read input file
emit_progress(10, "Reading input file...")

local fh, open_err = io.open(input_file, "r")
if not fh then
    emit_error("READ_FAIL", "Cannot open input file: " .. tostring(open_err), true)
    os.exit(2)
end

local content = fh:read("*a")
fh:close()

if content == nil then
    emit_error("READ_FAIL", "Failed to read input file content.", true)
    os.exit(2)
end

-- Validate UTF-8 (basic check — reject lone high bytes without continuation)
-- Lua processes bytes, so we check for obviously invalid sequences
local function is_valid_utf8(s)
    local i = 1
    while i <= #s do
        local b = s:byte(i)
        local extra = 0
        if     b < 0x80 then extra = 0
        elseif b < 0xC0 then return false   -- lone continuation byte
        elseif b < 0xE0 then extra = 1
        elseif b < 0xF0 then extra = 2
        elseif b < 0xF8 then extra = 3
        else                  return false
        end
        for _ = 1, extra do
            i = i + 1
            if i > #s then return false end
            local cb = s:byte(i)
            if cb < 0x80 or cb >= 0xC0 then return false end
        end
        i = i + 1
    end
    return true
end

if not is_valid_utf8(content) then
    emit_error("ENCODING_FAIL", "Input file is not valid UTF-8.", true)
    os.exit(2)
end

emit_progress(30, "Splitting phrases...")

-- Split into phrases and reverse
local phrases, trailing_newline = split_phrases(content)

if #phrases == 0 then
    -- Empty or whitespace-only file — write as-is, nothing to reverse
    emit_progress(60, "File has no phrases — writing empty output.")
else
    emit_progress(50, "Reversing " .. #phrases .. " phrase(s)...")

    -- Reverse in place
    local lo, hi = 1, #phrases
    while lo < hi do
        phrases[lo], phrases[hi] = phrases[hi], phrases[lo]
        lo = lo + 1
        hi = hi - 1
    end
end

emit_progress(70, "Writing output file...")

-- Determine output file name from input file name
local input_basename = input_file:match("([^/]+)$") or "output"
local stem, ext = input_basename:match("^(.+)(%.[^%.]+)$")
if not stem then
    stem = input_basename
    ext  = ""
end
local output_filename = stem .. "_reversed" .. ext

local output_path = output_dir .. "/" .. output_filename

local out_fh, write_err = io.open(output_path, "w")
if not out_fh then
    emit_error("WRITE_FAIL", "Cannot write output file: " .. tostring(write_err), true)
    os.exit(1)
end

if #phrases == 0 then
    out_fh:write(content)
else
    out_fh:write(table.concat(phrases, " "))
    if trailing_newline then
        out_fh:write("\n")
    end
end

out_fh:close()

emit_progress(90, "Computing checksum...")

-- Compute SHA-256 of output file
local checksum, sha_err = sha256_file(output_path)
if not checksum then
    emit_error("CHECKSUM_FAIL", "SHA-256 computation failed: " .. tostring(sha_err), true)
    os.exit(1)
end

emit_done(output_filename, checksum)
os.exit(0)
