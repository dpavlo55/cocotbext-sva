"""Draw the diagrams of the user guide as SVG.

Run from anywhere: python docs/guide/img/src/figures.py
Writes one .svg per figure into docs/guide/img/. Standard library only, deterministic output.
The attempts timing diagram is WaveJSON in docs/guide/how-it-works.md (docs/_ext/wavedrom_svg.py),
drawn with the same font and palette.
"""

from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent.parent

FONT = "Arial, Helvetica, sans-serif"
MONO = "Consolas, 'DejaVu Sans Mono', monospace"
INK = "#1f2933"
MUTED = "#52606d"
LINE = "#3e4c59"
FAINT = "#9aa5b1"
LIGHT_BLUE = "#c9d6e8"
LIGHT_GREEN = "#cfe6da"
ORANGE = "#e07a5f"
LIGHT_ORANGE = "#f6cdbf"
LIGHT_SAND = "#efe5c9"
GREY = "#e4e7eb"
NOTE = "#fff6d5"
REGION = "#f5f7fa"
REGION_SAND = "#f7f5ee"


class Svg:
    def __init__(self, width, height):
        self.w = width
        self.h = height
        self.items = []

    def add(self, s):
        self.items.append(s)

    def rect(self, x, y, w, h, fill="#ffffff", stroke=LINE, rx=4, dash=None, sw=1.2):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
                 f'stroke="{stroke}" stroke-width="{sw}"{d}/>')

    def text(self, x, y, s, size=12, anchor="middle", weight="normal", color=INK, mono=False):
        family = MONO if mono else FONT
        self.add(f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" text-anchor="{anchor}" '
                 f'font-weight="{weight}" fill="{color}">{escape(s)}</text>')

    def lines(self, x, y, rows, size=12, anchor="middle", gap=None):
        gap = gap or size * 1.3
        top = y - (len(rows) - 1) * gap / 2 + size * 0.35
        for i, (s, opts) in enumerate(rows):
            self.text(x, top + i * gap, s, size=opts.get("size", size), anchor=anchor,
                      weight=opts.get("weight", "normal"), mono=opts.get("mono", False),
                      color=opts.get("color", INK))

    def box(self, x, y, w, h, title, sub=(), fill=LIGHT_BLUE, mono_title=False, mono_sub=False):
        self.rect(x, y, w, h, fill=fill)
        rows = [(title, {"weight": "bold", "mono": mono_title})]
        rows += [(s, {"size": 10.5, "color": INK if mono_sub else MUTED, "mono": mono_sub}) for s in sub]
        self.lines(x + w / 2, y + h / 2, rows, gap=14)

    def path(self, pts, arrow_end=True, dash=None, color=LINE, sw=1.3):
        d = "M " + " L ".join(f"{x} {y}" for x, y in pts)
        da = f' stroke-dasharray="{dash}"' if dash else ""
        me = ' marker-end="url(#arrow)"' if arrow_end else ""
        self.add(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{sw}"{da}{me}/>')

    def line(self, x1, y1, x2, y2, color=LINE, sw=1.3, dash=None):
        self.path([(x1, y1), (x2, y2)], arrow_end=False, color=color, sw=sw, dash=dash)

    def dot(self, x, y, r=3, color=LINE):
        self.add(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{color}"/>')

    def save(self, name, title):
        head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                f'viewBox="0 0 {self.w} {self.h}" role="img">\n'
                f'<title>{escape(title)}</title>\n'
                '<defs>\n'
                f'<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" '
                f'orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" fill="{LINE}"/></marker>\n'
                '</defs>\n'
                f'<rect x="0" y="0" width="{self.w}" height="{self.h}" fill="#ffffff"/>\n')
        (OUT / f"{name}.svg").write_text(head + "\n".join(self.items) + "\n</svg>\n", encoding="utf-8",
                                         newline="\n")


# ---------------------------------------------------------------------------------------------
# Compile once, then one step per clock tick
# ---------------------------------------------------------------------------------------------

def pipeline():
    s = Svg(860, 360)
    s.rect(20, 20, 820, 140, fill=REGION, stroke=MUTED, rx=8, dash="6 4")
    s.text(36, 42, "Once, when the assertion is created", anchor="start", weight="bold", color=MUTED)
    s.rect(20, 190, 820, 150, fill=REGION_SAND, stroke=MUTED, rx=8, dash="6 4")
    s.text(36, 212, "At every clock tick", anchor="start", weight="bold", color=MUTED)

    y, h = 62, 66
    row = [(40, 150, "Your property", ("req |-> ##[1:3] ack",), GREY, True),
           (220, 130, "Parser", ("checks the syntax",), LIGHT_BLUE, False),
           (380, 130, "Syntax tree", ("names resolved",), LIGHT_BLUE, False),
           (540, 130, "Compiler", ("builds the machine",), LIGHT_BLUE, False),
           (700, 120, "Automaton", ("+ generated Python", "for the conditions"), LIGHT_GREEN, False)]
    for x, w, title, sub, fill, mono_sub in row:
        s.box(x, y, w, h, title, sub, fill=fill, mono_sub=mono_sub)
    for (x1, w1, *_), (x2, *_) in zip(row, row[1:]):
        s.path([(x1 + w1, y + h / 2), (x2, y + h / 2)])

    y2 = 232
    s.box(40, y2, 220, h, "Sampler", ("reads the signals the", "assertions need right now"))
    s.box(320, y2, 220, h, "Engine", ("one step of the automaton:", "a table lookup, usually"), fill=LIGHT_GREEN)
    s.box(600, y2, 220, h, "Reporter", ("logs failures as they happen,", "counts passes and cover hits"))
    s.path([(260, y2 + h / 2), (320, y2 + h / 2)])
    s.text(290, y2 + h / 2 - 8, "values", size=10.5, color=MUTED)
    s.path([(540, y2 + h / 2), (600, y2 + h / 2)])
    s.text(570, y2 + h / 2 - 8, "events", size=10.5, color=MUTED)

    s.path([(760, y + h), (760, 176), (430, 176), (430, y2)], dash="5 4")
    s.text(595, 170, "the engine steps this machine", size=10.5, color=MUTED)
    s.text(150, 320, "clock edge · start of time step · Probe · dump", size=10.5, color=MUTED)
    s.text(430, 320, "never talks to the simulator", size=10.5, color=MUTED)
    s.save("pipeline", "From SVA text to a check at every clock tick")


# ---------------------------------------------------------------------------------------------
# Merging attempts into one state; the cached table
# ---------------------------------------------------------------------------------------------

def merging():
    s = Svg(860, 450)
    s.text(20, 30, "Tick 4 of req |-> ##[1:3] ack, after req = 1 at ticks 1, 2 and 3",
           anchor="start", weight="bold")
    for i, (title, sub, origin) in enumerate([("waited 1 tick", "position 1", "from tick 3"),
                                              ("waited 2 ticks", "position 2", "from tick 2"),
                                              ("waited 3 ticks", "position 3, last chance", "from tick 1")]):
        x = 20 + i * 130
        s.box(x, 48, 120, 46, title, (sub,))
        s.dot(x + 60, 116, r=7)
        s.text(x + 60, 140, origin, size=10.5, color=MUTED)
    s.path([(410, 71), (466, 71)])
    s.text(438, 63, "merge", size=10.5, color=MUTED)
    s.rect(476, 44, 364, 104, fill=LIGHT_GREEN)
    s.lines(658, 96, [("One state for all of them", {"weight": "bold"}),
                      ("{1, 2, 3}", {"mono": True, "weight": "bold", "size": 13}),
                      ("plus the oldest start time and the count,", {"size": 10.5, "color": MUTED}),
                      ("for the failure report", {"size": 10.5, "color": MUTED})], gap=19)

    s.text(20, 190, "The table the engine fills in as it goes", anchor="start", weight="bold")
    cols = [32, 190, 400, 540]
    top, rh = 202, 28
    s.rect(20, top, 820, rh, fill=GREY, rx=0)
    for x, head in zip(cols, ["state now", "inputs at this tick", "next state", "events"]):
        s.text(x, top + 18, head, anchor="start", weight="bold")
    rows = [("{ }", "req=0", "{ }", "vacuous success", None),
            ("{ }", "req=1", "{1}", "a new attempt starts", None),
            ("{1}", "req=0  ack=0", "{2}", "still waiting", None),
            ("{1}", "req=1  ack=1", "{1}", "1 pass, and a new attempt", LIGHT_GREEN),
            ("{1, 2, 3}", "req=0  ack=0", "{2, 3}", "1 failure (the attempt from tick 1)", LIGHT_ORANGE),
            ("{1, 2, 3}", "req=0  ack=1", "{ }", "3 passes", LIGHT_GREEN)]
    for i, (state, inputs, nxt, event, fill) in enumerate(rows):
        y = top + rh * (i + 1)
        s.rect(20, y, 820, rh, fill="#ffffff" if i % 2 == 0 else REGION, stroke=GREY, rx=0, sw=1)
        if fill:
            s.rect(528, y + 4, 300, rh - 8, fill=fill, stroke=fill, rx=3, sw=1)
        for x, v in zip(cols[:3], (state, inputs, nxt)):
            s.text(x, y + 18, v, anchor="start", mono=True)
        s.text(cols[3], y + 18, event, anchor="start")
    s.text(20, 418, "A row is worked out the first time its (state, inputs) pair happens; "
                    "after that, it is a single lookup.", anchor="start", size=10.5, color=MUTED)
    s.save("merging", "Attempts at the same position are merged into one state; transitions are cached")


# ---------------------------------------------------------------------------------------------
# When values are sampled within the time step of a clock edge
# ---------------------------------------------------------------------------------------------

def sampling():
    s = Svg(860, 340)
    s.rect(230, 20, 390, 180, fill=REGION, stroke=MUTED, rx=8, dash="6 4")
    s.text(425, 42, "one time step: the simulation time of the clock edge", size=10.5,
           weight="bold", color=MUTED)

    s.text(30, 86, "clk", anchor="start", weight="bold", mono=True)
    s.path([(100, 92), (340, 92), (340, 68), (820, 68)], arrow_end=False, sw=1.6)
    s.text(30, 138, "q", anchor="start", weight="bold", mono=True)
    s.path([(100, 124), (446, 124), (454, 144), (820, 144)], arrow_end=False, color=INK, sw=1.4)
    s.path([(100, 144), (446, 144), (454, 124), (820, 124)], arrow_end=False, color=INK, sw=1.4)
    s.text(165, 138, "old value", mono=True)
    s.text(690, 138, "new value", mono=True)
    s.rect(100, 98, 130, 18, fill=LIGHT_GREEN, stroke=LIGHT_GREEN, rx=3, sw=1)
    s.text(165, 111, "what SVA samples", size=10.5, weight="bold")

    s.line(100, 178, 820, 178, color=FAINT, sw=1.2)
    for x, label in [(246, "start"), (340, "edge seen"), (450, "flip-flops update")]:
        s.dot(x, 178, r=4)
        s.text(x, 168, label, size=10.5, color=MUTED)

    s.box(40, 256, 370, 66, 'sampling="preponed"',
          ("a snapshot at the start of the time step:", "always the old value, whatever drives the clock"),
          fill=LIGHT_GREEN, mono_title=True)
    s.path([(246, 256), (246, 186)])
    s.box(450, 256, 370, 66, 'sampling="edge"',
          ("read when the edge is seen: the old value when cocotb", "drives the clock, maybe the new one if the HDL does"),
          mono_title=True)
    s.path([(520, 256), (520, 228), (340, 228), (340, 186)])
    s.save("sampling", "When values are sampled within the time step of a clock edge")


if __name__ == "__main__":
    pipeline()
    merging()
    sampling()
