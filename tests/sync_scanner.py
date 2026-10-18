#!/usr/bin/env python3
"""Dev helper: copy the name-only secret scanner (the SCANJS heredoc in detect_local.sh) into detect_local.ps1 as base64.
Run after editing the scanner; `import_plan.py --lint-scripts` fails while the two differ."""
import base64, os, re
d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "import-agents", "scripts")
sh = open(os.path.join(d, "detect_local.sh"), encoding="utf-8").read()
js = sh.split("<<'SCANJS'\n", 1)[1].split("\nSCANJS\n", 1)[0] + "\n"
p = os.path.join(d, "detect_local.ps1"); ps = open(p, encoding="utf-8").read()
new = re.sub(r'\$SCAN_B64 = "[^"]*"', '$SCAN_B64 = "' + base64.b64encode(js.encode("utf-8")).decode() + '"', ps, count=1)
if new != ps: open(p, "w", encoding="utf-8").write(new); print("updated detect_local.ps1")
else: print("already in sync")
