-- Who wrote a document, as read by the same document run as sat_document:
-- a link of the document hub to the author hub, in the order the authors are
-- listed. author_id is orcid: when the text has one, otherwise the surname
-- and initials, normalized; merging two keys of one person is later. Own key
-- run_id, inherited file_id (README, rule 5).
CREATE TABLE ml_data.link_document_author
(
    run_id      String,
    file_id     String,
    document_id String,
    author_id   String,
    name        String,
    position    UInt16,
    created_at  DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (author_id, document_id, run_id)
