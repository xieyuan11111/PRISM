"""Deterministic Graphiti 0.29.3 model clients for opt-in live tests only.

The clients perform no network I/O.  ``DeterministicGraphitiClientFactory``
still delegates database construction to PRISM's real client builder, so the
result uses the configured Neo4j URI and credentials while replacing all
three provider defaults that would otherwise use OpenAI.
"""

from __future__ import annotations

import json
import re
from typing import Any

from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.embedder.client import EMBEDDING_DIM, EmbedderClient
from graphiti_core.llm_client.client import LLMClient

from prism.config import GraphitiConfig
from prism.graph.graphiti_client import build_graphiti_client


class DeterministicLLMClient(LLMClient):
    """Return the small fixed extraction graph needed by the live boundary.

    Every episode is linked to two episode-specific synthetic test entities. A
    real Graphiti write therefore persists an episodic node and an entity edge
    carrying its Graphiti-assigned episode UUID, which is enough to exercise
    PRISM's write/search/registry behavior without external model inference.
    """

    def __init__(self) -> None:
        super().__init__(config=None, cache=False)
        self.response_models: list[str] = []
        self._episode_key = "unparsed-episode"
        self._case_id = "unparsed-case"

    def _remember_episode(self, messages: list[Any]) -> None:
        """Capture only PRISM's synthetic identifiers from the current JSON."""
        for message in messages:
            match = re.search(
                r"<JSON>\s*(\{.*?\})\s*</JSON>",
                str(message.content),
                flags=re.DOTALL,
            )
            if match is None:
                continue
            payload = json.loads(match.group(1))
            self._episode_key = str(payload["episode_key"])
            self._case_id = str(payload["case_id"])
            return

    @property
    def _subject_name(self) -> str:
        return f"PRISM live subject {self._episode_key}"

    @property
    def _record_name(self) -> str:
        return f"PRISM live record {self._episode_key}"

    async def _generate_response(
        self,
        messages: list[Any],
        response_model: type[Any] | None = None,
        max_tokens: int = 0,
        model_size: Any = None,
    ) -> dict[str, Any]:
        del max_tokens, model_size
        model_name = response_model.__name__ if response_model is not None else ""
        self.response_models.append(model_name)

        if model_name == "ExtractedEntities":
            self._remember_episode(messages)
            return {
                "extracted_entities": [
                    {
                        "name": self._subject_name,
                        "entity_type_id": 0,
                        "episode_indices": [0],
                    },
                    {
                        "name": self._record_name,
                        "entity_type_id": 0,
                        "episode_indices": [0],
                    },
                ]
            }
        if model_name == "ExtractedEdges":
            return {
                "edges": [
                    {
                        "source_entity_name": self._subject_name,
                        "target_entity_name": self._record_name,
                        "relation_type": "HAS_PRISM_LIVE_RECORD",
                        "fact": (
                            f"{self._case_id} has PRISM live record "
                            f"{self._episode_key}"
                        ),
                        "valid_at": None,
                        "invalid_at": None,
                        "episode_indices": [0],
                    }
                ]
            }
        if model_name == "EdgeTimestamps":
            return {"valid_at": None, "invalid_at": None}
        if model_name == "NodeResolutions":
            return {
                "entity_resolutions": [
                    {
                        "id": 0,
                        "name": self._subject_name,
                        "duplicate_candidate_id": -1,
                    },
                    {
                        "id": 1,
                        "name": self._record_name,
                        "duplicate_candidate_id": -1,
                    },
                ]
            }
        if model_name == "EdgeDuplicate":
            return {"duplicate_facts": [], "contradicted_facts": []}
        if model_name == "SummarizedEntities":
            return {"summaries": []}
        raise AssertionError(
            f"deterministic live LLM has no response for {model_name!r}"
        )


class DeterministicEmbedder(EmbedderClient):
    """Return a valid, constant Graphiti-dimension vector without a provider."""

    def __init__(self) -> None:
        self.calls = 0
        self._vector = [1.0] + [0.0] * (EMBEDDING_DIM - 1)

    async def create(self, input_data: Any) -> list[float]:
        del input_data
        self.calls += 1
        return list(self._vector)

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        self.calls += len(input_data_list)
        return [list(self._vector) for _ in input_data_list]


class DeterministicCrossEncoder(CrossEncoderClient):
    """Preserve input order with deterministic descending scores."""

    def __init__(self) -> None:
        self.calls = 0

    async def rank(
        self, query: str, passages: list[str]
    ) -> list[tuple[str, float]]:
        del query
        self.calls += 1
        count = max(len(passages), 1)
        return [
            (passage, (count - index) / count)
            for index, passage in enumerate(passages)
        ]


class DeterministicGraphitiClientFactory:
    """Create real Neo4j-backed Graphiti clients with no provider defaults."""

    def __init__(self) -> None:
        self.calls: list[GraphitiConfig] = []
        self.llm_clients: list[DeterministicLLMClient] = []
        self.embedders: list[DeterministicEmbedder] = []
        self.cross_encoders: list[DeterministicCrossEncoder] = []

    def __call__(self, config: GraphitiConfig) -> Any:
        llm_client = DeterministicLLMClient()
        embedder = DeterministicEmbedder()
        cross_encoder = DeterministicCrossEncoder()
        client = build_graphiti_client(
            config,
            llm_client=llm_client,
            embedder=embedder,
            cross_encoder=cross_encoder,
        )
        self.calls.append(config)
        self.llm_clients.append(llm_client)
        self.embedders.append(embedder)
        self.cross_encoders.append(cross_encoder)
        return client
