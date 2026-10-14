#!/usr/bin/env python3
"""Emit docs/diagrams/pipeline-flow.svg from the table of stages below.

The SVG is hand-drawn rather than mermaid because mermaid centres every line
of a node at the same weight, which turned the stages into unreadable blocks
of text: you could not tell a command from a description, nor see who runs
the stage. Here each card is a fixed grid — actor badge, title, commands in
mono, output line, note — and the colour says whether the machine, Claude or
you does the work.

**Edit STAGES, not the SVG.** A hand edit to docs/diagrams/pipeline-flow.svg
is lost the next time this runs. Re-run after editing:

    python3 scripts/gen-pipeline-diagram.py

Nothing is installed and nothing is fetched; it only writes the one file.
"""
import pathlib
from html import escape

MACHINE, AI, HUMAN = "#2F6F9F", "#7A4FB6", "#C0771B"

STAGES = [
    dict(color=MACHINE, icon="term", badge="CLI · PLAYWRIGHT",
         title="Collect and merge",
         cmds=["rent-assistant scrape --bands 4 --no-photos", "rent-assistant dedupe"],
         out="listings, gallery links, photo hashes",
         note="The only stage that talks to Facebook.",
         flow="raw ad text"),
    dict(color=AI, icon="spark", badge="SKILL  /analyze  ·  OPUS",
         title="Read the text",
         cmds=["rent-assistant ai-batch    →  Claude reads the ads", "rent-assistant ai-apply    →  rules.py decides"],
         out="filled columns, one-line verdict, auto-rejections",
         note="Never touches Facebook, so it can run while a scrape does.",
         flow="a scored table"),
    dict(color=HUMAN, icon="person", badge="YOU",
         title="Cut what is obviously wrong",
         cmds=["rent-assistant rate <id> 1–5", "rent-assistant reject <id> --reason \"…\""],
         out="decisions.json — your word beats the rules",
         note="Every rejection here is a gallery not downloaded and tokens not spent.",
         flow="the survivors, and only them"),
    dict(color=AI, icon="spark", badge="SKILL  /photo-analyze  ·  SONNET SUBAGENTS",
         title="Look at the photos",
         cmds=["rent-assistant photos-fetch  →  photo-batch", "one subagent per listing  →  rent-assistant photo-apply"],
         out="disagreements with the text, score recomputed",
         note="Runs only when you ask for it — never on its own.",
         flow="where the pictures contradict the ad"),
    dict(color=HUMAN, icon="person", badge="YOU",
         title="Shortlist and write",
         cmds=["rent-assistant shortlist <id> --reason \"…\"", ""],
         out="the Shortlist tab in rentals.html",
         note="The only output that leaves the machine: a message to an agent.",
         flow=None),
]

W = 820
CARD_X, CARD_W, CARD_H, GAP = 78, 706, 156, 66
TOP = 78
SPINE_X = 40
BODY_X = CARD_X + 26

def icon(kind, x, y, color):
    """A 16x16 mark drawn at (x, y) = its top-left corner."""
    c = f'stroke="{color}" stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round"'
    if kind == "term":
        return (f'<polyline points="{x+2},{y+3} {x+7},{y+8} {x+2},{y+13}" {c}/>'
                f'<line x1="{x+9}" y1="{y+13}" x2="{x+14}" y2="{y+13}" {c}/>')
    if kind == "spark":
        thin = c.replace('stroke-width="1.6"', 'stroke-width="1.15"')
        cx, cy = x + 8, y + 8
        return (f'<line x1="{cx}" y1="{cy-7}" x2="{cx}" y2="{cy+7}" {c}/>'
                f'<line x1="{cx-7}" y1="{cy}" x2="{cx+7}" y2="{cy}" {c}/>'
                f'<line x1="{cx-4.6}" y1="{cy-4.6}" x2="{cx+4.6}" y2="{cy+4.6}" {thin}/>'
                f'<line x1="{cx-4.6}" y1="{cy+4.6}" x2="{cx+4.6}" y2="{cy-4.6}" {thin}/>')
    cx = x + 8
    return (f'<circle cx="{cx}" cy="{y+5}" r="3.1" {c}/>'
            f'<path d="M {cx-6} {y+14.5} a 6 5.5 0 0 1 12 0" {c}/>')

parts = []
height = TOP + len(STAGES) * CARD_H + (len(STAGES) - 1) * GAP + 62

parts.append(
    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {height}" width="{W}" '
    f'height="{height}" role="img" aria-label="The rent-assistant pipeline in five stages: '
    f'collect, read the text, cut what is wrong, look at the photos, shortlist" '
    f'font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica, Arial, sans-serif">'
)
parts.append('<title>The rent-assistant pipeline, five stages top to bottom</title>')
parts.append(f'<rect width="{W}" height="{height}" fill="#ffffff"/>')

# header: title on the left, colour legend on the right
parts.append(f'<text x="{CARD_X}" y="34" font-size="16" font-weight="600" fill="#1a1d21">'
             'From a Facebook card to a phone call</text>')
parts.append(f'<text x="{CARD_X}" y="54" font-size="12" fill="#6b7280">'
             'Each stage runs on what the one above it left. Nothing here loops back.</text>')
legend = [(MACHINE, "machine"), (AI, "Claude"), (HUMAN, "you")]
lx = 784
for color, label in reversed(legend):
    lx -= len(label) * 6.6 + 26
    parts.append(f'<rect x="{lx:.0f}" y="26" width="10" height="10" rx="2.5" fill="{color}"/>')
    parts.append(f'<text x="{lx+16:.0f}" y="35" font-size="11.5" fill="#4b5563">{label}</text>')

# the spine: one straight line, numbered discs, no back edges
first_c = TOP + 30
last_c = TOP + (len(STAGES) - 1) * (CARD_H + GAP) + 30
parts.append(f'<line x1="{SPINE_X}" y1="{first_c}" x2="{SPINE_X}" y2="{last_c}" '
             'stroke="#d7dbe0" stroke-width="2"/>')

for i, s in enumerate(STAGES):
    top = TOP + i * (CARD_H + GAP)
    color = s["color"]
    parts.append(f'<!-- stage {i+1}: {s["title"]} -->')
    parts.append(f'<rect x="{CARD_X}" y="{top}" width="24" height="{CARD_H}" rx="10" fill="{color}"/>')
    parts.append(f'<rect x="{CARD_X+6}" y="{top}" width="{CARD_W-6}" height="{CARD_H}" rx="10" '
                 'fill="#ffffff" stroke="#e3e6ea"/>')
    parts.append(f'<circle cx="{SPINE_X}" cy="{top+30}" r="17" fill="{color}"/>')
    parts.append(f'<text x="{SPINE_X}" y="{top+36}" font-size="15" font-weight="700" fill="#ffffff" '
                 f'text-anchor="middle">{i+1}</text>')
    parts.append(icon(s["icon"], BODY_X, top + 15, color))
    parts.append(f'<text x="{BODY_X+26}" y="{top+28}" font-size="10.5" font-weight="700" '
                 f'letter-spacing="0.09em" fill="{color}">{escape(s["badge"]).upper()}</text>')
    parts.append(f'<text x="{BODY_X}" y="{top+58}" font-size="16.5" font-weight="600" fill="#1a1d21">'
                 f'{escape(s["title"])}</text>')
    mono = ('font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" '
            'font-size="12.5" fill="#374151"')
    for j, cmd in enumerate(s["cmds"]):
        if cmd:
            parts.append(f'<text x="{BODY_X}" y="{top+84+j*20}" {mono}>{escape(cmd)}</text>')
    parts.append(f'<text x="{BODY_X}" y="{top+128}" font-size="12.5" fill="#4b5563">'
                 f'<tspan fill="{color}" font-weight="700">→ </tspan>{escape(s["out"])}</text>')
    parts.append(f'<text x="{BODY_X}" y="{top+147}" font-size="11.5" fill="#8b93a1">'
                 f'{escape(s["note"])}</text>')

    if s["flow"]:
        gy = top + CARD_H
        parts.append(f'<line x1="{BODY_X-14}" y1="{gy+12}" x2="{BODY_X-14}" y2="{gy+GAP-16}" '
                     'stroke="#b6bcc5" stroke-width="1.5"/>')
        parts.append(f'<polygon points="{BODY_X-18},{gy+GAP-20} {BODY_X-10},{gy+GAP-20} '
                     f'{BODY_X-14},{gy+GAP-11}" fill="#b6bcc5"/>')
        parts.append(f'<text x="{BODY_X+2}" y="{gy+GAP/2+4:.0f}" font-size="12" fill="#6b7280" '
                     f'font-style="italic">{escape(s["flow"])}</text>')

foot = TOP + len(STAGES) * CARD_H + (len(STAGES) - 1) * GAP + 34
parts.append(f'<text x="{CARD_X}" y="{foot}" font-size="11.5" fill="#8b93a1">'
             'rent-assistant export refreshes rentals.csv and rentals.html after any stage — it only reads.'
             '</text>')
parts.append('</svg>')

target = pathlib.Path(__file__).resolve().parent.parent / "docs/diagrams/pipeline-flow.svg"
target.write_text("\n".join(parts) + "\n")
print(f"{target} — {len(STAGES)} stages, {height}px tall")
