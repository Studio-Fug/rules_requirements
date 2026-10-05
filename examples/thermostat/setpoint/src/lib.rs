// SPDX-License-Identifier: AGPL-3.0-or-later
//! Setpoint parsing for the wall panel.

use std::fmt;

pub const MIN_C: f64 = 5.0;
pub const MAX_C: f64 = 30.0;

#[derive(Debug, PartialEq)]
pub enum Error {
    Syntax(String),
    OutOfRange(f64),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Syntax(s) => write!(f, "cannot parse setpoint {s:?}; expected e.g. 21.5C or 70F"),
            Error::OutOfRange(c) => write!(f, "setpoint {c:.1} °C is outside {MIN_C}-{MAX_C} °C"),
        }
    }
}

// @rr(REQ-3, REQ-4): Explicit unit required; the result is range-checked in °C.
pub fn parse(text: &str) -> Result<f64, Error> {
    let t = text.trim();
    let (num, unit) = t.split_at(t.len().saturating_sub(1));
    let value: f64 = num.trim().parse().map_err(|_| Error::Syntax(text.to_string()))?;
    let celsius = match unit {
        "C" | "c" => value,
        "F" | "f" => (value - 32.0) * 5.0 / 9.0,
        _ => return Err(Error::Syntax(text.to_string())),
    };
    if !(MIN_C..=MAX_C).contains(&celsius) {
        return Err(Error::OutOfRange(celsius));
    }
    Ok(celsius)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_celsius_and_fahrenheit() {
        rr::verifies!("REQ-3");
        assert_eq!(parse("21.5C"), Ok(21.5));
        assert_eq!(parse(" 20 c "), Ok(20.0));
        assert!((parse("70F").unwrap() - 21.111).abs() < 1e-3);
    }

    #[test]
    fn requires_a_unit() {
        rr::verifies!("REQ-3");
        assert!(matches!(parse("21"), Err(Error::Syntax(_))));
        assert!(matches!(parse("warm"), Err(Error::Syntax(_))));
        assert!(matches!(parse(""), Err(Error::Syntax(_))));
    }

    #[test]
    fn rejects_out_of_range() {
        rr::verifies!("REQ-4");
        assert_eq!(parse("80C"), Err(Error::OutOfRange(80.0)));
        assert!(matches!(parse("30.1C"), Err(Error::OutOfRange(_))));
        assert!(matches!(parse("4.9C"), Err(Error::OutOfRange(_))));
        assert_eq!(parse("5C"), Ok(5.0));
        assert_eq!(parse("30C"), Ok(30.0));
    }

    #[test]
    fn checks_the_range_after_converting() {
        rr::verifies!("REQ-4");
        assert!(matches!(parse("40F"), Err(Error::OutOfRange(_))));
        assert_eq!(parse("41F"), Ok(5.0));
        assert_eq!(parse("86F"), Ok(30.0));
        assert!(matches!(parse("87F"), Err(Error::OutOfRange(_))));
    }
}
