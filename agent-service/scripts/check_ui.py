"""Verify the premium UI: JS↔HTML ID cross-references + API endpoints used."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"

js = (STATIC / "app.js").read_text()
html = (STATIC / "index.html").read_text()
css = (STATIC / "styles.css").read_text()

# 1. every getElementById-style reference in JS must exist in HTML
ids_js = set(re.findall(r"\$\(\"#([A-Za-z0-9-]+)\"\)", js)) | set(
    re.findall(r"\$\('#([A-Za-z0-9-]+)'\)", js)
)
# IDs created dynamically at runtime are allowed
dynamic = {"typing"}
missing = ids_js - set(re.findall(r'id="([A-Za-z0-9-]+)"', html)) - dynamic
assert not missing, f"JS references missing HTML ids: {missing}"

# 2. classes used by JS exist in CSS (spot-check the dynamic ones)
for cls in ["bubble", "msg-user", "msg-agent", "msg-blocked", "msg-error",
            "msg-system", "approval-card", "toolchip", "status-chip",
            "warnline", "toast", "audit-ev", "typing", "empty", "card"]:
    assert f".{cls}" in css, f"CSS missing .{cls}"

# 3. API paths used by JS must exist in FastAPI app
app_src = (STATIC.parent / "main.py").read_text()
for path in ["/agents/whatsapp/approve/", "/agents/whatsapp/actions", "/audit",
             "/health"]:
    assert path in app_src, f"endpoint {path} missing in main.py"
# and in the JS too
for path in ["/agents/whatsapp/approve/", "/agents/whatsapp/actions?status=pending",
             "/audit?", "/health"]:
    assert path.split("?")[0] in js, f"JS never calls {path}"

print("UI cross-reference checks PASSED")
print(f"  - {len(ids_js)} DOM ids wired")
