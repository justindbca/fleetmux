//! The JSON-lines event log at `$FT_PROBE_LOG` (README "Event log"): one object per line, appended and
//! flushed per event. Written by hand (the probe depends on crossterm and std only).
use std::fs::{File, OpenOptions};
use std::io::{self, Write};
use std::path::Path;
use std::time::Instant;

/// A field value.
pub enum J<'a> {
    Int(u64),
    Bool(bool),
    Str(&'a str),
    Null,
}

pub struct Log {
    file: File,
    start: Instant,
}

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn quote(s: &str, out: &mut String) {
    out.push('"');
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
    out.push('"');
}

impl Log {
    pub fn open(path: &Path, start: Instant) -> io::Result<Log> {
        let file = OpenOptions::new().create(true).append(true).open(path)?;
        Ok(Log { file, start })
    }

    /// Appends `{"t":…,"ev":ev,"hex":hex(raw), extra…}`.
    pub fn event(&mut self, ev: &str, raw: &[u8], extra: &[(&str, J)]) -> io::Result<()> {
        let mut line = format!("{{\"t\":{},\"ev\":", self.start.elapsed().as_millis());
        quote(ev, &mut line);
        line.push_str(",\"hex\":");
        quote(&hex(raw), &mut line);
        for (key, value) in extra {
            line.push(',');
            quote(key, &mut line);
            line.push(':');
            match value {
                J::Int(n) => line.push_str(&n.to_string()),
                J::Bool(b) => line.push_str(if *b { "true" } else { "false" }),
                J::Str(s) => quote(s, &mut line),
                J::Null => line.push_str("null"),
            }
        }
        line.push_str("}\n");
        self.file.write_all(line.as_bytes())?;
        self.file.flush()
    }
}
