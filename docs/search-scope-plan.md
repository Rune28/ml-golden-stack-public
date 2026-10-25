# Search scope: topics, authors and groups of sources

Agreed 2026-10-22. Slices are in BACKLOG, "Next: search scope".

## Why

Asked to "trace the author's path in the blogs 1–5" and "how the blogs differ from the Topology of the Path", the chat answered from every file at once, the book of Mamardashvili mixed in:

1. **A question cannot name where to look.** retrieve and gather read every file the user may see. route finds only the subject, so "автор" and "файлы блог" became search terms ("автор" → писатель, художник, поэт).
2. **The metadata cannot be filtered by.** read_document gave the lecture book on Proust the author "М. Пруст", the one it quotes; one blog has the author "автор", the others none.
3. **Sources cannot be grouped.** "My blogs" exist only in the user's head.

The goal: a chat has a scope (a group, an author, a kind, years, given files); its answers are built from that scope only and say what they were built from. The model reads the metadata; a person confirms or corrects it, and the correction wins.

## Decisions

1. **What a person enters is reference data, not the output of a run** (principle 4): a natural key, untouched by a new index or a new document run. It lives in ClickHouse next to `file_owners`; be_service alone writes it.
2. **Corrections are each user's own**, keyed `(user_id, file_id)`: one user's correction never changes what another sees, the isolation of A2. What the model extracted stays shared.
3. **Groups are personal collections.** A library shared with athletes changes the visibility model of A2.
4. **One view of a document per user**, `documents_visible(user_id)`: the user's correction over `sat_document_visible` and `link_document_author_visible`, with the source of every field (`edit` or `text`), whether it was confirmed, and the file's collections. The document page, the facets and the scope all read it.
5. **The scope narrows what a user may see and never widens it.** The steps of an answer add `file_id IN (…)` to their `*_visible` reads; an empty list is everything visible. A scope that matches nothing is answered as such, never from the whole corpus.
6. **A chat's scope is fixed by its first message**, like its profile, and carried over continue-as-new. Filters read from a question (M4) narrow it for that message only.
7. **Filters from a question are the user's own values its words name.** A group whose every word is in the message, an author whose surname is (in Latin letters, so Мамардашвили is Merab Mamardashvili), a kind whose word of the profile is (`kind_words`). Asked of qwen3:4b with the lists instead, it returned every value of them for almost any message: 3 right of 45.
8. **What a small model is told not to do, it often does.** The author of a document is picked in code from the role the model gives every person on the first pages (author, subject, quoted, editor…), and a person in the file name none of the page's authors shares a surname with wins: told to leave out whom the book quotes, the model gave the lectures on Proust the author "М. Пруст" 5 times of 5; asked roles, it still did 4 times of 5; with the rule in code, Merab Mamardashvili 5 of 5, and the blogs no author 5 of 5 instead of "автор" and "Меня".

## Tables

| Table | Key | Columns |
|---|---|---|
| `document_edits` (ReplacingMergeTree by `edited_at`) | `user_id, file_id` | `title`, `authors Array(String)`, `kind`, `year Nullable(UInt16)`, `confirmed`; an empty field is "not corrected" |
| `collections` (ReplacingMergeTree) | `user_id, collection_id` | `name`, `deleted` |
| `collection_files` (ReplacingMergeTree) | `user_id, collection_id, file_id` | `removed` |
| `documents_visible(user_id)` | view, one row per file | corrected fields with their sources, `confirmed`, `collections` |

## Slices

| # | Slice | Built | Done when |
|---|---|---|---|
| M0 | Terms do not name documents | `document_words` of a profile (автор, файл, блог, книга…): route drops a term every noun of which is one. Said in the prompt instead, qwen3:4b ignored it in 26 messages of 75 | the questions of chat `01a161e3` get no "автор" or "блог" terms; control questions unchanged |
| M1 | Metadata: extraction, correction, confirmation | `METADATA_PROMPT`: the author wrote the document, not the one it quotes; `document_edits`, `documents_visible`; `GET /api/documents` with sources, `PUT /api/files/{id}/document`, author and kind for a batch at upload; the documents on the chat page with a card to confirm or correct | the book's author is Mamardashvili; a correction survives a new index; another user does not see it |
| M2 | Groups of sources | `collections`, `collection_files`; the API to make, rename, delete a group and add or remove files; groups on the page | five blogs are one group "Блоги" |
| M3 | Scope of a chat | `Scope` in the contracts; activity `resolve_scope` behind `patched("scope")`; `file_ids` in retrieve, gather, similar, closest, summarize; `scope` in the message (be_service checks it); `GET /api/facets`; scope pickers on a new chat and its label on answers | a chat scoped to "Блоги" answers "trace the author's path" from `блог*.txt` only |
| M4 | Filters from the question | route matches the groups, authors and kinds the message names among the user's own (`where_in`, `kind_words` of a profile) | the same question in an unscoped chat picks the group "Блоги" itself; control questions get no filters |

The critical parts: the writes of `document_edits` and `collection_files` (only the user's own file and group, idempotent), and the scope filter (an `AND` over `*_visible` only), each with isolation tests: a scope with another user's `file_id` reads nothing; user B's corrections and groups are invisible to A; an empty scope is everything visible, not everything.

## Later

Comparing groups ("how the blogs differ from the Topology": a gather per group, then a comparison) · the design of a study and the metadata of papers from Crossref or PubMed, with the corpus on training · finding documents by topic (S7 `find_documents` of [documents-plan.md](documents-plan.md)) · merging authors (entity resolution).
