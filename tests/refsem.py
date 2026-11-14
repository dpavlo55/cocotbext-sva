"""Reference interpreter for SVA: slow, literal, independent of the engine.

Semantics on truncated words (IEEE 1800 Annex F; Eisner et al., "Reasoning
with temporal logic on truncated paths"): a finite prefix is extended with
special letters TOP (satisfies every boolean) or BOT (satisfies none).

  verdict FAIL at tick t  <=>  P does not hold on  x[0..t] TOP TOP ...
  verdict PASS at tick t  <=>  P holds on          x[0..t] BOT BOT ...

`not` swaps TOP and BOT; the antecedent of an implication is evaluated on the
swapped word. Booleans are whole leaf expressions (not decomposed).

Only the subset of expressions needed by the fuzzer is evaluated here
(identifiers, constants, !, &&, ||, ==, !=, local variables).
"""
from __future__ import annotations

import sys
from functools import lru_cache

from cocotbext.sva import _ast as A
from cocotbext.sva._parser import parse_assertion

TOP = "TOP"
BOT = "BOT"
PASS, VPASS, FAIL = "pass", "vpass", "fail"
PASS_ANY = "pass_any"      # success whose vacuity the reference does not model

sys.setrecursionlimit(10000)

_UNTIL = ("until", "s_until", "until_with", "s_until_with")


def _swap(letter):
    return BOT if letter is TOP else TOP if letter is BOT else letter


class Word:
    """A finite real prefix followed by a uniform tail of TOP or BOT letters."""

    def __init__(self, rows, n_real, tail, tail_len):
        self.rows = rows
        self.n_real = n_real
        self.tail = tail          # TOP, BOT or None (no tail: neutral, word ends)
        self.length = n_real + (tail_len if tail else 0)

    def letter(self, i):
        if i < self.n_real:
            return self.rows[i]
        return self.tail

    def swapped(self):
        w = Word(self.rows, self.n_real, _swap(self.tail) if self.tail else None, 0)
        w.length = self.length
        return w

    def key(self):
        return (self.n_real, self.tail, self.length)


def eval_expr(e, row, env):
    if isinstance(e, A.Num):
        return e.value
    if isinstance(e, A.Id):
        name = e.path[0]
        if name in env:
            v = env[name]
            return 0 if v is None else v
        return row[name]
    if isinstance(e, A.Unary) and e.op == "!":
        return int(not eval_expr(e.operand, row, env))
    if isinstance(e, A.Binary):
        if e.op == "&&":
            return int(bool(eval_expr(e.left, row, env)) and bool(eval_expr(e.right, row, env)))
        if e.op == "||":
            return int(bool(eval_expr(e.left, row, env)) or bool(eval_expr(e.right, row, env)))
        if e.op == "==":
            return int(eval_expr(e.left, row, env) == eval_expr(e.right, row, env))
        if e.op == "!=":
            return int(eval_expr(e.left, row, env) != eval_expr(e.right, row, env))
    raise NotImplementedError(f"reference cannot evaluate {e}")


def holds_bool(expr, letter, env):
    if letter is TOP:
        return True
    if letter is BOT:
        return False
    return bool(eval_expr(expr, letter, dict(env)))


class Ref:
    def __init__(self, text, rows, tail_len=70):
        self.spec = parse_assertion(text)
        self.rows = rows
        self.tail_len = tail_len
        self._memo = {}
        self._desugared = {}     # id(node) -> (node, replacement): built once, kept alive

    def _once(self, node, build):
        # keyed by node and by the call site (each site builds a different form)
        key = (id(node), build.__code__)
        hit = self._desugared.get(key)
        if hit is None:
            hit = self._desugared[key] = (node, build())
        return hit[1]

    # -- sequences: set of (end, env) ---------------------------------------
    def M(self, s, i, env, w):
        key = (id(s), i, env, w.key())
        hit = self._memo.get(key)
        if hit is not None:
            return hit
        res = self._M(s, i, env, w)
        self._memo[key] = res
        return res

    def _M(self, s, i, env, w):
        if A.is_expr(s):
            return self.M(self._once(s, lambda: A.SBool(s)), i, env, w)
        if isinstance(s, A.SBool):
            if i < w.length and holds_bool(s.expr, w.letter(i), env):
                return frozenset({(i, env)})
            return frozenset()
        if isinstance(s, A.SDelay):
            left_node = s.left if s.left is not None else self._once(s, lambda: A.SBool(A.Num(1, None, True)))
            left = self.M(left_node, i, env, w)
            lo = s.lo.value
            out = set()
            for k, e in left:
                n = lo
                while True:
                    if s.hi_inf:
                        if k + n > w.length:
                            break
                    elif n > s.hi.value:
                        break
                    if n == 0:
                        if k >= i:                      # left non-empty
                            out |= {(k2, e2) for k2, e2 in self.M(s.right, k, e, w) if k2 >= k}
                    else:
                        # ##n is ##1 1[*n-1] ##1: the skipped ticks must exist and
                        # satisfy `1` (BOT satisfies no boolean); this matters when
                        # the right operand matches empty.
                        for k2, e2 in self.M(s.right, k + n, e, w):
                            last_true = min(k2, k + n - 1)
                            if all(j < w.length and w.letter(j) is not BOT
                                   for j in range(k + 1, last_true + 1)):
                                out.add((k2, e2))
                    n += 1
            return frozenset(out)
        if isinstance(s, A.SRepeat):
            lo = s.lo.value
            hi = None if s.hi_inf else s.hi.value
            if s.kind == "->":
                def build():
                    notb = A.Unary("!", s.seq)
                    unit = A.SDelay(A.SRepeat(A.SBool(notb), "*", A.Num(0, None, True), None, True),
                                    A.Num(1, None, True), A.Num(1, None, True), A.SBool(s.seq))
                    return A.SRepeat(unit, "*", s.lo, s.hi, s.hi_inf)
                return self.M(self._once(s, build), i, env, w)
            if s.kind == "=":
                def build():
                    notb = A.Unary("!", s.seq)
                    goto = A.SRepeat(s.seq, "->", s.lo, s.hi, s.hi_inf)
                    tail = A.SRepeat(A.SBool(notb), "*", A.Num(0, None, True), None, True)
                    return A.SDelay(goto, A.Num(1, None, True), A.Num(1, None, True), tail)
                return self.M(self._once(s, build), i, env, w)
            out = set()
            cur = {(i - 1, env)}
            count = 0
            seen = set()
            while True:
                if count >= lo:
                    out |= cur
                if hi is not None and count >= hi:
                    break
                nxt = set()
                for k, e in cur:
                    nxt |= self.M(s.seq, k + 1, e, w)
                count += 1
                if hi is None and count >= lo:
                    # beyond the lower bound the count no longer matters: prune revisits
                    nxt -= seen
                    seen |= nxt
                if not nxt:
                    break
                cur = nxt
                if count > w.length + 2:
                    break
            return frozenset(out)
        if isinstance(s, A.SBin):
            a = self.M(s.left, i, env, w)
            if s.op == "or":
                return a | self.M(s.right, i, env, w)
            b = self.M(s.right, i, env, w)
            if s.op == "and":
                # empty matches of an operand do not take part (corpus-confirmed)
                return frozenset((max(k1, k2), e1) for k1, e1 in a for k2, e2 in b
                                 if k1 >= i and k2 >= i)
            if s.op == "intersect":
                return frozenset((k1, e1) for k1, e1 in a for k2, e2 in b if k1 == k2)
            if s.op == "within":
                def build():
                    star = A.SRepeat(A.SBool(A.Num(1, None, True)), "*", A.Num(0, None, True), None, True)
                    one = A.Num(1, None, True)
                    outer = A.SDelay(A.SDelay(star, one, one, s.left), one, one, star)
                    return A.SBin("intersect", outer, s.right)
                return self.M(self._once(s, build), i, env, w)
        if isinstance(s, A.SThroughout):
            def build():
                star = A.SRepeat(A.SBool(s.expr), "*", A.Num(0, None, True), None, True)
                return A.SBin("intersect", star, s.seq)
            return self.M(self._once(s, build), i, env, w)
        if isinstance(s, A.SFirstMatch):
            ms = self.M(s.seq, i, env, w)
            if not ms:
                return ms
            k0 = min(k for k, _ in ms)
            return frozenset((k, e) for k, e in ms if k == k0)
        if isinstance(s, A.SMatchItems):
            out = set()
            for k, e in self.M(s.seq, i, env, w):
                d = dict(e)
                letter = w.letter(k)
                for name, expr in s.assigns:
                    d[name] = None if letter in (TOP, BOT) else eval_expr(expr, letter, d)
                out.add((k, tuple(sorted(d.items()))))
            return frozenset(out)
        raise NotImplementedError(f"reference: sequence {type(s).__name__}")

    # -- properties ---------------------------------------------------------
    def _as_pseq(self, p):
        return self._once(p, lambda: A.PSeq(p if A.is_seq(p) else A.SBool(p)))

    def H(self, p, i, env, w):
        if A.is_expr(p) or A.is_seq(p):
            p = self._as_pseq(p)
        if isinstance(p, A.PSeq):
            return any(k >= i for k, _ in self.M(p.seq, i, env, w))
        if isinstance(p, A.PNot):
            return not self.H(p.prop, i, env, w.swapped())
        if isinstance(p, A.PImpl):
            if p.op in ("#-#", "#=#"):
                op = "|->" if p.op == "#-#" else "|=>"
                return self.H(self._once(p, lambda: A.PNot(A.PImpl(op, p.seq, A.PNot(p.prop)))), i, env, w)
            shift = 0 if p.op == "|->" else 1
            for k, e in self.M(p.seq, i, env, w.swapped()):
                if k < i and shift:
                    k = i                   # empty match of a |=> antecedent
                if k >= i and not self.H(p.prop, k + shift, e, w):
                    return False
            return True
        if isinstance(p, A.PUnaryTemporal):
            return self._H_temporal(p, i, env, w)
        if isinstance(p, A.PAbort):
            return self.H(p.prop, i, env, self._abort_word(p, i, env, w))
        if isinstance(p, A.PBin) and p.op in _UNTIL:
            strong, with_ = p.op.startswith("s_"), p.op.endswith("_with")
            last = self._last(w, i)
            for k in range(i, last + 1):
                if self.H(p.right, k, env, w) and all(self.H(p.left, j, env, w)
                                                      for j in range(i, k + (1 if with_ else 0))):
                    return True
            if strong:
                return False
            return all(self.H(p.left, j, env, w) for j in range(i, last + 1))
        if isinstance(p, A.PBin):
            if p.op == "and":
                return self.H(p.left, i, env, w) and self.H(p.right, i, env, w)
            if p.op == "or":
                return self.H(p.left, i, env, w) or self.H(p.right, i, env, w)
            if p.op == "implies":
                return self.H(self._once(p, lambda: A.PBin("or", A.PNot(p.left), p.right)), i, env, w)
            if p.op == "iff":
                return self.H(self._once(p, lambda: A.PBin("and", A.PBin("implies", p.left, p.right),
                                                           A.PBin("implies", p.right, p.left))), i, env, w)
        if isinstance(p, A.PIf):
            def build():
                then = A.PImpl("|->", A.SBool(p.cond), p.then)
                if p.other is None:
                    return then
                other = A.PImpl("|->", A.SBool(A.Unary("!", p.cond)), p.other)
                return A.PBin("and", then, other)
            return self.H(self._once(p, build), i, env, w)
        raise NotImplementedError(f"reference: property {type(p).__name__}")

    # -- temporal operators -------------------------------------------------
    @staticmethod
    def _range(p):
        """(lo, hi) of a temporal operator; hi None = unbounded."""
        base = p.op[2:] if p.op.startswith("s_") else p.op
        if base == "nexttime":
            n = 1 if p.lo is None else p.lo.value
            return n, n
        if p.lo is None:
            return 0, None
        return p.lo.value, (None if p.hi_inf else p.hi.value)

    def _last(self, w, first=0):
        """Last start tick worth checking for an unbounded quantifier whose
        first start is `first`: the tail is uniform, so starts deep inside it
        all behave alike (and starts near the end of the finite word would see
        a spurious end)."""
        if w.tail is None:
            return w.length - 1
        return min(w.length - 1, max(w.n_real, first) + 10)

    def _H_temporal(self, p, i, env, w):
        strong = p.op.startswith("s_")
        base = p.op[2:] if strong else p.op
        lo, hi = self._range(p)
        if base == "nexttime":
            j = i + lo
            return (not strong) if j >= w.length else self.H(p.prop, j, env, w)
        top = self._last(w, i + lo) if hi is None else min(i + hi, w.length - 1)
        js = range(i + lo, top + 1)
        if base == "always":
            if strong and i + hi >= w.length:
                return False
            return all(self.H(p.prop, j, env, w) for j in js)
        return any(self.H(p.prop, j, env, w) for j in js)

    def _abort_word(self, p, i, env, w):
        """accept_on / reject_on (sync forms identical on ticks): from the first
        tick where the condition holds, the word continues with TOP (accept)
        or BOT (reject) letters (IEEE 1800 Annex F)."""
        accept = p.op.endswith("accept_on")
        # only real ticks trigger an abort (the TOP/BOT tail stands for unknown values)
        for j in range(i, w.n_real):
            if holds_bool(p.cond, w.letter(j), env):
                if self._vacuous_by(p.prop, i, env, j):
                    return w                # a vacuous success stays (reference behaviour)
                w2 = Word(self.rows, j, TOP if accept else BOT, 0)
                w2.length = w.length
                return w2
        return w

    def _vacuous_by(self, p, i, env, j):
        """P (started at i) is decided true by tick j on the real letters, vacuously."""
        if not self.H(p, i, env, Word(self.rows, j + 1, BOT, self.tail_len)):
            return False
        return self.nonvacuous(p, i, env, j) is False

    # -- neutral (end of trace) ---------------------------------------------
    def neutral(self, p, i, env, strength):
        """Truth on the real trace alone (weak sequences: not failed; strong: matched)."""
        n = len(self.rows)
        if A.is_expr(p) or A.is_seq(p):
            p = self._as_pseq(p)
        if isinstance(p, A.PSeq):
            st = p.strength or strength
            tail = TOP if st == "weak" else BOT
            return self.H(p, i, env, Word(self.rows, n, tail, self.tail_len))
        if isinstance(p, A.PNot):
            # default strength comes from the directive only (IEEE 1800 16.12.2)
            return not self.neutral(p.prop, i, env, strength)
        if isinstance(p, A.PImpl):
            if p.op in ("#-#", "#=#"):
                shift = 0 if p.op == "#-#" else 1
                # antecedent could still complete after the end: not a failure
                wt = Word(self.rows, n, TOP, self.tail_len)
                if any(k >= n for k, _ in self.M(p.seq, i, env, wt)):
                    return True
                w = Word(self.rows, n, None, 0)
                for k, e in self.M(p.seq, i, env, w):
                    if k < i and shift:
                        k = i               # empty match of a #=# antecedent
                    if k < i:
                        continue
                    if k + shift >= n or self.neutral(p.prop, k + shift, e, strength):
                        return True
                return False
            shift = 0 if p.op == "|->" else 1
            w = Word(self.rows, n, None, 0)
            for k, e in self.M(p.seq, i, env, w):
                if k < i and shift:
                    k = i                   # empty match of a |=> antecedent
                if k < i:
                    continue
                if k + shift >= n:
                    # a consequent that has not seen a tick: only strong obligations fail
                    if not self._unstarted(p.prop, strength):
                        return False
                elif not self.neutral(p.prop, k + shift, e, strength):
                    return False
            return True
        if isinstance(p, A.PUnaryTemporal):
            strong = p.op.startswith("s_")
            base = p.op[2:] if strong else p.op
            st = "strong" if strong else strength
            lo, hi = self._range(p)
            if base == "nexttime":
                j = i + lo
                return (not strong) if j >= n else self.neutral(p.prop, j, env, st)
            js = range(i + lo, n if hi is None else min(i + hi + 1, n))
            beyond = hi is None or i + hi >= n
            if base == "always":
                if strong and beyond:
                    return False
                return all(self.neutral(p.prop, j, env, st) for j in js)
            if any(self.neutral(p.prop, j, env, st) for j in js):
                return True
            return (not strong) if beyond else False
        if isinstance(p, A.PAbort):
            accept = p.op.endswith("accept_on")
            for j in range(i, n):
                if holds_bool(p.cond, self.rows[j], env):
                    if self._vacuous_by(p.prop, i, env, j):
                        return True         # ended with a vacuous success at tick j
                    return self.H(p.prop, i, env, Word(self.rows, j, TOP if accept else BOT, self.tail_len))
            return self.neutral(p.prop, i, env, strength)
        if isinstance(p, A.PBin) and p.op in _UNTIL:
            strong, with_ = p.op.startswith("s_"), p.op.endswith("_with")
            st = "strong" if strong else strength
            for k in range(i, n):
                if self.neutral(p.right, k, env, st) and all(
                        self.neutral(p.left, j, env, st) for j in range(i, k + (1 if with_ else 0))):
                    return True
            if strong:
                return False
            return all(self.neutral(p.left, j, env, st) for j in range(i, n))
        if isinstance(p, A.PBin):
            if p.op == "and":
                return self.neutral(p.left, i, env, strength) and self.neutral(p.right, i, env, strength)
            if p.op == "or":
                return self.neutral(p.left, i, env, strength) or self.neutral(p.right, i, env, strength)
            if p.op == "implies":
                return (not self.neutral(p.left, i, env, strength)) or self.neutral(p.right, i, env, strength)
            if p.op == "iff":
                return self.neutral(p.left, i, env, strength) == self.neutral(p.right, i, env, strength)
        if isinstance(p, A.PIf):
            row = self.rows[i] if i < n else None
            if row is None:
                return True
            if holds_bool(p.cond, row, env):
                return self.neutral(p.then, i, env, strength)
            return True if p.other is None else self.neutral(p.other, i, env, strength)
        raise NotImplementedError

    def _unstarted(self, p, strength):
        """Truth of a consequent that has not seen any tick when the trace ends."""
        if A.is_expr(p) or A.is_seq(p):
            return strength == "weak"
        if isinstance(p, A.PSeq):
            return (p.strength or strength) == "weak"
        if isinstance(p, A.PNot):
            return True             # a negation is not an obligation (engine rule)
        if isinstance(p, A.PImpl):
            # #-# / #=#: not a failure (reference behaviour)
            return True
        if isinstance(p, A.PUnaryTemporal):
            return not p.op.startswith("s_")
        if isinstance(p, A.PBin) and p.op in _UNTIL:
            return not p.op.startswith("s_")
        if isinstance(p, A.PAbort):
            return self._unstarted(p.prop, strength)
        if isinstance(p, A.PBin) and p.op == "and":
            return self._unstarted(p.left, strength) and self._unstarted(p.right, strength)
        if isinstance(p, A.PBin) and p.op == "or":
            return self._unstarted(p.left, strength) or self._unstarted(p.right, strength)
        if isinstance(p, A.PIf):
            return True
        raise NotImplementedError

    # -- vacuity (subset) ---------------------------------------------------
    def nonvacuous(self, p, i, env, t):
        """True/False for sequence/implication/if properties; None if not modelled."""
        if isinstance(p, A.PAbort):
            return self.nonvacuous(p.prop, i, env, t)
        if A.is_expr(p) or A.is_seq(p) or isinstance(p, A.PSeq):
            return True
        if isinstance(p, A.PImpl) and p.op in ("|->", "|=>"):
            shift = 0 if p.op == "|->" else 1
            w = Word(self.rows, t + 1, None, 0)
            any_match = False
            for k, e in self.M(p.seq, i, env, w):
                if k < i and shift:
                    k = i
                if k >= i and k + shift <= t:
                    any_match = True
                    nv = self.nonvacuous(p.prop, k + shift, e, t)
                    if nv is None:
                        return None
                    if nv:
                        return True
            return False if any_match or True else None
        if isinstance(p, A.PIf):
            if holds_bool(p.cond, self.rows[i], env):
                return self.nonvacuous(p.then, i, env, t)
            if p.other is None:
                return False
            return self.nonvacuous(p.other, i, env, t)
        return None

    # -- per-attempt verdicts -----------------------------------------------
    def attempt(self, i, strength="weak"):
        """(verdict, tick) of the attempt starting at tick i; verdict None if pending."""
        p = self.spec.prop
        n = len(self.rows)
        for t in range(i, n):
            self._memo.clear()
            if not self.H(p, i, (), Word(self.rows, t + 1, TOP, self.tail_len)):
                return FAIL, t
            if self.H(p, i, (), Word(self.rows, t + 1, BOT, self.tail_len)):
                nv = self.nonvacuous(p, i, (), t)
                return {False: VPASS, True: PASS, None: PASS_ANY}[nv], t
        self._memo.clear()
        return None, self.neutral(p, i, (), strength)

    def run(self, strength="weak"):
        """List of (start, verdict, tick-or-end-truth) for every tick."""
        return [(i,) + self.attempt(i, strength) for i in range(len(self.rows))]
