-- What a document is, read from one uploaded file by the read_document step:
-- title, year, kind, abstract and the keywords its authors gave. A satellite
-- in Data Vault terms, of the document hub. The own key is run_id of the
-- document run (README, rule 5): a retried run rewrites only its own rows.
-- file_id is the input; document_id is the business key (doi:, isbn:, or
-- file: when neither is found), so a preprint and the published version, two
-- files, are one document. The link of a document to its files is this
-- table too: a run reads one file. Readers use sat_document_current; the hub
-- document is a view of the keys of current runs, since the keys are found by
-- a model and a failed or replaced run must not leave one behind.
CREATE TABLE ml_data.sat_document
(
    run_id             String,
    file_id            String,
    document_id        String,
    title              String,
    year               Nullable(UInt16),
    kind               LowCardinality(String),
    abstract           String,
    keywords           Array(String),
    abstract_embedding Array(Float32),
    embed_model        LowCardinality(String),
    created_at         DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (document_id, run_id)
