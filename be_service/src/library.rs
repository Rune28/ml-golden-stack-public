//! The user's library: what their files are as documents, corrected and
//! confirmed by them, and their groups of sources (docs/search-scope-plan.md,
//! M1 and M2). Both are the user's own reference data: written here, read
//! through documents_visible and collections_visible, never shared with
//! another user who has the same file.

use std::sync::Arc;

use axum::Json;
use axum::extract::{Path, State};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::api::{ApiError, AppState, validate_run_id};
use crate::auth::User;
use crate::store::{CollectionRow, DocumentEdit, DocumentFacet, DocumentRow};

/// The kinds of a document: the same as read_document's
/// (llm_worker/src/rag/documents.py, KINDS). A change to one is a change to both.
pub const KINDS: [&str; 4] = ["article", "book", "notes", "other"];
/// The fields a user may correct.
const FIELDS: [&str; 4] = ["title", "authors", "kind", "year"];
const MAX_TITLE: usize = 300;
const MAX_AUTHORS: usize = 20;
const MAX_AUTHOR: usize = 120;
const MAX_COLLECTION_NAME: usize = 60;
/// Themes shown with a document.
const TOP_THEMES: u32 = 15;

/// A save of the card of a document. A field present is corrected to its
/// value (`"year": null` is "no year"); a field absent keeps what it was;
/// `reset` gives fields back to what read_document found. `confirmed` says
/// the user checked the card.
#[derive(Deserialize, Default, Debug)]
#[serde(deny_unknown_fields)]
pub struct EditRequest {
    pub title: Option<String>,
    pub authors: Option<Vec<String>>,
    pub kind: Option<String>,
    /// A JSON value, so that null (no year) and absent (unchanged) differ.
    #[serde(default, deserialize_with = "present")]
    pub year: Option<Value>,
    pub confirmed: Option<bool>,
    #[serde(default)]
    pub reset: Vec<String>,
}

fn present<'de, D: serde::Deserializer<'de>>(d: D) -> Result<Option<Value>, D::Error> {
    Value::deserialize(d).map(Some)
}

/// The correction after a save, or why the save is refused. Checked whole
/// before anything is written.
pub fn apply(mut edit: DocumentEdit, req: &EditRequest) -> Result<DocumentEdit, String> {
    let set = |field: &str, edit: &mut DocumentEdit| {
        if !edit.corrected.iter().any(|f| f == field) {
            edit.corrected.push(field.to_owned());
        }
    };
    if let Some(title) = &req.title {
        let title = title.trim();
        if title.chars().count() > MAX_TITLE {
            return Err(format!("title: at most {MAX_TITLE} characters"));
        }
        edit.title = title.to_owned();
        set("title", &mut edit);
    }
    if let Some(authors) = &req.authors {
        let authors: Vec<String> = authors.iter().map(|a| a.trim().to_owned()).filter(|a| !a.is_empty()).collect();
        if authors.len() > MAX_AUTHORS || authors.iter().any(|a| a.chars().count() > MAX_AUTHOR) {
            return Err(format!("authors: at most {MAX_AUTHORS}, each at most {MAX_AUTHOR} characters"));
        }
        edit.authors = authors;
        set("authors", &mut edit);
    }
    if let Some(kind) = &req.kind {
        if !KINDS.contains(&kind.as_str()) {
            return Err(format!("kind: one of {}", KINDS.join(", ")));
        }
        edit.kind = kind.clone();
        set("kind", &mut edit);
    }
    if let Some(year) = &req.year {
        edit.year = match year {
            Value::Null => None,
            Value::Number(n) => match n.as_u64() {
                Some(y @ 1000..=2100) => Some(y as u16),
                _ => return Err("year: from 1000 to 2100, or null".into()),
            },
            _ => return Err("year: from 1000 to 2100, or null".into()),
        };
        set("year", &mut edit);
    }
    for field in &req.reset {
        if !FIELDS.contains(&field.as_str()) {
            return Err(format!("reset: of {}", FIELDS.join(", ")));
        }
        edit.corrected.retain(|f| f != field);
    }
    if let Some(confirmed) = req.confirmed {
        edit.confirmed = confirmed;
    }
    Ok(edit)
}

/// What an upload may say of its files: one author and a kind, for a whole
/// batch ("five blogs of mine"). None when it says nothing.
pub fn upload_edit(author: Option<&str>, kind: Option<&str>) -> Result<Option<EditRequest>, String> {
    let author = author.map(str::trim).filter(|a| !a.is_empty());
    let kind = kind.map(str::trim).filter(|k| !k.is_empty());
    if author.is_none() && kind.is_none() {
        return Ok(None);
    }
    let req = EditRequest {
        authors: author.map(|a| vec![a.to_owned()]),
        kind: kind.map(str::to_owned),
        ..Default::default()
    };
    apply(DocumentEdit::default(), &req)?;
    Ok(Some(req))
}

fn collection_name(name: &str) -> Result<String, String> {
    let name = name.trim();
    if name.is_empty() || name.chars().count() > MAX_COLLECTION_NAME || name.chars().any(char::is_control) {
        return Err(format!("name: 1-{MAX_COLLECTION_NAME} characters"));
    }
    Ok(name.to_owned())
}

/// The card of one of the user's files after a save: `PUT /api/files/<file_id>/document`
/// with an EditRequest. The request is checked whole before the file is
/// looked up; a file of someone else is not found.
pub async fn put_document(
    State(state): State<Arc<AppState>>,
    User(user): User,
    Path(file_id): Path<String>,
    Json(req): Json<EditRequest>,
) -> Result<Json<DocumentRow>, ApiError> {
    let file_id = validate_run_id("file_id", file_id)?;
    apply(DocumentEdit::default(), &req).map_err(ApiError::BadRequest)?;
    save_edit(&state, &user, &file_id, &req).await?;
    let visible = state.store.visible(&user);
    let mut rows = visible.documents(Some(&file_id), 1, TOP_THEMES).await?;
    rows.pop().map(Json).ok_or_else(|| ApiError::NotFound(format!("no file {file_id}")))
}

/// The user's correction of one of their files, the save applied over it.
pub async fn save_edit(state: &AppState, user: &str, file_id: &str, req: &EditRequest) -> Result<(), ApiError> {
    if state.store.visible(user).file(file_id).await?.is_none() {
        return Err(ApiError::NotFound(format!("no file {file_id}")));
    }
    let edit = apply(state.store.document_edit(user, file_id).await?, req).map_err(ApiError::BadRequest)?;
    state.store.put_document_edit(user, file_id, &edit).await?;
    Ok(())
}

/// The user's groups of sources: `GET /api/collections`.
pub async fn list_collections(
    State(state): State<Arc<AppState>>,
    User(user): User,
) -> Result<Json<Vec<CollectionRow>>, ApiError> {
    Ok(Json(state.store.visible(&user).collections().await?))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NewCollection {
    name: String,
}

/// A new group: `POST /api/collections` with `{"name": "Блоги"}`; its id is made here.
pub async fn new_collection(
    State(state): State<Arc<AppState>>,
    User(user): User,
    Json(req): Json<NewCollection>,
) -> Result<Json<CollectionRow>, ApiError> {
    let name = collection_name(&req.name).map_err(ApiError::BadRequest)?;
    let collection_id = uuid::Uuid::now_v7().to_string();
    state.store.put_collection(&user, &collection_id, &name, false).await?;
    Ok(Json(CollectionRow { collection_id, name, file_ids: vec![] }))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CollectionChange {
    name: Option<String>,
    #[serde(default)]
    deleted: bool,
}

/// Rename or delete a group: `PUT /api/collections/<id>` with `{"name": "..."}`
/// or `{"deleted": true}`. Its files stay the user's files.
pub async fn change_collection(
    State(state): State<Arc<AppState>>,
    User(user): User,
    Path(collection_id): Path<String>,
    Json(req): Json<CollectionChange>,
) -> Result<Json<Value>, ApiError> {
    let collection_id = validate_run_id("collection_id", collection_id)?;
    let name = req.name.as_deref().map(collection_name).transpose().map_err(ApiError::BadRequest)?;
    let Some(current) = state.store.visible(&user).collection(&collection_id).await? else {
        return Err(ApiError::NotFound(format!("no collection {collection_id}")));
    };
    let name = name.unwrap_or(current.name);
    state.store.put_collection(&user, &collection_id, &name, req.deleted).await?;
    Ok(Json(serde_json::json!({ "collection_id": collection_id, "name": name, "deleted": req.deleted })))
}

/// Put one of the user's files in one of their groups:
/// `POST /api/collections/<id>/files/<file_id>`; DELETE takes it out.
pub async fn add_to_collection(
    State(state): State<Arc<AppState>>,
    User(user): User,
    Path((collection_id, file_id)): Path<(String, String)>,
) -> Result<Json<Value>, ApiError> {
    place(&state, &user, collection_id, file_id, false).await
}

pub async fn remove_from_collection(
    State(state): State<Arc<AppState>>,
    User(user): User,
    Path((collection_id, file_id)): Path<(String, String)>,
) -> Result<Json<Value>, ApiError> {
    place(&state, &user, collection_id, file_id, true).await
}

async fn place(
    state: &AppState,
    user: &str,
    collection_id: String,
    file_id: String,
    removed: bool,
) -> Result<Json<Value>, ApiError> {
    let collection_id = validate_run_id("collection_id", collection_id)?;
    let file_id = validate_run_id("file_id", file_id)?;
    let visible = state.store.visible(user);
    if visible.collection(&collection_id).await?.is_none() {
        return Err(ApiError::NotFound(format!("no collection {collection_id}")));
    }
    if visible.file(&file_id).await?.is_none() {
        return Err(ApiError::NotFound(format!("no file {file_id}")));
    }
    state.store.put_collection_file(user, &collection_id, &file_id, removed).await?;
    Ok(Json(serde_json::json!({ "collection_id": collection_id, "file_id": file_id, "in": !removed })))
}

/// Where a chat looks (docs/search-scope-plan.md, M3), as the page asks
/// for it with the first message: the worker resolves it to files through
/// the user's own views, so here only its form is checked.
#[derive(Deserialize, Serialize, Default, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ScopeRequest {
    #[serde(default)]
    pub file_ids: Vec<String>,
    #[serde(default)]
    pub collections: Vec<String>,
    #[serde(default)]
    pub authors: Vec<String>,
    #[serde(default)]
    pub kinds: Vec<String>,
    pub year_from: Option<u16>,
    pub year_to: Option<u16>,
}

const MAX_SCOPE_FILES: usize = 500;
const MAX_SCOPE_VALUES: usize = 20;

fn id_like(id: &str) -> bool {
    (1..=64).contains(&id.len()) && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

/// Why a scope is refused, if it is.
pub fn check_scope(scope: &ScopeRequest) -> Result<(), String> {
    if scope.file_ids.len() > MAX_SCOPE_FILES || !scope.file_ids.iter().all(|f| id_like(f)) {
        return Err(format!("scope.file_ids: at most {MAX_SCOPE_FILES} file ids"));
    }
    if scope.collections.len() > MAX_SCOPE_VALUES || !scope.collections.iter().all(|c| id_like(c)) {
        return Err(format!("scope.collections: at most {MAX_SCOPE_VALUES} group ids"));
    }
    if scope.authors.len() > MAX_SCOPE_VALUES
        || scope.authors.iter().any(|a| a.trim().is_empty() || a.chars().count() > MAX_AUTHOR)
    {
        return Err(format!("scope.authors: at most {MAX_SCOPE_VALUES} names of at most {MAX_AUTHOR} characters"));
    }
    if !scope.kinds.iter().all(|k| KINDS.contains(&k.as_str())) {
        return Err(format!("scope.kinds: of {}", KINDS.join(", ")));
    }
    let year = |y: Option<u16>| y.is_none_or(|y| (1000..=2100).contains(&y));
    if !year(scope.year_from)
        || !year(scope.year_to)
        || matches!((scope.year_from, scope.year_to), (Some(a), Some(b)) if a > b)
    {
        return Err("scope.year_from, year_to: from 1000 to 2100, from not after to".into());
    }
    Ok(())
}

/// What a scope can be made of: the values the user's own documents have,
/// for the pickers of a new chat and for the route to choose among.
#[derive(Serialize, Debug, PartialEq)]
pub struct Facets {
    pub collections: Vec<FacetCollection>,
    pub authors: Vec<FacetValue>,
    pub kinds: Vec<FacetValue>,
    pub years: Option<(u16, u16)>,
}

#[derive(Serialize, Debug, PartialEq)]
pub struct FacetCollection {
    pub collection_id: String,
    pub name: String,
    pub files: usize,
}

#[derive(Serialize, Debug, PartialEq)]
pub struct FacetValue {
    pub value: String,
    pub documents: usize,
}

/// The facets of the user's documents: each author and kind with the number
/// of documents, the most frequent first; the range of years; the groups.
pub fn facets(documents: &[DocumentFacet], collections: Vec<CollectionRow>) -> Facets {
    fn counted(values: impl Iterator<Item = String>) -> Vec<FacetValue> {
        let mut counts: std::collections::BTreeMap<String, usize> = Default::default();
        for value in values.filter(|v| !v.trim().is_empty()) {
            *counts.entry(value).or_default() += 1;
        }
        let mut out: Vec<FacetValue> =
            counts.into_iter().map(|(value, documents)| FacetValue { value, documents }).collect();
        out.sort_by(|a, b| b.documents.cmp(&a.documents).then(a.value.cmp(&b.value)));
        out
    }
    let years = documents.iter().filter_map(|d| d.year);
    Facets {
        collections: collections
            .into_iter()
            .map(|c| FacetCollection { files: c.file_ids.len(), collection_id: c.collection_id, name: c.name })
            .collect(),
        authors: counted(documents.iter().flat_map(|d| d.authors.iter().cloned())),
        kinds: counted(documents.iter().map(|d| d.kind.clone())),
        years: years.clone().min().zip(years.max()),
    }
}

/// `GET /api/facets`: what the user's documents can be scoped by.
pub async fn list_facets(State(state): State<Arc<AppState>>, User(user): User) -> Result<Json<Facets>, ApiError> {
    let visible = state.store.visible(&user);
    Ok(Json(facets(&visible.facet_rows().await?, visible.collections().await?)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(json: &str) -> EditRequest {
        serde_json::from_str(json).unwrap()
    }

    #[test]
    fn a_save_corrects_the_fields_it_has_and_keeps_the_others() {
        let first = apply(DocumentEdit::default(), &request(r#"{"authors": [" Мераб Мамардашвили ", ""]}"#)).unwrap();
        assert_eq!(
            (first.corrected.clone(), first.authors.clone()),
            (vec!["authors".into()], vec!["Мераб Мамардашвили".into()])
        );

        let second = apply(first, &request(r#"{"kind": "book", "year": null, "confirmed": true}"#)).unwrap();
        assert_eq!(second.corrected, ["authors", "kind", "year"]);
        assert_eq!((second.year, second.confirmed, second.authors.len()), (None, true, 1));

        // Back to what read_document found; the value no longer counts.
        let reset = apply(second, &request(r#"{"reset": ["authors"]}"#)).unwrap();
        assert_eq!(reset.corrected, ["kind", "year"]);
    }

    #[test]
    fn a_save_that_cannot_be_kept_is_refused_whole() {
        for (json, message) in [
            (r#"{"kind": "poem"}"#, "kind: one of"),
            (r#"{"year": 3021}"#, "year:"),
            (r#"{"year": "1997"}"#, "year:"),
            (r#"{"reset": ["name"]}"#, "reset:"),
        ] {
            assert!(apply(DocumentEdit::default(), &request(json)).unwrap_err().starts_with(message), "{json}");
        }
        let many = serde_json::json!({ "authors": vec!["a"; MAX_AUTHORS + 1] }).to_string();
        assert!(apply(DocumentEdit::default(), &request(&many)).is_err());
        assert!(serde_json::from_str::<EditRequest>(r#"{"owner": "bob"}"#).is_err());
    }

    #[test]
    fn an_upload_may_name_one_author_and_a_kind_for_its_batch() {
        assert!(upload_edit(None, Some(" ")).unwrap().is_none());
        let req = upload_edit(Some("Я"), Some("notes")).unwrap().unwrap();
        assert_eq!((req.authors, req.kind), (Some(vec!["Я".into()]), Some("notes".into())));
        assert!(upload_edit(None, Some("blog")).is_err());
    }

    #[test]
    fn a_group_has_a_short_name() {
        assert_eq!(collection_name("  Блоги ").unwrap(), "Блоги");
        assert!(
            collection_name(" ").is_err()
                && collection_name(&"а".repeat(61)).is_err()
                && collection_name("a\nb").is_err()
        );
    }

    #[test]
    fn a_scope_is_checked_in_form_only() {
        assert!(check_scope(&ScopeRequest::default()).is_ok());
        let ok = ScopeRequest {
            collections: vec!["c-1".into()],
            kinds: vec!["notes".into()],
            year_from: Some(2015),
            ..Default::default()
        };
        assert!(check_scope(&ok).is_ok());
        for bad in [
            ScopeRequest { file_ids: vec!["../x".into()], ..Default::default() },
            ScopeRequest { kinds: vec!["blog".into()], ..Default::default() },
            ScopeRequest { authors: vec![" ".into()], ..Default::default() },
            ScopeRequest { year_from: Some(2020), year_to: Some(2010), ..Default::default() },
            ScopeRequest { collections: vec!["c".into(); MAX_SCOPE_VALUES + 1], ..Default::default() },
        ] {
            assert!(check_scope(&bad).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn facets_count_the_documents_of_each_value() {
        let doc = |authors: &[&str], kind: &str, year| DocumentFacet {
            authors: authors.iter().map(|a| a.to_string()).collect(),
            kind: kind.into(),
            year,
        };
        let documents = [
            doc(&["Merab Mamardashvili"], "book", Some(1997)),
            doc(&[], "notes", None),
            doc(&[], "notes", Some(2025)),
            doc(&[], "", None),
        ];
        let groups = vec![CollectionRow {
            collection_id: "c-1".into(),
            name: "Блоги".into(),
            file_ids: vec!["a".into(), "b".into()],
        }];

        let got = facets(&documents, groups);

        assert_eq!(got.authors, [FacetValue { value: "Merab Mamardashvili".into(), documents: 1 }]);
        assert_eq!(
            got.kinds,
            [FacetValue { value: "notes".into(), documents: 2 }, FacetValue { value: "book".into(), documents: 1 }]
        );
        assert_eq!((got.years, got.collections[0].files), (Some((1997, 2025)), 2));
    }
}
