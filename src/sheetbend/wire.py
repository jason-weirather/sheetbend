"""Bounded JSON and the explicitly supported gateway wire contracts.

This module does not execute tools, fetch image URLs, or certify model accuracy.
"""

from copy import deepcopy
import json
import math
from typing import Any

from .errors import ProbeError, SelectionError


def _constant(_: str) -> None:
    raise ValueError('Non-finite JSON is not supported.')


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON fields are not supported.')
        result[key] = value
    return result


def json_object(data: bytes | str) -> dict[str, Any]:
    try:
        if isinstance(data, bytes):
            data = data.decode('utf-8')
        result = json.loads(data, parse_constant=_constant, object_pairs_hook=_object)
        if not isinstance(result, dict):
            raise ValueError('Expected an object.')
        # Also catches exponent overflow (e.g. 1e999), unlike parse_constant alone.
        json.dumps(result, allow_nan=False, ensure_ascii=False).encode('utf-8')
        return result
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise ProbeError('Expected a finite, unambiguous UTF-8 JSON object.') from None


def read_json_response(response: Any, limit: int = 33554432) -> dict[str, Any]:
    data = bytearray()
    for chunk in response.iter_bytes():
        data.extend(chunk)
        if len(data) > limit:
            raise ProbeError('Provider response exceeds the size limit.')
    return json_object(bytes(data))


def token_usage(body: dict[str, Any]) -> tuple[int | None, int | None]:
    usage = body.get('usage')
    if not isinstance(usage, dict):
        return None, None
    values = [usage.get('prompt_tokens', usage.get('input_tokens')),
              usage.get('completion_tokens', usage.get('output_tokens'))]
    return tuple(value if type(value) is int and value >= 0 else None for value in values)


_CHAT_FIELDS = {
    'model', 'messages', 'stream', 'stream_options', 'temperature', 'top_p', 'n',
    'max_tokens', 'max_completion_tokens', 'stop', 'presence_penalty', 'frequency_penalty',
    'logit_bias', 'logprobs', 'top_logprobs', 'seed', 'user', 'tools', 'tool_choice',
    'parallel_tool_calls', 'response_format', 'reasoning_effort', 'chat_template_kwargs',
}


def prepare_chat(body: dict[str, Any], definition: dict[str, Any], plan: Any) -> dict[str, Any]:
    """Validate transport capabilities before credentials/admission; bind reasoning."""
    if set(body) - _CHAT_FIELDS:
        raise SelectionError('Unsupported Chat Completions field; no request was forwarded.')
    caps = definition['capabilities']
    if not caps['chat']:
        raise SelectionError('Selected model does not declare chat.')
    payload = deepcopy(body)
    if type(payload.get('stream', False)) is not bool:
        raise SelectionError('stream must be a boolean.')
    if payload.get('stream') and not caps['streaming']:
        raise SelectionError('Selected model does not declare streaming.')
    messages = payload.get('messages')
    if not isinstance(messages, list) or not messages:
        raise SelectionError('messages must be a nonempty array.')
    for message in messages:
        if not isinstance(message, dict) or message.get('role') not in {
            'system', 'developer', 'user', 'assistant', 'tool', 'function',
        }:
            raise SelectionError('Unsupported message role or shape.')
        if set(message) - {'role', 'content', 'name', 'tool_calls', 'tool_call_id', 'refusal',
                           'reasoning_content', 'function_call'}:
            raise SelectionError('Unsupported message field.')
        if message['role'] in {'system', 'developer'} and not caps['system_prompt']:
            raise SelectionError('Selected model does not declare system_prompt.')
        if (message['role'] in {'tool', 'function'} or message.get('tool_calls')
                or message.get('function_call')) and not caps['tools']:
            raise SelectionError('Selected model does not declare tools.')
        content = message.get('content')
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    raise SelectionError('Unsupported message content.')
                if part.get('type') == 'text':
                    if set(part) != {'type', 'text'} or not isinstance(part['text'], str):
                        raise SelectionError('Invalid text content part.')
                elif part.get('type') == 'image_url':
                    if not caps['vision']:
                        raise SelectionError('Selected model does not declare vision.')
                    image = part.get('image_url')
                    if (set(part) != {'type', 'image_url'} or not isinstance(image, dict)
                            or set(image) - {'url', 'detail'}
                            or not isinstance(image.get('url'), str)
                            or not image['url'].startswith(('data:image/png;base64,',
                                'data:image/jpeg;base64,', 'data:image/webp;base64,'))):
                        raise SelectionError('Only inline base64 PNG/JPEG/WebP images are accepted.')
                else:
                    raise SelectionError('Unsupported message content type.')
        elif content is not None and not isinstance(content, str):
            raise SelectionError('Unsupported message content.')
    if payload.get('tools') or payload.get('tool_choice') or payload.get('parallel_tool_calls'):
        if not caps['tools']:
            raise SelectionError('Selected model does not declare tools.')
    tools = payload.get('tools')
    if tools is not None:
        if not isinstance(tools, list):
            raise SelectionError('tools must be an array of function definitions.')
        for tool in tools:
            if (not isinstance(tool, dict) or set(tool) != {'type', 'function'}
                    or tool['type'] != 'function' or not isinstance(tool['function'], dict)):
                raise SelectionError('Only client-executed function tools are supported.')
            function = tool['function']
            if (set(function) - {'name', 'description', 'parameters', 'strict'}
                    or not isinstance(function.get('name'), str) or not function['name']
                    or ('parameters' in function and not isinstance(function['parameters'], dict))
                    or ('description' in function and not isinstance(function['description'], str))
                    or ('strict' in function and type(function['strict']) is not bool)):
                raise SelectionError('Invalid function tool definition.')
    choice = payload.get('tool_choice')
    if choice is not None and not (isinstance(choice, str) and choice in {'none', 'auto', 'required'}):
        if (not isinstance(choice, dict) or set(choice) != {'type', 'function'}
                or choice['type'] != 'function' or not isinstance(choice['function'], dict)
                or set(choice['function']) != {'name'}
                or not isinstance(choice['function']['name'], str) or not choice['function']['name']):
            raise SelectionError('Unsupported tool_choice.')
    fmt = payload.get('response_format')
    if fmt is not None:
        if not isinstance(fmt, dict) or fmt.get('type') not in {'text', 'json_object', 'json_schema'}:
            raise SelectionError('Unsupported response_format.')
        if fmt['type'] != 'text' and not caps['json_schema']:
            raise SelectionError('Selected model does not declare json_schema.')
    effort = payload.pop('reasoning_effort', None)
    template = payload.pop('chat_template_kwargs', None)
    if template is not None and (not isinstance(template, dict)
            or set(template) != {'enable_thinking'}
            or type(template['enable_thinking']) is not bool):
        raise SelectionError('Only the declared enable_thinking template control is supported.')
    if plan.selected != 'provider':
        expected_effort = plan.value if plan.parameter == 'reasoning_effort' else None
        expected_template = ({'enable_thinking': plan.value}
                             if plan.parameter == 'chat_template_kwargs.enable_thinking' else None)
        if effort is not None and effort != expected_effort:
            raise SelectionError('Request conflicts with the bound reasoning policy.')
        if template is not None and template != expected_template:
            raise SelectionError('Request conflicts with the bound reasoning policy.')
        if expected_effort is not None:
            payload['reasoning_effort'] = expected_effort
        if expected_template is not None:
            payload['chat_template_kwargs'] = expected_template
    else:
        if effort is not None:
            values = definition['reasoning'].get('values', {}).values()
            if (plan.control != 'reasoning_effort' or not isinstance(effort, str)
                    or effort not in values):
                raise SelectionError('Request reasoning_effort is not declared.')
            payload['reasoning_effort'] = effort
        if template is not None:
            if plan.control != 'chat_template_kwargs':
                raise SelectionError('Request thinking control is not declared.')
            payload['chat_template_kwargs'] = template
    return payload


def prepare_system_one(body: dict[str, Any]) -> dict[str, Any]:
    """Text/JSON state and noul/choice/score subset of the System One HTTP interface."""
    if set(body) != {'model', 'state', 'questions'}:
        raise SelectionError('System One requires exactly model, state, and questions.')
    if not isinstance(body['model'], str) or not body['model']:
        raise SelectionError('System One model must be a nonempty string.')
    if not isinstance(body['state'], (str, dict, list)):
        raise SelectionError('System One state must be text, an object, or an array.')
    questions = body['questions']
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 64:
        raise SelectionError('System One requires 1 to 64 named questions.')
    for name, question in questions.items():
        if (not isinstance(name, str) or not name or not isinstance(question, dict)
                or set(question) - {'type', 'instructions', 'criteria'}
                or question.get('type') not in {'noul', 'choice', 'score'}
                or not isinstance(question.get('instructions'), str)):
            raise SelectionError('Invalid System One question; use noul, choice, or score.')
        criteria = question.get('criteria')
        if question['type'] == 'noul':
            if criteria is not None and (not isinstance(criteria, dict)
                    or set(criteria) != {'true', 'false'}
                    or not all(isinstance(v, str) for v in criteria.values())):
                raise SelectionError('noul criteria must describe true and false.')
        elif question['type'] == 'choice':
            if (not isinstance(criteria, (dict, list)) or not 2 <= len(criteria) <= 255
                    or not all(isinstance(v, str) and v for v in criteria)
                    or (isinstance(criteria, list) and len(set(criteria)) != len(criteria))
                    or (isinstance(criteria, dict) and not all(
                        v is None or isinstance(v, str) for v in criteria.values()))):
                raise SelectionError('choice criteria must contain distinct named options.')
        else:
            if (not isinstance(criteria, list) or not 2 <= len(criteria) <= 255
                    or not all(isinstance(v, str) and v for v in criteria)):
                raise SelectionError('score criteria must be a list of ordered descriptions.')
    try:
        return json_object(json.dumps(body, allow_nan=False, ensure_ascii=False).encode('utf-8'))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise SelectionError('System One request must be finite JSON.') from None


def _probability(value: Any) -> bool:
    return type(value) in (float, int) and 0 <= value <= 1


def validate_answers(request: dict[str, Any], response: dict[str, Any]) -> None:
    answers = response.get('answers')
    if not isinstance(answers, dict) or set(answers) != set(request['questions']):
        raise ProbeError('System One response must answer every requested question exactly once.')
    for name, question in request['questions'].items():
        answer = answers[name]
        kind = question['type']
        if not isinstance(answer, dict) or answer.get('type') != kind:
            raise ProbeError('System One answer type does not match the question.')
        if kind == 'noul':
            if not _probability(answer.get('noul')):
                raise ProbeError('Invalid noul probability.')
            continue
        probabilities = answer.get('probabilities')
        expected = (set(question['criteria']) if kind == 'choice'
                    else {str(i) for i in range(len(question['criteria']))})
        if (not isinstance(probabilities, dict) or set(probabilities) != expected
                or not all(_probability(v) for v in probabilities.values())
                or not math.isclose(sum(probabilities.values()), 1.0, abs_tol=0.01)):
            raise ProbeError('Invalid System One option probabilities.')
        if 'confidence' in answer and not _probability(answer['confidence']):
            raise ProbeError('Invalid System One confidence.')
        if kind == 'choice' and answer.get('choice') not in expected:
            raise ProbeError('System One chose an unknown option.')
        if kind == 'score':
            score = answer.get('score')
            if type(score) not in (float, int) or not 0 <= score <= len(expected) - 1:
                raise ProbeError('Invalid System One score.')
