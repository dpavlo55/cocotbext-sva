"""Sphinx configuration (local build: `nox -s docs`; Read the Docs: .readthedocs.yaml)."""
from importlib.metadata import version as _version

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
]

source_suffix = {".md": "markdown"}
exclude_patterns = ["_build"]

# the docstrings write `name` for code, as in Markdown
default_role = "literal"

myst_heading_anchors = 3      # links like guide.md#semantics-notes
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
html_theme_options = {
    "source_repository": "https://github.com/dpavlo55/cocotbext-sva",
    "source_branch": "main",
    "source_directory": "docs/",
}
