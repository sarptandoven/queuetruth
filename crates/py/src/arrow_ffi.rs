//! Arrow interchange with Python through the Arrow PyCapsule interface (`__arrow_c_schema__`,
//! `__arrow_c_stream__`) on arrow-rs's own C Data / C Stream types, so the bindings follow the workspace's arrow-rs
//! release instead of a binding crate's. pyarrow (>= 14) imports and exports these capsules; so do polars and others.

use std::ffi::CStr;
use std::sync::Mutex;

use arrow_array::ffi_stream::{ArrowArrayStreamReader, FFI_ArrowArrayStream};
use arrow_array::{RecordBatch, RecordBatchIterator, RecordBatchReader};
use arrow_schema::ffi::FFI_ArrowSchema;
use arrow_schema::{ArrowError, SchemaRef};
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::PyCapsule;

const SCHEMA_CAPSULE: &CStr = c"arrow_schema";
const STREAM_CAPSULE: &CStr = c"arrow_array_stream";

fn arrow_err(e: ArrowError) -> PyErr {
    PyValueError::new_err(e.to_string())
}

/// A schema, or a schema and its record batches exported once as a stream (the capsule protocol's producer).
#[pyclass(module = "queuetruth._core", frozen)]
struct ArrowExport {
    schema: SchemaRef,
    batches: Mutex<Option<Vec<RecordBatch>>>,
}

#[pymethods]
impl ArrowExport {
    fn __arrow_c_schema__<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyCapsule>> {
        let ffi = FFI_ArrowSchema::try_from(self.schema.as_ref()).map_err(arrow_err)?;
        PyCapsule::new_with_value(py, ffi, SCHEMA_CAPSULE)
    }

    /// The batches in their own schema (a `requested_schema` is not applied, which the protocol allows).
    #[pyo3(signature = (requested_schema = None))]
    fn __arrow_c_stream__<'py>(
        &self,
        py: Python<'py>,
        requested_schema: Option<Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyCapsule>> {
        let _ = requested_schema;
        let batches = self
            .batches
            .lock()
            .map_err(|_| PyValueError::new_err("Arrow export lock poisoned"))?
            .take()
            .ok_or_else(|| PyValueError::new_err("this Arrow stream was already consumed"))?;
        let reader = RecordBatchIterator::new(batches.into_iter().map(Ok), self.schema.clone());
        let ffi = FFI_ArrowArrayStream::new(Box::new(reader));
        PyCapsule::new_with_value(py, ffi, STREAM_CAPSULE)
    }
}

/// `pyarrow.schema` of an arrow-rs schema (field and schema metadata included).
pub fn schema_to_pyarrow(py: Python<'_>, schema: SchemaRef) -> PyResult<Bound<'_, PyAny>> {
    let export = Bound::new(
        py,
        ArrowExport {
            schema,
            batches: Mutex::new(None),
        },
    )?;
    py.import(intern!(py, "pyarrow"))?
        .getattr(intern!(py, "schema"))?
        .call1((export,))
}

/// `pyarrow.table` of record batches and their schema.
pub fn table_to_pyarrow(py: Python<'_>, batches: Vec<RecordBatch>, schema: SchemaRef) -> PyResult<Bound<'_, PyAny>> {
    let export = Bound::new(
        py,
        ArrowExport {
            schema,
            batches: Mutex::new(Some(batches)),
        },
    )?;
    py.import(intern!(py, "pyarrow"))?
        .getattr(intern!(py, "table"))?
        .call1((export,))
}

/// Record batches and schema of any object with `__arrow_c_stream__` (a `pyarrow.Table` or `RecordBatchReader`, a
/// polars `DataFrame`, ...).
pub fn stream_from_python(ob: &Bound<'_, PyAny>) -> PyResult<(Vec<RecordBatch>, SchemaRef)> {
    let py = ob.py();
    if !ob.hasattr(intern!(py, "__arrow_c_stream__"))? {
        return Err(PyTypeError::new_err(
            "expected an Arrow stream object (with __arrow_c_stream__), such as a pyarrow.Table",
        ));
    }
    let capsule = ob
        .call_method0(intern!(py, "__arrow_c_stream__"))?
        .cast_into::<PyCapsule>()
        .map_err(|_| PyTypeError::new_err("__arrow_c_stream__ did not return a PyCapsule"))?;
    let ptr = capsule
        .pointer_checked(Some(STREAM_CAPSULE))?
        .cast::<FFI_ArrowArrayStream>();
    // SAFETY: the capsule's name says it holds an ArrowArrayStream, and the capsule outlives this call. `from_raw`
    // moves the stream out and leaves a released one behind, so the capsule's destructor does not release it again.
    let stream = unsafe { FFI_ArrowArrayStream::from_raw(ptr.as_ptr()) };
    let reader = ArrowArrayStreamReader::try_new(stream).map_err(arrow_err)?;
    let schema = reader.schema();
    let batches = reader.collect::<Result<Vec<_>, _>>().map_err(arrow_err)?;
    Ok((batches, schema))
}
