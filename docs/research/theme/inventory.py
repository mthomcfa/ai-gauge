"""Colour-literal inventory for src/aigauge. Usage: python3 inventory.py <src/aigauge dir> [-v]"""
import io, re, sys, tokenize, ast
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(sys.argv[1]); VERBOSE = "-v" in sys.argv
HEX = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b")
PROP = re.compile(r"([a-z-]+)\s*:\s*[^;:{}]*$")

def docstring_lines(tree):
    lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
                lines.update(range(body[0].lineno, body[0].end_lineno + 1))
    return lines

rows = []
for path in sorted(ROOT.rglob("*.py")):
    if "__pycache__" in path.parts: continue
    src = path.read_text(encoding="utf-8")
    doc = docstring_lines(ast.parse(src))
    lines = src.splitlines()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type not in (tokenize.STRING, tokenize.COMMENT) and tok.type != getattr(tokenize, "FSTRING_MIDDLE", -1):
            continue
        text = tok.string
        for m in HEX.finditer(text):
            # absolute line of the match
            line = tok.start[0] + text[: m.start()].count("\n")
            in_qss_comment = text[: m.start()].rfind("/*") > text[: m.start()].rfind("*/")
            if tok.type == tokenize.COMMENT or in_qss_comment or line in doc or (tok.type == tokenize.STRING and tok.start[0] in doc):
                kind = "comment"
            else:
                kind = "code"
            before = text[: m.start()]
            seg = re.split(r"[;{}]", before)[-1]
            pm = PROP.search(seg)
            prop = pm.group(1) if pm else None
            src_line = lines[line - 1]
            if prop is None:
                if "QColor(" in src_line: prop = "QColor"
                elif re.search(r"_color\"?\s*[:=]|COLOR\s*=|color\s*=|NS_COLOR|or \"#", src_line) or re.search(r"^\s*\"(low|med|high)\"", src_line): prop = "constant"
                elif re.search(r"return \"#", src_line): prop = "constant"
                elif re.search(r"(normal|active)=", src_line): prop = "icon-pen"
                elif re.search(r"^\s*[A-Z_]+\s*=\s*\"#", src_line): prop = "constant"
                else: prop = "?"
            rows.append(dict(file=str(path.relative_to(ROOT)), line=line, hex=m.group(0).lower(), prop=prop, kind=kind, src=src_line.strip()))

# Role classification -------------------------------------------------------
BANDS = {"#22c55e": "green", "#f59e0b": "yellow", "#f97316": "orange", "#ef4444": "red"}
def role(r):
    h, p, f, s = r["hex"], r["prop"], r["file"], r["src"]
    if r["kind"] == "comment": return "comment/docstring (no code change)"
    if f in ("config.py",) or (f == "menubar.py" and h in BANDS): return "gauge band default"
    if h == "#6b7280" and (f in ("gauge.py", "macos_status_item.py", "app.py") or "NEUTRAL" in s): return "gauge neutral (no data)"
    if h == "#38bdf8": return "status: setup (menubar)"
    if h == "#92400e": return "status: warning fill"
    if h == "#fef3c7": return "status: warning surface"
    if h in ("#ef4444", "#dc2626") : return "status: error text"
    if h == "#f59e0b": return "status: warning text"
    if h in ("#60a5fa", "#93c5fd"): return "accent: link / chart"
    if h in ("#2563eb", "#1d4ed8", "#3b82f6"): return "accent: control"
    if p in ("background", "selection-background-color", "QColor-brush") or (p == "QColor" and ("setBrush" in s or "_BASE" in s or "FILL" in s)):
        if h == "#111827": return "surface: panel/field (gray-900)"
        if h == "#1f2937": return "surface: dialog/raised (gray-800)"
        if h in ("#374151", "#4b5563", "#6b7280"): return "surface: control/track/hover"
    if p == "constant" and f == "ui_style.py":
        return {"#111827": "surface: panel/field (gray-900)", "#1f2937": "surface: dialog/raised (gray-800)", "#374151": "surface: control/track/hover"}.get(h, "surface: scroll handle states")
    if p == "constant" and f == "widget.py" and h == "#374151": return "surface: control/track/hover"
    if p in ("border", "border-left", "border-color") or (p == "QColor" and ("BORDER" in s or "AXIS" in s or "QPen(QColor(\"#1f2937\")" in s)): return "border / divider"
    if p in ("color", "QColor", "icon-pen", "constant") or "_TEXT" in s or "QPen" in s:
        if h in ("#f9fafb", "#f3f4f6", "#e5e7eb"): return "text: primary"
        if h in ("#d1d5db", "#cbd5e1", "#9ca3af"): return "text: secondary"
        if h == "#6b7280": return "text: muted"
        if h in ("#111827", "#374151"): return "text: on-colour (dark on light fill)"
    return f"UNCLASSIFIED({p})"

for r in rows: r["role"] = role(r)
code = [r for r in rows if r["kind"] == "code"]
print(f"total hex literals: {len(rows)}  in code: {len(code)}  in comments/docstrings: {len(rows)-len(code)}")
print(f"distinct values in code: {len(set(r['hex'] for r in code))}")
print("\nby file (code only):", dict(Counter(r["file"] for r in code)))
print("\nby role:")
for k, v in sorted(Counter(r["role"] for r in rows).items(), key=lambda kv: -kv[1]):
    vals = Counter(r["hex"] for r in rows if r["role"] == k)
    print(f"  {v:4d}  {k:42s} {dict(vals)}")
print("\nby value (code):")
for k, v in Counter(r["hex"] for r in code).most_common():
    print(f"  {k} {v:3d}  roles={dict(Counter(r['role'] for r in code if r['hex']==k))}")
if VERBOSE:
    for r in rows: print(f"{r['file']}:{r['line']}  {r['hex']}  {r['prop']:28s} {r['role']}")
