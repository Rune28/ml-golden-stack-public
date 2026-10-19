-- The document hub: the keys of the documents there are now. A view and not
-- a table, since document_id is found in the text by a run: a key written by
-- a failed attempt or by a run replaced since must not stay visible.
CREATE VIEW IF NOT EXISTS ml_data.hub_document AS
SELECT DISTINCT document_id
FROM ml_data.sat_document_current
