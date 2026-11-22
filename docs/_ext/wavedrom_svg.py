"""A `wavedrom` directive: a WaveJSON timing diagram, rendered to SVG at build
time with wavedrompy (pure Python: no browser, no JavaScript, no Cairo).

    ```{wavedrom}
    :alt: what the diagram shows
    { "signal": [ { "name": "clk", "wave": "p...." } ] }
    ```
"""
import hashlib
from pathlib import Path

import wavedrom
from docutils import nodes
from docutils.parsers.rst import directives
from sphinx.util.docutils import SphinxDirective


# WaveDrom's own skin -> the font and palette of the other diagrams
# (docs/guide/img/src/figures.py): Arial, slate ink and lines, muted fills.
RESTYLE = {
    "font-family:Helvetica": "font-family:Arial, Helvetica, sans-serif",
    "#0041c4": "#1f2933",    # lane names and group labels
    "stroke:#000": "stroke:#1f2933",
    'fill="#000"': 'fill="#1f2933"',
    "fill:#000": "fill:#1f2933",
    "stroke:#888": "stroke:#9aa5b1",
    "#ffffb4": "#efe5c9",    # data 3: sand
    "#ffe0b9": "#f6cdbf",    # data 4: light orange
    "#b9e0ff": "#c9d6e8",    # data 5: light blue
    "#ccfdfe": "#e4e7eb",    # data 6: grey
    "#cdfdc5": "#cfe6da",    # data 7: light green (pass)
    "#f5c2c0": "#f6cdbf",    # data 9: light orange (fail)
}


def restyle(svg: str) -> str:
    for old, new in RESTYLE.items():
        svg = svg.replace(old, new)
    return svg


class WaveDrom(SphinxDirective):
    has_content = True
    option_spec = {"alt": directives.unchanged}

    def run(self):
        source = "\n".join(self.content)
        try:
            svg = restyle(wavedrom.render(source).tostring())
        except Exception as e:  # invalid WaveJSON: a build error at this line
            raise self.error(f"wavedrom: {e}") from e
        # named after the finished drawing: a change of source or of style is a
        # new URL, so browsers do not keep showing a cached old version
        name = f"wavedrom-{hashlib.sha1(svg.encode()).hexdigest()[:12]}.svg"
        images = Path(self.env.app.outdir) / "_images"
        images.mkdir(parents=True, exist_ok=True)
        (images / name).write_text(svg, encoding="utf-8")
        up = "../" * self.env.docname.count("/")
        alt = self.options.get("alt", "timing diagram").replace('"', "&quot;")
        html = f'<div class="wavedrom"><img src="{up}_images/{name}" alt="{alt}"></div>'
        return [nodes.raw("", html, format="html")]


def setup(app):
    app.add_directive("wavedrom", WaveDrom)
    return {"parallel_read_safe": True, "parallel_write_safe": True}
