//! `--record <dir>` (PROTOCOL.md §7): every pane's PTY output (never input) appended to `<dir>/<pane id>.rec`.
//!
//! Package B20 `record-hook`: [`PaneStore::spawn`](super::pane::PaneStore::spawn) opens one [`RecordFile`] per
//! pane and hands it to the pane's PTY reader thread, which appends every successful read before sending its event. Entries
//! use the `fleetmux_proto::record` format unchanged. The file is written unbuffered, so a reader sees each read
//! as soon as it is appended.

use std::fs::{DirBuilder, File, OpenOptions, Permissions};
use std::io;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use std::path::PathBuf;

use fleetmux_proto::record::{self, Entry, MAX_ENTRY};

/// The `--record` directory.
pub struct Recorder {
    dir: PathBuf,
}

/// One open `<pane id>.rec` file.
pub struct RecordFile {
    file: File,
    /// The largest `offset_us` written so far: a smaller one is raised to it, so offsets never decrease.
    last_us: u64,
}

impl Recorder {
    /// Added in stage A beyond the brief-m03 §3.3 literal: `dir` is private, so without a constructor neither
    /// `boot.rs` nor the tests could build the `Recorder` that `PaneStore::new` takes.
    pub fn new(dir: PathBuf) -> Recorder {
        Recorder { dir }
    }

    /// Create `<dir>/<pane>.rec`, mode 0600, truncating any earlier file of that name.
    /// The directory is created (mode 0700) when missing; a symlink in place of the file is refused.
    pub fn open(&self, pane: &str) -> io::Result<RecordFile> {
        if pane.is_empty() || pane.contains('/') || pane == "." || pane == ".." {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("pane id {pane:?} is not a file name"),
            ));
        }
        DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&self.dir)?;
        let file = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(self.dir.join(format!("{pane}.rec")))?;
        // `mode` applies only to a new file: an earlier file of that name keeps its mode unless reset here.
        file.set_permissions(Permissions::from_mode(0o600))?;
        Ok(RecordFile { file, last_us: 0 })
    }
}

impl Recorder {
    /// Remove `<dir>/<pane>.rec` (a pane whose start failed); errors are ignored.
    pub fn remove(&self, pane: &str) {
        if !(pane.is_empty() || pane.contains('/') || pane == "." || pane == "..") {
            let _ = std::fs::remove_file(self.dir.join(format!("{pane}.rec")));
        }
    }
}

impl RecordFile {
    /// Append the bytes of one PTY read as `fleetmux_proto::record` entries: more than 1 MiB
    /// (`record::MAX_ENTRY`) is split into several entries with the same `offset_us`; empty `bytes` writes nothing.
    /// `offset_us` = microseconds since the pane was spawned, non-decreasing per file.
    pub fn append(&mut self, offset_us: u64, bytes: &[u8]) -> io::Result<()> {
        if bytes.is_empty() {
            return Ok(());
        }
        let offset_us = offset_us.max(self.last_us);
        for chunk in bytes.chunks(MAX_ENTRY) {
            let entry = Entry {
                offset_us,
                bytes: chunk.to_vec(),
            };
            record::write(&mut self.file, &entry)?;
        }
        self.last_us = offset_us;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU32, Ordering};

    static SEQ: AtomicU32 = AtomicU32::new(0);

    /// A fresh `<tmp>/frr<pid>-<n>` path (not created), removed on drop.
    struct Tmp(PathBuf);

    impl Tmp {
        fn new() -> Tmp {
            let p = std::env::temp_dir().join(format!(
                "frr{}-{}",
                std::process::id(),
                SEQ.fetch_add(1, Ordering::SeqCst)
            ));
            let _ = std::fs::remove_dir_all(&p);
            Tmp(p)
        }
    }

    impl Drop for Tmp {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn entries(path: &std::path::Path) -> Vec<Entry> {
        let mut f = File::open(path).unwrap();
        let mut out = Vec::new();
        while let Some(e) = record::read(&mut f).unwrap() {
            out.push(e);
        }
        out
    }

    fn mode(path: &std::path::Path) -> u32 {
        std::fs::metadata(path).unwrap().permissions().mode() & 0o777
    }

    #[test]
    fn open_creates_missing_dir_0700_and_truncates_earlier_file_to_0600() {
        let tmp = Tmp::new();
        let dir = tmp.0.join("a/b");
        let rec = Recorder::new(dir.clone());
        let mut f = rec.open("p-1").unwrap();
        assert_eq!(mode(&dir), 0o700);
        f.append(1, b"old").unwrap();
        drop(f);
        let path = dir.join("p-1.rec");
        std::fs::set_permissions(&path, Permissions::from_mode(0o644)).unwrap();

        let mut f = rec.open("p-1").unwrap();
        assert_eq!(mode(&path), 0o600, "an earlier file is reset to 0600");
        assert!(entries(&path).is_empty(), "an earlier file is truncated");
        f.append(2, b"new").unwrap();
        assert_eq!(
            entries(&path),
            vec![Entry {
                offset_us: 2,
                bytes: b"new".to_vec()
            }],
            "written unbuffered: visible before the file is dropped"
        );
    }

    #[test]
    fn offsets_never_decrease_within_a_file() {
        let tmp = Tmp::new();
        let rec = Recorder::new(tmp.0.clone());
        let mut f = rec.open("p-2").unwrap();
        f.append(50, b"a").unwrap();
        f.append(40, b"b").unwrap();
        f.append(0, b"").unwrap();
        f.append(60, b"c").unwrap();
        let offsets: Vec<u64> = entries(&tmp.0.join("p-2.rec"))
            .iter()
            .map(|e| e.offset_us)
            .collect();
        assert_eq!(offsets, vec![50, 50, 60]);
    }

    #[test]
    fn exact_max_entry_is_one_entry() {
        let tmp = Tmp::new();
        let rec = Recorder::new(tmp.0.clone());
        let mut f = rec.open("p-3").unwrap();
        f.append(7, &vec![b'x'; MAX_ENTRY]).unwrap();
        f.append(8, &vec![b'y'; MAX_ENTRY + 1]).unwrap();
        let lens: Vec<(u64, usize)> = entries(&tmp.0.join("p-3.rec"))
            .iter()
            .map(|e| (e.offset_us, e.bytes.len()))
            .collect();
        assert_eq!(lens, vec![(7, MAX_ENTRY), (8, MAX_ENTRY), (8, 1)]);
    }

    #[test]
    fn refuses_symlink_and_non_file_name_pane_ids() {
        let tmp = Tmp::new();
        let rec = Recorder::new(tmp.0.clone());
        drop(rec.open("p-4").unwrap());
        let target = tmp.0.join("target");
        std::fs::write(&target, b"keep").unwrap();
        std::os::unix::fs::symlink(&target, tmp.0.join("p-5.rec")).unwrap();
        assert!(rec.open("p-5").is_err(), "a symlinked .rec is not followed");
        assert_eq!(std::fs::read(&target).unwrap(), b"keep");
        for bad in ["", ".", "..", "x/y"] {
            assert_eq!(
                rec.open(bad).err().map(|e| e.kind()),
                Some(io::ErrorKind::InvalidInput),
                "{bad:?}"
            );
        }
    }
}
