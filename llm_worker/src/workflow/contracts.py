"""Inputs and outputs of the workflows and activities of the `llm` queue.

These dataclasses are the contract with whoever starts the workflows
(be_service): they travel as JSON, so field names are the API. New fields
only with defaults, so running workflows keep decoding.
"""

from dataclasses import dataclass


@dataclass
class IndexInput:
    # The uploaded file, made by be_service (POST /api/files).
    file_id: str
    run_id: str = ""


@dataclass
class IndexResult:
    run_id: str
    file_id: str
    name: str
    chunks: int
    chars: int
    embed_model: str
