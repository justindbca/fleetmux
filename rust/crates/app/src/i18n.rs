//! Interface language (docs/design.md §63, `fleet.yaml#ui.language`).
//!
//! Every text the terminal draws is a key plus its English default, written in the code:
//! `tr(lang, "bottom.sidebar.hide", "hide sidebar")`. The Chinese texts are one table shared with Python,
//! `fleetmux/locales/zh-Hans.json`, embedded here at build time; a key missing from it falls back to English.
//!
//! The terminal never decides the language itself: Python resolves `ui.language` (private config, shipped
//! default, `system`) and starts the server with [`ENV`] set to the result. The server reads it once at start
//! ([`Lang::from_env`]) and keeps it in [`crate::render::sidebar::Side::lang`]; panes do not inherit it
//! (`server/pty.rs`). No yaml, no system language here.

use std::collections::HashMap;
use std::sync::OnceLock;

/// The variable Python sets for the server it starts.
pub const ENV: &str = "FLEETMUX_UI_LANGUAGE";

/// The embedded zh-Hans table (single source: the Python package's file).
static ZH_HANS: &str = include_str!("../../../../fleetmux/locales/zh-Hans.json");

/// A supported interface language.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Lang {
    En,
    ZhHans,
}

impl Default for Lang {
    /// English; the crate's own unit tests run in zh-Hans, the texts they were written against (as the Python
    /// suite does), and set [`Lang::En`] where they check the English texts.
    fn default() -> Self {
        if cfg!(test) { Lang::ZhHans } else { Lang::En }
    }
}

impl Lang {
    /// `zh-Hans` → [`Lang::ZhHans`]; anything else (absent, `en`, unknown) → [`Lang::En`].
    pub fn parse(value: Option<&str>) -> Lang {
        match value.map(str::trim) {
            Some("zh-Hans") => Lang::ZhHans,
            _ => Lang::En,
        }
    }

    /// The language Python passed in [`ENV`].
    pub fn from_env() -> Lang {
        Lang::parse(std::env::var(ENV).ok().as_deref())
    }
}

fn zh_hans() -> &'static HashMap<String, String> {
    static TABLE: OnceLock<HashMap<String, String>> = OnceLock::new();
    TABLE.get_or_init(|| serde_json::from_str(ZH_HANS).unwrap_or_default())
}

/// `en` (the English default) in `lang`: the table's text for `key`, or `en` when it has none.
pub fn tr(lang: Lang, key: &str, en: &str) -> String {
    match lang {
        Lang::En => en.to_string(),
        Lang::ZhHans => zh_hans().get(key).cloned().unwrap_or_else(|| en.to_string()),
    }
}

/// [`tr`], then every `{name}` of `args` replaced by its value in one pass (a value is never scanned again; a
/// name not given stays as it is).
pub fn tr_fmt(lang: Lang, key: &str, en: &str, args: &[(&str, &str)]) -> String {
    let text = tr(lang, key, en);
    let mut out = String::with_capacity(text.len());
    let mut rest = text.as_str();
    while let Some(open) = rest.find('{') {
        out.push_str(&rest[..open]);
        let after = &rest[open + 1..];
        match after.find('}').map(|close| (&after[..close], close)) {
            Some((name, close)) if args.iter().any(|(k, _)| *k == name) => {
                let value = args.iter().find(|(k, _)| *k == name).map(|(_, v)| *v).unwrap_or_default();
                out.push_str(value);
                rest = &after[close + 1..];
            }
            _ => {
                out.push('{');
                rest = after;
            }
        }
    }
    out.push_str(rest);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_embedded_table_parses_and_has_the_terminal_keys() {
        let table: HashMap<String, String> = serde_json::from_str(ZH_HANS).expect("zh-Hans.json is a flat object");
        assert!(table.contains_key("bottom.sidebar.hide"));
        assert_eq!(tr(Lang::ZhHans, "bottom.sidebar.hide", "hide sidebar"), "隐藏侧栏");
    }

    #[test]
    fn english_is_the_default_text_and_a_missing_key_falls_back_to_it() {
        assert_eq!(tr(Lang::En, "bottom.sidebar.hide", "hide sidebar"), "hide sidebar");
        assert_eq!(tr(Lang::ZhHans, "no.such.key", "only English"), "only English");
    }

    #[test]
    fn placeholders_fill_once() {
        assert_eq!(
            tr_fmt(Lang::En, "x.y", "close {title}? {other} {", &[("title", "{other}")]),
            "close {other}? {other} {"
        );
        assert_eq!(tr_fmt(Lang::ZhHans, "top.stop.refused", "close refused: {reason}", &[("reason", "r")]), "关闭被拒：r");
    }

    #[test]
    fn only_zh_hans_selects_chinese() {
        assert_eq!(Lang::parse(Some("zh-Hans")), Lang::ZhHans);
        for v in [None, Some("en"), Some("zh"), Some("fr"), Some("")] {
            assert_eq!(Lang::parse(v), Lang::En, "{v:?}");
        }
    }
}
