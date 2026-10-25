//! The `dbn` 0.71.0 up-front metadata allocation (reported, not fixed here; `known-crashes/dbn-alloc/`): a DBN file
//! of a few bytes whose prelude announces up to `MAX_METADATA_BYTES` (256 MiB) of metadata makes the decoder
//! allocate the announced length before reading any of it. This binary has its own global allocator that records the
//! largest single allocation, so it holds one test only.
//!
//! When this test fails because the allocation is gone, `DbnReader::open` (or the decoder) was fixed: move the inputs
//! to `regressions/dbn/` and drop `dbn_known_metadata_alloc` from the `dbn` and `ingest` harness filters.

use std::alloc::{GlobalAlloc, Layout, System};
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};

/// The system allocator, recording the largest single request.
struct Peak;

static LARGEST: AtomicUsize = AtomicUsize::new(0);

// SAFETY: forwards every call to `System` unchanged; only records the requested sizes.
unsafe impl GlobalAlloc for Peak {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        LARGEST.fetch_max(layout.size(), Ordering::Relaxed);
        // SAFETY: the caller upholds `GlobalAlloc::alloc`'s contract, which `System` shares.
        unsafe { System.alloc(layout) }
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        LARGEST.fetch_max(layout.size(), Ordering::Relaxed);
        // SAFETY: as for `alloc`.
        unsafe { System.alloc_zeroed(layout) }
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        // SAFETY: `ptr` came from `System` through this allocator with this layout.
        unsafe { System.dealloc(ptr, layout) }
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        LARGEST.fetch_max(new_size, Ordering::Relaxed);
        // SAFETY: as for `dealloc`; `new_size` obeys the caller's contract.
        unsafe { System.realloc(ptr, layout, new_size) }
    }
}

#[global_allocator]
static GLOBAL: Peak = Peak;

#[test]
fn known_dbn_metadata_allocation_still_reproduces() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("known-crashes/dbn-alloc");
    let mut files: Vec<_> = std::fs::read_dir(&dir).unwrap().map(|e| e.unwrap().path()).collect();
    files.sort();
    assert!(files.len() >= 2, "{} reproducers", files.len());
    for f in files {
        let data = std::fs::read(&f).unwrap();
        assert!(data.len() < 64, "{}: a few bytes", f.display());
        assert!(qt_fuzz::dbn_known_metadata_alloc(&data[1..]), "{}", f.display());
        // The filtered harnesses skip it without allocating.
        LARGEST.store(0, Ordering::Relaxed);
        qt_fuzz::dbn(&data);
        let mut ingest_input = data.clone();
        ingest_input[0] = 2;
        qt_fuzz::ingest(&ingest_input);
        assert!(LARGEST.load(Ordering::Relaxed) < 16 << 20, "{}: filtered", f.display());
        // Unfiltered: an input error after one allocation of the announced 256 MiB.
        LARGEST.store(0, Ordering::Relaxed);
        qt_fuzz::dbn_unfiltered(&data);
        let largest = LARGEST.load(Ordering::Relaxed);
        assert!(
            largest >= 256 << 20,
            "{}: largest allocation {largest} bytes; the up-front allocation is gone",
            f.display()
        );
    }
    qt_fuzz::remove_scratch();
}
