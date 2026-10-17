"""The pipeline steps. Each returns a JSON-serialisable dict, the
contract with the batch_worker activity that started the Job."""

import json
import logging
from datetime import datetime

import mlflow
import mlflow.pytorch
import numpy as np
from mlflow.models import ModelSignature
from mlflow.types import Schema, TensorSpec

from jobs import clickhouse, registry
from mnist.model import IMAGE_SIZE, NUM_CLASSES
from mnist.report import digit_report, most_biased
from mnist.training import TrainConfig, accuracy, predict, to_tensor, train

log = logging.getLogger(__name__)

# Images normalised to [0, 1] in, logits out.
SIGNATURE = ModelSignature(
    inputs=Schema([TensorSpec(np.dtype(np.float32), (-1, 1, IMAGE_SIZE, IMAGE_SIZE))]),
    outputs=Schema([TensorSpec(np.dtype(np.float32), (-1, NUM_CLASSES))]),
)


def train_step(run_id: str, epochs: int, pool: str) -> dict:
    """Train on every train example in ClickHouse, evaluate on the test split,
    register a new version of mnist-cnn. Promotion is decided by the
    workflow, from `accuracy` and `champion` in the result."""
    client = clickhouse.connect()
    _, labels, images, batches = clickhouse.read_split(client, "train")
    _, test_labels, test_images, _ = clickhouse.read_split(client, "test")
    if len(images) == 0:
        raise ValueError("no train examples in ClickHouse: add data first")
    log.info("training on %d examples from %d batches", len(images), len(batches))

    config = TrainConfig(epochs=epochs)
    mlflow.set_experiment(registry.EXPERIMENT)
    with mlflow.start_run(run_name=run_id) as run:
        mlflow.set_tags({"pipeline_run_id": run_id, "pool": pool})
        mlflow.log_params({"train_samples": len(images), "train_batches": len(batches), **vars(config)})
        # Lineage: which batches (be_service batch_id) the model was trained on.
        mlflow.log_text(json.dumps(batches), "train_batches.json")

        model = train(images, labels, config, on_epoch=lambda e, loss: mlflow.log_metric("loss", loss, step=e))
        test_accuracy = accuracy(predict(model, test_images)[0], test_labels)
        mlflow.log_metric("accuracy", test_accuracy)

        # pt2 (torch.export, the default format) traces the model on the
        # example and needs a tensor signature: the model's contract. The
        # example has 2 images: torch.export fixes a batch dimension of size 1
        # as a constant, and the loaded model would accept only 1 image.
        info = mlflow.pytorch.log_model(
            model,
            name="model",
            registered_model_name=registry.MODEL_NAME,
            input_example=to_tensor(test_images[:2]).numpy(),
            signature=SIGNATURE,
        )

    return {
        "run_id": run_id,
        "version": str(info.registered_model_version),
        "accuracy": test_accuracy,
        "train_samples": len(images),
        "mlflow_run_id": run.info.run_id,
        "champion": registry.champion(),
    }


def predict_step(run_id: str, model: str, started_at: datetime) -> dict:
    """Predict the whole test split with a model version or alias, write the
    predictions under run_id."""
    version = registry.resolve(model)
    net = mlflow.pytorch.load_model(f"models:/{registry.MODEL_NAME}/{version}")

    client = clickhouse.connect()
    idx, labels, images, _ = clickhouse.read_split(client, "test")
    predicted, confidence = predict(net, images)
    clickhouse.write_predictions(client, run_id, version, idx, labels, predicted, confidence)
    clickhouse.commit_run(client, "predict", run_id, started_at, len(idx), {"model_version": version})

    return {"run_id": run_id, "model_version": version, "accuracy": accuracy(predicted, labels), "rows": len(idx)}


def promote_step(version: str) -> dict:
    registry.set_champion(version)
    return {"champion": version}


def report_step(run_id: str, prediction_run_id: str, started_at: datetime) -> dict:
    """The distribution of recognized digits in a prediction run, next to the
    true one: what the recognition adds up to, and where the model leans."""
    client = clickhouse.connect()
    version, predicted, labels = clickhouse.read_predictions(client, prediction_run_id)
    rows = digit_report(predicted, labels)
    clickhouse.write_digit_report(client, prediction_run_id, run_id, version, rows)
    clickhouse.commit_run(client, "report", run_id, started_at, len(rows), {"prediction_run_id": prediction_run_id})

    over, under = most_biased(rows)
    return {
        "run_id": run_id,
        "prediction_run_id": prediction_run_id,
        "model_version": version,
        "total": int(len(predicted)),
        "digits": [{"digit": r.digit, "recognized": r.recognized, "actual": r.actual} for r in rows],
        "most_over": {"digit": over.digit, "bias": over.bias},
        "most_under": {"digit": under.digit, "bias": under.bias},
    }

