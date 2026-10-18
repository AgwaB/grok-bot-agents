#!/usr/bin/env python3
"""Sanitize text crossing an agent boundary (bot <-> local agent).

- regular files only (no symlinks, devices, dirs)
- hard size cap (default 1 MB), truncated with a marker
- strips ANSI escape sequences, C0/C1 control chars (keeps \\n and \\t), bidi overrides/isolates, zero-width chars
Usage: sanitize_result.py IN_FILE [OUT_FILE] [--max-bytes N]   (OUT defaults to stdout)
       ... | sanitize_result.py -   (stdin)
"""
import os, re, stat, sys

args = [a for a in sys.argv[1:] if not a.startswith("--max-bytes")]
max_bytes = 1_000_000
for a in sys.argv[1:]:
    if a.startswith("--max-bytes="):
        max_bytes = int(a.split("=", 1)[1])
if not args:
    sys.exit(__doc__)
src = args[0]
dst = args[1] if len(args) > 1 else None

if src == "-":
    data = sys.stdin.buffer.read(max_bytes + 1)
else:
    st = os.lstat(src)
    if not stat.S_ISREG(st.st_mode):
        sys.exit(f"refusing non-regular file: {src}")
    with open(src, "rb") as f:
        data = f.read(max_bytes + 1)

truncated = len(data) > max_bytes
data = data[:max_bytes]
text = data.decode("utf-8", errors="replace")

ANSI = re.compile(r"\x1b(\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)?|[@-Z\\-_])")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
BIDI_ZW = re.compile("[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
text = BIDI_ZW.sub("", CONTROL.sub("", ANSI.sub("", text.replace("\r\n", "\n"))))
if truncated:
    text += "\n[truncated at %d bytes]\n" % max_bytes

if dst:
    with open(dst, "w", encoding="utf-8") as f:
        f.write(text)
else:
    sys.stdout.write(text)
