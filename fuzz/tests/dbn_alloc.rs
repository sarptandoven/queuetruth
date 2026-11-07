//! The `dbn` 0.71.0 up-front metadata allocation stays fixed (`regressions/dbn/alloc_*`): a DBN file of a few bytes
//! whose prelude announces up to `MAX_METADATA_BYTES` (256 MiB) of metadata made the decoder allocate the announced
//! length before reading any of it. `qt_ingest::dbn::FrameCheck` now rejects a length the file cannot hold before the
//! decoder sees it. This binary has its own global allocator that records the largest single allocation, so it holds
//! one test only.

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
fn dbn_metadata_announcing_more_than_the_file_holds_allocates_nothing_large() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("regressions/dbn");
    let mut files: Vec<_> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.file_name().is_some_and(|n| n.to_string_lossy().starts_with("alloc_")))
        .collect();
    files.sort();
    assert!(files.len() >= 2, "{} reproducers", files.len());
    for f in files {
        let data = std::fs::read(&f).unwrap();
        assert!(data.len() < 64, "{}: a few bytes", f.display());
        LARGEST.store(0, Ordering::Relaxed);
        qt_fuzz::dbn(&data);
        let mut ingest_input = data.clone();
        ingest_input[0] = 2;
        qt_fuzz::ingest(&ingest_input);
        let largest = LARGEST.load(Ordering::Relaxed);
        assert!(
            largest < 16 << 20,
            "{}: largest allocation {largest} bytes",
            f.display()
        );
    }
    qt_fuzz::remove_scratch();
}
