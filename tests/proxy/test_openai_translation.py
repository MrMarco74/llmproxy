"""Exercises the real OpenAI-compat <-> native Ollama translation layer in
proxy/llmproxy.py -- all pure data transforms, zero I/O.
"""
import json

import pytest


# ── _extract_last_user_message ───────────────────────────────────────────

def test_extract_last_user_message_simple(llmproxy_module):
    body = {"messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}]}
    assert llmproxy_module._extract_last_user_message(body) == "hello"


def test_extract_last_user_message_picks_last_of_several(llmproxy_module):
    body = {"messages": [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]}
    assert llmproxy_module._extract_last_user_message(body) == "second"


def test_extract_last_user_message_flattens_list_content(llmproxy_module):
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "part1"}, {"type": "text", "text": "part2"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
    ]}]}
    assert llmproxy_module._extract_last_user_message(body) == "part1 part2"


def test_extract_last_user_message_truncates_to_500(llmproxy_module):
    body = {"messages": [{"role": "user", "content": "x" * 600}]}
    assert len(llmproxy_module._extract_last_user_message(body)) == 500


def test_extract_last_user_message_falls_back_to_prompt(llmproxy_module):
    body = {"prompt": "generate style prompt"}
    assert llmproxy_module._extract_last_user_message(body) == "generate style prompt"


def test_extract_last_user_message_no_user_no_prompt_returns_empty(llmproxy_module):
    body = {"messages": [{"role": "assistant", "content": "hi"}]}
    assert llmproxy_module._extract_last_user_message(body) == ""


# ── _parse_openai_content ────────────────────────────────────────────────

def test_parse_openai_content_string(llmproxy_module):
    text, images = llmproxy_module._parse_openai_content("plain text")
    assert text == "plain text"
    assert images == []


def test_parse_openai_content_list_splits_text_and_images(llmproxy_module):
    content = [
        {"type": "text", "text": "line1"},
        {"type": "text", "text": "line2"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,ABC123"}},
    ]
    text, images = llmproxy_module._parse_openai_content(content)
    assert text == "line1\nline2"
    assert images == ["ABC123"]


def test_parse_openai_content_non_data_url_image_ignored(llmproxy_module):
    content = [{"type": "image_url", "image_url": {"url": "https://example.com/x.png"}}]
    text, images = llmproxy_module._parse_openai_content(content)
    assert images == []


def test_parse_openai_content_other_type_falls_back_to_str(llmproxy_module):
    text, images = llmproxy_module._parse_openai_content(42)
    assert text == "42"
    assert images == []


# ── _openai_to_native_chat ────────────────────────────────────────────────

def test_openai_to_native_chat_basic(llmproxy_module):
    body = {"model": "qwen3:8b", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    native = llmproxy_module._openai_to_native_chat(body)
    assert native["model"] == "qwen3:8b"
    assert native["stream"] is True
    assert native["messages"] == [{"role": "user", "content": "hi"}]


def test_openai_to_native_chat_merges_consecutive_same_role_messages(llmproxy_module):
    body = {"model": "m", "messages": [
        {"role": "user", "content": "part1"},
        {"role": "user", "content": "part2"},
    ]}
    native = llmproxy_module._openai_to_native_chat(body)
    assert len(native["messages"]) == 1
    assert native["messages"][0]["content"] == "part1\n\npart2"


def test_openai_to_native_chat_does_not_merge_across_system(llmproxy_module):
    body = {"model": "m", "messages": [
        {"role": "user", "content": "a"},
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "b"},
    ]}
    native = llmproxy_module._openai_to_native_chat(body)
    assert len(native["messages"]) == 3


def test_openai_to_native_chat_parses_tool_call_json_string_arguments(llmproxy_module):
    body = {"model": "m", "messages": [{
        "role": "assistant", "content": "",
        "tool_calls": [{"function": {"name": "f", "arguments": '{"x": 1}'}}],
    }]}
    native = llmproxy_module._openai_to_native_chat(body)
    assert native["messages"][0]["tool_calls"][0]["function"]["arguments"] == {"x": 1}


def test_openai_to_native_chat_max_tokens_maps_to_num_predict(llmproxy_module):
    body = {"model": "m", "messages": [], "max_tokens": 256}
    native = llmproxy_module._openai_to_native_chat(body)
    assert native["options"]["num_predict"] == 256


def test_openai_to_native_chat_temperature_and_stop_forwarded(llmproxy_module):
    body = {"model": "m", "messages": [], "temperature": 0.7, "stop": "STOP"}
    native = llmproxy_module._openai_to_native_chat(body)
    assert native["options"]["temperature"] == 0.7
    assert native["options"]["stop"] == ["STOP"]


def test_openai_to_native_chat_applies_num_ctx_override(llmproxy_module):
    override_model = next(iter(llmproxy_module.MODEL_NUM_CTX_OVERRIDES), None)
    if not override_model:
        pytest.skip("no MODEL_NUM_CTX_OVERRIDES configured")
    body = {"model": override_model, "messages": []}
    native = llmproxy_module._openai_to_native_chat(body)
    assert native["options"]["num_ctx"] == llmproxy_module.MODEL_NUM_CTX_OVERRIDES[override_model]


# ── _repair_json_arguments ────────────────────────────────────────────────

def test_repair_json_arguments_valid_json_unchanged(llmproxy_module):
    valid = '{"a": 1, "b": 2}'
    assert llmproxy_module._repair_json_arguments(valid) == valid


def test_repair_json_arguments_fixes_missing_comma(llmproxy_module):
    broken = '{"a": 1 "b": 2}'
    repaired = llmproxy_module._repair_json_arguments(broken)
    assert json.loads(repaired) == {"a": 1, "b": 2}


def test_repair_json_arguments_gives_up_on_unfixable(llmproxy_module):
    broken = "{not json at all"
    assert llmproxy_module._repair_json_arguments(broken) == broken


# ── _native_to_openai ────────────────────────────────────────────────────

def test_native_to_openai_non_streaming(llmproxy_module):
    data = {"message": {"content": "hello"}, "done": True,
            "prompt_eval_count": 10, "eval_count": 5}
    out = llmproxy_module._native_to_openai(data, "qwen3:8b")
    assert out["object"] == "chat.completion"
    assert out["choices"][0]["message"]["content"] == "hello"
    assert out["choices"][0]["finish_reason"] == "stop"
    assert out["usage"]["total_tokens"] == 15


def test_native_to_openai_streaming_chunk(llmproxy_module):
    data = {"message": {"content": "partial"}, "done": False}
    out = llmproxy_module._native_to_openai(data, "qwen3:8b", stream=True)
    assert out["object"] == "chat.completion.chunk"
    assert out["choices"][0]["delta"]["content"] == "partial"
    assert out["choices"][0]["finish_reason"] is None


def test_native_to_openai_tool_calls_get_repaired_and_wrapped(llmproxy_module):
    data = {"message": {"content": "", "tool_calls": [
        {"function": {"name": "search", "arguments": '{"q": "x"}'}}
    ]}, "done": True}
    out = llmproxy_module._native_to_openai(data, "m")
    tc = out["choices"][0]["message"]["tool_calls"][0]
    assert tc["function"]["name"] == "search"
    assert json.loads(tc["function"]["arguments"]) == {"q": "x"}
    assert out["choices"][0]["finish_reason"] == "tool_calls"


# ── _native_request_to_openai ────────────────────────────────────────────

def test_native_request_to_openai_chat_messages(llmproxy_module):
    body = {"messages": [{"role": "user", "content": "hi"}]}
    out = llmproxy_module._native_request_to_openai(body, "m")
    assert out["messages"] == [{"role": "user", "content": "hi"}]
    assert out["stream"] is False


def test_native_request_to_openai_generate_prompt_becomes_user_message(llmproxy_module):
    body = {"prompt": "do a thing", "system": "be nice"}
    out = llmproxy_module._native_request_to_openai(body, "m")
    assert out["messages"] == [
        {"role": "system", "content": "be nice"},
        {"role": "user", "content": "do a thing"},
    ]


def test_native_request_to_openai_maps_options(llmproxy_module):
    body = {"messages": [], "options": {"num_predict": 100, "temperature": 0.5, "top_p": 0.9}}
    out = llmproxy_module._native_request_to_openai(body, "m")
    assert out["max_tokens"] == 100
    assert out["temperature"] == 0.5
    assert out["top_p"] == 0.9


def test_native_request_to_openai_json_format_maps_response_format(llmproxy_module):
    body = {"messages": [], "format": "json"}
    out = llmproxy_module._native_request_to_openai(body, "m")
    assert out["response_format"] == {"type": "json_object"}


# ── _openai_response_to_native ───────────────────────────────────────────

def test_openai_response_to_native_basic(llmproxy_module):
    data = {"choices": [{"message": {"content": "answer"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4}}
    out = llmproxy_module._openai_response_to_native(data, "m")
    assert out["message"]["content"] == "answer"
    assert out["done"] is True
    assert out["prompt_eval_count"] == 3
    assert out["eval_count"] == 4


def test_openai_response_to_native_parses_string_tool_call_arguments(llmproxy_module):
    data = {"choices": [{"message": {"content": "", "tool_calls": [
        {"function": {"name": "f", "arguments": '{"x": 1}'}}
    ]}}]}
    out = llmproxy_module._openai_response_to_native(data, "m")
    assert out["message"]["tool_calls"][0]["function"]["arguments"] == {"x": 1}


def test_openai_response_to_native_no_choices_returns_empty_message(llmproxy_module):
    out = llmproxy_module._openai_response_to_native({"choices": []}, "m")
    assert out["message"]["content"] == ""
