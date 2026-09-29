"""Own model client (stage B, docs/LLM_LAYER_2026-09-21.md): prompts and schemas are files, providers are replay and openrouter.

The product builds every request itself from the prompt file of an agent and its input. The SHA-256 of the canonical
JSON of agent, prompt version and hash, schema hash and messages identifies the call. replay answers from records made
in advance by a developer model strictly from the request file (docs/LLM_LAYER_2026-09-21.md, section 3); openrouter
calls the live model once the owner sets the key (docs/PROJECT_DECISIONS.md, items 9-11). A missing record, a missing
key, a network failure or an answer outside the schema is a failed call: the caller takes its deterministic fallback.
Tests never use the network. Every call returns a record for run.json.
"""
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
REGISTRY = "profiles/llm/agents.json"
MODELS = "profiles/llm/models.json"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(root, name):
    return (Path(root) / name).read_bytes().decode("utf-8")


def build_request(agent, payload, root=ROOT):
    """The exact messages the product sends for one call of an agent, with the identity of the call."""
    spec = json.loads(_read(root, REGISTRY))["agents"][agent]
    prompt = _read(root, spec["prompt"])
    schema = json.loads(_read(root, spec["schema"]))
    # C7, question 6: a payload may carry images; they follow the JSON as image parts of the user message (the format of
    # OpenAI-compatible vision models) and are part of the identity of the call.
    images = payload.get("images") if isinstance(payload, dict) else None
    if images:
        text = canonical({k: v for k, v in payload.items() if k != "images"})
        content = [{"type": "text", "text": text}] + [{"type": "image_url", "image_url": {"url": f"data:{i['mime']};base64,{i['data']}"}} for i in images]
    else:
        content = canonical(payload)
    messages = [{"role": "system", "content": prompt}, {"role": "user", "content": content}]
    identity = {"agent": agent, "prompt_version": spec["version"], "prompt_sha256": sha256(prompt),
                "schema_sha256": sha256(canonical(schema)), "messages": messages}
    return {"schema": "vsp.llm-request/1", **identity, "sha256": sha256(canonical(identity)), "response_schema": schema,
            "options": {k: spec[k] for k in ("reasoning_effort", "temperature", "max_tokens") if k in spec}}


def component_versions(root=ROOT):
    """Plan J: the agents of the release with the version and the SHA-256 of their prompt and schema, computed as a call
    records them in run.json (build_request), and the providers of models.json without secrets (only whether the key
    variable is set)."""
    registry = json.loads(_read(root, REGISTRY))
    models = json.loads(_read(root, MODELS))
    agents = [{"agent": name, "prompt_version": spec["version"], "prompt": spec["prompt"], "prompt_sha256": sha256(_read(root, spec["prompt"])),
               "schema": spec["schema"], "schema_sha256": sha256(canonical(json.loads(_read(root, spec["schema"])))),
               "options": {k: spec[k] for k in ("reasoning_effort", "temperature", "max_tokens") if k in spec}}
              for name, spec in registry["agents"].items()]
    providers = {}
    for name, config in models["providers"].items():
        providers[name] = {k: config[k] for k in ("model", "url", "store") if k in config}
        if config.get("key_env"):
            providers[name].update({"key_env": config["key_env"], "key_set": bool(os.environ.get(config["key_env"]))})
    return {"schema": "vsp.component-versions/1", "agents": agents, "default_provider": models["default_provider"], "providers": providers}


# ---- JSON schema: the subset the agent schemas use -------------------------------------------------------------
TYPES = {"object": dict, "array": list, "string": str, "boolean": bool}


def validate(value, schema, path="$"):
    """Errors of value against a JSON schema subset: type, enum, properties, required, additionalProperties false,
    items, minItems, maxItems, minLength, maxLength, pattern, minimum, maximum."""
    errors = []
    kind = schema.get("type")
    if kind == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif kind == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    else:
        ok = kind is None or isinstance(value, TYPES[kind])
    if not ok:
        return [f"{path}: ожидается {kind}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: значение вне перечня")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", len(value)):
            errors.append(f"{path}: длина {len(value)} вне границ")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: не соответствует шаблону")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            errors.append(f"{path}: число вне границ")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            errors.append(f"{path}: число элементов {len(value)} вне границ")
        for i, item in enumerate(value):
            errors += validate(item, schema.get("items", {}), f"{path}[{i}]")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: обязательное поле отсутствует")
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                errors += validate(item, properties[key], f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{key}: лишнее поле")
    return errors


def extract_json(text):
    """The JSON object of a model answer: the whole text, a fenced block, or the outermost braces."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    for candidate in (text, fenced.group(1) if fenced else None, text[text.find("{"):text.rfind("}") + 1] if "{" in text else None):
        if candidate:
            try:
                value = json.loads(candidate)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    raise ValueError("В ответе модели нет объекта JSON")


# ---- providers -------------------------------------------------------------------------------------------------------
class CallFailed(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason, self.detail = reason, detail


def replay(request, config, root=ROOT):
    """A record made in advance for exactly this request, or a failed call (no-record)."""
    path = Path(root) / config["store"] / request["agent"] / f"{request['sha256']}.json"
    if not path.is_file():
        raise CallFailed("no-record", path.name)
    record = json.loads(path.read_bytes().decode("utf-8"))
    if record.get("request_sha256") != request["sha256"]:
        raise CallFailed("record-mismatch", path.name)
    return {"text": record.get("raw_text") or canonical(record["response"]), "producer": record.get("producer", {}), "tokens": None}


# Qwen3.8-27B on OpenRouter (27.09.2026, run Q1): with reasoning_effort "low" the model thought for 2268 tokens and the
# answer of outline_planner (max_tokens 3000) was cut, or came with no content at all; the reasoning counts in max_tokens.
# max_tokens of an agent limits its answer, so a call that reasons gets this much more room for the reasoning.
REASONING_ROOM = 6000


def strict_schema(schema):
    """The schema of an agent as a strict decoder takes it: without the keys $id and $comment."""
    if isinstance(schema, dict):
        return {k: strict_schema(v) for k, v in schema.items() if not k.startswith("$")}
    if isinstance(schema, list):
        return [strict_schema(v) for v in schema]
    return schema


def answer_format(request, config):
    """response_format of a call. Qwen3.8-27B in json_object mode loses the name of the first field now and then ({"": ...},
    {}): 86 of 564 calls of QL3 and K3 on 28.09.2026 (analysis/style-experiments/20260928-qwen-strict). A provider may ask for
    decoding held to the schema of the agent (models.json, response_format "json_schema"); agents in json_schema_except keep
    json_object."""
    if config.get("response_format") == "json_schema" and request.get("agent") not in config.get("json_schema_except", []):
        return {"type": "json_schema", "json_schema": {"name": request["agent"], "strict": True, "schema": strict_schema(request["response_schema"])}}
    return {"type": "json_object"}


def openrouter(request, config, root=ROOT, opener=urllib.request.urlopen):
    """One live call through OpenRouter with pinned providers, at most `retries` repeats on network errors and 5xx."""
    # A local OpenAI-compatible endpoint of a development stand-in model (models.json, kind openai-compatible) has no key
    # and no provider routing of OpenRouter; the request itself is the same.
    key = os.environ.get(config["key_env"], "") if config.get("key_env") else None
    if key == "":
        raise CallFailed("no-key", config["key_env"])
    options = request["options"]
    # A provider may set its own effort (models.json, openrouter: Qwen thinks thousands of tokens at any effort, 28.09.2026).
    effort = config.get("reasoning_effort") or options.get("reasoning_effort", "none")
    messages = request["messages"]
    note = (config.get("agent_notes") or {}).get(request.get("agent"))
    if note and isinstance(messages[1]["content"], str):
        # A provider may add a line to the system prompt of an agent (models.json, agent_notes): the identity of the request
        # and the recorded answers stay as they are. {facts_max} = slides x facts_per_slide_max of the payload.
        values = json.loads(messages[1]["content"])
        values["facts_max"] = (values.get("slides") or 0) * (values.get("facts_per_slide_max") or 0)
        try:
            messages = [{**messages[0], "content": messages[0]["content"] + "\n\n" + note.format(**values)}] + messages[1:]
        except (KeyError, IndexError, ValueError):
            pass
    body = {"model": config["model"], "messages": messages, "temperature": options.get("temperature", 0),
            "max_tokens": options.get("max_tokens", 2000) + (REASONING_ROOM if effort != "none" else 0),
            "response_format": answer_format(request, config), "reasoning": {"effort": effort}}
    if config.get("provider"):
        body["provider"] = config["provider"]
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"} | ({"Authorization": f"Bearer {key}"} if key else {})

    def send(payload=data):
        return _send(config, payload, headers, body["response_format"]["type"], opener)

    hedge = (config.get("hedge_seconds") or {}).get(request.get("agent"))
    if not hedge:
        return send()
    # The second request goes to the next provider of the pinned order: when one provider is slow for every request at once
    # (QS2, 28.09.2026: DeepInfra 33-47 s on a request Parasail answered in 13-14 s), a second request to it does not help.
    order = (body.get("provider") or {}).get("order") or []
    again = data
    if len(order) > 1:
        again = json.dumps({**body, "provider": {**body["provider"], "order": order[1:] + order[:1]}}, ensure_ascii=False).encode("utf-8")
    return hedged(send, hedge, lambda: send(again))


def hedged(send, seconds, send_again=None):
    """send() once and, when it has not returned within `seconds`, send_again() (or send() once more) in parallel; the first
    answer wins. Qwen3.8-27B
    on OpenRouter answers most calls in seconds but now and then queues one for minutes: outline_planner 522 tokens in
    249.9 s took a deck of QS1 to 423.9 s (28.09.2026, analysis/style-experiments/20260928-qwen-strict). A call that fails
    before the threshold fails as before; the slower request is left to finish in a daemon thread and its answer is dropped.
    A failure of a request is raised as it came (CallFailed, or any other exception the caller already handles)."""
    results = queue.Queue()

    def run(number):
        # Any exception goes back to the caller: a thread that died silently would leave the call waiting for ever.
        try:
            results.put((number, (send if number == 1 or send_again is None else send_again)(), None))
        except Exception as failure:  # noqa: BLE001 — re-raised in the calling thread as it was
            results.put((number, None, failure))

    threading.Thread(target=run, args=(1,), daemon=True).start()
    try:
        number, answer, failure = results.get(timeout=seconds)
    except queue.Empty:
        threading.Thread(target=run, args=(2,), daemon=True).start()
        last = None
        for _ in range(2):
            number, answer, failure = results.get()
            if answer is not None:
                return {**answer, "hedge": {"after_seconds": seconds, "winner": number}}
            last = failure
        raise last
    if answer is None:
        raise failure
    return answer


def _send(config, data, headers, answer_format_type, opener):
    """One request with at most `retries` repeats on network errors and 5xx."""
    last = None
    for attempt in range(1 + config.get("retries", 1)):
        http = urllib.request.Request(config["url"], data=data, method="POST", headers=headers)
        try:
            with opener(http, timeout=config.get("timeout_seconds", 90)) as response:
                answer = json.loads(response.read().decode("utf-8"))
            # An answer with no content (all the tokens went to the reasoning) is an empty answer, never None: the call
            # then fails as cut or without JSON and the agent repeats or takes its fallback (run Q1 of 27.09 crashed here).
            choice = answer["choices"][0]["message"].get("content") or ""
            usage = answer.get("usage", {})
            return {"text": choice, "producer": {"kind": config.get("producer_kind", "live"), "model": answer.get("model", config["model"]), "provider": answer.get("provider"),
                                                 "response_format": answer_format_type},
                    "tokens": {"prompt": usage.get("prompt_tokens"), "completion": usage.get("completion_tokens"),
                               "reasoning": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")},
                    "finish_reason": answer["choices"][0].get("finish_reason")}
        except urllib.error.HTTPError as error:
            last = CallFailed("http", str(error.code))
            if error.code < 500:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last = CallFailed("network", type(error).__name__)
        except (KeyError, IndexError, ValueError) as error:
            last = CallFailed("bad-answer", type(error).__name__)
            break
    raise last


PROVIDERS = {"replay": replay, "openrouter": openrouter, "openai-compatible": openrouter}


def is_live(provider=None, root=ROOT):
    """Whether calls of this provider (the default one when None) go to a model rather than to recorded answers."""
    models = json.loads(_read(root, MODELS))
    name = provider or models["default_provider"]
    return models["providers"][name].get("kind", name) != "replay"


# Owner decision 55 (29.09.2026): the page shows a run as it goes. A run names the listener of its requests by the directory
# its calls record into; every call there tells the listener when the request goes out and when the answer is back (with the
# time measured here). A listener that fails never fails a call.
LISTENERS = {}


def _place(record_dir):
    return str(Path(record_dir).resolve())


def listen(record_dir, listener):
    LISTENERS[_place(record_dir)] = listener


def forget(record_dir):
    LISTENERS.pop(_place(record_dir), None)


def _tell(listener, record):
    if listener is None:
        return
    try:
        listener(record)
    except Exception:  # noqa: BLE001 — the page is not worth a call
        pass


def call(agent, payload, provider=None, record_dir=None, root=ROOT, repair=None):
    """Call one agent; returns (response or None, record). The record goes to run.json whatever the outcome.

    repair(value, errors): a caller's narrow repair of schema errors it knows to be harmless (planner: outline titles a few
    signs too long); the repaired value must pass the schema, and the record keeps the errors it repaired.

    record_dir: the request file is always written there (llm/requests/<agent>/<sha256>.json) so that a developer model
    can answer exactly this request later, and the raw answer of a completed call next to it (llm/responses/...).
    """
    request = build_request(agent, payload, root)
    models = json.loads(_read(root, MODELS))
    provider = provider or models["default_provider"]
    if record_dir is not None:
        target = Path(record_dir) / "llm" / "requests" / agent / f"{request['sha256']}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((json.dumps(request, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    record = {"agent": agent, "request_sha256": request["sha256"], "prompt_version": request["prompt_version"],
              "prompt_sha256": request["prompt_sha256"], "provider": provider, "ok": False}
    listener = LISTENERS.get(_place(record_dir)) if record_dir is not None and LISTENERS else None
    told = {"agent": agent, "id": f"{request['sha256'][:12]}-{threading.get_ident()}-{time.monotonic_ns()}",
            "again": isinstance(payload, dict) and "previous_answer_problems" in payload}
    _tell(listener, {**told, "state": "sent"})
    started = time.perf_counter()
    try:
        config = models["providers"][provider]
        answer = PROVIDERS[config.get("kind", provider)](request, config, root)
        record.update({"producer": answer["producer"], "tokens": answer["tokens"]})
        if answer.get("hedge"):
            record["hedge"] = answer["hedge"]
        if answer.get("finish_reason"):
            record["finish_reason"] = answer["finish_reason"]
        if record_dir is not None:
            target = Path(record_dir) / "llm" / "responses" / agent / f"{request['sha256']}.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(answer["text"].encode("utf-8"))
        try:
            value = extract_json(answer["text"])
        except ValueError:
            # Twelfth session: an answer cut by the limit of tokens of the agent (a long content package for brief_analyst
            # on a slow provider) is named as such, not as "no JSON" (docs/LLM_AGENT_ROLE_2026-09-25.md).
            if answer.get("finish_reason") == "length":
                raise CallFailed("truncated", f"max_tokens {request['options'].get('max_tokens')}")
            raise
        errors = validate(value, request["response_schema"])
        if errors and repair is not None:
            fixed = repair(value, errors)
            if fixed is not None and not validate(fixed, request["response_schema"]):
                record["repaired"] = errors[:5]
                value, errors = fixed, []
        if errors:
            raise CallFailed("schema", "; ".join(errors[:5]))
        record["ok"] = True
        return value, record
    except CallFailed as failure:
        record.update({"reason": failure.reason, "detail": failure.detail})
        return None, record
    except ValueError as failure:
        record.update({"reason": "no-json", "detail": str(failure)})
        return None, record
    finally:
        record["milliseconds"] = round((time.perf_counter() - started) * 1000, 1)
        _tell(listener, {**told, "state": "answered", "ok": record["ok"], "milliseconds": record["milliseconds"],
                         "reason": record.get("reason"), "second_request": bool(record.get("hedge")),
                         "answer_tokens": (record.get("tokens") or {}).get("completion")})
