//! FST waveform reading for the offline checker (cocotbext.sva.TraceSampler).
//!
//! `FstFile` exposes the variables of the dump (full dotted name, handle,
//! width) and streams the value changes of chosen signals to a Python
//! callback, one time step at a time, so memory stays bounded on long dumps. Interpretation (names,
//! sampling) is left to the Python side, as for VCD.

use fst_reader::{FstFilter, FstHierarchyEntry, FstReader, FstSignalHandle, FstSignalValue};
use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;
use std::fs::File;
use std::io::BufReader;

#[pyclass(unsendable)]
pub struct FstFile {
    reader: FstReader<BufReader<File>>,
    vars: Vec<(String, u32, u32, bool)>,
    exponent: i8,
}

fn err(path: &str, e: impl std::fmt::Debug) -> PyErr {
    PyValueError::new_err(format!("{path}: cannot read FST file ({e:?})"))
}

#[pymethods]
impl FstFile {
    #[new]
    fn new(path: &str) -> PyResult<Self> {
        let f = File::open(path).map_err(|e| PyOSError::new_err(format!("{path}: {e}")))?;
        let mut reader =
            FstReader::open(BufReader::new(f)).map_err(|e| err(path, e))?;
        let header = reader.get_header();
        let mut scope: Vec<String> = Vec::new();
        let mut vars = Vec::new();
        reader
            .read_hierarchy(|entry| match entry {
                FstHierarchyEntry::Scope { name, .. } => scope.push(name),
                FstHierarchyEntry::UpScope => {
                    scope.pop();
                }
                FstHierarchyEntry::Var { name, length, handle, tpe, .. } => {
                    let mut full = scope.join(".");
                    if !full.is_empty() {
                        full.push('.');
                    }
                    full.push_str(&name);
                    let real = matches!(
                        tpe,
                        fst_reader::FstVarType::Real
                            | fst_reader::FstVarType::RealParameter
                            | fst_reader::FstVarType::RealTime
                            | fst_reader::FstVarType::ShortReal
                    );
                    vars.push((full, handle.get_index() as u32, length, real));
                }
                _ => {}
            })
            .map_err(|e| err(path, e))?;
        Ok(FstFile { reader, vars, exponent: header.timescale_exponent })
    }

    /// The timescale is 10**exponent seconds.
    #[getter]
    fn timescale_exponent(&self) -> i8 {
        self.exponent
    }

    /// [(full dotted name, handle, width, is real)] in hierarchy order.
    fn vars(&self) -> Vec<(String, u32, u32, bool)> {
        self.vars.clone()
    }

    /// Stream the value changes of the given handles, in one pass over the
    /// dump: callback(time, [(handle, value string), ...]) once per time
    /// step, in time order. Values are "0"/"1"/"x"/"z" strings (vectors MSB
    /// first). An exception raised by the callback stops the reading and
    /// propagates.
    fn read_all(&mut self, py: Python<'_>, handles: Vec<u32>, callback: PyObject) -> PyResult<()> {
        let filter = FstFilter::filter_signals(
            handles.iter().map(|&h| FstSignalHandle::from_index(h as usize)).collect(),
        );
        let mut cur_time: Option<u64> = None;
        let mut step: Vec<(u32, String)> = Vec::new();
        let flush = |t: u64, step: &mut Vec<(u32, String)>| -> PyResult<()> {
            callback.call1(py, (t, std::mem::take(step)))?;
            Ok(())
        };
        let res = self.reader.read_signals(&filter, |time, handle, value| -> Result<(), PyErr> {
            let v = match value {
                FstSignalValue::String(bytes) => String::from_utf8_lossy(bytes).to_ascii_lowercase(),
                FstSignalValue::Real(_) => {
                    return Err(PyValueError::new_err("real-valued signals are not supported"))
                }
            };
            match cur_time {
                Some(t) if t == time => {}
                Some(t) if time < t => {
                    return Err(PyValueError::new_err(format!(
                        "FST value changes out of time order ({time} after {t})"
                    )))
                }
                Some(t) => {
                    flush(t, &mut step)?;
                    cur_time = Some(time);
                }
                None => cur_time = Some(time),
            }
            step.push((handle.get_index() as u32, v));
            Ok(())
        });
        match res {
            Ok(()) => {}
            Err(fst_reader::ReadSignalsError::CallbackError(e)) => return Err(e),
            Err(e) => return Err(PyValueError::new_err(format!("cannot read FST signals ({e:?})"))),
        }
        if let Some(t) = cur_time {
            if !step.is_empty() {
                flush(t, &mut step)?;
            }
        }
        Ok(())
    }
}
