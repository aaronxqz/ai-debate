#!/usr/bin/env python3
"""A checkpointed, resumable two-agent AI debate using Claude Code CLI.

Only the Python standard library is required. Each model call is stateless: the
controller sends a bounded summary and recent exchanges, then persists results.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_DIR = ROOT / "state"
LOG_DIR = ROOT / "logs"
ARCHIVE_DIR = ROOT / "archive"
STATE_PATH = STATE_DIR / "run_state.json"
JSONL_PATH = LOG_DIR / "transcript.jsonl"
MARKDOWN_PATH = LOG_DIR / "transcript.md"
ERROR_LOG_PATH = LOG_DIR / "errors.log"

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "response": {"type": "string", "minLength": 20},
        "next_question": {"type": ["string", "null"], "maxLength": 500},
        "key_insights": {
            "type": "array", "items": {"type": "string", "maxLength": 280}, "maxItems": 3
        },
        "disagreements": {
            "type": "array", "items": {"type": "string", "maxLength": 280}, "maxItems": 2
        },
        "assumptions": {
            "type": "array", "items": {"type": "string", "maxLength": 280}, "maxItems": 2
        },
        "predictions_to_test": {
            "type": "array", "items": {"type": "string", "maxLength": 280}, "maxItems": 2
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1}
    },
    "required": [
        "response", "next_question", "key_insights", "disagreements",
        "assumptions", "predictions_to_test", "confidence"
    ]
}

MEMORY_CATEGORIES = {
    "key_insights": "Key insights",
    "disagreements": "Open disagreements",
    "assumptions": "Assumptions to revisit",
    "predictions_to_test": "Predictions to test"
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def load_config() -> dict[str, Any]:
    config = load_json(CONFIG_PATH)
    maximum = int(config.get("max_questions", 200))
    default = int(config.get("default_target_questions", 5))
    checkpoint = int(config.get("checkpoint_every", 10))
    if maximum < 1 or maximum > 200:
        raise ValueError("config.max_questions must be between 1 and 200 for this experiment.")
    if default < 1 or default > maximum:
        raise ValueError("config.default_target_questions must be between 1 and max_questions.")
    if checkpoint < 1:
        raise ValueError("config.checkpoint_every must be at least 1.")
    max_budget = config.get("max_budget_usd_per_call")
    if max_budget is not None and (not isinstance(max_budget, (int, float)) or max_budget <= 0):
        raise ValueError("config.max_budget_usd_per_call must be a positive number or null.")
    if not isinstance(config.get("stages"), list) or not config["stages"]:
        raise ValueError("config.stages must be a non-empty list.")
    return config


def ensure_dirs() -> None:
    for directory in (STATE_DIR, LOG_DIR, ARCHIVE_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def initial_state(topic: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "topic": topic,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "status": "in_progress",
        "last_target": None,
        "next_agent": "A",
        "questions": [],
        "turns": [],
        "shared_memory": {category: [] for category in MEMORY_CATEGORIES},
        "last_stop_reason": None
    }


def question_count(state: dict[str, Any]) -> int:
    return len(state.get("questions", []))


def latest_question_is_answered(state: dict[str, Any]) -> bool:
    questions = state.get("questions", [])
    return bool(questions) and questions[-1].get("answer") is not None


def stage_for(config: dict[str, Any], number: int) -> dict[str, Any]:
    for stage in config["stages"]:
        if int(stage["start"]) <= number <= int(stage["end"]):
            return stage
    # Defensive fallback if someone edits the ranges incompletely.
    return config["stages"][-1]


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text))


def compact_text(value: Any, maximum: int = 280) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    if len(text) > maximum:
        text = text[: maximum - 1].rstrip() + "…"
    return text


def normalize_question(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def add_memory_updates(state: dict[str, Any], output: dict[str, Any], agent: str, config: dict[str, Any]) -> None:
    memory = state.setdefault("shared_memory", {category: [] for category in MEMORY_CATEGORIES})
    per_category = int(config.get("max_memory_items_per_category", 6))
    for category in MEMORY_CATEGORIES:
        target = memory.setdefault(category, [])
        for raw in output.get(category, []):
            note = compact_text(raw)
            if not note:
                continue
            tagged = f"Agent {agent}: {note}"
            if tagged.casefold() not in {item.casefold() for item in target}:
                target.append(tagged)
        memory[category] = target[-per_category:]


def format_memory(state: dict[str, Any]) -> str:
    memory = state.get("shared_memory", {})
    chunks: list[str] = []
    for category, label in MEMORY_CATEGORIES.items():
        values = memory.get(category, [])
        if values:
            chunks.append(label + ":\n" + "\n".join(f"- {item}" for item in values))
    return "\n\n".join(chunks) if chunks else "No accumulated notes yet."


def format_recent_history(state: dict[str, Any], count: int) -> str:
    questions = state.get("questions", [])
    recent = questions[-count:]
    if not recent:
        return "No previous exchanges."
    out: list[str] = []
    for q in recent:
        answer = q.get("answer")
        answer_text = answer if answer else "[Not answered yet]"
        out.append(
            f"Question {q['number']} ({q['stage']}; asked by Agent {q['asked_by']}): {q['text']}\n"
            f"Answer ({'Agent ' + q['answered_by'] if q.get('answered_by') else 'pending'}): {answer_text}"
        )
    return "\n\n".join(out)


def build_user_prompt(
    state: dict[str, Any], config: dict[str, Any], agent: str, mode: str
) -> tuple[str, int | None, int | None]:
    """Return prompt, question being answered, question expected to be asked."""
    questions = state["questions"]
    count = len(questions)
    checkpoint = int(config["checkpoint_every"])
    answered_question: int | None = None
    asked_question: int | None = None

    if mode == "opening":
        asked_question = 1
        target_stage = stage_for(config, 1)
        task = (
            "Give your opening position on the overall topic, then ask exactly Question 1. "
            "This is the start of the debate; no prior question needs an answer."
        )
    elif mode == "answer_and_ask":
        answered_question = count
        asked_question = count + 1
        current_stage = stage_for(config, count)
        target_stage = stage_for(config, count + 1)
        task = (
            f"Answer Question {count} directly before moving the debate forward. Then ask exactly "
            f"Question {count + 1}. Your question should be relevant to the answer and to the "
            f"next topic stage. Do not ask multiple questions."
        )
        task += f"\nQuestion {count} belongs to stage: {current_stage['name']}."
    elif mode == "answer_only":
        answered_question = count
        current_stage = stage_for(config, count)
        target_stage = current_stage
        task = (
            f"Answer Question {count} directly and finish the thought. Do not ask a new question, "
            "do not include a rhetorical question, and set next_question to null. This answer-only "
            "turn occurs because the question target or a usage checkpoint has been reached."
        )
    elif mode == "continue_and_ask":
        asked_question = count + 1
        target_stage = stage_for(config, count + 1)
        task = (
            f"The previous question has already been answered. Continue the discussion from the "
            f"latest exchange and ask exactly Question {count + 1}. Do not pretend an unanswered "
            "question exists and do not repeat the previous question."
        )
    else:
        raise ValueError(f"Unknown task mode: {mode}")

    q_to_answer = questions[-1] if answered_question is not None else None
    # Supply recent pairs and state rather than a growing full transcript.
    recent_history = format_recent_history(state, int(config.get("recent_history_pairs", 4)))
    memory = format_memory(state)
    next_stage_text = f"{target_stage['name']}: {target_stage['guidance']}"
    stage_question = (
        f"\nUNANSWERED QUESTION {q_to_answer['number']}: {q_to_answer['text']}"
        if q_to_answer else ""
    )

    prompt = f"""RESEARCH TOPIC
{state['topic']}

YOUR IDENTITY
You are Agent {agent}. Follow the role prompt supplied as your system prompt.

TASK MODE: {mode.upper()}
{task}

QUESTION TARGET
The experiment target is {config['max_questions']} questions maximum. This run is targeting cumulative question {asked_question or answered_question or 1} only. The Python controller, not you, determines when to stop. Never try to extend the conversation beyond the task described here.

TOPIC STAGE FOR THE NEXT QUESTION OR CURRENT ANSWER
{next_stage_text}
{stage_question}

ACCUMULATED SHARED MEMORY (short notes, not unquestionable facts)
{memory}

RECENT EXCHANGE HISTORY
{recent_history}

OUTPUT RULES
- `response` should be a concise, thoughtful argument. In ANSWER_AND_ASK or ANSWER_ONLY modes, directly answer the unanswered question before adding analysis.
- `next_question` must contain only one question when this mode asks for one; it must be null in ANSWER_ONLY mode.
- Do not invent factual citations or statistics. State uncertainty honestly.
- Memory notes should be short and useful to future turns. Use at most 3 key insights, 2 disagreements, 2 assumptions, and 2 predictions to test.
- Confidence is your confidence in the main claim of this response, from 0 to 1; it is not a measure of objective truth.
- Return the requested structured output and nothing else.
"""
    return prompt, answered_question, asked_question


def parse_cli_result(stdout: str) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        preview = stdout[-2500:] if stdout else "<empty stdout>"
        raise RuntimeError(f"Claude CLI did not return valid JSON. Last output:\n{preview}") from exc

    if not isinstance(envelope, dict):
        raise RuntimeError("Claude CLI JSON response was not an object.")

    metadata = {
        key: envelope.get(key)
        for key in ("subtype", "is_error", "duration_ms", "duration_api_ms", "num_turns", "session_id")
        if key in envelope
    }
    if envelope.get("is_error"):
        raise RuntimeError(f"Claude CLI reported an error: {envelope.get('result', envelope)}")

    payload = envelope.get("structured_output")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            pass
    if not isinstance(payload, dict):
        result_value = envelope.get("result")
        if isinstance(result_value, dict):
            payload = result_value
        elif isinstance(result_value, str):
            try:
                parsed_result = json.loads(result_value)
                if isinstance(parsed_result, dict):
                    payload = parsed_result
            except json.JSONDecodeError:
                pass
    # Accommodate CLI versions that return the structured object at the root.
    if not isinstance(payload, dict) and "response" in envelope and "next_question" in envelope:
        payload = envelope

    if not isinstance(payload, dict):
        raise RuntimeError(
            "No structured_output object found in Claude CLI response. "
            "Run `claude update` and check the installed CLI version."
        )
    return payload, metadata


def validate_output(output: dict[str, Any], mode: str) -> None:
    required = [
        "response", "next_question", "key_insights", "disagreements",
        "assumptions", "predictions_to_test", "confidence"
    ]
    missing = [key for key in required if key not in output]
    if missing:
        raise RuntimeError(f"Structured response is missing required fields: {', '.join(missing)}")
    if not isinstance(output["response"], str) or not output["response"].strip():
        raise RuntimeError("Structured response has an empty `response` field.")
    wants_question = mode in {"opening", "answer_and_ask", "continue_and_ask"}
    next_question = output["next_question"]
    if wants_question and (not isinstance(next_question, str) or not next_question.strip()):
        raise RuntimeError(f"Mode {mode} requires exactly one non-empty `next_question`.")
    if not wants_question and next_question is not None:
        raise RuntimeError("ANSWER_ONLY mode requires `next_question` to be null.")
    for key in MEMORY_CATEGORIES:
        if not isinstance(output[key], list) or not all(isinstance(x, str) for x in output[key]):
            raise RuntimeError(f"Structured response field `{key}` must be a list of strings.")
    if not isinstance(output["confidence"], (int, float)) or not 0 <= float(output["confidence"]) <= 1:
        raise RuntimeError("Structured response field `confidence` must be between 0 and 1.")


def call_claude(
    config: dict[str, Any], agent: str, mode: str, prompt: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    cli = str(config.get("cli_executable", "claude"))
    prompt_file = ROOT / "prompts" / f"agent_{agent.lower()}.txt"
    if not prompt_file.is_file():
        raise FileNotFoundError(f"Missing role prompt: {prompt_file}")

    cmd = [
        cli, "-p",
        "--model", str(config.get("model", "sonnet")),
        "--max-turns", "1",
        "--tools", "",
        "--disallowedTools", "mcp__*",
        "--system-prompt-file", str(prompt_file),
        "--output-format", "json",
        "--json-schema", json.dumps(OUTPUT_SCHEMA, separators=(",", ":"), ensure_ascii=False),
    ]
    max_budget = config.get("max_budget_usd_per_call")
    if max_budget is not None:
        cmd.extend(["--max-budget-usd", str(max_budget)])
    if config.get("use_bare_mode", True):
        cmd.append("--bare")
    if config.get("disable_session_persistence", True):
        cmd.append("--no-session-persistence")
    cmd.append(prompt)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=int(config.get("cli_timeout_seconds", 300)),
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Could not find `{cli}`. Run this script inside WSL where `claude` works, "
            "and verify `command -v claude`."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Claude CLI exceeded the {config.get('cli_timeout_seconds', 300)}-second timeout. "
            "No state was committed for this call; check CLI/network status before retrying."
        ) from exc

    if proc.returncode != 0:
        details = (proc.stderr or proc.stdout or "No error text returned").strip()
        hint = ""
        if "Not logged in" in details and config.get("use_bare_mode", True):
            hint = (
                "\nHINT: config.use_bare_mode is true, which adds --bare. In --bare mode the CLI "
                "only reads ANTHROPIC_API_KEY or apiKeyHelper; it never reads OAuth/keychain login. "
                "If you authenticate via `claude /login` (claude.ai / Pro subscription), set "
                "use_bare_mode to false in config.json."
            )
        raise RuntimeError(
            f"Claude CLI exited with code {proc.returncode}.\n"
            f"Command: {cli} -p ... (prompt omitted)\n"
            f"Last output:\n{details[-4000:]}{hint}"
        )
    output, metadata = parse_cli_result(proc.stdout)
    validate_output(output, mode)
    return output, metadata


def log_error(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with ERROR_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] {message}\n\n")


def update_state_after_turn(
    state: dict[str, Any], config: dict[str, Any], agent: str, mode: str,
    output: dict[str, Any], metadata: dict[str, Any], answered_q: int | None, asked_q: int | None
) -> None:
    response = output["response"].strip()
    questions = state["questions"]

    if answered_q is not None:
        question = questions[answered_q - 1]
        if question.get("answer") is not None:
            raise RuntimeError(f"Internal state error: Question {answered_q} was already answered.")
        question["answer"] = response
        question["answered_by"] = agent
        question["answered_at"] = now_iso()

    if asked_q is not None:
        question_text = output["next_question"].strip()
        norm_new = normalize_question(question_text)
        duplicates = [
            q for q in questions
            if norm_new and difflib.SequenceMatcher(None, norm_new, normalize_question(q["text"])).ratio() >= 0.92
        ]
        if duplicates:
            print(f"[WARN] The new question resembles Question {duplicates[-1]['number']}; it was kept for review.")
        stage = stage_for(config, asked_q)
        questions.append({
            "number": asked_q,
            "stage": stage["name"],
            "asked_by": agent,
            "text": question_text,
            "asker_context": response,
            "asked_at": now_iso(),
            "answer": None,
            "answered_by": None,
            "answered_at": None
        })
        if "?" not in question_text:
            print("[WARN] The model's next_question has no question mark; review it in the transcript.")

    add_memory_updates(state, output, agent, config)
    state["turns"].append({
        "turn_number": len(state["turns"]) + 1,
        "timestamp": now_iso(),
        "agent": agent,
        "mode": mode,
        "answered_question": answered_q,
        "asked_question": asked_q,
        "response": response,
        "key_insights": output.get("key_insights", []),
        "disagreements": output.get("disagreements", []),
        "assumptions": output.get("assumptions", []),
        "predictions_to_test": output.get("predictions_to_test", []),
        "confidence": float(output.get("confidence", 0.5)),
        "cli_metadata": metadata
    })
    state["next_agent"] = "B" if agent == "A" else "A"
    state["updated_at"] = now_iso()
    state["last_stop_reason"] = None


def render_markdown(state: dict[str, Any]) -> str:
    lines = [
        "# Two-Agent AI Debate Transcript",
        "",
        f"- Topic: {state['topic']}",
        f"- Started: {state['created_at']}",
        f"- Last updated: {state['updated_at']}",
        f"- Status: {state['status']}",
        f"- Questions currently recorded: {len(state['questions'])}",
        "",
        "> Note: Agent A and Agent B are role-conditioned calls through the configured Claude model, not independently developed AI systems.",
        ""
    ]
    for turn in state.get("turns", []):
        agent = turn["agent"]
        mode = turn["mode"]
        response = turn["response"].strip()
        answered = turn.get("answered_question")
        asked = turn.get("asked_question")
        if mode == "opening":
            lines.extend([f"## Agent {agent} — Opening position", "", response, ""])
            if asked:
                q = state["questions"][asked - 1]
                lines.extend([f"**Question {asked} (asked by Agent {agent}; {q['stage']})**", "", q["text"], ""])
        elif mode == "answer_and_ask":
            lines.extend([f"## Agent {agent} — Answer to Question {answered}", "", response, ""])
            if asked:
                q = state["questions"][asked - 1]
                lines.extend([f"**Question {asked} (asked by Agent {agent}; {q['stage']})**", "", q["text"], ""])
        elif mode == "answer_only":
            lines.extend([f"## Agent {agent} — Answer to Question {answered}", "", response, ""])
        elif mode == "continue_and_ask":
            lines.extend([f"## Agent {agent} — Continue the discussion", "", response, ""])
            if asked:
                q = state["questions"][asked - 1]
                lines.extend([f"**Question {asked} (asked by Agent {agent}; {q['stage']})**", "", q["text"], ""])

    lines.extend(["---", "", "## Questions and Answers", ""])
    for q in state.get("questions", []):
        lines.extend([
            f"### Question {q['number']} — {q['stage']}",
            "",
            f"**Asked by Agent {q['asked_by']}**",
            "",
            q["text"],
            ""
        ])
        if q.get("answer") is not None:
            lines.extend([f"**Answered by Agent {q['answered_by']}**", "", q["answer"], ""])
        else:
            lines.extend(["_Not answered yet._", ""])

    lines.extend(["---", "", "## Shared Memory at Last Save", ""])
    memory = state.get("shared_memory", {})
    for category, label in MEMORY_CATEGORIES.items():
        lines.extend([f"### {label}", ""])
        values = memory.get(category, [])
        if values:
            lines.extend([f"- {value}" for value in values])
        else:
            lines.append("- None recorded yet.")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_logs(state: dict[str, Any]) -> None:
    ensure_dirs()
    jsonl = "".join(json.dumps(turn, ensure_ascii=False) + "\n" for turn in state.get("turns", []))
    atomic_write_text(JSONL_PATH, jsonl)
    atomic_write_text(MARKDOWN_PATH, render_markdown(state))


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    atomic_write_json(STATE_PATH, state)
    write_logs(state)


def archive_current_run() -> None:
    existing = [path for path in (STATE_PATH, JSONL_PATH, MARKDOWN_PATH) if path.exists()]
    if not existing:
        print("No existing run files to archive.")
        return
    archive_name = datetime.now().strftime("run-%Y%m%d-%H%M%S")
    destination = ARCHIVE_DIR / archive_name
    suffix = 1
    while destination.exists():
        destination = ARCHIVE_DIR / f"{archive_name}-{suffix}"
        suffix += 1
    destination.mkdir(parents=True)
    for source in existing:
        shutil.copy2(source, destination / source.name)
    print(f"Archived the existing run to: {destination.relative_to(ROOT)}")
    for source in existing:
        source.unlink()


def run_check(config: dict[str, Any]) -> int:
    cli = str(config.get("cli_executable", "claude"))
    print(f"Project directory: {ROOT}")
    try:
        version = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=20, check=False)
        if version.returncode != 0:
            print(f"[FAIL] `{cli} --version` exited {version.returncode}: {(version.stderr or version.stdout).strip()}")
            return 1
        print(f"[OK] Claude CLI: {version.stdout.strip() or version.stderr.strip()}")
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"[FAIL] Could not run `{cli} --version`: {exc}")
        print("Run this from WSL, and check `command -v claude`.")
        return 1

    try:
        auth = subprocess.run([cli, "auth", "status"], capture_output=True, text=True, timeout=20, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"[FAIL] Could not check Claude authentication: {exc}")
        return 1
    if auth.returncode != 0:
        print(f"[FAIL] `claude auth status` failed: {(auth.stderr or auth.stdout).strip()}")
        return 1
    try:
        auth_data = json.loads(auth.stdout)
        method = auth_data.get("authMethod", auth_data.get("auth_method", "unknown"))
        print(f"[OK] Claude authentication status returned; auth method: {method}")
        if method in {"api_key", "api_key_helper", "third_party"}:
            print("[WARN] This authentication may use API or third-party billing, not your Pro subscription. Verify before running the debate.")
        elif method == "none":
            print("[FAIL] Claude reports no active authentication. Run `claude auth login` first.")
            return 1
    except json.JSONDecodeError:
        print("[WARN] Authentication status output was not JSON; inspect `claude auth status` manually.")
        print(auth.stdout.strip()[:1000])
    print("[OK] This check does not intentionally make a model-generation request.")
    print("Next: python3 main.py --questions 2  (a small CLI smoke test; this uses model allowance).")
    return 0


def choose_mode(state: dict[str, Any], target: int, checkpoint: int) -> str | None:
    count = question_count(state)
    if count == 0:
        return "opening"
    answered = latest_question_is_answered(state)
    if count >= target and answered:
        return None
    if not answered:
        if count >= target or count % checkpoint == 0:
            return "answer_only"
        return "answer_and_ask"
    # The previous question is already answered, usually because a previous
    # run stopped at its target or paused on a usage checkpoint.
    if count < target:
        return "continue_and_ask"
    return None


def checkpoint_prompt(question_no: int, target: int) -> bool:
    print("\n" + "=" * 68)
    print(f"USAGE CHECKPOINT: Question {question_no} has been answered; target is {target}.")
    print("Check your real plan usage before continuing. In a separate WSL terminal, run `claude`, then enter `/usage`.")
    print("The script cannot reliably read or enforce your remaining Pro percentage.")
    try:
        answer = input("Press Enter to continue, or type 'stop' to save and exit: ").strip().lower()
    except EOFError:
        answer = "stop"
    print("=" * 68)
    return answer not in {"stop", "s", "quit", "q", "exit"}


def run_debate(config: dict[str, Any], target: int) -> int:
    ensure_dirs()
    if STATE_PATH.exists():
        state = load_json(STATE_PATH)
        if state.get("schema_version") != 1:
            print("State file schema is unsupported. Back it up and start a new run with --reset.", file=sys.stderr)
            return 2
        if state.get("topic") != config["topic"]:
            print("The topic in config.json differs from the saved run. Use --reset to start a new run, or restore the old topic.", file=sys.stderr)
            return 2
    else:
        state = initial_state(config["topic"])
        save_state(state)

    current = question_count(state)
    if target < current:
        print(f"Target {target} is below the {current} questions already saved. This project only supports extending a run; use --reset for a fresh run.", file=sys.stderr)
        return 2
    if target > int(config.get("max_questions", 200)):
        print(f"Target cannot exceed config.max_questions={config.get('max_questions', 200)}.", file=sys.stderr)
        return 2

    if current >= target and latest_question_is_answered(state):
        print(f"The saved run already has {current} answered question(s), meeting this target.")
        print(f"Transcript: {MARKDOWN_PATH.relative_to(ROOT)}")
        print("To extend the same run, use a larger target, for example `python3 main.py --questions 20`.")
        return 0

    state["status"] = "in_progress"
    state["last_stop_reason"] = None
    state["last_target"] = target
    save_state(state)
    print(f"Topic: {state['topic']}")
    print(f"Saved questions: {current}; target: {target}; next agent: {state['next_agent']}")
    print(f"Model alias: {config.get('model', 'sonnet')}; checkpoint every {config['checkpoint_every']} questions.")
    print("Each call is a fresh, tool-restricted Claude Code CLI invocation; only compact memory and recent exchanges are sent.")

    while True:
        mode = choose_mode(state, target, int(config["checkpoint_every"]))
        if mode is None:
            state["status"] = "completed_target"
            state["last_target"] = target
            state["last_stop_reason"] = f"Target of {target} questions completed."
            save_state(state)
            print(f"\nDONE: {question_count(state)} question(s) asked and answered for this target.")
            print(f"Markdown transcript: {MARKDOWN_PATH.relative_to(ROOT)}")
            print(f"Structured transcript: {JSONL_PATH.relative_to(ROOT)}")
            print("Review usage now. Increase the target explicitly to continue, e.g. `python3 main.py --questions 20`.")
            return 0

        agent = state["next_agent"]
        prompt, answered_q, asked_q = build_user_prompt(state, config, agent, mode)
        qno_for_display = asked_q if asked_q is not None else answered_q
        print(f"\n[CALL {len(state['turns']) + 1}] Agent {agent} | {mode} | question reference: {qno_for_display}")
        print("Waiting for Claude CLI response... (Ctrl+C stops between calls; the last completed call remains saved.)")
        try:
            output, metadata = call_claude(config, agent, mode, prompt)
            response_words = word_count(output["response"])
            if response_words > int(config.get("max_response_words", 150)):
                print(f"[WARN] Response is {response_words} words (configured target: <= {config.get('max_response_words', 150)}). It is kept as returned.")
            update_state_after_turn(state, config, agent, mode, output, metadata, answered_q, asked_q)
            save_state(state)
        except KeyboardInterrupt:
            state["status"] = "paused"
            state["last_stop_reason"] = "User interrupted while a CLI call was in progress; this call may have consumed usage but was not committed."
            save_state(state)
            print("\nInterrupted. The last fully completed call is saved. The interrupted call may still have consumed usage.", file=sys.stderr)
            return 130
        except Exception as exc:  # fail closed; do not advance the question counter
            message = f"Agent {agent}, mode {mode}: {type(exc).__name__}: {exc}"
            log_error(message)
            state["status"] = "paused_error"
            state["last_stop_reason"] = message
            save_state(state)
            print(f"\n[ERROR] {message}", file=sys.stderr)
            print(f"No question was added for this failed call. Error details are in {ERROR_LOG_PATH.relative_to(ROOT)}.", file=sys.stderr)
            print("Check the problem before rerunning. The failed request may have consumed some usage.", file=sys.stderr)
            return 1

        if answered_q is not None:
            current_answered = state["questions"][answered_q - 1]
            print(f"[SAVED] Answered Question {answered_q} ({word_count(output['response'])} words).")
        if asked_q is not None:
            print(f"[SAVED] Added Question {asked_q}: {output['next_question']}")

        # Checkpoint only after a multiple-of-N question has an answer saved.
        if answered_q is not None and answered_q % int(config["checkpoint_every"]) == 0 and answered_q < target:
            if not checkpoint_prompt(answered_q, target):
                state["status"] = "paused_at_checkpoint"
                state["last_stop_reason"] = f"User stopped at usage checkpoint after Question {answered_q}."
                save_state(state)
                print(f"Saved at Question {answered_q}. Resume with `python3 main.py --questions {target}`.")
                return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a bounded two-agent AI debate through Claude Code CLI (WSL-friendly)."
    )
    parser.add_argument(
        "--questions", type=int, default=None,
        help="Cumulative question target (default: config.json default_target_questions; maximum 200)."
    )
    parser.add_argument("--check", action="store_true", help="Check Claude CLI availability and authentication; no model call.")
    parser.add_argument("--reset", action="store_true", help="Archive current state/transcripts and start a fresh run.")
    args = parser.parse_args()

    try:
        config = load_config()
        ensure_dirs()
        if args.check:
            return run_check(config)
        if args.reset:
            archive_current_run()
        target = args.questions if args.questions is not None else int(config.get("default_target_questions", 5))
        if target < 1:
            print("--questions must be at least 1.", file=sys.stderr)
            return 2
        if target > int(config.get("max_questions", 200)):
            print(f"--questions cannot exceed {config.get('max_questions', 200)}.", file=sys.stderr)
            return 2
        return run_debate(config, target)
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"[ERROR] Configuration/state problem: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
