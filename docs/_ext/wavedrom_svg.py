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


class WaveDrom(SphinxDirective):
    has_content = True
    option_spec = {"alt": directives.unchanged}

    def run(self):
        source = "\n".join(self.content)
        name = f"wavedrom-{hashlib.sha1(source.encode()).hexdigest()[:12]}.svg"
        images = Path(self.env.app.outdir) / "_images"
        images.mkdir(parents=True, exist_ok=True)
        try:
            wavedrom.render(source).saveas(str(images / name))
        except Exception as e:  # invalid WaveJSON: a build error at this line
            raise self.error(f"wavedrom: {e}") from e
        up = "../" * self.env.docname.count("/")
        alt = self.options.get("alt", "timing diagram").replace('"', "&quot;")
        html = f'<div class="wavedrom"><img src="{up}_images/{name}" alt="{alt}"></div>'
        return [nodes.raw("", html, format="html")]


def setup(app):
    app.add_directive("wavedrom", WaveDrom)
    return {"parallel_read_safe": True, "parallel_write_safe": True}
