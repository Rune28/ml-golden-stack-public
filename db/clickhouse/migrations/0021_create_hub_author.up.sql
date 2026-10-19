-- The author hub: the keys of the authors of current documents, a view for
-- the same reason as hub_document.
CREATE VIEW IF NOT EXISTS ml_data.hub_author AS
SELECT DISTINCT author_id
FROM ml_data.link_document_author_current
