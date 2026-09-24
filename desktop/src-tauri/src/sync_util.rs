//! Poison-tolerant locking for the shell's long-lived threads.
//!
//! A panic while a `Mutex` guard is held poisons it, and a bare
//! `lock().unwrap()` then panics every later caller too — one bad moment would
//! take down the notification pump or the UI thread for the rest of the
//! process. Every value these locks guard (a cursor, a discovery, window flags)
//! stays meaningful after a panic elsewhere, so the shell recovers the inner
//! value instead of propagating the poison.

use std::sync::{Mutex, MutexGuard, PoisonError};

pub(crate) trait LockExt<T> {
    /// `lock()`, recovering the guard from a poisoned mutex.
    fn lock_or_recover(&self) -> MutexGuard<'_, T>;
}

impl<T> LockExt<T> for Mutex<T> {
    fn lock_or_recover(&self) -> MutexGuard<'_, T> {
        self.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    #[test]
    fn a_poisoned_lock_still_yields_its_value() {
        let m = Arc::new(Mutex::new(7));
        let poisoner = m.clone();
        let _ = std::thread::spawn(move || {
            let _guard = poisoner.lock().unwrap();
            panic!("poison the lock");
        })
        .join();
        assert!(m.is_poisoned());
        *m.lock_or_recover() += 1;
        assert_eq!(*m.lock_or_recover(), 8);
    }
}
