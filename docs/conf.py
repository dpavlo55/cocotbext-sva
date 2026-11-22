"""Sphinx configuration (local build: `nox -s docs`; Read the Docs: .readthedocs.yaml)."""
import sys
from importlib.metadata import version as _version
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "_ext"))   # local extensions

project = "cocotbext-sva"
author = "David Pavlović"
copyright = "2026, David Pavlović"
release = _version("cocotbext-sva")
version = ".".join(release.split(".")[:2])

extensions = [
    "myst_parser",            # the pages are Markdown
    "sphinx.ext.autodoc",     # API reference from the docstrings
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "sphinx_copybutton",
    "wavedrom_svg",           # ```{wavedrom} timing diagrams (_ext/wavedrom_svg.py)
]

source_suffix = {".md": "markdown"}
exclude_patterns = ["_build", "_ext"]

# the docstrings write `name` for code, as in Markdown
default_role = "literal"

myst_heading_anchors = 3      # links like page.md#section-title
myst_enable_extensions = ["colon_fence", "deflist"]

autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_default_options = {"members": True, "show-inheritance": False}

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "cocotb": ("https://docs.cocotb.org/en/stable", None),
}

html_theme = "furo"
html_title = f"cocotbext-sva {release}"
html_static_path = ["_static"]
html_css_files = ["custom.css"]
html_favicon = "_static/logo-icon.svg"
html_theme_options = {
    "light_logo": "logo-icon.svg",   # the owl above the project name in the sidebar
    "dark_logo": "logo-icon.svg",
    "source_repository": "https://github.com/dpavlo55/cocotbext-sva",
    "source_branch": "main",
    "source_directory": "docs/",
}
