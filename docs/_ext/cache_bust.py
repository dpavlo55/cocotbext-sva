"""Add `?v=<content hash>` to the images of every HTML page.

Read the Docs serves images with a 30-minute cache lifetime, and Sphinx
publishes them under their own names: a changed diagram keeps its URL, and a
browser that saw the old one keeps showing it. With the hash in the URL, a
changed image is a new URL and is fetched at once.
"""
import hashlib
import re
from pathlib import Path


def _versions(app):
    versions = {}
    for src, (_docs, published) in app.env.images.items():
        path = Path(app.srcdir) / src
        if path.is_file():
            versions[published] = hashlib.sha1(path.read_bytes()).hexdigest()[:10]
    return versions


def _bust(app, pagename, templatename, context, doctree):
    body = context.get("body")
    if not body:
        return
    versions = _versions(app)

    def add_version(m):
        v = versions.get(m.group(2))
        return f'{m.group(1)}?v={v}"' if v else m.group(0)
    context["body"] = re.sub(r'(src="[^"?]*_images/([^"?/]+))"', add_version, body)


def setup(app):
    app.connect("html-page-context", _bust)
    return {"parallel_read_safe": True, "parallel_write_safe": True}
