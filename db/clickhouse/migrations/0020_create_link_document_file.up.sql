-- Which files a document came in: a preprint and the published version are
-- two files of one document.
CREATE VIEW IF NOT EXISTS ml_data.link_document_file AS
SELECT DISTINCT document_id, file_id
FROM ml_data.sat_document_current
