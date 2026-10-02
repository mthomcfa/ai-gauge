"""WCAG 2.x contrast ratios for theme-toggle.md §1.3.

Usage: python3 docs/research/theme/contrast.py
"""
from PyQt6.QtGui import QColor
def lum(h):
    c = QColor(h); out = []
    for v in (c.redF(), c.greenF(), c.blueF()):
        out.append(v/12.92 if v <= 0.04045 else ((v+0.055)/1.055)**2.4)
    r,g,b = out; return 0.2126*r+0.7152*g+0.0722*b
def cr(a,b):
    la, lb = sorted((lum(a), lum(b)), reverse=True); return (la+0.05)/(lb+0.05)
bands = {"green":"#22c55e","yellow":"#f59e0b","orange":"#f97316","red":"#ef4444"}
surf = {"dark panel #111827":"#111827","dark track #374151":"#374151","light panel #ffffff":"#ffffff","light panel #f9fafb":"#f9fafb","light track #e5e7eb":"#e5e7eb", "light track #d1d5db":"#d1d5db"}
print("== Band defaults (WCAG 1.4.11 non-text needs 3:1; 1.4.3 text needs 4.5:1)")
print("band".ljust(8), *[k.ljust(20) for k in surf])
for n,h in bands.items():
    print(n.ljust(8), *[f"{cr(h,v):5.2f}".ljust(20) for v in surf.values()])
print("\n== Chip: #f9fafb text on darker(135) fill (dark theme today)")
for n,h in bands.items():
    d = QColor(h).darker(135).name()
    print(f"{n:7s} fill {d}  text {cr('#f9fafb', d):.2f}  fill-vs-#1f2937 {cr(d,'#1f2937'):.2f}  fill-vs-#ffffff {cr(d,'#ffffff'):.2f}")
print(f"auth    fill #92400e  text {cr('#f9fafb','#92400e'):.2f}")
print(f"neutral fill #374151  text {cr('#f9fafb','#374151'):.2f}")
print("\n== Text tokens today on dark surfaces")
for t in ["#f9fafb","#f3f4f6","#e5e7eb","#d1d5db","#cbd5e1","#9ca3af","#6b7280","#60a5fa","#ef4444","#f59e0b"]:
    print(f"{t}: on #111827 {cr(t,'#111827'):5.2f}  on #1f2937 {cr(t,'#1f2937'):5.2f}   | same literal on #ffffff {cr(t,'#ffffff'):5.2f}  on #f3f4f6 {cr(t,'#f3f4f6'):5.2f}")
print("\n== Candidate light tokens")
for t,role in [("#111827","text primary"),("#1f2937","text primary alt"),("#374151","text secondary"),("#4b5563","text secondary alt"),("#6b7280","text muted"),("#2563eb","link"),("#1d4ed8","link alt"),("#dc2626","error text"),("#b91c1c","error text alt"),("#b45309","warning text"),("#92400e","warning text alt"),("#d97706","warning amber-600")]:
    print(f"{t} {role:20s} on #ffffff {cr(t,'#ffffff'):5.2f}  on #f9fafb {cr(t,'#f9fafb'):5.2f}  on #f3f4f6 {cr(t,'#f3f4f6'):5.2f}")
print("\n== Tray dot vs taskbar (Windows 11 taskbar approx: light #f3f3f3, dark #202020 - unverified values)")
for n,h in list(bands.items())+[("neutral","#6b7280"),("setup","#38bdf8")]:
    print(f"{n:8s} {h}: on light {cr(h,'#f3f3f3'):.2f}  on dark {cr(h,'#202020'):.2f}")
print("\n== Pace tick (rgba) composited: #f3f4f6@180/255 over band chunk; shadow #111827@120")
def over(fg, a, bg):
    f, b = QColor(fg), QColor(bg); al = a/255
    return QColor(round(f.red()*al+b.red()*(1-al)), round(f.green()*al+b.green()*(1-al)), round(f.blue()*al+b.blue()*(1-al))).name()
for n,h in bands.items():
    t = over("#f3f4f6",180,h); print(f"{n:7s} tick {t} vs chunk {cr(t,h):.2f}")
t = over("#f3f4f6",180,"#e5e7eb"); print(f"light track: tick {t} vs track #e5e7eb {cr(t,'#e5e7eb'):.2f}")
t = over("#f3f4f6",180,"#374151"); print(f"dark track: tick {t} vs track #374151 {cr(t,'#374151'):.2f}")
print("\n== Candidate LIGHT band colours vs light track #e5e7eb / panel #ffffff / tile #f9fafb")
for n,h in [("green-600","#16a34a"),("green-700","#15803d"),("amber-600","#d97706"),("amber-700","#b45309"),("yellow-600","#ca8a04"),("yellow-700","#a16207"),("orange-600","#ea580c"),("orange-700","#c2410c"),("red-600","#dc2626"),("red-700","#b91c1c")]:
    print(f"{n:10s} {h}: track {cr(h,'#e5e7eb'):.2f}  panel {cr(h,'#ffffff'):.2f}  tile {cr(h,'#f9fafb'):.2f}  as-text-on-white {cr(h,'#ffffff'):.2f}  chip-text(#fff) {cr('#ffffff',h):.2f}")
print("\n== dark-theme pre-existing: red #ef4444 vs dark track #374151:", round(cr('#ef4444','#374151'),2))
# pace tick light-variant: #111827@200 over light track and chunk
for n,h in [("track","#e5e7eb"),("green-600","#16a34a"),("amber-600","#d97706"),("red-600","#dc2626")]:
    t = over("#111827",200,h); print(f"light tick #111827@200 over {n}: {t} {cr(t,h):.2f}")
