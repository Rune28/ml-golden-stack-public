"""Entry point of the image: `python -m jobs <step> ...`.

Every step prints its result as JSON and, with --result-key, writes it to
the object store, where the batch_worker activity reads it after the Job ends.
"""

import argparse
import json
import logging
import os
from datetime import datetime, timezone

import boto3
import torch

from jobs import steps


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Match the container CPU limit instead of every core of the node.
    torch.set_num_threads(int(os.environ.get("TORCH_THREADS", "2")))

    parser = argparse.ArgumentParser(prog="jobs")
    parser.add_argument("--result-key", help="object key for result.json in S3_BUCKET")
    # batch_worker passes when the workflow scheduled the step: the same on every
    # retry, so the run registry orders runs by when they were asked for.
    parser.add_argument(
        "--started-at", type=datetime.fromisoformat, help="logical start of the run, ISO 8601; now when absent"
    )
    sub = parser.add_subparsers(dest="step", required=True)

    p = sub.add_parser("train", help="train a new model version on all train examples")
    p.add_argument("--run-id", required=True)
    p.add_argument("--epochs", type=int, default=3)
    p.add_argument("--pool", default="cpu", help="node pool the Job runs on, recorded in MLflow")
    p.set_defaults(run=lambda a: steps.train_step(a.run_id, a.epochs, a.pool))

    p = sub.add_parser("predict", help="predict the test split and write the predictions")
    p.add_argument("--run-id", required=True)
    p.add_argument("--model", default="champion", help="model version or alias")
    p.set_defaults(run=lambda a: steps.predict_step(a.run_id, a.model, a.started_at))

    p = sub.add_parser("report", help="distribution of recognized digits in a prediction run")
    p.add_argument("--run-id", required=True)
    p.add_argument("--prediction-run-id", required=True)
    p.set_defaults(run=lambda a: steps.report_step(a.run_id, a.prediction_run_id, a.started_at))

    p = sub.add_parser("promote", help="move the champion alias to a version")
    p.add_argument("--version", required=True)
    p.set_defaults(run=lambda a: steps.promote_step(a.version))

    args = parser.parse_args()
    args.started_at = args.started_at or datetime.now(timezone.utc)
    payload = json.dumps(args.run(args))

    if args.result_key:
        # S3 settings from the environment: AWS_ENDPOINT_URL_S3 and the keys.
        boto3.client("s3").put_object(
            Bucket=os.environ.get("S3_BUCKET", "pipeline"), Key=args.result_key, Body=payload.encode()
        )
    print(payload)


if __name__ == "__main__":
    main()
