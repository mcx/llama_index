from unittest.mock import AsyncMock, Mock

import pytest
from llama_index.core.base.llms.types import ChatMessage, ChatResponse, MessageRole
from llama_index.core.callbacks import CallbackManager
from llama_index.core.llms.function_calling import FunctionCallingLLM
from llama_index.core.tools.types import BaseTool
from llama_index.core.tools import FunctionTool
from llama_index.llms.oci_data_science import OCIDataScience
from llama_index.llms.oci_data_science.base import OCIDataScience
from llama_index.llms.oci_data_science.client import AsyncClient, Client


# Shared tool for testing
def search(query: str) -> str:
    """Search for information about a query."""
    return f"Results for {query}"


search_tool = FunctionTool.from_defaults(
    fn=search, name="search_tool", description="A tool for searching information"
)


def test_embedding_class():
    names_of_base_classes = [b.__name__ for b in OCIDataScience.__mro__]
    assert FunctionCallingLLM.__name__ in names_of_base_classes


@pytest.fixture()
def llm():
    endpoint = "https://example.com/api"
    auth = {"signer": Mock()}
    model = "odsc-llm"
    temperature = 0.7
    max_tokens = 100
    timeout = 60
    max_retries = 3
    additional_kwargs = {"top_p": 0.9}
    callback_manager = CallbackManager([])

    llm_instance = OCIDataScience(
        endpoint=endpoint,
        auth=auth,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        max_retries=max_retries,
        additional_kwargs=additional_kwargs,
        callback_manager=callback_manager,
    )
    # Mock the client
    llm_instance._client = Mock(spec=Client)
    llm_instance._async_client = AsyncMock(spec=AsyncClient)

    return llm_instance


def test_complete_success(llm):
    prompt = "What is the capital of France?"
    response_data = {
        "choices": [
            {
                "text": "The capital of France is Paris.",
                "logprobs": {},
            }
        ],
        "usage": {
            "prompt_tokens": 5,
            "completion_tokens": 7,
            "total_tokens": 12,
        },
    }
    # Mock the client's generate method
    llm.client.generate.return_value = response_data

    response = llm.complete(prompt)

    # Assertions
    llm.client.generate.assert_called_once()
    assert response.text == "The capital of France is Paris."
    assert response.additional_kwargs["total_tokens"] == 12


def test_complete_invalid_response(llm):
    prompt = "What is the capital of France?"
    response_data = {}  # Empty response
    llm.client.generate.return_value = response_data

    with pytest.raises(ValueError):
        llm.complete(prompt)


def test_chat_success(llm):
    messages = [ChatMessage(role=MessageRole.USER, content="Tell me a joke.")]
    response_data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Why did the chicken cross the road? To get to the other side!",
                },
                "logprobs": {},
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 15,
            "total_tokens": 25,
        },
    }
    llm.client.chat.return_value = response_data

    response = llm.chat(messages)

    llm.client.chat.assert_called_once()
    assert (
        response.message.content
        == "Why did the chicken cross the road? To get to the other side!"
    )
    assert response.additional_kwargs["total_tokens"] == 25


def test_stream_complete(llm):
    prompt = "Once upon a time"
    # Mock the client's generate method to return an iterator
    response_data = iter(
        [
            {"choices": [{"text": "Once"}], "usage": {}},
            {"choices": [{"text": " upon"}], "usage": {}},
            {"choices": [{"text": " a"}], "usage": {}},
            {"choices": [{"text": " time."}], "usage": {}},
        ]
    )
    llm.client.generate.return_value = response_data

    responses = list(llm.stream_complete(prompt))

    llm.client.generate.assert_called_once()
    assert len(responses) == 4
    assert responses[0].delta == "Once"
    assert responses[1].delta == " upon"
    assert responses[2].delta == " a"
    assert responses[3].delta == " time."
    assert responses[-1].text == "Once upon a time."


def test_stream_chat(llm):
    messages = [ChatMessage(role=MessageRole.USER, content="Tell me a joke.")]
    response_data = iter(
        [
            {"choices": [{"delta": {"content": "Why"}}], "usage": {}},
            {"choices": [{"delta": {"content": " did"}}], "usage": {}},
            {"choices": [{"delta": {"content": " the"}}], "usage": {}},
            {
                "choices": [{"delta": {"content": " chicken cross the road?"}}],
                "usage": {},
            },
        ]
    )
    llm.client.chat.return_value = response_data

    responses = list(llm.stream_chat(messages))

    llm.client.chat.assert_called_once()
    assert len(responses) == 4
    content = "".join([r.delta for r in responses])
    assert content == "Why did the chicken cross the road?"
    assert responses[-1].message.content == content


def test_prepare_chat_with_tools(llm):
    # Mock tools
    tool1 = Mock(spec=BaseTool)
    tool1.metadata.to_openai_tool.return_value = {
        "name": "tool1",
        "type": "function",
        "function": {
            "name": "tool1",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    }
    tool2 = Mock(spec=BaseTool)
    tool2.metadata.to_openai_tool.return_value = {
        "name": "tool2",
        "type": "function",
        "function": {
            "name": "tool2",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    }

    user_msg = "Calculate the result of 2 + 2."
    chat_history = [ChatMessage(role=MessageRole.USER, content="Previous message")]

    result = llm._prepare_chat_with_tools(
        tools=[tool1, tool2],
        user_msg=user_msg,
        chat_history=chat_history,
    )

    # Check that 'function' key has been updated as expected
    for tool_spec in result["tools"]:
        assert "function" in tool_spec
        assert "parameters" in tool_spec["function"]
        assert tool_spec["function"]["parameters"]["additionalProperties"] is False

    assert "messages" in result
    assert "tools" in result
    assert len(result["tools"]) == 2
    assert result["messages"][-1].content == user_msg


def test_get_tool_calls_from_response(llm):
    response_data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_abc123",
                            "type": "function",
                            "function": {
                                "name": "search",
                                "arguments": '{"query": "test"}',
                            },
                        }
                    ],
                }
            }
        ]
    }
    response = ChatResponse(
        message=ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            additional_kwargs=response_data["choices"][0]["message"],
        )
    )

    tool_calls = llm.get_tool_calls_from_response(response)
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "search"
    assert tool_calls[0].tool_kwargs == {"query": "test"}


def test_resolve_tool_choice_tool_required():
    """Test that tool_required=True correctly sets tool_choice to 'required'."""
    from llama_index.llms.oci_data_science.utils import _resolve_tool_choice

    # Test with tool_required=True and no explicit tool_choice
    result = _resolve_tool_choice(tool_choice=None, tool_required=True)
    assert result == "required"

    # Test with tool_required=False and no explicit tool_choice
    result = _resolve_tool_choice(tool_choice=None, tool_required=False)
    assert result == "auto"

    # Test that explicit tool_choice overrides tool_required
    result = _resolve_tool_choice(tool_choice="none", tool_required=True)
    assert result == "none"


def test_prepare_chat_with_tools_tool_required(llm):
    """Test that tool_required correctly influences the tool_choice in _prepare_chat_with_tools."""
    user_msg = "Search for information about Python"

    # Test with tool_required=True
    result = llm._prepare_chat_with_tools(
        tools=[search_tool], user_msg=user_msg, tool_required=True
    )

    assert result["tool_choice"] == "required"
    assert len(result["tools"]) == 1
    assert result["tools"][0]["function"]["name"] == "search_tool"


def test_prepare_chat_with_tools_tool_not_required(llm):
    """Test that tool_required=False correctly sets tool_choice to 'auto'."""
    user_msg = "Search for information about Python"

    # Test with tool_required=False (default)
    result = llm._prepare_chat_with_tools(
        tools=[search_tool], user_msg=user_msg, tool_required=False
    )

    assert result["tool_choice"] == "auto"
    assert len(result["tools"]) == 1
    assert result["tools"][0]["function"]["name"] == "search_tool"


def test_prepare_chat_with_tools_explicit_tool_choice_overrides_tool_required(llm):
    """Test that explicit tool_choice parameter overrides tool_required."""
    user_msg = "Search for information about Python"

    # Test that explicit tool_choice overrides tool_required=True
    result = llm._prepare_chat_with_tools(
        tools=[search_tool], user_msg=user_msg, tool_required=True, tool_choice="none"
    )

    assert result["tool_choice"] == "none"
    assert len(result["tools"]) == 1
    assert result["tools"][0]["function"]["name"] == "search_tool"


@pytest.mark.asyncio
async def test_acomplete_success(llm):
    prompt = "What is the capital of France?"
    response_data = {
        "choices": [
            {
                "text": "The capital of France is Paris.",
                "logprobs": {},
            }
        ],
        "usage": {
            "prompt_tokens": 5,
            "completion_tokens": 7,
            "total_tokens": 12,
        },
    }
    llm.async_client.generate.return_value = response_data

    response = await llm.acomplete(prompt)

    llm.async_client.generate.assert_called_once()
    assert response.text == "The capital of France is Paris."
    assert response.additional_kwargs["total_tokens"] == 12


@pytest.mark.asyncio
async def test_astream_complete(llm):
    prompt = "Once upon a time"

    async def async_generator():
        response_data = [
            {"choices": [{"text": "Once"}], "usage": {}},
            {"choices": [{"text": " upon"}], "usage": {}},
            {"choices": [{"text": " a"}], "usage": {}},
            {"choices": [{"text": " time."}], "usage": {}},
        ]
        for item in response_data:
            yield item

    llm.async_client.generate.return_value = async_generator()

    responses = []
    async for response in await llm.astream_complete(prompt):
        responses.append(response)

    llm.async_client.generate.assert_called_once()
    assert len(responses) == 4
    assert responses[0].delta == "Once"
    assert responses[-1].text == "Once upon a time."


@pytest.mark.asyncio
async def test_achat_success(llm):
    messages = [ChatMessage(role=MessageRole.USER, content="Tell me a joke.")]
    response_data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Why did the chicken cross the road? To get to the other side!",
                },
                "logprobs": {},
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 15,
            "total_tokens": 25,
        },
    }
    llm.async_client.chat.return_value = response_data

    response = await llm.achat(messages)

    llm.async_client.chat.assert_called_once()
    assert (
        response.message.content
        == "Why did the chicken cross the road? To get to the other side!"
    )
    assert response.additional_kwargs["total_tokens"] == 25


@pytest.mark.asyncio
async def test_astream_chat(llm):
    messages = [ChatMessage(role=MessageRole.USER, content="Tell me a joke.")]

    async def async_generator():
        response_data = [
            {"choices": [{"delta": {"content": "Why"}}], "usage": {}},
            {"choices": [{"delta": {"content": " did"}}], "usage": {}},
            {"choices": [{"delta": {"content": " the"}}], "usage": {}},
            {
                "choices": [{"delta": {"content": " chicken cross the road?"}}],
                "usage": {},
            },
        ]
        for item in response_data:
            yield item

    llm.async_client.chat.return_value = async_generator()

    responses = []
    async for response in await llm.astream_chat(messages):
        responses.append(response)

    llm.async_client.chat.assert_called_once()
    assert len(responses) == 4
    content = "".join([r.delta for r in responses])
    assert content == "Why did the chicken cross the road?"
    assert responses[-1].message.content == content
