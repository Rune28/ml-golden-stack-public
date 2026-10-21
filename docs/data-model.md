# Data model

The stores of the platform and what lives in each, as it stands after the login stage ([auth-plan.md](auth-plan.md)). Tables and views marked **A2** are planned, not built yet; the rest is in `db/clickhouse/migrations`. The MNIST tables (`samples`, `predictions`, `digit_report`) are left out: the login stage does not touch them.

Two stores, by what the data needs:

- **Postgres** (database `chat`, owned by chat_service): users, how they log in, sessions. Data that changes in place and needs transactions and point reads.
- **ClickHouse** (database `ml_data`): files, the outputs of runs and what readers see of them. Append-only rows under the `run_id` of the run that wrote them (rule 5), seen through views over the run registry (rule 11).

## Postgres: `chat` (A1)

```mermaid
erDiagram
    users ||--o{ identities : "logs in by"
    users ||--o{ sessions : "has"

    users {
        uuid user_id PK "ours; stays when an identity provider comes"
        text email UK "lowercase"
        timestamptz created_at
        timestamptz disabled_at "NULL while active"
    }
    identities {
        text provider PK "password now, oidc:issuer later"
        text subject PK "the email now, the provider's sub later"
        uuid user_id FK
        text secret "argon2id hash; empty for OIDC"
    }
    sessions {
        bytea token_hash PK "sha256 of the cookie's token"
        uuid user_id FK
        timestamptz created_at
        timestamptz expires_at "12 hours, renewed while used"
        timestamptz revoked_at "logout"
    }
    login_failures {
        text email "index (email, at)"
        timestamptz at "5 in 15 minutes: refused (429)"
    }
```

A user is our own `user_id`; how they log in is an identity. An identity provider later is another row in `identities` for the same `user_id`, so nothing that refers to users changes.

## ClickHouse: `ml_data`, tables

Relations are logical: ClickHouse has no foreign keys, and tables join on keys that keep their name in every table (rule 5).

```mermaid
erDiagram
    pg_users ||--o{ file_owners : "user_id (A2)"
    pg_users ||--o{ chat_owners : "user_id (A2)"
    files ||--o{ file_owners : "file_id"
    chat_owners ||--|{ chat_messages : "chat_id"

    files ||--o{ doc_chunks : "file_id"
    files ||--o{ sat_document : "file_id"
    files ||--o{ document_themes : "file_id"
    sat_document ||--o{ link_document_author : "run_id of the document run"
    doc_chunks ||--o{ chunk_mentions : "index_run_id + chunk_id"
    doc_chunks ||--o{ chat_messages : "sources = file_id, index_run_id, chunk_id"

    runs ||--|{ doc_chunks : "kind=index"
    runs ||--|{ sat_document : "kind=document"
    runs ||--|{ chunk_mentions : "kind=mentions"
    runs ||--|{ chat_messages : "kind=answer"

    pg_users {
        uuid user_id "lives in Postgres chat.users"
    }
    files {
        String file_id PK "sha256 of the bytes; reference data"
        String name
        UInt64 size
        String object_key "files/file_id in the object store"
        DateTime64 uploaded_at
    }
    file_owners {
        String user_id PK "A2"
        String file_id PK "A2; a link, not a column"
        DateTime added_at
    }
    chat_owners {
        String user_id PK "A2"
        String chat_id PK "A2"
    }
    runs {
        String run_id PK "the run registry; the marker comes last"
        String kind "index, document, mentions, answer, predict, report"
        Map parents "lineage: file_id, index_run_id, embed_model, ..."
        DateTime64 started_at "logical start, the same on retries"
        UInt64 rows
    }
    doc_chunks {
        String run_id PK "index run"
        UInt32 chunk_id PK
        String file_id
        String embed_model
        String text
        Array_Float32 embedding "bge-m3, 1024"
    }
    sat_document {
        String run_id PK "document run"
        String file_id
        String document_id "doi:, isbn:, file:"
        String title
        UInt16 year
        String kind "article, book, notes, other"
        String abstract
        Array_String keywords "given by the authors"
        Array_Float32 abstract_embedding
    }
    link_document_author {
        String run_id PK
        String document_id
        String author_id "orcid: or name:surname initials"
        String name
        UInt16 position
    }
    chunk_mentions {
        String run_id PK "mentions run"
        String index_run_id
        UInt32 chunk_id
        String file_id
        String term "a lemma; ORDER BY term"
        UInt8 rank "1 to 5 within the chunk"
        Float32 score
    }
    document_themes {
        String run_id PK "the same mentions run"
        String file_id
        String term
        UInt32 rank "every theme; readers take the top N"
        UInt32 chunks
        Float32 share
        Float32 zipf
    }
    chat_messages {
        String run_id PK "answer run"
        String chat_id
        UInt32 turn
        String role "user, assistant"
        String text
        Array_Tuple sources
    }
```

## ClickHouse: layers of views

```mermaid
flowchart LR
    subgraph T["tables: written by runs"]
        dc[doc_chunks]
        sd[sat_document]
        la[link_document_author]
        cm[chunk_mentions]
        dt[document_themes]
        msg[chat_messages]
    end
    subgraph C["*_current: the latest committed run, of a file that exists"]
        dcc[doc_chunks_current]
        sdc[sat_document_current]
        lac[link_document_author_current]
        cmc[chunk_mentions_current]
        dtc[document_themes_current]
        mc[chat_messages_committed]
    end
    subgraph H["Data Vault hubs: views of keys"]
        hd[hub_document]
        ha[hub_author]
        ldf[link_document_file]
    end
    subgraph V["*_visible(user_id): the isolation boundary, A2"]
        fv[files_visible]
        dcv[doc_chunks_visible]
        dco[doc_chunks_owned]
        sdv[sat_document_visible]
        lav[link_document_author_visible]
        cmv[chunk_mentions_visible]
        dtv[document_themes_visible]
        mv[chat_messages_visible]
    end
    runs[(runs)] -.marker.-> C
    fo[(file_owners)] -.-> V
    co[(chat_owners)] -.-> mv

    dc --> dcc --> dcv
    dc --> dco
    sd --> sdc --> sdv
    sdc --> hd
    sdc --> ldf
    la --> lac --> lav
    lac --> ha
    cm --> cmc --> cmv
    dt --> dtc --> dtv
    msg --> mc --> mv

    V --> R["readers: be_service visible.rs,<br/>retrieve, gather, summarize, generate"]
```

- **Writers write tables only**, each row under the `run_id` of its run, the marker in `runs` last.
- **`*_current`** leaves out runs halfway, runs replaced by a newer one, and files deleted since (rule 11).
- **Hubs** (`hub_document`, `hub_author`, `link_document_file`) are views of the keys of current runs and have no table of their own: their keys are found by a model, so a key of a failed or replaced run must not stay visible.
- **`*_visible(user_id)`** (A2) is all that be_service and the steps of an answer read: a user sees a file only through their own link in `file_owners`.
- **`doc_chunks_owned`** (A2) reads texts by key from every run, of the user's files only: an index committed while an answer is being written must not hide the chunks the answer is built from.
