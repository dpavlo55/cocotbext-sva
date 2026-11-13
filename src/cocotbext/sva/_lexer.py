"""Tokenizer for SVA text."""
from __future__ import annotations

import re
from dataclasses import dataclass

from ._errors import SvaSyntaxError

KEYWORDS = {
    "not", "and", "or", "iff", "implies", "until", "s_until", "until_with", "s_until_with",
    "always", "s_always", "eventually", "s_eventually", "nexttime", "s_nexttime",
    "if", "else", "case", "endcase", "default", "throughout", "within", "intersect",
    "first_match", "strong", "weak", "accept_on", "reject_on", "sync_accept_on",
    "sync_reject_on", "disable", "posedge", "negedge", "edge",
}

# Longest first.
PUNCT = [
    "|->", "|=>", "#-#", "#=#", "[->", "[+]", "<<<", ">>>", "===", "!==", "==?", "!=?",
    "##", "[*", "[=", "==", "!=", "<=", ">=", "&&", "||", "<<", ">>", "**", "~&", "~|",
    "~^", "^~", "+:", "-:",
    "@", "(", ")", "[", "]", "{", "}", ",", ":", "?", ";", ".", "+", "-", "*", "/", "%",
    "&", "|", "^", "~", "!", "<", ">", "=", "$",
]

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+|//[^\n]*|/\*.*?\*/)
  | (?P<based>(?:\d[\d_]*)?\s*'[sS]?[bBoOdDhH]\s*[0-9a-fA-FxXzZ?_]+)
  | (?P<unbased>'[01xXzZ](?![0-9a-zA-Z_]))
  | (?P<num>\d[\d_]*)
  | (?P<sys>\$[a-zA-Z_][a-zA-Z0-9_$]*)
  | (?P<id>[a-zA-Z_][a-zA-Z0-9_$]*)
  | (?P<punct>""" + "|".join(re.escape(p) for p in PUNCT) + r""")
    """,
    re.VERBOSE | re.DOTALL,
)


@dataclass
class Token:
    kind: str      # 'num', 'id', 'kw', 'sys', 'op', 'eof'
    text: str
    pos: int
    value: object = None   # parsed literal for 'num'

    def __repr__(self):
        return f"{self.kind}:{self.text!r}"


def _parse_based(text: str):
    """Return (value, width or None, signed, xmask) for an SV based literal."""
    m = re.fullmatch(r"(\d[\d_]*)?\s*'([sS]?)([bBoOdDhH])\s*([0-9a-fA-FxXzZ?_]+)", text)
    size_s, sign_s, base_s, digits = m.groups()
    width = int(size_s.replace("_", "")) if size_s else None
    base = {"b": 2, "o": 8, "d": 10, "h": 16}[base_s.lower()]
    digits = digits.replace("_", "")
    bits_per = {2: 1, 8: 3, 16: 4}.get(base)
    value = 0
    xmask = 0
    if base == 10:
        if any(c in "xXzZ?" for c in digits):
            xmask = (1 << (width or 32)) - 1
        else:
            value = int(digits, 10)
    else:
        for c in digits:
            value <<= bits_per
            xmask <<= bits_per
            if c in "xXzZ?":
                xmask |= (1 << bits_per) - 1
            else:
                value |= int(c, base)
    if width is not None:
        value &= (1 << width) - 1
        xmask &= (1 << width) - 1
    return value, width, bool(sign_s), xmask


def tokenize(text: str) -> list[Token]:
    tokens = []
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise SvaSyntaxError(f"unexpected character {text[pos]!r}", text, pos)
        kind = m.lastgroup
        tok_text = m.group(kind)
        if kind == "ws":
            pass
        elif kind == "based":
            tokens.append(Token("num", tok_text, pos, _parse_based(tok_text)))
        elif kind == "unbased":
            tokens.append(Token("num", tok_text, pos, ("fill", tok_text[1].lower())))
        elif kind == "num":
            tokens.append(Token("num", tok_text, pos, (int(tok_text.replace("_", "")), None, True, 0)))
        elif kind == "sys":
            tokens.append(Token("sys", tok_text, pos))
        elif kind == "id":
            tokens.append(Token("kw" if tok_text in KEYWORDS else "id", tok_text, pos))
        else:
            tokens.append(Token("op", tok_text, pos))
        pos = m.end()
    tokens.append(Token("eof", "", n))
    return tokens
