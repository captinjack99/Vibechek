//! Bounded file sink for the shell's own diagnostics.
//!
//! Why this exists: `main.rs` builds release binaries for the Windows GUI
//! subsystem (`windows_subsystem = "windows"`), so the process has no console
//! and `GetStdHandle(STD_ERROR_HANDLE)` is NULL. Rust's std treats a write to
//! that handle as a *successful* no-op — `eprintln!` reports a full-length
//! success and the bytes go nowhere, with no error to notice. Every diagnostic
//! this shell produced was an `eprintln!`, so on the platform most users run,
//! the shell was silent by construction.
//!
//! The Python side keeps a rotating file log (`vibechek.log`, surfaced through
//! the `get_log_tail` RPC), but it cannot record the failures that happen on
//! our side of the pipe: losing the child, killing it, dropping a JSON-RPC
//! frame, or a Python crash that happens *before* `logging_setup.configure()`
//! runs. Those had no sink at all.
//!
//! So: every diagnostic goes through [`shell_log!`], which still writes to
//! stderr (dev builds have a console) *and* appends a timestamped line to
//! `<data dir>/logs/vibechek-shell.log`, beside `vibechek.log`, rotated at
//! 1 MB with one backup.
//!
//! No logging crate is pulled in for this on purpose: the shell's whole
//! dependency set is tauri + serde + tokio, and a rolling appender is ~100
//! lines of std.

use std::fmt::Arguments;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

/// Rotate at 1 MB, keeping a single backup. A bug report wants the last few
/// thousand lines; 2 MB of text is already more than anyone reads, and the
/// cap is what makes the relayed child stderr safe to persist.
const MAX_BYTES: u64 = 1024 * 1024;

const LOG_NAME: &str = "vibechek-shell.log";

/// Log a shell diagnostic to stderr AND the rolling file sink.
///
/// Same call shape as `eprintln!` — this replaced every `eprintln!` in the
/// shell, so a windowed release build leaves a trace.
#[macro_export]
macro_rules! shell_log {
    ($($arg:tt)*) => {
        $crate::shell_log::write(format_args!($($arg)*))
    };
}

/// Open the sink and record a start banner.
///
/// Called once from `run()`. Forcing the sink here means a broken log
/// directory is reported at startup (loudly, on stderr) rather than being
/// discovered when something has already gone wrong.
pub fn init() {
    match log_path() {
        Some(path) => write(format_args!(
            "[shell] Vibechek desktop shell {} starting; shell log: {}",
            env!("CARGO_PKG_VERSION"),
            path.display()
        )),
        None => write(format_args!(
            "[shell] Vibechek desktop shell {} starting; shell log unavailable",
            env!("CARGO_PKG_VERSION")
        )),
    }
}

/// Write one diagnostic line. Prefer the [`shell_log!`] macro.
pub fn write(args: Arguments<'_>) {
    let line = format!("{} {}", timestamp(SystemTime::now()), args);

    // Keep stderr: `npm run tauri dev` and the CLI-launched builds have a real
    // console, and that is where a developer looks first.
    eprintln!("{line}");

    append(&line);
}

/// Send panics to the file sink as well.
///
/// Same failure shape as the `eprintln!`s this module replaced: the default
/// panic hook writes to stderr, which a windowed release build discards — so a
/// panic in a background reader task, or in `run()`'s final `.expect(...)`,
/// looked exactly like "the app froze / vanished" with nothing written down.
/// The previous hook still runs, so dev builds keep the usual message and
/// `RUST_BACKTRACE` output on the console, unduplicated.
pub fn install_panic_hook() {
    let previous = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        let line = format!("{} PANIC: {info}", timestamp(SystemTime::now()));
        // try_lock, not lock: the hook can fire while THIS thread already holds
        // the sink lock (a panic inside the writer itself), and std's Mutex is
        // not reentrant — blocking there would hang the process instead of
        // reporting the panic. The previous hook still prints to stderr.
        match sink().try_lock() {
            Ok(mut guard) => write_into(&mut guard, &line),
            Err(std::sync::TryLockError::Poisoned(p)) => write_into(&mut p.into_inner(), &line),
            Err(std::sync::TryLockError::WouldBlock) => {}
        }
        previous(info);
    }));
}

fn append(line: &str) {
    // A writer that panicked mid-line must not silence every later line, so we
    // take the inner value of a poisoned lock rather than propagating.
    let mut guard = match sink().lock() {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    };
    write_into(&mut guard, line);
}

fn write_into(guard: &mut Option<Sink>, line: &str) {
    if let Some(open) = guard.as_mut() {
        if let Err(e) = open.append(line, MAX_BYTES) {
            // Say so on stderr and stop trying: repeating a failing write for
            // every subsequent line would be its own noise source.
            eprintln!("[shell] shell log write failed ({e}) — file logging disabled");
            *guard = None;
        }
    }
}

/// Full path of the current shell log, when the sink is open.
pub fn log_path() -> Option<PathBuf> {
    let guard = match sink().lock() {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    };
    guard.as_ref().map(|open| open.path.clone())
}

static SINK: OnceLock<Mutex<Option<Sink>>> = OnceLock::new();

fn sink() -> &'static Mutex<Option<Sink>> {
    SINK.get_or_init(|| Mutex::new(open_default_sink()))
}

fn open_default_sink() -> Option<Sink> {
    let Some(dir) = log_dir() else {
        eprintln!(
            "[shell] no user data directory (HOME/LOCALAPPDATA unset) — shell file logging disabled"
        );
        return None;
    };
    if let Err(e) = fs::create_dir_all(&dir) {
        eprintln!(
            "[shell] could not create {} ({e}) — shell file logging disabled",
            dir.display()
        );
        return None;
    }
    match Sink::open(dir.join(LOG_NAME)) {
        Ok(open) => Some(open),
        Err(e) => {
            eprintln!(
                "[shell] could not open {} ({e}) — shell file logging disabled",
                dir.join(LOG_NAME).display()
            );
            None
        }
    }
}

/// `<user data dir>/logs`, i.e. the directory Python's `logging_setup` writes
/// `vibechek.log` into. Mirrors `platformdirs.user_data_dir("Vibechek")`
/// (appauthor defaults to the app name on Windows, hence `Vibechek\Vibechek`)
/// so both halves of the app log to one folder — a bug report is then a single
/// directory, not a scavenger hunt.
fn log_dir() -> Option<PathBuf> {
    data_dir().map(|dir| dir.join("logs"))
}

#[cfg(windows)]
fn data_dir() -> Option<PathBuf> {
    std::env::var_os("LOCALAPPDATA")
        .filter(|v| !v.is_empty())
        .map(|v| PathBuf::from(v).join("Vibechek").join("Vibechek"))
}

#[cfg(target_os = "macos")]
fn data_dir() -> Option<PathBuf> {
    std::env::var_os("HOME")
        .filter(|v| !v.is_empty())
        .map(|v| {
            PathBuf::from(v)
                .join("Library")
                .join("Application Support")
                .join("Vibechek")
        })
}

#[cfg(not(any(windows, target_os = "macos")))]
fn data_dir() -> Option<PathBuf> {
    if let Some(xdg) = std::env::var_os("XDG_DATA_HOME").filter(|v| !v.is_empty()) {
        return Some(PathBuf::from(xdg).join("Vibechek"));
    }
    std::env::var_os("HOME")
        .filter(|v| !v.is_empty())
        .map(|v| PathBuf::from(v).join(".local").join("share").join("Vibechek"))
}

/// An open log file plus the byte count that decides when to roll it.
struct Sink {
    path: PathBuf,
    /// `None` only between closing the handle and reopening it during a
    /// rotation — Windows will not rename a file we still hold open.
    file: Option<File>,
    written: u64,
}

impl Sink {
    fn open(path: PathBuf) -> io::Result<Self> {
        let file = open_append(&path)?;
        // Start from the existing size so a restart doesn't reset the budget
        // and let the file grow past the cap.
        let written = file.metadata()?.len();
        Ok(Self {
            path,
            file: Some(file),
            written,
        })
    }

    fn append(&mut self, line: &str, max_bytes: u64) -> io::Result<()> {
        let bytes = line.len() as u64 + 1;
        if self.written > 0 && self.written + bytes > max_bytes {
            self.rotate()?;
        }
        let file = self
            .file
            .as_mut()
            .ok_or_else(|| io::Error::other("shell log handle is closed"))?;
        file.write_all(line.as_bytes())?;
        file.write_all(b"\n")?;
        // No BufWriter: the lines worth having are the ones written just before
        // a crash, and a buffered one is exactly the line we'd lose.
        self.written += bytes;
        Ok(())
    }

    fn rotate(&mut self) -> io::Result<()> {
        let backup = backup_path(&self.path);
        // Drop our handle first: on Windows a surviving handle keeps appending
        // to the renamed (now backup) file. `rename` replaces the old backup.
        self.file = None;
        let renamed = fs::rename(&self.path, &backup);
        // Reopen either way — a failed rotation must not leave logging dead.
        self.file = Some(open_append(&self.path)?);
        self.written = self.file.as_ref().expect("just opened").metadata()?.len();
        renamed
    }
}

fn open_append(path: &Path) -> io::Result<File> {
    OpenOptions::new().create(true).append(true).open(path)
}

fn backup_path(path: &Path) -> PathBuf {
    let mut backup = path.to_path_buf().into_os_string();
    backup.push(".1");
    PathBuf::from(backup)
}

/// `YYYY-MM-DDThh:mm:ssZ`.
///
/// UTC on purpose: local time needs a tz database (chrono/time), and this
/// crate deliberately carries no logging dependency. The `Z` says so plainly
/// when the line is read next to Python's local-time `vibechek.log`.
fn timestamp(now: SystemTime) -> String {
    let secs = match now.duration_since(UNIX_EPOCH) {
        Ok(d) => d.as_secs() as i64,
        // A clock set before 1970 is absurd, but reporting it as the epoch
        // would be quietly wrong; count backwards instead.
        Err(e) => -(e.duration().as_secs() as i64),
    };
    let (year, month, day) = civil_from_days(secs.div_euclid(86_400));
    let tod = secs.rem_euclid(86_400);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        tod / 3600,
        (tod % 3600) / 60,
        tod % 60
    )
}

/// Days-since-epoch -> (year, month, day), Howard Hinnant's `civil_from_days`.
/// Proleptic Gregorian, valid far outside any range a log line will see.
fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = doy - (153 * mp + 2) / 5 + 1; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 }; // [1, 12]
    (if m <= 2 { y + 1 } else { y }, m, d)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU32, Ordering};
    use std::time::Duration;

    fn at(secs: u64) -> SystemTime {
        UNIX_EPOCH + Duration::from_secs(secs)
    }

    #[test]
    fn timestamps_are_utc_iso8601() {
        assert_eq!(timestamp(at(0)), "1970-01-01T00:00:00Z");
        // Leap day, and a second past midnight on a leap-year boundary.
        assert_eq!(timestamp(at(951_782_400)), "2000-02-29T00:00:00Z");
        assert_eq!(timestamp(at(1_757_167_931)), "2025-09-06T14:12:11Z");
        // Pre-epoch clocks count backwards instead of clamping to 1970.
        assert_eq!(
            timestamp(UNIX_EPOCH - Duration::from_secs(1)),
            "1969-12-31T23:59:59Z"
        );
    }

    fn scratch(name: &str) -> PathBuf {
        static N: AtomicU32 = AtomicU32::new(0);
        let dir = std::env::temp_dir().join(format!(
            "vibechek-shell-log-{}-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::SeqCst),
            name
        ));
        fs::create_dir_all(&dir).expect("scratch dir");
        dir
    }

    #[test]
    fn appends_lines_to_the_file() {
        let dir = scratch("append");
        let path = dir.join(LOG_NAME);
        let mut sink = Sink::open(path.clone()).expect("open sink");
        sink.append("first", MAX_BYTES).expect("append");
        sink.append("second", MAX_BYTES).expect("append");
        assert_eq!(fs::read_to_string(&path).unwrap(), "first\nsecond\n");
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn rotates_at_the_cap_and_keeps_one_backup() {
        let dir = scratch("rotate");
        let path = dir.join(LOG_NAME);
        let mut sink = Sink::open(path.clone()).expect("open sink");

        // Cap of 20 bytes: "0123456789" + \n is 11, so the second line rolls.
        sink.append("0123456789", 20).expect("append");
        sink.append("abcdefghij", 20).expect("append");

        assert_eq!(fs::read_to_string(&path).unwrap(), "abcdefghij\n");
        assert_eq!(
            fs::read_to_string(backup_path(&path)).unwrap(),
            "0123456789\n"
        );

        // A second rotation replaces the backup rather than piling up files.
        sink.append("klmnopqrst", 20).expect("append");
        assert_eq!(fs::read_to_string(&path).unwrap(), "klmnopqrst\n");
        assert_eq!(
            fs::read_to_string(backup_path(&path)).unwrap(),
            "abcdefghij\n"
        );
        assert_eq!(fs::read_dir(&dir).unwrap().count(), 2);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn reopening_resumes_the_existing_size_budget() {
        let dir = scratch("resume");
        let path = dir.join(LOG_NAME);
        {
            let mut sink = Sink::open(path.clone()).expect("open sink");
            sink.append("0123456789", 20).expect("append");
        }
        // A restart must not reset the budget and let the file grow past cap.
        let mut sink = Sink::open(path.clone()).expect("reopen sink");
        sink.append("abcdefghij", 20).expect("append");
        assert_eq!(fs::read_to_string(&path).unwrap(), "abcdefghij\n");
        assert_eq!(
            fs::read_to_string(backup_path(&path)).unwrap(),
            "0123456789\n"
        );
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn the_log_lands_beside_the_python_log() {
        // Both halves of the app must write into one folder — `logging_setup`
        // puts vibechek.log in `<user data dir>/logs`.
        let dir = log_dir().expect("a data dir on a normal desktop environment");
        assert_eq!(dir.file_name().and_then(|s| s.to_str()), Some("logs"));
        assert!(
            dir.to_string_lossy().contains("Vibechek"),
            "shell log dir {} is not under the Vibechek data dir",
            dir.display()
        );
    }
}
