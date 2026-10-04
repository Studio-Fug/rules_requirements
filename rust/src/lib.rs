// SPDX-License-Identifier: AGPL-3.0-or-later
//! Requirements traceability for Rust tests.
//!
//! ```
//! #[test]
//! fn cutoff_at_limit() {
//!     rr::verifies!("REQ-4");                       // the ONE requirement this test verifies
//!     assert!(true);
//! }
//!
//! #[test]
//! fn cutoff_logged() {
//!     rr::verifies!("REQ-6"; level = "sil");        // ...optionally with a level
//!     assert!(true);
//! }
//! ```
//!
//! The id is a declared tag: which requirement the case verifies is decided by
//! attribution. A test case verifies at most one requirement: several ids in
//! one test (in one call, over several calls, or in one string separated by
//! commas or whitespace) are deprecated; they are all
//! recorded, so attribution quarantines the case and it counts for none of
//! them, and `rr wrap` warns about them on stderr (RR-E101).
//!
//! libtest has no stable machine-readable output, so traces are recorded out of
//! band: each call appends one JSON line to the file named by `$RR_TRACE_FILE`,
//! keyed by the test's name (libtest names each test thread after the test).
//! The `rr_rust_test` Bazel macro (or `rr wrap --format libtest -- <binary>`)
//! sets the variable, runs the binary, parses its output, and writes JUnit with
//! the traces attached. Without the variable the macro is a no-op, so plain
//! `cargo test` is unaffected.

use std::fs::OpenOptions;
use std::io::Write;

/// Declare the ONE entity id the running test verifies (several are deprecated).
#[macro_export]
macro_rules! verifies {
    ($($id:expr),+ $(,)?) => {
        $crate::record(&[$($id),+], "")
    };
    ($($id:expr),+ ; level = $level:expr) => {
        $crate::record(&[$($id),+], $level)
    };
}

fn escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

/// The JSON trace line for one call (exposed for testing).
///
/// One id is written as `"requirement":"<id>"`. Several (a deprecated call)
/// keep the list form `"requirements":[...]`, which ingest still reads and
/// quarantines.
pub fn trace_line(test: &str, ids: &[&str], level: &str) -> String {
    let mut line = format!("{{\"test\":\"{}\"", escape(test));
    if let [id] = ids {
        line.push_str(&format!(",\"requirement\":\"{}\"", escape(id)));
    } else {
        let ids: Vec<String> = ids.iter().map(|i| format!("\"{}\"", escape(i))).collect();
        line.push_str(&format!(",\"requirements\":[{}]", ids.join(",")));
    }
    if !level.is_empty() {
        line.push_str(&format!(",\"level\":\"{}\"", escape(level)));
    }
    line.push_str("}\n");
    line
}

/// Implementation of [`verifies!`]; call the macro instead.
pub fn record(ids: &[&str], level: &str) {
    let Ok(path) = std::env::var("RR_TRACE_FILE") else {
        return;
    };
    let thread = std::thread::current();
    let test = thread.name().unwrap_or("");
    let line = trace_line(test, ids, level);
    // One write() per line with O_APPEND keeps concurrent tests' lines intact.
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(path) {
        let _ = f.write_all(line.as_bytes());
    }
}
