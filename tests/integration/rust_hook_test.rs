// SPDX-License-Identifier: AGPL-3.0-or-later

#[test]
fn verifies_is_traced() {
    rr::verifies!("REQ-4"; level = "sil");
    assert_eq!(4 * 4, 16);
}

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
    assert_eq!(
        rr::trace_line("a::b", &["REQ-1", "q\"x"], "sil"),
        "{\"test\":\"a::b\",\"requirements\":[\"REQ-1\",\"q\\\"x\"],\"level\":\"sil\"}\n"
    );
    assert_eq!(rr::trace_line("t", &["REQ-1"], ""), "{\"test\":\"t\",\"requirements\":[\"REQ-1\"]}\n");
}
