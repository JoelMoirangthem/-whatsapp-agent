"""Forensic QR validation for the pairing endpoint.

Decodes the served PNG like a phone camera and verifies geometry against
the QR spec (quiet zone >= 4 modules on every side).

Usage: uv run python scripts/check_qr.py [path.png]
Exit 0 only when the QR is valid AND spec-compliant.
"""

import sys
from pathlib import Path

import zxingcpp
from PIL import Image

path = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/opencode/live-qr.png")
img = Image.open(path).convert("L")
w, h = img.size
px = img.load()

# --- decode ---
results = zxingcpp.read_barcodes(img)
if not results:
    print("✗ DECODE FAILED — no QR content recognized")
    sys.exit(1)
text = results[0].text
print(f"✓ DECODED: {text[:60]!r}…")
assert text.startswith("https://wa.me/"), "unexpected QR payload"

# --- geometry: dark bounding box ---
dark_cols = [x for x in range(w) if any(px[x, y] < 128 for y in range(h))]
dark_rows = [y for y in range(h) if any(px[x, y] < 128 for x in range(w))]
left, right = min(dark_cols), max(dark_cols)
top, bottom = min(dark_rows), max(dark_rows)
code_w, code_h = right - left + 1, bottom - top + 1
quiet = [left, w - 1 - right, top, h - 1 - bottom]
print(f"image={w}x{h}  code={code_w}x{code_h}px  quiet L/R/T/B={quiet}")

# --- derive module size: must divide both code width and quiet margin ---
module = None
for m in range(min(quiet), 1, -1):
    if code_w % m == 0 and all(q % m == 0 for q in quiet):
        modules = code_w // m
        if 21 <= modules <= 177:  # valid QR versions 1..40
            module = m
            break
assert module, "could not derive a consistent module size"
modules = code_w // module
print(f"module={module}px  grid={modules}x{modules} modules")

ratio = min(quiet) / module
ok_quiet = ratio >= 4.0
square = code_w == code_h
print(f"{'✓' if ok_quiet else '✗'} quiet zone = {ratio:.2f} modules (spec: >=4)")
print(f"{'✓' if square else '✗'} code is square")
sys.exit(0 if (ok_quiet and square) else 1)
