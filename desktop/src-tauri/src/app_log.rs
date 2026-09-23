//! Persistent log for a GUI launch. Started from Finder/Dock/the updater, the
//! app's stderr — and the sidecar's, which inherits it — goes to /dev/null, so
//! a failure (e.g. a banner falling back to osascript) left no trace. When
//! stderr is not a terminal, it is redirected through a pipe into a
//! size-capped, timestamped file; a terminal launch (`tauri dev`) is untouched.

use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};

/// Rotate once the file reaches this size; one previous file (`.1`) is kept.
pub const MAX_BYTES: u64 = 5 * 1024 * 1024;

/// `~/Library/Logs/herdeck` on macOS, `$XDG_STATE_HOME/herdeck` (default
/// `~/.local/state/herdeck`) elsewhere.
pub fn log_dir(home: &Path, xdg_state_home: Option<&str>) -> PathBuf {
    if cfg!(target_os = "macos") {
        return home.join("Library/Logs/herdeck");
    }
    match xdg_state_home.filter(|dir| Path::new(dir).is_absolute()) {
        Some(dir) => Path::new(dir).join("herdeck"),
        None => home.join(".local/state/herdeck"),
    }
}

/// The dev build runs beside the release app; each keeps its own file.
pub fn log_file_name(dev: bool) -> &'static str {
    if dev {
        "herdeck-dev.log"
    } else {
        "herdeck.log"
    }
}

/// Appends to `path`, moving it to `path.1` once `max` bytes are reached.
pub struct RotatingLog {
    path: PathBuf,
    max: u64,
    file: File,
    written: u64,
}

impl RotatingLog {
    pub fn open(path: &Path, max: u64) -> io::Result<Self> {
        if let Some(dir) = path.parent() {
            fs::create_dir_all(dir)?;
        }
        let file = OpenOptions::new().create(true).append(true).open(path)?;
        let written = file.metadata()?.len();
        Ok(Self {
            path: path.to_path_buf(),
            max,
            file,
            written,
        })
    }

    pub fn write(&mut self, buf: &[u8]) -> io::Result<()> {
        if self.written > 0 && self.written + buf.len() as u64 > self.max {
            self.rotate()?;
        }
        self.file.write_all(buf)?;
        self.written += buf.len() as u64;
        Ok(())
    }

    fn rotate(&mut self) -> io::Result<()> {
        let mut old = self.path.clone().into_os_string();
        old.push(".1");
        fs::rename(&self.path, &old)?;
        self.file = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(&self.path)?;
        self.written = 0;
        Ok(())
    }
}

/// Prefixes every line with a timestamp; a line split across reads is
/// stamped once.
pub struct LineStamper {
    at_line_start: bool,
}

impl Default for LineStamper {
    fn default() -> Self {
        Self {
            at_line_start: true,
        }
    }
}

impl LineStamper {
    pub fn stamp(&mut self, chunk: &[u8], now: &str) -> Vec<u8> {
        let mut out = Vec::with_capacity(chunk.len() + 32);
        for &byte in chunk {
            if self.at_line_start {
                out.extend_from_slice(b"[");
                out.extend_from_slice(now.as_bytes());
                out.extend_from_slice(b"] ");
                self.at_line_start = false;
            }
            out.push(byte);
            if byte == b'\n' {
                self.at_line_start = true;
            }
        }
        out
    }
}

#[cfg(unix)]
fn local_timestamp() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as libc::time_t)
        .unwrap_or(0);
    // SAFETY: localtime_r only writes the caller-owned `tm`.
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    if unsafe { libc::localtime_r(&secs, &mut tm) }.is_null() {
        return secs.to_string();
    }
    format!(
        "{:04}-{:02}-{:02} {:02}:{:02}:{:02}",
        tm.tm_year + 1900,
        tm.tm_mon + 1,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min,
        tm.tm_sec
    )
}

/// Redirect stderr (fd 2) into the log file unless it is a terminal. Returns
/// the log path when capturing. The reader thread never stops before EOF — a
/// full, unread pipe would block every writer of stderr — and drops data it
/// cannot write instead.
#[cfg(unix)]
pub fn capture_stderr(path: &Path) -> io::Result<Option<PathBuf>> {
    use std::io::Read;
    use std::os::fd::FromRawFd;

    // SAFETY: plain libc calls on fds this function owns.
    if unsafe { libc::isatty(libc::STDERR_FILENO) } == 1 {
        return Ok(None);
    }
    let mut log = RotatingLog::open(path, MAX_BYTES)?;
    let mut fds = [0 as libc::c_int; 2];
    if unsafe { libc::pipe(fds.as_mut_ptr()) } != 0 {
        return Err(io::Error::last_os_error());
    }
    let (read_fd, write_fd) = (fds[0], fds[1]);
    if unsafe { libc::dup2(write_fd, libc::STDERR_FILENO) } < 0 {
        let err = io::Error::last_os_error();
        unsafe {
            libc::close(read_fd);
            libc::close(write_fd);
        }
        return Err(err);
    }
    unsafe { libc::close(write_fd) };
    // SAFETY: read_fd is a fresh pipe end owned only by this File.
    let mut reader = unsafe { File::from_raw_fd(read_fd) };
    std::thread::Builder::new()
        .name("herdeck-log".into())
        .spawn(move || {
            let mut stamper = LineStamper::default();
            let mut buf = [0u8; 8192];
            loop {
                match reader.read(&mut buf) {
                    Ok(0) => break,
                    Ok(n) => {
                        let _ = log.write(&stamper.stamp(&buf[..n], &local_timestamp()));
                    }
                    Err(err) if err.kind() == io::ErrorKind::Interrupted => {}
                    Err(_) => break,
                }
            }
        })?;
    Ok(Some(path.to_path_buf()))
}

#[cfg(not(unix))]
pub fn capture_stderr(_path: &Path) -> io::Result<Option<PathBuf>> {
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("herdeck-app-log-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        dir
    }

    #[test]
    fn rotates_into_one_previous_file() {
        let dir = temp_dir("rotate");
        let path = dir.join("herdeck.log");
        let mut log = RotatingLog::open(&path, 10).unwrap();
        log.write(b"0123456789").unwrap();
        log.write(b"abc").unwrap(); // over the cap -> rotate first
        log.write(b"def").unwrap();
        assert_eq!(fs::read(dir.join("herdeck.log.1")).unwrap(), b"0123456789");
        assert_eq!(fs::read(&path).unwrap(), b"abcdef");
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn reopening_appends_and_counts_the_existing_size() {
        let dir = temp_dir("append");
        let path = dir.join("herdeck.log");
        RotatingLog::open(&path, 8).unwrap().write(b"12345").unwrap();
        let mut log = RotatingLog::open(&path, 8).unwrap();
        log.write(b"6789").unwrap(); // 5 + 4 > 8 -> rotated
        assert_eq!(fs::read(dir.join("herdeck.log.1")).unwrap(), b"12345");
        assert_eq!(fs::read(&path).unwrap(), b"6789");
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn a_single_oversized_write_still_lands() {
        let dir = temp_dir("oversized");
        let path = dir.join("herdeck.log");
        let mut log = RotatingLog::open(&path, 4).unwrap();
        log.write(b"longer than four").unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"longer than four");
        assert!(!dir.join("herdeck.log.1").exists());
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn stamps_each_line_once_across_chunks() {
        let mut stamper = LineStamper::default();
        let a = stamper.stamp(b"one\ntw", "T1");
        let b = stamper.stamp(b"o\nthree\n", "T2");
        assert_eq!(
            String::from_utf8([a, b].concat()).unwrap(),
            "[T1] one\n[T1] two\n[T2] three\n"
        );
    }

    #[test]
    fn log_location_per_platform_and_channel() {
        let home = Path::new("/home/u");
        let dir = log_dir(home, Some("/state"));
        if cfg!(target_os = "macos") {
            assert_eq!(dir, home.join("Library/Logs/herdeck"));
        } else {
            assert_eq!(dir, Path::new("/state/herdeck"));
            assert_eq!(log_dir(home, Some("relative")), home.join(".local/state/herdeck"));
        }
        assert_eq!(log_file_name(false), "herdeck.log");
        assert_eq!(log_file_name(true), "herdeck-dev.log");
    }
}
