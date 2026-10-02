"""AST inventory of hard-coded geometry in a source file.

Usage: python3 inventory.py src/aigauge/widget.py
Lists every call to a geometry setter whose arguments include a numeric
literal (or a module constant), and every px literal inside a string.
"""
import ast, re, sys
from collections import Counter

SETTERS = {
    "setFixedWidth", "setFixedHeight", "setFixedSize", "setMinimumWidth",
    "setMinimumHeight", "setMinimumSize", "setMaximumWidth", "setMaximumHeight",
    "setMaximumSize", "setPixelSize", "setPointSize", "setContentsMargins",
    "setSpacing", "setIconSize", "QSize", "setGeometry", "addSpacing", "resize",
}
path = sys.argv[1]
src = open(path).read()
tree = ast.parse(src)
rows = []
for node in ast.walk(tree):
    if isinstance(node, ast.Call):
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
        if name in SETTERS:
            seg = ast.get_source_segment(src, node).replace("\n", " ")
            seg = re.sub(r"\s+", " ", seg)
            has_lit = any(isinstance(a, ast.Constant) and isinstance(a.value, (int, float)) and not isinstance(a.value, bool) for a in ast.walk(node))
            has_const = any(isinstance(a, ast.Name) and a.id.isupper() for a in ast.walk(node))
            if has_lit or has_const:
                rows.append((node.lineno, name, seg[:110]))
rows.sort()
c = Counter(r[1] for r in rows)
for r in rows:
    print(f"{path}:{r[0]}  {r[1]:20s} {r[2]}")
print()
print("by setter:", dict(c), "total", len(rows))
# px literals inside string constants (stylesheets)
px = Counter()
lines = []
for node in ast.walk(tree):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        for m in re.finditer(r"([a-z-]+)\s*:\s*([0-9 ]+(?:px)?[^;}]*)", node.value):
            if "px" in m.group(2):
                px[m.group(1)] += 1
                lines.append((node.lineno, m.group(0)))
print("stylesheet px properties:", dict(px), "total", sum(px.values()))
