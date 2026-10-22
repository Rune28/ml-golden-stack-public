-- One row per file of the user: what the document is, the user's correction
-- over what read_document found (docs/search-scope-plan.md). Every field
-- comes with its source, edit or text; a file not read yet has empty fields
-- from text. The groups of the file are the user's. The document page, the
-- facets of a chat and the scope of an answer read this view.
CREATE VIEW IF NOT EXISTS ml_data.documents_visible AS
SELECT
    f.file_id AS file_id,
    o.name AS name,
    d.document_id AS document_id,
    d.run_id AS document_run_id,
    if(has(e.corrected, 'title'), e.title, d.title) AS title,
    if(has(e.corrected, 'title'), 'edit', 'text') AS title_source,
    if(has(e.corrected, 'authors'), e.authors, a.authors) AS authors,
    if(has(e.corrected, 'authors'), 'edit', 'text') AS authors_source,
    if(has(e.corrected, 'kind'), e.kind, d.kind) AS kind,
    if(has(e.corrected, 'kind'), 'edit', 'text') AS kind_source,
    if(has(e.corrected, 'year'), e.year, d.year) AS year,
    if(has(e.corrected, 'year'), 'edit', 'text') AS year_source,
    e.confirmed AS confirmed,
    d.keywords AS keywords,
    c.collection_ids AS collection_ids,
    o.added_at AS uploaded_at
FROM ml_data.files AS f FINAL
INNER JOIN (
    SELECT file_id, name, added_at FROM ml_data.file_owners FINAL WHERE user_id = {user_id:String}
) AS o ON o.file_id = f.file_id
LEFT JOIN (
    SELECT run_id, file_id, document_id, title, year, kind, keywords FROM ml_data.sat_document_current
) AS d ON d.file_id = f.file_id
LEFT JOIN (
    SELECT run_id, arrayMap(p -> p.2, arraySort(groupArray((position, name)))) AS authors
    FROM ml_data.link_document_author_current GROUP BY run_id
) AS a ON a.run_id = d.run_id
LEFT JOIN (
    SELECT file_id, corrected, title, authors, kind, year, confirmed
    FROM ml_data.document_edits FINAL WHERE user_id = {user_id:String}
) AS e ON e.file_id = f.file_id
LEFT JOIN (
    SELECT cf.file_id AS file_id, groupArray(cf.collection_id) AS collection_ids
    FROM ml_data.collection_files AS cf FINAL
    WHERE cf.user_id = {user_id:String} AND NOT cf.removed
    AND cf.collection_id IN (SELECT collection_id FROM ml_data.collections FINAL WHERE user_id = {user_id:String} AND NOT deleted)
    GROUP BY cf.file_id
) AS c ON c.file_id = f.file_id
