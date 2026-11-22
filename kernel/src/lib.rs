//! Optional compiled kernel for cocotbext-sva (plus the FST reader of the
//! offline checker, in fst.rs).
//!
//! Holds, for the fast-path assertions of one clocking domain, everything the
//! per-tick evaluation touches: the current global state, the cached
//! transitions (global state, input bits) -> successor, the metadata of the
//! attempts in flight (count, start ticks), the counters and the idle state
//! used by idle sleep. Transitions are not computed here: on a cache miss the
//! kernel calls the monitor's Python `_kexpand`, which stays the only
//! definition of the semantics.
//!
//! One tick is two calls: `tick_a` with the idle-state input bits of every
//! monitor (steps the idle monitors, returns which busy-state inputs are
//! needed), then, if needed, `tick_b` with those (steps the busy monitors and
//! returns the events to report).
//!
//! A gated monitor (multi-clock assertion) starts an attempt only at the
//! ticks where its gate, a phase A atom (the leading clock ticks), is true;
//! the kernel counts those attempts.

mod fst;

use pyo3::prelude::*;
use pyo3::types::{PyList, PyTuple};
use std::collections::HashMap;
use std::hash::{BuildHasherDefault, Hasher};

const PASS: i32 = 1;
const VPASS: i32 = 2;
const FAIL: i32 = 3;
const DISABLED: i32 = 4;
const VFAIL: i32 = 5;
const NONE: i32 = i32::MIN; // no new attempt / idle_v None
const BUSY: i32 = 1; // idle_v of a monitor with attempts in flight
const NO_GATE: u32 = u32::MAX;
const CACHE_LIMIT: usize = 100_000;

#[derive(Default)]
struct Fx(u64);

impl Hasher for Fx {
    fn finish(&self) -> u64 {
        self.0
    }
    fn write(&mut self, bytes: &[u8]) {
        for b in bytes {
            self.write_u64(*b as u64);
        }
    }
    fn write_u64(&mut self, i: u64) {
        self.0 = (self.0.rotate_left(5) ^ i).wrapping_mul(0x517c_c1b7_2722_0a95);
    }
}

type FxMap<V> = HashMap<u64, V, BuildHasherDefault<Fx>>;

#[derive(Clone, Default)]
struct Meta {
    count: u64,
    s0: u64,        // first start tick (stored inline: no allocation for single attempts)
    rest: Vec<u64>, // further start ticks of merged attempts
}

impl Meta {
    fn one(tick: u64) -> Meta {
        Meta { count: 1, s0: tick, rest: Vec::new() }
    }

    fn starts(&self) -> Vec<u64> {
        let mut v = Vec::with_capacity(1 + self.rest.len());
        v.push(self.s0);
        v.extend_from_slice(&self.rest);
        v
    }

    fn merge(&mut self, other: Meta, max_starts: usize) {
        self.count += other.count;
        let mut v = self.starts();
        v.extend(other.starts());
        if v.len() > max_starts {
            v.sort_unstable();
            v.truncate(max_starts.max(1));
        }
        self.s0 = v[0];
        self.rest = v[1..].to_vec();
    }
}

struct Trans {
    next: u32,
    moves: Box<[i32]>, // per attempt in flight: destination index, or -verdict
    new: i32,          // the new attempt: destination, -verdict, or NONE
    perm: Option<Box<[i32]>>, // destination j <- source perm[j] (-1: new); None: attempts merge
}

/// Transition tables of one monitor.
#[derive(Default)]
struct Tables {
    need: Vec<u64>, // per global state id: atoms it reads
    len: Vec<u32>,  // per global state id: attempts in flight
    trans: Vec<FxMap<u32>>,
    tr: Vec<Trans>,
}

/// Evaluation state of one monitor.
struct State {
    active: bool,
    amap: Box<[u32]>, // global index of each of the monitor's atoms (< na: phase A word bit)
    bneed: u64,       // phase B words the monitor's atoms use (0: all in phase A)
    sb: u64,
    start_bit: u64,
    gate: u32,     // phase A atom that must be true to start an attempt (NO_GATE: none)
    gated: u64,    // attempts started by a gated monitor
    state: u32,
    meta: Vec<Meta>,
    spare: Vec<Meta>, // scratch buffer for the next meta vector
    passes: u64,
    vacuous: u64,
    failures: u64,
    disabled: u64,
    idle_v: i32,
    cover: bool,
    wants_pass: bool,
    wants_vac: bool,
}

impl State {
    /// The start bit of this tick (gate applied, gated attempts counted).
    #[inline]
    fn start(&mut self, a: &[u64]) -> u64 {
        if self.gate == NO_GATE || self.sb == 0 {
            return self.sb;
        }
        if (a[(self.gate >> 6) as usize] >> (self.gate & 63)) & 1 == 0 {
            return 0;
        }
        self.gated += 1;
        self.sb
    }

    fn sleepable(&self) -> bool {
        match self.idle_v {
            NONE => true,
            BUSY => false,
            v if v == -VPASS => !self.wants_vac,
            v if v == -PASS => !(self.cover || self.wants_pass),
            v if v == -FAIL || v == -VFAIL => self.cover,
            v if v == -DISABLED => true,
            _ => false,
        }
    }
}

struct Mon {
    tab: Tables,
    st: State,
    expand: Py<PyAny>,
}

struct Event {
    j: u32,
    v: i32,
    count: u64,
    starts: Vec<u64>,
    tick: u64,
}

fn verdict(st: &mut State, events: &mut Vec<Event>, j: usize, v: i32, meta: Option<&Meta>, tick: u64) {
    let count = meta.map_or(1, |m| m.count);
    let report = match v {
        VPASS => {
            st.vacuous += count;
            st.wants_vac
        }
        PASS => {
            st.passes += count;
            st.cover || st.wants_pass
        }
        FAIL | VFAIL => {
            if st.cover {
                return; // a cover attempt that does not match is not an error
            }
            st.failures += count;
            true
        }
        _ => {
            st.disabled += count;
            false
        }
    };
    if report {
        events.push(Event {
            j: j as u32,
            v: if v == VFAIL { FAIL } else { v },
            count,
            starts: meta.map_or_else(|| vec![tick], |m| m.starts()),
            tick,
        });
    }
}

#[pyclass(module = "cocotbext_sva_kernel")]
struct Kernel {
    mons: Vec<Mon>,
    busy: Vec<usize>,
    events: Vec<Event>,
    started: bool,
    max_starts: usize, // start ticks remembered per merged attempt
    na: u32,           // number of phase A atoms
    a: Vec<u64>,       // phase A atom words of the current tick
}

/// The monitor's input bits for the atoms in `need` (start bit not included).
#[inline]
fn gather(amap: &[u32], need: u64, na: u32, a: &[u64], b: &[u64]) -> u64 {
    let mut key = 0u64;
    let mut m = need;
    while m != 0 {
        let i = m.trailing_zeros();
        let g = amap[i as usize];
        let bit = if g < na {
            a[(g >> 6) as usize] >> (g & 63)
        } else {
            let h = g - na;
            b[(h >> 6) as usize] >> (h & 63)
        };
        key |= (bit & 1) << i;
        m &= m - 1;
    }
    key
}

impl Kernel {
    fn trans_index(&mut self, py: Python<'_>, j: usize, key: u64) -> PyResult<usize> {
        let m = &mut self.mons[j];
        let s = m.st.state as usize;
        if let Some(&i) = m.tab.trans[s].get(&key) {
            return Ok(i as usize);
        }
        let r = m.expand.call1(py, (m.st.state, key))?;
        let (next, need, len, moves, new, perm): (u32, u64, u32, Vec<i32>, Option<i32>, Option<Vec<i32>>) =
            r.extract(py)?;
        let tab = &mut m.tab;
        if tab.tr.len() >= CACHE_LIMIT {
            for t in tab.trans.iter_mut() {
                t.clear();
            }
            tab.tr.clear();
        }
        while tab.need.len() <= next as usize {
            tab.need.push(0);
            tab.len.push(0);
            tab.trans.push(FxMap::default());
        }
        tab.need[next as usize] = need;
        tab.len[next as usize] = len;
        let idx = tab.tr.len();
        tab.tr.push(Trans {
            next,
            moves: moves.into_boxed_slice(),
            new: new.unwrap_or(NONE),
            perm: perm.map(|p| p.into_boxed_slice()),
        });
        tab.trans[s].insert(key, idx as u32);
        Ok(idx)
    }

    fn step(&mut self, py: Python<'_>, j: usize, key: u64, tick: u64) -> PyResult<()> {
        let ti = self.trans_index(py, j, key)?;
        let Kernel { mons, events, started, max_starts, .. } = self;
        let max_starts = *max_starts;
        let Mon { tab, st, .. } = &mut mons[j];
        let t = &tab.tr[ti];
        st.state = t.next;
        if t.moves.is_empty() {
            // nothing in flight before this tick
            if t.new == NONE {
                st.idle_v = NONE;
            } else if t.new < 0 {
                st.idle_v = t.new;
                verdict(st, events, j, -t.new, None, tick);
            } else {
                st.idle_v = BUSY;
                st.meta.clear();
                st.meta.push(Meta::one(tick));
                *started = true;
            }
            return Ok(());
        }
        st.idle_v = BUSY;
        let mut old = std::mem::take(&mut st.meta);
        for (i, &mv) in t.moves.iter().enumerate() {
            if mv < 0 {
                verdict(st, events, j, -mv, Some(&old[i]), tick);
            }
        }
        if let Some(perm) = &t.perm {
            if t.new != NONE && t.new < 0 {
                verdict(st, events, j, -t.new, None, tick);
            }
            let mut meta = std::mem::take(&mut st.spare);
            meta.clear();
            for &p in perm.iter() {
                if p >= 0 {
                    meta.push(std::mem::take(&mut old[p as usize]));
                } else {
                    meta.push(Meta::one(tick));
                    *started = true;
                }
            }
            st.meta = meta;
            old.clear();
            st.spare = old; // reuse the allocation next tick
        } else {
            let n = tab.len[t.next as usize] as usize;
            let mut meta: Vec<Option<Meta>> = vec![None; n];
            for (i, &mv) in t.moves.iter().enumerate() {
                if mv >= 0 {
                    let m = std::mem::take(&mut old[i]);
                    match &mut meta[mv as usize] {
                        Some(d) => d.merge(m, max_starts),
                        slot => *slot = Some(m),
                    }
                }
            }
            if t.new != NONE {
                if t.new >= 0 {
                    let m = Meta::one(tick);
                    *started = true;
                    match &mut meta[t.new as usize] {
                        Some(d) => d.merge(m, max_starts),
                        slot => *slot = Some(m),
                    }
                } else {
                    verdict(st, events, j, -t.new, None, tick);
                }
            }
            st.meta = meta.into_iter().map(|m| m.unwrap_or_default()).collect();
        }
        Ok(())
    }

    fn take_events(&mut self, py: Python<'_>) -> PyResult<PyObject> {
        if self.events.is_empty() {
            return Ok(py.None());
        }
        // report in assertion order (idle assertions were stepped before busy ones)
        self.events.sort_by_key(|e| e.j);
        let list = PyList::empty(py);
        for e in self.events.drain(..) {
            let starts = PyTuple::new(py, e.starts)?;
            list.append((e.j, e.v, e.count, starts, e.tick))?;
        }
        Ok(list.into_any().unbind())
    }
}

#[pymethods]
impl Kernel {
    #[new]
    #[pyo3(signature = (max_starts=16))]
    fn new(max_starts: usize) -> Self {
        Kernel { mons: Vec::new(), busy: Vec::new(), events: Vec::new(), started: false, max_starts,
                 na: 0, a: Vec::new() }
    }

    /// Register a monitor (idle, global state 0); returns its index.
    #[pyo3(signature = (start_bit, need0, cover, wants_pass, wants_vac, expand))]
    fn add(&mut self, start_bit: u64, need0: u64, cover: bool, wants_pass: bool, wants_vac: bool,
           expand: Py<PyAny>) -> usize {
        let mut tab = Tables::default();
        tab.need.push(need0);
        tab.len.push(0);
        tab.trans.push(FxMap::default());
        self.mons.push(Mon {
            tab,
            st: State {
                active: true,
                amap: Box::new([]),
                bneed: 0,
                sb: start_bit,
                start_bit,
                gate: NO_GATE,
                gated: 0,
                state: 0,
                meta: Vec::new(),
                spare: Vec::new(),
                passes: 0,
                vacuous: 0,
                failures: 0,
                disabled: 0,
                idle_v: BUSY,
                cover,
                wants_pass,
                wants_vac,
            },
            expand,
        });
        self.mons.len() - 1
    }

    /// Number of phase A atoms (the atoms idle states read; always passed).
    fn set_layout(&mut self, na: u32) {
        self.na = na;
    }

    /// Where the monitor's atoms are in the atom words, and which phase B
    /// words it needs.
    fn set_inputs(&mut self, j: usize, amap: Vec<u32>, bneed: u64) {
        self.mons[j].st.amap = amap.into_boxed_slice();
        self.mons[j].st.bneed = bneed;
    }

    /// Gate the monitor's attempts on a phase A atom (None: ungated).
    #[pyo3(signature = (j, atom))]
    fn set_gate(&mut self, j: usize, atom: Option<u32>) {
        self.mons[j].st.gate = atom.unwrap_or(NO_GATE);
    }

    /// Attempts started by a gated monitor.
    fn attempts(&self, j: usize) -> u64 {
        self.mons[j].st.gated
    }

    fn set_enabled(&mut self, j: usize, enabled: bool) {
        let st = &mut self.mons[j].st;
        st.sb = if enabled { st.start_bit } else { 0 };
    }

    fn deactivate(&mut self, j: usize) {
        self.mons[j].st.active = false;
    }

    /// Step every monitor whose inputs are all phase A atoms (idle monitors
    /// and most busy ones), given the phase A atom words. Returns bit 0:
    /// events pending; bit 1: an attempt stayed in flight (its start time is
    /// needed); bit 2: some monitor is not sleepable; bits 3..: the phase B
    /// words tick_b needs.
    fn tick_a(&mut self, py: Python<'_>, a: Vec<u64>, tick: u64) -> PyResult<u64> {
        self.busy.clear();
        self.started = false;
        self.a = a;
        let na = self.na;
        let mut need: u64 = 0;
        for j in 0..self.mons.len() {
            let m = &self.mons[j];
            let st = &m.st;
            if !st.active {
                continue;
            }
            if st.state == 0 || st.bneed == 0 {
                let key = gather(&st.amap, m.tab.need[st.state as usize], na, &self.a, &[]);
                let key = key | self.mons[j].st.start(&self.a);
                self.step(py, j, key, tick)?;
            } else {
                need |= st.bneed;
                self.busy.push(j);
            }
        }
        let mut r = (need << 3) | ((self.started as u64) << 1) | (!self.events.is_empty() as u64);
        if need != 0 || self.mons.iter().any(|m| m.st.active && !m.st.sleepable()) {
            r |= 4;
        }
        Ok(r)
    }

    /// Step the remaining busy monitors, given the phase B words asked for;
    /// returns the events of the tick ((index, verdict, count, start ticks,
    /// tick), ...) or None.
    fn tick_b(&mut self, py: Python<'_>, b: Vec<u64>, tick: u64) -> PyResult<PyObject> {
        let busy = std::mem::take(&mut self.busy);
        let na = self.na;
        for &j in busy.iter() {
            let m = &self.mons[j];
            let key = gather(&m.st.amap, m.tab.need[m.st.state as usize], na, &self.a, &b);
            let key = key | self.mons[j].st.start(&self.a);
            self.step(py, j, key, tick)?;
        }
        self.busy = busy;
        self.take_events(py)
    }

    /// True if an attempt stayed in flight during the last tick_b.
    fn started(&self) -> bool {
        self.started
    }

    /// (passes, vacuous, failures, disabled)
    fn stats(&self, j: usize) -> (u64, u64, u64, u64) {
        let st = &self.mons[j].st;
        (st.passes, st.vacuous, st.failures, st.disabled)
    }

    fn sleepable(&self, j: usize) -> bool {
        self.mons[j].st.sleepable()
    }

    fn idle_v(&self, j: usize) -> Option<i32> {
        let v = self.mons[j].st.idle_v;
        if v == NONE { None } else { Some(v) }
    }

    /// Account for k idle ticks identical to the last one (attempt counts are
    /// kept on the Python side).
    fn skip(&mut self, j: usize, k: u64) {
        let st = &mut self.mons[j].st;
        match st.idle_v {
            v if v == -VPASS => st.vacuous += k,
            v if v == -PASS => st.passes += k,
            v if v == -DISABLED => st.disabled += k,
            _ => {}
        }
    }

    /// skip(j, k) for every active monitor.
    fn skip_all(&mut self, k: u64) {
        for j in 0..self.mons.len() {
            if self.mons[j].st.active {
                self.skip(j, k);
            }
        }
    }

    /// (global state id, ((count, start ticks), ...)) of the attempts in flight.
    fn peek(&self, py: Python<'_>, j: usize) -> PyResult<(u32, PyObject)> {
        let st = &self.mons[j].st;
        let list = PyList::empty(py);
        for m in st.meta.iter() {
            list.append((m.count, PyTuple::new(py, m.starts())?))?;
        }
        Ok((st.state, list.into_any().unbind()))
    }

    /// Drop the attempts in flight (back to the idle global state).
    fn reset(&mut self, j: usize) {
        let st = &mut self.mons[j].st;
        st.state = 0;
        st.meta.clear();
    }

    /// Oldest start tick of any attempt in flight.
    fn min_start(&self) -> Option<u64> {
        self.mons
            .iter()
            .filter(|m| m.st.active)
            .flat_map(|m| m.st.meta.iter().flat_map(|x| x.starts()))
            .min()
    }
}

#[pymodule]
fn cocotbext_sva_kernel(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Kernel>()?;
    m.add_class::<fst::FstFile>()?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
