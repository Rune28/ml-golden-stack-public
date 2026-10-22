-- A user's groups of sources ("Блоги", "Лекции Мамардашвили"): what a chat
-- can be scoped to. Reference data of the user, written by be_service; the
-- id is made there (UUID v7). A rename or a deletion writes the row again,
-- the latest by changed_at wins; a deleted group stays as a row with
-- deleted = true. Readers use collections_visible.
CREATE TABLE ml_data.collections
(
    user_id       String,
    collection_id String,
    name          String,
    deleted       Bool DEFAULT false,
    changed_at    DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(changed_at)
ORDER BY (user_id, collection_id)
