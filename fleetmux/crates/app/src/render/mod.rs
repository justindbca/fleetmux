//! Server-side drawing (design.md §2.7): a ratatui backend that writes escape sequences into a byte buffer,
//! and the three-group screen drawn per viewer.
pub mod backend;
pub mod draw;
pub mod sidebar;
