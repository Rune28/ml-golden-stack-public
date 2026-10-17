"""ClickHouse access of the jobs. Tables come from db/clickhouse/migrations.

samples are written by be_service (Add data); the jobs only read them and
write predictions and reports."""

import os
from datetime import datetime

import clickhouse_connect
import numpy as np

DATABASE = "ml_data"
PIXELS = 28 * 28

# RowBinary row of `SELECT idx, label, pixels`: UInt32, UInt8, then the
# array as a varint length (784 = 0x90 0x06) and 784 bytes. Fixed width, so
# numpy parses all rows in one go instead of building Python lists.
_ROW = np.dtype([("idx", "<u4"), ("label", "u1"), ("len", "u1", 2), ("pixels", "u1", PIXELS)])
_LEN_784 = (0x90, 0x06)

INSERT_CHUNK = 5000


def connect():
    return clickhouse_connect.get_client(
        host=os.environ["CLICKHOUSE_HOST"],
        port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
        username=os.environ["CLICKHOUSE_USER"],
        password=os.environ["CLICKHOUSE_PASSWORD"],
        database=DATABASE,
    )


def parse_samples(raw: bytes) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """RowBinary of (idx, label, pixels) -> idx (N,), labels (N,), images (N, 784)."""
    rows = np.frombuffer(raw, dtype=_ROW)
    if len(rows) * _ROW.itemsize != len(raw) or (rows["len"] != _LEN_784).any():
        raise ValueError(f"samples must have exactly {PIXELS} pixels")
    return rows["idx"].copy(), rows["label"].copy(), rows["pixels"].copy()


def read_split(client, split: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """All examples of a split and the batches they came from."""
    raw = client.raw_query(
        "SELECT idx, label, pixels FROM samples WHERE split = {s:String} ORDER BY idx FORMAT RowBinary",
        parameters={"s": split},
    )
    batches = client.query(
        "SELECT DISTINCT batch_id FROM samples WHERE split = {s:String} ORDER BY batch_id",
        parameters={"s": split},
    ).result_columns
    return (*parse_samples(raw), list(batches[0]) if batches else [])


def write_predictions(
    client,
    run_id: str,
    model_version: str,
    idx: np.ndarray,
    labels: np.ndarray,
    predicted: np.ndarray,
    confidence: np.ndarray,
) -> None:
    """Idempotent per run_id: a complete run is kept, a partial one (the
    previous attempt died mid-insert) is deleted and written again."""
    existing = client.query(
        "SELECT count() FROM predictions WHERE run_id = {r:String}", parameters={"r": run_id}
    ).result_rows[0][0]
    if existing == len(idx):
        return
    if existing:
        client.command("DELETE FROM predictions WHERE run_id = {r:String}", parameters={"r": run_id})

    for start in range(0, len(idx), INSERT_CHUNK):
        part = slice(start, start + INSERT_CHUNK)
        n = len(idx[part])
        client.insert(
            "predictions",
            [[run_id] * n, [model_version] * n, idx[part], labels[part], predicted[part], confidence[part]],
            column_names=["run_id", "model_version", "idx", "label", "predicted", "confidence"],
            column_oriented=True,
        )


def read_predictions(client, run_id: str) -> tuple[str, np.ndarray, np.ndarray]:
    """Model version, predicted digits and true digits of one prediction run."""
    result = client.query(
        "SELECT any(model_version), groupArray(predicted), groupArray(label) FROM predictions WHERE run_id = {r:String}",
        parameters={"r": run_id},
    ).result_rows[0]
    version, predicted, labels = result
    if not predicted:
        raise ValueError(f"no predictions for run {run_id}")
    return version, np.asarray(predicted, dtype=np.uint8), np.asarray(labels, dtype=np.uint8)


def write_digit_report(client, prediction_run_id: str, run_id: str, model_version: str, rows) -> None:
    """Append-only: every report run keeps its own rows, and readers take the
    latest one per prediction run from the digit_report_latest view. A retry
    keeps a complete report and rewrites a partial one; either way only the
    rows of this run are touched, never an earlier or a later report."""
    own = {"p": prediction_run_id, "r": run_id}
    done = client.query(
        "SELECT count() FROM digit_report WHERE prediction_run_id = {p:String} AND run_id = {r:String}",
        parameters=own,
    ).result_rows[0][0]
    if done == len(rows):
        return
    if done:
        client.command(
            "DELETE FROM digit_report WHERE prediction_run_id = {p:String} AND run_id = {r:String}", parameters=own
        )
    # report_run_id is the old name of run_id (migration 0005). Written as
    # well until the contract step drops it: Jobs of the previous image may
    # still be running and check their retries by it.
    client.insert(
        "digit_report",
        [[prediction_run_id, run_id, run_id, model_version, r.digit, r.recognized, r.actual] for r in rows],
        column_names=["prediction_run_id", "run_id", "report_run_id", "model_version", "digit", "recognized", "actual"],
    )


def commit_run(client, kind: str, run_id: str, started_at: datetime, rows: int, parents: dict[str, str]) -> None:
    """The last action of a step that wrote rows: they are complete. Readers
    see a run only through views over the `runs` registry. A repeated commit
    (the Job retried after this point) is a duplicate that merges away.
    A copy in llm_worker/src/workflow/activities/index.py commits stream
    runs: keep the two writing the same marker."""
    client.insert(
        "runs",
        [[run_id, kind, parents, started_at, rows]],
        column_names=["run_id", "kind", "parents", "started_at", "rows"],
    )

