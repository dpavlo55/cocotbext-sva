//! Assertions with local variables (symbolic stepping, see `_symenv.py`).
//!
//! An attempt is a shape id plus the tuple of its distinct envs (local
//! values), kept as a Python object. Attempts with equal (shape, envs) merge.
//! The kernel mirrors the transitions cached in Python per (shape, input
//! bits): a decision tree over conditions on local values (generated Python
//! functions) whose leaves hold a verdict, or the next shape plus a
//! generated Python function computing the next envs. It calls those
//! functions, and falls back to a Python step for a branch not built yet;
//! the semantics stay defined in Python.

use pyo3::prelude::*;
use pyo3::types::{PyList, PyTuple};
use std::collections::HashMap;

use crate::{verdict, Event, FxMap, Meta, State, BUSY, DISABLED, NONE};

const UNKNOWN: u64 = u64::MAX;
const NODE_LIMIT: usize = 100_000;

enum Node {
    /// v: verdict or NONE; else next raw shape sid2, envs function mk (None:
    /// envs unchanged), ok: the result is canonical as is
    Leaf { v: i32, sid2: u32, mk: Option<Py<PyAny>>, ok: bool },
    /// a condition on local values (f(envs, c, R, cx, RX, H)) and the
    /// subtrees for false and true (None: not built yet)
    Query { f: Py<PyAny>, no: Option<Box<Node>>, yes: Option<Box<Node>> },
}

/// Mirror of a node of SymCache's tree: a leaf tuple (v, sid, mk, ok) or a
/// list [condition, key, if false, if true, answers]. Returns the node and
/// its size.
fn convert(obj: &Bound<'_, PyAny>) -> PyResult<(Node, usize)> {
    if let Ok(list) = obj.downcast::<PyList>() {
        let f = list.get_item(0)?.unbind();
        let mut size = 1;
        let mut child = |i: usize| -> PyResult<Option<Box<Node>>> {
            let c = list.get_item(i)?;
            if c.is_none() {
                return Ok(None);
            }
            let (n, k) = convert(&c)?;
            size += k;
            Ok(Some(Box::new(n)))
        };
        let no = child(2)?;
        let yes = child(3)?;
        return Ok((Node::Query { f, no, yes }, size));
    }
    let (v, sid2, mk, ok): Leaf = obj.extract()?;
    Ok((Node::Leaf { v: v.unwrap_or(NONE), sid2: sid2.unwrap_or(0), mk, ok }, 1))
}

/// (envs, c, R, cx, RX, H)
fn with_envs<'py>(py: Python<'py>, envs: &Py<PyAny>, args: &Bound<'py, PyTuple>) -> PyResult<Bound<'py, PyTuple>> {
    let mut call: Vec<Bound<'py, PyAny>> = Vec::with_capacity(6);
    call.push(envs.bind(py).clone());
    call.extend(args.iter());
    PyTuple::new(py, call)
}

pub(crate) struct Tok {
    pub sid: u32,
    pub env: Py<PyAny>,
    hash: isize,
    pub meta: Meta,
}

pub(crate) struct EnvPart {
    init_sid: u32,
    init_envs: Py<PyAny>,
    init_hash: isize,
    pub idle_mask: u64, // atoms an idle tick reads (the first step, disable)
    pub all_mask: u64,  // all the monitor's atoms
    disable: u64,       // disable iff bit (0: none)
    need: Vec<u64>,     // per shape: atoms its step reads (UNKNOWN: not asked yet)
    trans: Vec<FxMap<Node>>,
    nodes: usize,
    pub toks: Vec<Tok>,
    spare: Vec<Tok>,
    step_cb: Py<PyAny>,  // (sid, envs, bits, args) -> (v, sid2, envs2, tree node)
    need_cb: Py<PyAny>,  // sid -> need
    canon_cb: Py<PyAny>, // (raw sid, envs) -> (sid, envs)
}

type Step = (i32, u32, Option<Py<PyAny>>, isize);
type Leaf = (Option<i32>, Option<u32>, Option<Py<PyAny>>, bool);

impl EnvPart {
    #[allow(clippy::too_many_arguments)]
    pub fn new(py: Python<'_>, init_sid: u32, init_envs: Py<PyAny>, idle_mask: u64, all_mask: u64,
               disable: u64, step_cb: Py<PyAny>, need_cb: Py<PyAny>, canon_cb: Py<PyAny>) -> PyResult<Self> {
        let init_hash = init_envs.bind(py).hash()?;
        Ok(EnvPart { init_sid, init_envs, init_hash, idle_mask, all_mask, disable, need: Vec::new(),
                     trans: Vec::new(), nodes: 0, toks: Vec::new(), spare: Vec::new(), step_cb, need_cb, canon_cb })
    }

    fn need_of(&mut self, py: Python<'_>, sid: u32) -> PyResult<u64> {
        let s = sid as usize;
        if s >= self.need.len() {
            self.need.resize(s + 1, UNKNOWN);
            self.trans.resize_with(s + 1, FxMap::default);
        }
        if self.need[s] == UNKNOWN {
            self.need[s] = self.need_cb.call1(py, (sid,))?.extract(py)?;
        }
        Ok(self.need[s])
    }

    fn canonical(&self, py: Python<'_>, rsid: u32, envs: Py<PyAny>, ok: bool) -> PyResult<Step> {
        if ok {
            let h = envs.bind(py).hash()?;
            return Ok((NONE, rsid, Some(envs), h));
        }
        let (sid, envs): (u32, Py<PyAny>) = self.canon_cb.call1(py, (rsid, envs))?.extract(py)?;
        let h = envs.bind(py).hash()?;
        Ok((NONE, sid, Some(envs), h))
    }

    /// One tick of the attempt (sid, envs): (verdict or NONE, next sid, next
    /// envs, their hash).
    fn advance(&mut self, py: Python<'_>, sid: u32, envs: &Py<PyAny>, hash: isize, bits: u64,
               args: &Bound<'_, PyTuple>) -> PyResult<Step> {
        let key = bits & self.need_of(py, sid)?;
        let s = sid as usize;
        let mut call: Option<Bound<'_, PyTuple>> = None;
        let mut node = self.trans[s].get(&key);
        let hit = loop {
            match node {
                None => break None,
                Some(Node::Leaf { v, sid2, mk, ok }) => break Some((*v, *sid2, mk.as_ref().map(|f| f.clone_ref(py)), *ok)),
                Some(Node::Query { f, no, yes }) => {
                    if call.is_none() {
                        call = Some(with_envs(py, envs, args)?);
                    }
                    let r = f.bind(py).call1(call.clone().unwrap())?.is_truthy()?;
                    node = if r { yes.as_deref() } else { no.as_deref() };
                }
            }
        };
        if let Some((v, sid2, mk, ok)) = hit {
            if v != NONE {
                return Ok((v, 0, None, 0));
            }
            let Some(mk) = mk else {
                if ok {
                    return Ok((NONE, sid2, Some(envs.clone_ref(py)), hash));
                }
                return self.canonical(py, sid2, envs.clone_ref(py), false);
            };
            let call = match call {
                Some(c) => c,
                None => with_envs(py, envs, args)?,
            };
            let e2 = mk.call1(py, call)?;
            return self.canonical(py, sid2, e2, ok);
        }
        // not built yet: a Python step builds it; mirror the new tree
        let r = self.step_cb.call1(py, (sid, envs.clone_ref(py), bits, args.clone()))?;
        let (v, sid2, e2, tree): (Option<i32>, Option<u32>, Py<PyAny>, Py<PyAny>) = r.extract(py)?;
        if self.nodes >= NODE_LIMIT {
            for t in self.trans.iter_mut() {
                t.clear();
            }
            self.nodes = 0;
        }
        let (n, size) = convert(tree.bind(py))?;
        self.trans[s].insert(key, n);
        self.nodes += size;
        match v {
            Some(v) => Ok((v, 0, None, 0)),
            None => {
                let h = e2.bind(py).hash()?;
                Ok((NONE, sid2.unwrap_or(0), Some(e2), h))
            }
        }
    }
}

const SCAN: usize = 16; // attempts searched linearly before an index is built

/// Add an attempt to the next tick's list, merging it with an equal one.
fn insert(py: Python<'_>, nxt: &mut Vec<Tok>, index: &mut HashMap<(u32, isize), Vec<usize>>, tok: Tok,
          max_starts: usize) -> PyResult<()> {
    let k = (tok.sid, tok.hash);
    let same = |o: &Tok| -> PyResult<bool> {
        Ok(o.sid == tok.sid && o.hash == tok.hash && (o.env.is(&tok.env) || o.env.bind(py).eq(tok.env.bind(py))?))
    };
    let mut found = None;
    if index.is_empty() && nxt.len() <= SCAN {
        for (i, o) in nxt.iter().enumerate() {
            if same(o)? {
                found = Some(i);
                break;
            }
        }
    } else {
        if index.is_empty() {
            for (i, o) in nxt.iter().enumerate() {
                index.entry((o.sid, o.hash)).or_default().push(i);
            }
        }
        if let Some(list) = index.get(&k) {
            for &i in list {
                if same(&nxt[i])? {
                    found = Some(i);
                    break;
                }
            }
        }
    }
    match found {
        Some(i) => nxt[i].meta.merge(tok.meta, max_starts),
        None => {
            if !index.is_empty() {
                index.entry(k).or_default().push(nxt.len());
            }
            nxt.push(tok);
        }
    }
    Ok(())
}

/// One tick of a local-variable monitor (as Monitor.tick_sym).
#[allow(clippy::too_many_arguments)]
pub(crate) fn step(py: Python<'_>, e: &mut EnvPart, st: &mut State, events: &mut Vec<Event>, started: &mut bool,
                   max_starts: usize, args: &Bound<'_, PyTuple>, j: usize, bits: u64, start: u64,
                   tick: u64) -> PyResult<()> {
    let mut toks = std::mem::take(&mut e.toks);
    let had = !toks.is_empty();
    if !had && start == 0 {
        st.idle_v = NONE;
        return Ok(());
    }
    if bits & e.disable != 0 {
        for t in toks.drain(..) {
            verdict(st, events, j, DISABLED, Some(&t.meta), tick);
        }
        e.spare = toks;
        st.idle_v = if had { BUSY } else if start != 0 { -DISABLED } else { NONE };
        if start != 0 {
            verdict(st, events, j, DISABLED, None, tick);
        }
        return Ok(());
    }
    let mut nxt = std::mem::take(&mut e.spare);
    nxt.clear();
    let mut index: HashMap<(u32, isize), Vec<usize>> = HashMap::new();
    for t in toks.drain(..) {
        let (v, sid2, env2, h2) = e.advance(py, t.sid, &t.env, t.hash, bits, args)?;
        if v != NONE {
            verdict(st, events, j, v, Some(&t.meta), tick);
        } else if let Some(env) = env2 {
            insert(py, &mut nxt, &mut index, Tok { sid: sid2, env, hash: h2, meta: t.meta }, max_starts)?;
        }
    }
    let mut iv = NONE;
    if start != 0 {
        let init = e.init_envs.clone_ref(py);
        let (v, sid2, env2, h2) = e.advance(py, e.init_sid, &init, e.init_hash, bits, args)?;
        if v != NONE {
            iv = -v;
            verdict(st, events, j, v, None, tick);
        } else if let Some(env) = env2 {
            *started = true;
            insert(py, &mut nxt, &mut index, Tok { sid: sid2, env, hash: h2, meta: Meta::one(tick) }, max_starts)?;
        }
    }
    e.toks = nxt;
    e.spare = toks; // (empty: its allocation is reused next tick)
    st.idle_v = if had || !e.toks.is_empty() { BUSY } else { iv };
    Ok(())
}
