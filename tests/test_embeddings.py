"""Embeddings configuration and the vector-indexed memory path.

No test here talks to a real provider. FAKE_MODEL=true swaps in a deterministic
hash embedder: the vectors mean nothing, but identical text maps to the same
vector, which is enough to prove that search ranks by the query rather than
returning arbitrary facts.
"""

import asyncio
import logging

import pytest
from langchain_core.embeddings import Embeddings
from starlette.testclient import TestClient
from tests.conftest import TEST_DATABASE_URL, TestApp, build_settings, requires_infra

from waypoint.agents.models import EmbeddingsConfigError, build_embeddings, check_embedding_dims

FACTS = ["prefers metric units", "works in aerospace", "allergic to meetings before ten"]


def test_unset_means_no_index() -> None:
    assert build_embeddings(build_settings()) is None


def test_openai_without_a_key_fails_at_boot_with_the_fix_in_the_message() -> None:
    settings = build_settings(
        fake_model=False, embeddings_model="openai:text-embedding-3-small", openai_api_key=None
    )
    with pytest.raises(EmbeddingsConfigError, match="OPENAI_API_KEY"):
        build_embeddings(settings)


def test_blank_key_from_compose_counts_as_unset() -> None:
    assert build_settings(openai_api_key="  ").openai_api_key is None


def test_spec_without_a_provider_is_rejected() -> None:
    settings = build_settings(fake_model=False, embeddings_model="text-embedding-3-small")
    with pytest.raises(EmbeddingsConfigError, match="provider:model"):
        build_embeddings(settings)


def test_openai_key_is_passed_explicitly_not_read_from_os_environ() -> None:
    settings = build_settings(
        fake_model=False,
        embeddings_model="openai:text-embedding-3-small",
        openai_api_key="sk-test-not-real",
    )
    embeddings = build_embeddings(settings)
    assert embeddings is not None
    key = getattr(embeddings, "openai_api_key", None)
    assert key is not None
    assert key.get_secret_value() == "sk-test-not-real"


class _FixedEmbeddings(Embeddings):
    def __init__(self, dims: int = 3, error: Exception | None = None) -> None:
        self.dims, self.error = dims, error

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        if self.error:
            raise self.error
        return [0.0] * self.dims


class _AuthError(Exception):
    status_code = 401


def test_dimension_mismatch_stops_the_boot() -> None:
    with pytest.raises(EmbeddingsConfigError, match="EMBEDDINGS_DIMS=3"):
        asyncio.run(check_embedding_dims(_FixedEmbeddings(dims=3), expected=1536))


def test_rejected_key_stops_the_boot() -> None:
    with pytest.raises(EmbeddingsConfigError, match="401"):
        asyncio.run(check_embedding_dims(_FixedEmbeddings(error=_AuthError()), expected=3))


def test_network_error_is_logged_not_fatal(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        asyncio.run(check_embedding_dims(_FixedEmbeddings(error=ConnectionError()), expected=3))
    assert "not verified" in caplog.text


def _assert_search_ranks_by_query(client: TestClient, headers: dict[str, str]) -> None:
    for fact in FACTS:
        created = client.post("/api/v1/memories", json={"fact": fact}, headers=headers)
        assert created.status_code == 201
    for fact in FACTS:
        found = client.get(
            "/api/v1/memories", params={"q": fact, "limit": 1}, headers=headers
        ).json()["memories"]
        assert [m["value"]["fact"] for m in found] == [fact]


def test_in_memory_store_gets_the_vector_index(make_app: object) -> None:
    app: TestApp = make_app(embeddings_model="openai:text-embedding-3-small", embeddings_dims=64)  # type: ignore[operator]
    _assert_search_ranks_by_query(app.test_client(), {})


@pytest.mark.integration
@requires_infra
def test_pgvector_store_ranks_by_query(make_app: object, auth: dict[str, str]) -> None:
    """Real Postgres: setup() must create the extension and store_vectors table."""
    from tests.conftest import BOOTSTRAP_KEY, TEST_REDIS_URL

    app: TestApp = make_app(  # type: ignore[operator]
        database_url=TEST_DATABASE_URL,
        redis_url=TEST_REDIS_URL,
        auth_enabled=True,
        bootstrap_api_key=BOOTSTRAP_KEY,
        embeddings_model="openai:text-embedding-3-small",
        embeddings_dims=1536,
    )
    _assert_search_ranks_by_query(app.test_client(), auth)
