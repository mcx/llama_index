"""
Redis Vector store index.

An index that is built on top of an existing vector store.
"""

import logging
from typing import Any, Dict, List, Optional, Pattern

import fsspec
import re
from redis import Redis
import redis.asyncio as redis_async
from redis.asyncio import Redis as RedisAsync
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redisvl.index import SearchIndex, AsyncSearchIndex
from redisvl.query import CountQuery, FilterQuery, VectorQuery
from redisvl.query.filter import FilterExpression, Tag
from redisvl.redis.utils import array_to_buffer
from redisvl.schema import IndexSchema
from redisvl.schema.fields import BaseField

from llama_index.core.bridge.pydantic import PrivateAttr
from llama_index.core.schema import (
    BaseNode,
    MetadataMode,
    NodeRelationship,
    RelatedNodeInfo,
    TextNode,
)
from llama_index.core.vector_stores.types import (
    BasePydanticVectorStore,
    MetadataFilter,
    MetadataFilters,
    VectorStoreQuery,
    VectorStoreQueryResult,
    FilterOperator,
)
from llama_index.core.vector_stores.utils import (
    metadata_dict_to_node,
    node_to_metadata_dict,
)
from llama_index.vector_stores.redis.schema import (
    DOC_ID_FIELD_NAME,
    NODE_CONTENT_FIELD_NAME,
    NODE_ID_FIELD_NAME,
    TEXT_FIELD_NAME,
    VECTOR_FIELD_NAME,
    RedisVectorStoreSchema,
)
from llama_index.vector_stores.redis.utils import REDIS_LLAMA_FIELD_SPEC

logger = logging.getLogger(__name__)
NO_DOCS = "No docs found on index"


class TokenEscaper:
    """
    Escape punctuation within an input string. Taken from RedisOM Python.
    """

    # Characters that RediSearch requires us to escape during queries.
    # Source: https://redis.io/docs/stack/search/reference/escaping/#the-rules-of-text-field-tokenization
    DEFAULT_ESCAPED_CHARS = r"[,.<>{}\[\]\\\"\':;!@#$%^&*()\-+=~\/ ]"

    def __init__(self, escape_chars_re: Optional[Pattern] = None):
        if escape_chars_re:
            self.escaped_chars_re = escape_chars_re
        else:
            self.escaped_chars_re = re.compile(self.DEFAULT_ESCAPED_CHARS)

    def escape(self, value: str) -> str:
        def escape_symbol(match: re.Match) -> str:
            value = match.group(0)
            return f"\\{value}"

        return self.escaped_chars_re.sub(escape_symbol, value)


class RedisVectorStore(BasePydanticVectorStore):
    """
    RedisVectorStore.

    The RedisVectorStore takes a user-defined schema object and a Redis connection
    client or URL string. The schema is optional, but useful for:
    - Defining a custom index name, key prefix, and key separator.
    - Defining *additional* metadata fields to use as query filters.
    - Setting custom specifications on fields to improve search quality, e.g
    which vector index algorithm to use.

    Other Notes:
    - All embeddings and docs are stored in Redis. During query time, the index
    uses Redis to query for the top k most similar nodes.
    - Redis & LlamaIndex expect at least 4 *required* fields for any schema, default or custom,
    `id`, `doc_id`, `text`, `vector`.

    Args:
        schema (IndexSchema, optional): Redis index schema object.
        redis_client (Redis, optional): Redis client connection.
        redis_url (str, optional): Redis server URL.
            Defaults to "redis://localhost:6379".
        overwrite (bool, optional): Whether to overwrite the index if it already exists.
            Defaults to False.

    Raises:
        ValueError: If your Redis server does not have search or JSON enabled.
        ValueError: If a Redis connection failed to be established.
        ValueError: If an invalid schema is provided.

    Example:
        from redisvl.schema import IndexSchema
        from llama_index.vector_stores.redis import RedisVectorStore

        # Use default schema
        rds = RedisVectorStore(redis_url="redis://localhost:6379")

        # Use custom schema from dict
        schema = IndexSchema.from_dict({
            "index": {"name": "my-index", "prefix": "docs"},
            "fields": [
                {"name": "id", "type": "tag"},
                {"name": "doc_id", "type": "tag},
                {"name": "text", "type": "text"},
                {"name": "vector", "type": "vector", "attrs": {"dims": 1536, "algorithm": "flat"}}
            ]
        })
        vector_store = RedisVectorStore(
            schema=schema,
            redis_url="redis://localhost:6379"
        )

    """

    stores_text: bool = True
    stores_node: bool = True
    flat_metadata: bool = False
    created_async_index: bool = False
    legacy_filters: bool = False

    _index: SearchIndex = PrivateAttr()
    _async_index: AsyncSearchIndex = PrivateAttr()
    _tokenizer: Any = PrivateAttr()
    _redis_client: Any = PrivateAttr()
    _redis_client_async: Any = PrivateAttr()
    _prefix: str = PrivateAttr()
    _index_name: str = PrivateAttr()
    _index_args: Dict[str, Any] = PrivateAttr()
    _metadata_fields: List[str] = PrivateAttr()
    _overwrite: bool = PrivateAttr()
    _return_fields: List[str] = PrivateAttr()

    def __init__(
        self,
        schema: Optional[IndexSchema] = None,
        redis_client: Optional[Redis] = None,
        redis_client_async: Optional[RedisAsync] = None,
        redis_url: Optional[str] = None,
        overwrite: bool = False,
        return_fields: Optional[List[str]] = None,
        legacy_filters: Optional[bool] = False,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        # check for indicators of old schema
        self._flag_old_kwargs(**kwargs)
        self.legacy_filters = legacy_filters
        # Setup schema
        if not schema:
            logger.info("Using default RedisVectorStore schema.")
            schema = RedisVectorStoreSchema()

        self._validate_schema(schema)
        self._return_fields = return_fields or [
            NODE_ID_FIELD_NAME,
            DOC_ID_FIELD_NAME,
            TEXT_FIELD_NAME,
            NODE_CONTENT_FIELD_NAME,
        ]
        self._overwrite = overwrite
        self._index = SearchIndex(
            schema=schema, redis_client=redis_client, redis_url=redis_url
        )
        self._redis_client_async = redis_client_async
        if redis_client or redis_url:
            if redis_url and not redis_client:
                redis_client = Redis.from_url(redis_url)
            self._redis_client = redis_client
            self.create_index()
            if not self._redis_client_async:
                self._redis_client_async = redis_async.Redis(
                    host=redis_client.connection_pool.connection_kwargs["host"],
                    port=redis_client.connection_pool.connection_kwargs["port"],
                    **{
                        k: v
                        for k, v in redis_client.connection_pool.connection_kwargs.items()
                        if k not in ["host", "port"]
                    },
                )
        if not redis_client and not redis_url and not redis_client_async:
            raise Exception(
                "Either redis_client, redis_url, or redis_client_async need to be defined"
            )
        self._async_index = AsyncSearchIndex(
            schema=schema, redis_client=self._redis_client_async
        )

    def _flag_old_kwargs(self, **kwargs):
        old_kwargs = [
            "index_name",
            "index_prefix",
            "prefix_ending",
            "index_args",
            "metadata_fields",
        ]
        for kwarg in old_kwargs:
            if kwarg in kwargs:
                raise ValueError(
                    f"Deprecated kwarg, {kwarg}, found upon initialization. "
                    "RedisVectorStore now requires an IndexSchema object. "
                    "See the documentation for a complete example: https://docs.llamaindex.ai/en/stable/examples/vector_stores/RedisIndexDemo/"
                )

    def _validate_schema(self, schema: IndexSchema) -> str:
        base_schema = RedisVectorStoreSchema()
        for name, field in base_schema.fields.items():
            if (name not in schema.fields) or (
                not schema.fields[name].type == field.type
            ):
                raise ValueError(
                    f"Required field {name} must be present in the index "
                    f"and of type {schema.fields[name].type}"
                )

    @property
    def client(self) -> "Redis":
        """Return the redis client instance."""
        if self._async_index:
            return self._async_index.client
        return self._index.client

    @property
    def index_name(self) -> str:
        """Return the name of the index based on the schema."""
        return self._index.name

    @property
    def schema(self) -> IndexSchema:
        """Return the index schema."""
        if self._async_index:
            return self._async_index.schema
        return self._index.schema

    def set_return_fields(self, return_fields: List[str]) -> None:
        """Update the return fields for the query response."""
        self._return_fields = return_fields

    def index_exists(self) -> bool:
        """
        Check whether the index exists in Redis.

        Returns:
            bool: True or False.

        """
        return self._index.exists()

    async def async_index_exists(self) -> bool:
        """
        Check whether the index exists in Redis.

        Returns:
            bool: True or False.

        """
        if not self.created_async_index:
            await self.async_create_index()
        return True

    def create_index(self, overwrite: Optional[bool] = None) -> None:
        """Create an index in Redis."""
        if overwrite is None:
            overwrite = self._overwrite
        # Create index honoring overwrite policy
        if overwrite:
            self._index.create(overwrite=overwrite, drop=True)
        else:
            self._index.create()

    async def async_create_index(self, overwrite: Optional[bool] = None) -> None:
        """Create an async index in Redis."""
        if overwrite is None:
            overwrite = self._overwrite
        # Create index honoring overwrite policy
        if overwrite:
            await self._async_index.create(overwrite=True, drop=True)
        else:
            await self._async_index.create()
        self.created_async_index = True

    async def async_add(self, nodes: List[BaseNode], **add_kwargs: Any) -> List[str]:
        """
        Add nodes to the index.

        Args:
            nodes (List[BaseNode]): List of nodes with embeddings

        Returns:
            List[str]: List of ids of the documents added to the index.

        Raises:
            ValueError: If the index already exists and overwrite is False.

        """
        # Check to see if empty document list was passed
        await self.async_index_exists()
        if len(nodes) == 0:
            return []

        # Now check for the scenario where user is trying to index embeddings that don't align with schema
        embedding_len = len(nodes[0].get_embedding())
        expected_dims = self._async_index.schema.fields[VECTOR_FIELD_NAME].attrs.dims
        if expected_dims != embedding_len:
            raise ValueError(
                f"Attempting to index embeddings of dim {embedding_len} "
                f"which doesn't match the index schema expectation of {expected_dims}. "
                "Please review the Redis integration example to learn how to customize schema. "
                ""
            )

        data: List[Dict[str, Any]] = []
        for node in nodes:
            embedding = node.get_embedding()
            record = {
                NODE_ID_FIELD_NAME: node.node_id,
                DOC_ID_FIELD_NAME: node.ref_doc_id,
                TEXT_FIELD_NAME: node.get_content(metadata_mode=MetadataMode.NONE),
                VECTOR_FIELD_NAME: array_to_buffer(embedding, dtype="FLOAT32"),
            }
            # parse and append metadata
            additional_metadata = node_to_metadata_dict(
                node, remove_text=True, flat_metadata=self.flat_metadata
            )
            data.append({**record, **additional_metadata})

        # Load nodes to Redis
        for mapping in data:
            mapping.pop(
                "sub_dicts", None
            )  # Remove if present from VectorMemory to avoid serialization issues
        keys = await self._async_index.load(
            data, id_field=NODE_ID_FIELD_NAME, **add_kwargs
        )
        logger.info(f"Added {len(keys)} documents to index {self._async_index.name}")
        return [
            key.strip(self._async_index.prefix + self._async_index.key_separator)
            for key in keys
        ]

    def add(self, nodes: List[BaseNode], **add_kwargs: Any) -> List[str]:
        """
        Add nodes to the index.

        Args:
            nodes (List[BaseNode]): List of nodes with embeddings

        Returns:
            List[str]: List of ids of the documents added to the index.

        Raises:
            ValueError: If the index already exists and overwrite is False.

        """
        # Check to see if empty document list was passed
        if len(nodes) == 0:
            return []

        # Now check for the scenario where user is trying to index embeddings that don't align with schema
        embedding_len = len(nodes[0].get_embedding())
        expected_dims = self._index.schema.fields[VECTOR_FIELD_NAME].attrs.dims
        if expected_dims != embedding_len:
            raise ValueError(
                f"Attempting to index embeddings of dim {embedding_len} "
                f"which doesn't match the index schema expectation of {expected_dims}. "
                "Please review the Redis integration example to learn how to customize schema. "
                ""
            )

        data: List[Dict[str, Any]] = []
        for node in nodes:
            embedding = node.get_embedding()
            record = {
                NODE_ID_FIELD_NAME: node.node_id,
                DOC_ID_FIELD_NAME: node.ref_doc_id,
                TEXT_FIELD_NAME: node.get_content(metadata_mode=MetadataMode.NONE),
                VECTOR_FIELD_NAME: array_to_buffer(embedding, dtype="FLOAT32"),
            }
            # parse and append metadata
            additional_metadata = node_to_metadata_dict(
                node, remove_text=True, flat_metadata=self.flat_metadata
            )
            data.append({**record, **additional_metadata})

        # Load nodes to Redis
        keys = self._index.load(data, id_field=NODE_ID_FIELD_NAME, **add_kwargs)
        logger.info(f"Added {len(keys)} documents to index {self._index.name}")
        return [
            key.strip(self._index.prefix + self._index.key_separator) for key in keys
        ]

    def delete_nodes(self, node_ids: list):
        for node_id in node_ids:
            self._redis_client.delete(
                "_".join([self._async_index.prefix, str(node_id)])
            )

    async def adelete_nodes(self, node_ids: list):
        await self.async_index_exists()
        for node_id in node_ids:
            await self._redis_client_async.delete(
                "_".join([self._async_index.prefix, str(node_id)])
            )

    async def adelete(self, ref_doc_id: str, **delete_kwargs: Any) -> None:
        """
        Delete nodes using the ref_doc_id.

        Args:
            ref_doc_id (str): The doc_id of the document to delete.

        """
        await self.async_index_exists()
        # build a filter to target specific docs by doc ID
        doc_filter = Tag(DOC_ID_FIELD_NAME) == ref_doc_id
        total = await self._async_index.query(CountQuery(doc_filter))
        delete_query = FilterQuery(
            return_fields=[NODE_ID_FIELD_NAME],
            filter_expression=doc_filter,
            num_results=total,
        )
        # fetch docs to delete and flush them
        docs_to_delete = await self._async_index.search(
            delete_query.query, delete_query.params
        )
        async with self._async_index.client.pipeline(transaction=False) as pipe:
            for doc in docs_to_delete.docs:
                await pipe.delete(doc.id)
            await pipe.execute()

        logger.info(
            f"Deleted {len(docs_to_delete.docs)} documents from index {self._async_index.name}"
        )

    def delete(self, ref_doc_id: str) -> None:
        """
        Delete nodes using the ref_doc_id.

        Args:
            ref_doc_id (str): The doc_id of the document to delete.

        """
        # build a filter to target specific docs by doc ID
        doc_filter = Tag(DOC_ID_FIELD_NAME) == ref_doc_id
        total = self._index.query(CountQuery(doc_filter))
        delete_query = FilterQuery(
            return_fields=[NODE_ID_FIELD_NAME],
            filter_expression=doc_filter,
            num_results=total,
        )
        # fetch docs to delete and flush them
        docs_to_delete = self._index.search(delete_query.query, delete_query.params)
        with self._index.client.pipeline(transaction=False) as pipe:
            for doc in docs_to_delete.docs:
                pipe.delete(doc.id)
            pipe.execute()

        logger.info(
            f"Deleted {len(docs_to_delete.docs)} documents from index {self._index.name}"
        )

    def delete_index(self) -> None:
        """Delete the index and all documents."""
        logger.info(f"Deleting index {self._index.name}")
        self._index.delete(drop=True)

    async def async_delete_index(self) -> None:
        """Delete the index and all documents."""
        logger.info(f"Deleting index {self._async_index.name}")
        await self._async_index.delete(drop=True)

    @staticmethod
    def _to_redis_filter(field: BaseField, filter: MetadataFilter) -> FilterExpression:
        """
        Translate a standard metadata filter to a Redis specific filter expression.

        Args:
            field (BaseField): The field to be filtered on, must have a type attribute.
            filter (MetadataFilter): The filter to apply, must have operator and value attributes.

        Returns:
            FilterExpression: A Redis-specific filter expression constructed from the input.

        Raises:
            ValueError: If the field type is unsupported or if the operator is not supported for the field type.

        """
        # Check for unsupported field type
        if field.type not in REDIS_LLAMA_FIELD_SPEC:
            raise ValueError(f"Unsupported field type {field.type} for {field.name}")

        field_info = REDIS_LLAMA_FIELD_SPEC[field.type]

        # Check for unsupported operator
        if filter.operator not in field_info["operators"]:
            raise ValueError(
                f"Filter operator {filter.operator} not supported for {field.name} of type {field.type}"
            )

        # Create field instance and apply the operator function
        field_instance = field_info["class"](field.name)
        return field_info["operators"][filter.operator](field_instance, filter.value)

    def _create_redis_filter_expression(
        self, metadata_filters: MetadataFilters
    ) -> FilterExpression:
        """
        Generate a Redis Filter Expression as a combination of metadata filters.

        Args:
            metadata_filters (MetadataFilters): List of metadata filters to use.

        Returns:
            FilterExpression: A Redis filter expression.

        """
        filter_expression = FilterExpression("*")
        if metadata_filters:
            if metadata_filters.filters:
                for filter in metadata_filters.filters:
                    # Handle nested MetadataFilters recursively
                    if isinstance(filter, MetadataFilters):
                        redis_filter = self._create_redis_filter_expression(filter)
                    else:
                        # Index must be created with the metadata field in the index schema
                        field = self._index.schema.fields.get(filter.key)
                        if not field:
                            logger.warning(
                                f"{filter.key} field was not included as part of the index schema, and thus cannot be used as a filter condition."
                            )
                            continue
                        # Extract redis filter
                        redis_filter = self._to_redis_filter(field, filter)

                    # Combine with conditional
                    if metadata_filters.condition == "and":
                        filter_expression = filter_expression & redis_filter
                    else:
                        filter_expression = filter_expression | redis_filter
        return filter_expression

    def _to_redis_filters(self, metadata_filters: MetadataFilters) -> str:
        tokenizer = TokenEscaper()

        filter_strings = []
        filter_in_strings = {}
        for filter in metadata_filters.legacy_filters():
            # adds quotes around the value to ensure that the filter is treated as an
            #   exact
            field = self._index.schema.fields.get(filter.key)
            if not field:
                logger.warning(
                    f"{filter.key} field was not included as part of the index schema, and thus cannot be used as a filter condition."
                )
                continue
            if filter.operator == FilterOperator.IN:
                if len(filter.value.split()) > 1:
                    filter.value = f'"{filter.value}"'
                if filter.key in filter_in_strings:
                    filter_in_strings[filter.key].append(filter.value)
                else:
                    filter_in_strings[filter.key] = [filter.value]
            else:
                filter_string = (
                    f"@{filter.key}:{{{tokenizer.escape(str(filter.value))}}}"
                )
                filter_strings.append(filter_string)
        for key, value_list in filter_in_strings.items():
            values = "|".join(value_list)
            filter_string = f"@{key}:{{{tokenizer.escape(str(values))}}}"
            filter_strings.append(filter_string)
        # A space can be used for the AND operator: https://redis.io/docs/latest/develop/interact/search-and-query/query/combined/
        filter_strings_base = [f"({filter_string})" for filter_string in filter_strings]
        joined_filter_strings = " ".join(filter_strings_base)
        print("Using filter string: ", joined_filter_strings)
        return f"({joined_filter_strings})"

    def _to_redis_query(self, query: VectorStoreQuery) -> VectorQuery:
        """Creates a RedisQuery from a VectorStoreQuery."""
        # TODO: Figure out why create_redis_filter_expression doesn't handle IN properly
        if self.legacy_filters:
            filter_expression = self._to_redis_filters(query.filters)
        else:
            filter_expression = self._create_redis_filter_expression(query.filters)
        return_fields = self._return_fields.copy()
        return VectorQuery(
            vector=query.query_embedding,
            vector_field_name=VECTOR_FIELD_NAME,
            num_results=query.similarity_top_k,
            filter_expression=filter_expression,
            return_fields=return_fields,
        )

    def _extract_node_and_score(self, doc, redis_query: VectorQuery):
        """Extracts a node and its score from a document."""
        try:
            node = metadata_dict_to_node(
                {NODE_CONTENT_FIELD_NAME: doc[NODE_CONTENT_FIELD_NAME]}
            )
            node.text = doc[TEXT_FIELD_NAME]
        except Exception:
            # Handle legacy metadata format
            node = TextNode(
                text=doc[TEXT_FIELD_NAME],
                id_=doc[NODE_ID_FIELD_NAME],
                embedding=None,
                relationships={
                    NodeRelationship.SOURCE: RelatedNodeInfo(
                        node_id=doc[DOC_ID_FIELD_NAME]
                    )
                },
            )
        score = 1 - float(doc[redis_query.DISTANCE_ID])
        return node, score

    def _process_query_results(
        self, results, redis_query: VectorQuery
    ) -> VectorStoreQueryResult:
        """Processes query results and returns a VectorStoreQueryResult."""
        ids, nodes, scores = [], [], []
        for doc in results:
            node, score = self._extract_node_and_score(doc, redis_query)
            ids.append(doc[NODE_ID_FIELD_NAME])
            nodes.append(node)
            scores.append(score)
        logger.info(f"Found {len(nodes)} results for query with id {ids}")
        return VectorStoreQueryResult(nodes=nodes, ids=ids, similarities=scores)

    def query(self, query: VectorStoreQuery, **kwargs: Any) -> VectorStoreQueryResult:
        """
        Query the index.

        Args:
            query (VectorStoreQuery): query object

        Returns:
            VectorStoreQueryResult: query result

        Raises:
            ValueError: If query.query_embedding is None.
            redis.exceptions.RedisError: If there is an error querying the index.
            redis.exceptions.TimeoutError: If there is a timeout querying the index.

        """
        if not query.query_embedding:
            raise ValueError("Query embedding is required for querying.")

        redis_query = self._to_redis_query(query)
        logger.info(f"Querying index {self._index.name} with query {redis_query!s}")

        try:
            results = self._index.query(redis_query)
        except RedisTimeoutError as e:
            logger.error(f"Query timed out on {self._index.name}: {e}")
            raise
        except RedisError as e:
            logger.error(f"Error querying {self._index.name}: {e}")
            raise

        return self._process_query_results(results, redis_query)

    async def aquery(
        self, query: VectorStoreQuery, **kwargs: Any
    ) -> VectorStoreQueryResult:
        """
        Query the index.

        Args:
            query (VectorStoreQuery): query object

        Returns:
            VectorStoreQueryResult: query result

        Raises:
            ValueError: If query.query_embedding is None.
            redis.exceptions.RedisError: If there is an error querying the index.
            redis.exceptions.TimeoutError: If there is a timeout querying the index.

        """
        await self.async_index_exists()
        if not query.query_embedding:
            raise ValueError("Query embedding is required for querying.")

        redis_query = self._to_redis_query(query)
        logger.info(f"Querying index {self._index.name} with query {redis_query!s}")
        try:
            results = await self._async_index.query(redis_query)
        except RedisTimeoutError as e:
            logger.error(f"Query timed out on {self._index.name}: {e}")
            raise
        except RedisError as e:
            logger.error(f"Error querying {self._index.name}: {e}")
            raise

        return self._process_query_results(results, redis_query)

    def persist(
        self,
        persist_path: Optional[str] = None,
        fs: Optional[fsspec.AbstractFileSystem] = None,
        in_background: bool = True,
    ) -> None:
        """
        Persist the vector store to disk.

        For Redis, more notes here: https://redis.io/docs/management/persistence/

        Args:
            persist_path (str): Path to persist the vector store to. (doesn't apply)
            in_background (bool, optional): Persist in background. Defaults to True.
            fs (fsspec.AbstractFileSystem, optional): Filesystem to persist to.
                (doesn't apply)

        Raises:
            redis.exceptions.RedisError: If there is an error
                                         persisting the index to disk.

        """
        try:
            if in_background:
                logger.info("Saving index to disk in background")
                self._index.client.bgsave()
            else:
                logger.info("Saving index to disk")
                self._index.client.save()

        except RedisError as e:
            logger.error(f"Error saving index to disk: {e}")
            raise
