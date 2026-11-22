"""Syntax tree for SVA expressions, sequences and properties.

Nodes are small immutable-by-convention classes. Every node carries `span`
(start, end) offsets into the source text for error messages and reports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

Span = tuple[int, int]


@dataclass(eq=False)
class Node:
    span: Span = field(default=(0, 0), kw_only=True, repr=False)


# --------------------------------------------------------------------------
# Expressions
# --------------------------------------------------------------------------

@dataclass(eq=False)
class Num(Node):
    value: int
    width: Optional[int]       # None = unsized
    signed: bool
    xmask: int = 0             # bits written as x/z/?
    fill: Optional[str] = None  # '0 / '1 / 'x unbased unsized literal


@dataclass(eq=False)
class Id(Node):
    path: tuple[str, ...]      # hierarchical name, e.g. ("u_ctrl", "state")

    @property
    def name(self) -> str:
        return ".".join(self.path)


@dataclass(eq=False)
class Index(Node):
    base: Node
    index: Node


@dataclass(eq=False)
class Slice(Node):
    base: Node
    msb: Node
    lsb: Node


@dataclass(eq=False)
class PartSelect(Node):          # base[start +: width] / base[start -: width]
    base: Node
    start: Node
    width: Node
    up: bool


@dataclass(eq=False)
class Unary(Node):
    op: str
    operand: Node


@dataclass(eq=False)
class Binary(Node):
    op: str
    left: Node
    right: Node


@dataclass(eq=False)
class Cond(Node):
    cond: Node
    then: Node
    other: Node


@dataclass(eq=False)
class Concat(Node):
    items: list


@dataclass(eq=False)
class Repl(Node):
    count: Node
    items: list


@dataclass(eq=False)
class Call(Node):
    name: str                  # "$past", or a user name (named sequence/property instance)
    args: list                 # expressions, or None for an omitted argument


# --------------------------------------------------------------------------
# Sequences
# --------------------------------------------------------------------------

INF = None  # upper bound "$"


@dataclass(eq=False)
class SBool(Node):
    expr: Node


@dataclass(eq=False)
class SDelay(Node):
    left: Optional[Node]       # None for a leading ##
    lo: Node                   # constant expressions
    hi: Optional[Node]         # None = $ ; same object as lo for ##n
    right: Node
    hi_inf: bool = False


@dataclass(eq=False)
class SRepeat(Node):
    seq: Node
    kind: str                  # '*', '->', '='
    lo: Node
    hi: Optional[Node]
    hi_inf: bool = False


@dataclass(eq=False)
class SBin(Node):
    op: str                    # 'and', 'or', 'intersect', 'within'
    left: Node
    right: Node


@dataclass(eq=False)
class SThroughout(Node):
    expr: Node
    seq: Node


@dataclass(eq=False)
class SFirstMatch(Node):
    seq: Node


@dataclass(eq=False)
class SMatchItems(Node):
    seq: Node
    assigns: list              # [(local name, expr)]


# --------------------------------------------------------------------------
# Properties
# --------------------------------------------------------------------------

@dataclass(eq=False)
class PSeq(Node):
    seq: Node
    strength: Optional[str] = None   # None (context default), 'weak', 'strong'


@dataclass(eq=False)
class PNot(Node):
    prop: Node


@dataclass(eq=False)
class PBin(Node):
    op: str                    # 'and', 'or', 'iff', 'implies', 'until', 's_until', 'until_with', 's_until_with'
    left: Node
    right: Node
    tick: Optional[Node] = None      # until family: see PUnaryTemporal.tick


@dataclass(eq=False)
class PImpl(Node):
    op: str                    # '|->', '|=>', '#-#', '#=#'
    seq: Node
    prop: Node


@dataclass(eq=False)
class PIf(Node):
    cond: Node
    then: Node
    other: Optional[Node]


@dataclass(eq=False)
class PCase(Node):
    expr: Node
    items: list                # [(list of exprs or None for default, prop)]


@dataclass(eq=False)
class PUnaryTemporal(Node):
    op: str                    # always, s_always, eventually, s_eventually, nexttime, s_nexttime
    prop: Node
    lo: Optional[Node] = None
    hi: Optional[Node] = None
    hi_inf: bool = False
    # Multi-clock properties (_multiclock): the operator counts only the
    # ticks at which this expression is true (the ticks of its clock).
    tick: Optional[Node] = None


@dataclass(eq=False)
class PAbort(Node):
    op: str                    # accept_on, reject_on, sync_accept_on, sync_reject_on
    cond: Node
    prop: Node


@dataclass(eq=False)
class PAlign(Node):
    """prop, evaluated from the first tick at which cond is true (made by
    _multiclock: a property waiting for the next tick of its clock)."""
    cond: Node
    prop: Node


# --------------------------------------------------------------------------
# Assertion header
# --------------------------------------------------------------------------

@dataclass(eq=False)
class ClockEvent(Node):
    edge: Optional[str]        # 'posedge', 'negedge', 'edge' or None (any change)
    signal: Node
    iff: Optional[Node] = None


@dataclass(eq=False)
class Clocked(Node):
    """`@(event) body` inside a sequence or property: body is clocked by
    event (multi-clock properties; removed by _multiclock before compiling).

    clock None: a parenthesized group that contains clocking events. They do
    not extend out of the parentheses: what follows the group is clocked like
    what precedes it."""
    clock: Optional[ClockEvent]
    body: Node


@dataclass(eq=False)
class AssertionSpec(Node):
    clock: Optional[ClockEvent]
    disable: Optional[Node]
    prop: Node


SEQ_NODES = (SBool, SDelay, SRepeat, SBin, SThroughout, SFirstMatch, SMatchItems)
EXPR_NODES = (Num, Id, Index, Slice, PartSelect, Unary, Binary, Cond, Concat, Repl, Call)


def is_seq(node: Node) -> bool:
    if isinstance(node, Clocked):
        return is_seq(node.body) or is_expr(node.body)
    return isinstance(node, SEQ_NODES)


def is_expr(node: Node) -> bool:
    return isinstance(node, EXPR_NODES)
