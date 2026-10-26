import copy
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_service.runtime.service import LLMService


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configured,requested,expected",
    [(1024, None, 1024), (1024, 512, 512), (None, None, None)],
)
async def test_embedding_submission_resolves_dimensions(
    config, configured, requested, expected,
):
    config = copy.deepcopy(config)
    config["embedding"]["dimensions"] = configured
    service = LLMService(MagicMock(), MagicMock(), config)
    service._submit_with_idempotency = AsyncMock(return_value="task-id")

    task_id = await service.submit_embedding(["sample"], dimensions=requested)

    assert task_id == "task-id"
    submitted = service._submit_with_idempotency.call_args.kwargs
    payload = json.loads(submitted["request_params"][4])
    assert payload == {
        "texts": ["sample"], "model": "embedding-3", "dimensions": expected,
    }


@pytest.mark.asyncio
async def test_embedding_submission_without_configured_dimensions(config):
    config = copy.deepcopy(config)
    config["embedding"].pop("dimensions")
    service = LLMService(MagicMock(), MagicMock(), config)
    service._submit_with_idempotency = AsyncMock(return_value="task-id")

    await service.submit_embedding(["sample"])

    submitted = service._submit_with_idempotency.call_args.kwargs
    assert json.loads(submitted["request_params"][4])["dimensions"] is None
