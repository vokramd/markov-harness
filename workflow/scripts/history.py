"""Turn a session JSONL file (one Messages API message per line) into a `messages` array.

Consecutive messages with the same role are merged into one, so several tools that each
append their own tool_result line still produce the single user message the API expects.

    python3 history.py messages <session.jsonl> [<context>]   -> JSON array on stdout
    python3 history.py assemble < http_request output   -> the streamed reply as one message

A non-empty context file (memory.py context's snapshot of the memory index) goes at the start of
the first user message, inside <system-reminder> tags, the way Claude Code delivers MEMORY.md.

`assemble` reads a streaming http_request's output ({status_code, body: [{event, data}, ...]}), or a
non-streaming one whose body is the message itself, and prints {content, stop_reason, usage, invalid}, or {error, status} when the call or the stream
failed. `invalid` lists the ids of tool_use blocks whose streamed input was not a JSON object (with
eager input streaming the API does not check it, and max_tokens can cut it off); their input is
set to {} and the harness answers them with an error instead of running them.
"""
import json
import sys


def blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return list(content)


def messages(path, context_path=None):
    merged = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = [line for line in f if line.strip()]
    except FileNotFoundError:
        lines = []
    for line in lines:
        msg = json.loads(line)
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1]["content"] = blocks(merged[-1]["content"]) + blocks(msg["content"])
        else:
            merged.append({"role": msg["role"], "content": msg["content"]})
    # tool_result blocks must come first in a user message
    for msg in merged:
        if msg["role"] == "user" and isinstance(msg["content"], list):
            msg["content"].sort(key=lambda b: b.get("type") != "tool_result")
    context = ""
    if context_path:
        try:
            with open(context_path, encoding="utf-8") as f:
                context = f.read().strip()
        except FileNotFoundError:
            pass
    if context and merged and merged[0]["role"] == "user":
        reminder = {"type": "text", "text": f"<system-reminder>\n{context}\n</system-reminder>"}
        merged[0]["content"] = [reminder] + blocks(merged[0]["content"])
    return merged


def assemble(response):
    status, body = response.get("status_code"), response.get("body")
    if status == 200 and isinstance(body, dict) and "content" in body:   # not streamed: already one message
        return {"content": body["content"], "stop_reason": body.get("stop_reason"),
                "usage": body.get("usage", {}), "invalid": []}
    if status != 200 or not isinstance(body, list):
        return {"error": body, "status": status}
    message, blocks, partial_json, invalid = {}, {}, {}, []
    for event in body:
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        kind = data.get("type")
        if kind == "message_start":
            message = dict(data.get("message", {}))
        elif kind == "content_block_start":
            blocks[data["index"]] = dict(data["content_block"])
        elif kind == "content_block_delta":
            block, delta = blocks[data["index"]], data["delta"]
            dtype = delta.get("type")
            if dtype == "text_delta":
                block["text"] = block.get("text", "") + delta["text"]
            elif dtype == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta["thinking"]
            elif dtype == "signature_delta":
                block["signature"] = block.get("signature", "") + delta["signature"]
            elif dtype == "input_json_delta":
                partial_json[data["index"]] = partial_json.get(data["index"], "") + delta["partial_json"]
            elif dtype == "citations_delta":
                block.setdefault("citations", []).append(delta["citation"])
        elif kind == "content_block_stop":
            i = data["index"]
            if i in partial_json:
                try:
                    tool_input = json.loads(partial_json.pop(i) or "{}")
                except json.JSONDecodeError:
                    tool_input = None
                if not isinstance(tool_input, dict):
                    invalid.append(blocks[i].get("id"))
                    tool_input = {}
                blocks[i]["input"] = tool_input
        elif kind == "message_delta":
            message.update(data.get("delta", {}))
            message["usage"] = {**message.get("usage", {}), **data.get("usage", {})}
        elif kind == "error":
            return {"error": data, "status": status}
    for i in partial_json:  # a tool_use block the stream never closed
        invalid.append(blocks[i].get("id"))
        blocks[i]["input"] = {}
    if "stop_reason" not in message:
        return {"error": {"error": {"type": "incomplete_stream", "message": "stream ended before message_delta"}}, "status": status}
    return {
        "content": [blocks[i] for i in sorted(blocks)],
        "stop_reason": message.get("stop_reason"),
        "usage": message.get("usage", {}),
        "invalid": invalid,
    }


def main(argv):
    if len(argv) in (3, 4) and argv[1] == "messages":
        json.dump(messages(*argv[2:]), sys.stdout)
    elif len(argv) == 2 and argv[1] == "assemble":
        json.dump(assemble(json.load(sys.stdin)), sys.stdout)
    else:
        sys.exit("usage: history.py messages <session.jsonl> [<context>] | history.py assemble < response.json")


if __name__ == "__main__":
    main(sys.argv)
