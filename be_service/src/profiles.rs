//! The profiles a chat may name: what counts as which kind of question and
//! which words are synonyms in a corpus. The profiles themselves are files
//! of the llm worker (llm_worker/src/workflow/profiles); this is the list of
//! their names from the same settings as the worker's default
//! (`llm.profiles` in envs/<name>), so the chat page can offer them and a
//! name that is none is refused before the workflow sees it.

use anyhow::{Result, bail};

pub struct Profiles {
    /// Empty when not configured: then any well-formed name goes on to the
    /// worker, which knows its files.
    pub names: Vec<String>,
    /// Of a chat that names none: the worker's LLM_DEFAULT_PROFILE, shown
    /// as chosen on a new chat.
    pub default: String,
}

impl Profiles {
    /// `LLM_PROFILES` (comma-separated) and `LLM_DEFAULT_PROFILE`; a
    /// malformed name, or a default not in the list, stops the service at
    /// start rather than at the first chat.
    pub fn from_env() -> Result<Self> {
        let names = std::env::var("LLM_PROFILES").unwrap_or_default();
        let default = std::env::var("LLM_DEFAULT_PROFILE").unwrap_or_default();
        Self::new(names.split(',').map(str::trim).filter(|n| !n.is_empty()).map(String::from).collect(), default)
    }

    pub fn new(names: Vec<String>, default: String) -> Result<Self> {
        for name in names.iter().chain(std::iter::once(&default)).filter(|n| !n.is_empty()) {
            if !well_formed(name) {
                bail!("profile {name:?}: at most 40 of a-z, 0-9, '-' or '_', from a letter or a digit");
            }
        }
        if !default.is_empty() && !names.is_empty() && !names.contains(&default) {
            bail!("LLM_DEFAULT_PROFILE {default:?} is not in LLM_PROFILES {names:?}");
        }
        Ok(Self { names, default })
    }

    /// Why a profile asked for in a message is refused, if it is: empty is
    /// the default; otherwise well formed and, with a list, in it.
    pub fn refusal(&self, profile: &str) -> Option<String> {
        if profile.is_empty() {
            None
        } else if !well_formed(profile) {
            Some("profile: at most 40 of a-z, 0-9, '-' or '_', from a letter or a digit".into())
        } else if !self.names.is_empty() && !self.names.iter().any(|n| n == profile) {
            Some(format!("unknown profile {profile:?}; there are {}", self.names.join(", ")))
        } else {
            None
        }
    }
}

/// A profile name becomes a file name in the worker: a-z, 0-9, '_' and '-',
/// from a letter or a digit, at most 40. The worker checks the same form
/// (llm_worker/src/workflow/profiles/__init__.py, NAME).
fn well_formed(name: &str) -> bool {
    name.len() <= 40
        && name.bytes().next().is_some_and(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
        && name.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-' || b == b'_')
}

#[cfg(test)]
mod tests {
    use super::*;

    fn two() -> Profiles {
        Profiles::new(vec!["philosophy".into(), "default".into()], "philosophy".into()).unwrap()
    }

    #[test]
    fn a_message_names_a_listed_profile_or_none() {
        assert_eq!(two().refusal(""), None);
        assert_eq!(two().refusal("default"), None);
        assert!(two().refusal("fitness").unwrap().contains("there are philosophy, default"));
        for malformed in ["../secrets", "Philosophy", "-x", &"a".repeat(41)] {
            assert!(two().refusal(malformed).unwrap().starts_with("profile: at most 40"), "{malformed}");
        }
    }

    #[test]
    fn without_a_list_any_well_formed_name_goes_on_to_the_worker() {
        let none = Profiles::new(vec![], String::new()).unwrap();
        assert_eq!(none.refusal("fitness"), None);
        assert!(none.refusal("../x").is_some());
    }

    #[test]
    fn settings_that_cannot_work_stop_the_start() {
        assert!(Profiles::new(vec!["philosophy".into()], "fitness".into()).is_err());
        assert!(Profiles::new(vec!["Bad Name".into()], String::new()).is_err());
    }
}
