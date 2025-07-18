from collections import Counter
from typing import Callable, List

from llama_index.core.bridge.pydantic import Field
from llama_index.core.base.embeddings.base_sparse import (
    BaseSparseEmbedding,
    SparseEmbedding,
)


def get_default_tokenizer() -> Callable:
    try:
        from transformers import BertTokenizerFast
    except ImportError:
        raise ImpotError(
            "In order to run `llama-index-vector-stores-moorcheh` you need to have `transformers` installed. Please run `pip install transformers`"
        )

    orig_tokenizer = BertTokenizerFast.from_pretrained("bert-base-uncased")

    def _tokenizer(texts: List[str]) -> List[List[int]]:
        return orig_tokenizer(texts, padding=True, truncation=True, max_length=512)[
            "input_ids"
        ]

    return _tokenizer


class DefaultMoorchehSparseEmbedding(BaseSparseEmbedding):
    """Default Moorcheh sparse embedding."""

    tokenizer: Callable = Field(
        default_factory=get_default_tokenizer,
        description="A callable that returns token input ids.",
    )

    def build_sparse_embeddings(
        self, input_batch: List[List[int]]
    ) -> List[SparseEmbedding]:
        # store a batch of sparse embeddings
        sparse_emb_list = []
        # iterate through input batch
        for token_ids in input_batch:
            sparse_emb = {}
            # convert the input_ids list to a dictionary of key to frequency values
            d = dict(Counter(token_ids))
            for idx in d:
                sparse_emb[idx] = float(d[idx])
            sparse_emb_list.append(sparse_emb)
        # return sparse_emb list
        return sparse_emb_list

    def _get_query_embedding(self, query: str) -> SparseEmbedding:
        """Embed the input query synchronously."""
        token_ids = self.tokenizer([query])[0]
        return self.build_sparse_embeddings([token_ids])[0]

    async def _aget_query_embedding(self, query: str) -> SparseEmbedding:
        """Embed the input query asynchronously."""
        return self._get_query_embedding(query)

    def _get_text_embedding(self, text: str) -> SparseEmbedding:
        """Embed the input text synchronously."""
        return self._get_query_embedding(text)

    async def _aget_text_embedding(self, text: str) -> SparseEmbedding:
        """Embed the input text asynchronously."""
        return self._get_query_embedding(text)
