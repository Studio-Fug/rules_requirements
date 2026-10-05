// SPDX-License-Identifier: AGPL-3.0-or-later

#[test]
fn verifies_is_traced() {
    rr::verifies!("REQ-4"; level = "sil");
    assert_eq!(4 * 4, 16);
}

// Quarantine case: two ids in one test are deprecated but still all recorded
// (declared_ids_check asserts both reach the evidence), so attribution
// quarantines the case and it verifies neither requirement.
#[test]
fn multiple_ids() {
    rr::verifies!("REQ-4", "REQ-1");
}

#[test]
#[ignore = "demonstrates skipped tests"]
fn ignored_test() {
    rr::verifies!("REQ-4");
}

#[test]
fn trace_line_is_json() {
    // One id: the singular form (0.3).
    assert_eq!(rr::trace_line("t", &["REQ-1"], ""), "{\"test\":\"t\",\"requirement\":\"REQ-1\"}\n");
    assert_eq!(
        rr::trace_line("a::b", &["q\"x"], "sil"),
        "{\"test\":\"a::b\",\"requirement\":\"q\\\"x\",\"level\":\"sil\"}\n"
    );
    // Several ids (deprecated): the list form, which is quarantined.
    assert_eq!(
        rr::trace_line("a::b", &["REQ-1", "REQ-2"], ""),
        "{\"test\":\"a::b\",\"requirements\":[\"REQ-1\",\"REQ-2\"]}\n"
    );
}
