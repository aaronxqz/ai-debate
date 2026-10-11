# Two-Agent AI Debate (Claude Code CLI, WSL)

A small, resumable experiment in which two role-conditioned Claude calls debate the current and future influence of AI, especially cybersecurity. Python controls the question count; the agents do not control their own stopping condition.

## What this kit does

- Runs Agent A (Capability Mapper) and Agent B (Security Critic) in alternating Claude Code CLI calls.
- Counts exactly one numbered question whenever the controller accepts a `next_question` field.
- Enforces a cumulative target of at most 200 questions.
- Uses structured JSON output and validates each response before committing it.
- Starts with a **5-question pilot by default**. You must explicitly raise the target.
- Pauses after every 10 answered questions (except at the target) so you can check actual subscription usage.
- Saves after every successful call and can continue the same run with a larger target.
- Stores a full Markdown transcript, JSON Lines turn log, structured run state, and bounded shared memory.
- Disables built-in tools and MCP tools for each agent call; agents do not need file or shell access.
- Uses only the Python standard library; there is no `pip install` step.

## Important limitations

1. **This script cannot read the remaining Claude Pro percentage or enforce a real Pro usage cap.** The usage checkpoint is a manual stop. Check `/usage` in an interactive Claude Code session or your Claude account's Usage settings before deciding to continue.
2. **The target is 200 questions, not 200 model calls.** Normally the run needs 201 calls: one opening call, calls that answer a question and ask the next one, and a final call that answers Question 200. Checkpoint pauses can add a question-only continuation call when you resume a run.
3. Agent A and Agent B are not independent model families. They are role-conditioned calls through whichever model alias you configure.
4. The script saves a response only after a complete successful CLI call. If you interrupt during a call, it may have consumed some usage without being saved; retrying may consume more.
5. No automatic retry is attempted after a failed or invalid model response. This is deliberate to avoid unnoticed repeated consumption.
6. The first version is text-only. It does not send images, audio, video, browse the web, or use external tools. Add multimodal evidence only after the text-only pilot works.

## 1. Open the project inside WSL

Run the following in your WSL terminal. Replace the path with the directory where you placed these files:

```bash
cd ~/ai-debate-claude-wsl
```

If you downloaded and unzipped the kit somewhere else, `cd` to that extracted directory instead. Keep the project on the WSL Linux filesystem (for example, under `~/`) rather than `/mnt/c/...` when possible.

Expected layout:

```text
ai-debate-claude-wsl/
├── main.py
├── config.json
├── prompts/
│   ├── agent_a.txt
│   ├── agent_b.txt
│   └── final_judge.txt
├── logs/
├── state/
├── archive/
└── tests/
    └── test_main.py
```

The `logs/`, `state/`, and `archive/` directories can be empty when you start. The script also creates them automatically.

## 2. Check Python and Claude CLI

Python 3.10+ is recommended. Confirm both programs are available in the **same WSL terminal**:

```bash
python3 --version
command -v claude
claude --version
claude auth status
```

Then run the included non-billable setup check:

```bash
python3 main.py --check
```

`--check` only checks the local CLI version and authentication status; it does not intentionally issue a model-generation request. If `claude auth status` shows an API key or third-party auth method, confirm which billing source is active before proceeding. A Pro subscription is not the same thing as unlimited API billing.

If the CLI is missing or too old for the flags used here, update it using the installation method you already use:

```bash
claude update
```

Then rerun `python3 main.py --check`.

## 3. Test the local controller without using Claude

Run the unit tests:

```bash
python3 -m unittest discover -s tests -v
```

These tests check stage routing, the question-count state machine, and response validation. They do not call Claude.

## 4. Run a tiny model smoke test

This **does use your model allowance**. First run only two questions:

```bash
python3 main.py --questions 2
```

The controller asks Agent A to open, then alternates agents and ensures both questions receive answers. Read the transcript:

```bash
less logs/transcript.md
```

Structured records are in:

```bash
less logs/transcript.jsonl
```

The authoritative resumable state is `state/run_state.json`. Do not manually edit it while a run is in progress.

## 5. Recommended staged experiment

Each target is the **cumulative total**, not the number of additional questions:

```bash
# First pilot (default is 5)
python3 main.py

# If the pilot is useful and usage looks acceptable, extend the same run to 20 total
python3 main.py --questions 20

# Later, extend to 50 total
python3 main.py --questions 50

# Full experiment, only after reviewing usage and transcript quality
python3 main.py --questions 200
```

When you extend a run, the script continues from the last saved state. It does not erase the earlier questions. If the run reaches a checkpoint, it waits for your choice. In a second WSL terminal, run `claude`, then enter `/usage`; return to this script and press Enter to continue or type `stop` to save and exit.

If the `--questions` target has already been completed, nothing new is generated. Use a larger target to continue. The controller refuses targets above `config.json`'s `max_questions` or below the number of questions already saved.

## 6. Understand the turn and question accounting

A normal uninterrupted run follows this pattern:

1. Agent A produces an opening position and Question 1.
2. Agent B answers Question 1 and asks Question 2.
3. Agent A answers Question 2 and asks Question 3.
4. This alternates until the target question is generated.
5. The other agent answers the final question without asking another question.

For a target of N questions, the final state should contain N questions, each with an `answer` and `answered_by`. Intermediate responses generally answer the previous question and ask the next. At a usage checkpoint, a response answers the checkpoint question without asking the next one; after you resume, the next agent creates the following question.

The stages are a guide rail, not a fixed script. The agents decide the specific questions while the controller assigns the stage based on the question number.

## 7. Configuration

Open `config.json` to change settings. Relevant fields:

- `model`: Claude Code model alias, initially `sonnet`. Use an alias supported by your installed CLI.
- `use_bare_mode`: adds `--bare` to each call when `true`. **Leave this `false` if you authenticate via `claude /login` (claude.ai account / Pro subscription)** — in `--bare` mode the CLI only reads `ANTHROPIC_API_KEY` or `apiKeyHelper`; it never reads OAuth or keychain credentials, and every call fails with `Not logged in · Please run /login`. Only set it to `true` if you intentionally run on `ANTHROPIC_API_KEY` billing.
- `max_questions`: hard project limit, currently 200.
- `default_target_questions`: default run size, intentionally 5.
- `checkpoint_every`: manual usage check every 10 answered questions.
- `cli_timeout_seconds`: maximum time for one CLI call.
- `max_budget_usd_per_call`: passed to the CLI as `--max-budget-usd`; the CLI aborts a single call if its API cost would exceed this. Set to `null` to disable. This is a per-call cap, not a run-wide budget; it does not replace the manual `/usage` checkpoints.
- `recent_history_pairs`: number of recent question-and-answer pairs sent to each agent.
- `max_memory_items_per_category`: cap on stored shared memory notes in each category.
- `stages`: topic plan and question ranges.

The agents receive the most recent exchanges plus compact shared memory, not the entire growing transcript. The full transcript is still saved locally. This reduces repeated context and helps prevent the prompt from growing without limit, but it does not guarantee a particular Pro usage percentage.

## 8. Check usage

This script intentionally does **not** try to calculate Pro percentage from token counts. Model tokens, cached context, CLI metadata, and subscription usage bars are not interchangeable. Claude Code's `/usage` screen is the source to inspect while deciding whether to proceed. Usage limits can also depend on model choice, context size, and other Claude activity.

Do not leave a full 200-question run unattended. The controller pauses every 10 answered questions, but you can still reach a usage limit during a single model call. If preserving remaining allowance is important, stop after the 2- or 5-question pilot, inspect the transcript and actual usage change, and only then continue.

## 9. Recover from a stop or error

- **Manual checkpoint stop:** type `stop`; state and transcript are saved. Run the same larger target again to resume.
- **Ctrl+C between calls:** the last successful turn is saved. Run the same command again.
- **CLI/model error:** the script records the error in `logs/errors.log`, saves the last known state, and exits without advancing the question count. Read the error before retrying because the failed attempt might still have consumed usage.
- **Bad/invalid structured output:** the script fails closed rather than attempting automatic retries.
- **Start fresh:** `python3 main.py --reset --questions 5` archives existing state and transcript in a timestamped `archive/` directory before starting a new experiment.
- **Change the topic:** edit `config.json`. A saved run with a different topic is rejected so one transcript cannot accidentally mix topics. Use `--reset` for a new topic.

## 10. Generate an optional final evaluation

After finishing the debate, you can ask a separate Claude call to evaluate the Markdown transcript. This is an **additional model request** and sends the whole transcript as input, so it may be comparatively expensive for a 200-question run. Run it only if your remaining usage allows it:

```bash
cat logs/transcript.md | claude -p \
  --model sonnet \
  --max-turns 1 \
  --tools "" \
  --disallowedTools "mcp__*" \
  --system-prompt-file prompts/final_judge.txt \
  --output-format text \
  "Evaluate the supplied debate transcript according to your system instructions. Return the requested report. Do not use tools. This is analysis of the transcript, not a request to continue the debate." \
  | tee logs/final_evaluation.md
```

This report should evaluate quality, uncertainty, disagreements, and testable predictions. It should not treat consensus or confidence as proof.

## 11. Troubleshooting

### `claude: command not found`
Run the project from the WSL distribution where Claude Code is installed. Check `command -v claude`, and make sure that terminal can run `claude` interactively.

### Authentication errors
Run `claude auth status`. If necessary, sign in with `claude auth login`. Do not switch to `claude auth login --console` unless you intend to use Console/API billing.

### `Not logged in · Please run /login` even though `claude auth status` shows `loggedIn: true`
This means `config.json` has `use_bare_mode: true` while you authenticate via OAuth (claude.ai account / Pro subscription). `--bare` mode deliberately ignores OAuth and keychain credentials and only reads `ANTHROPIC_API_KEY`/`apiKeyHelper`. Set `use_bare_mode` to `false` in `config.json` and rerun.

### Unknown flag such as `--bare`, `--json-schema`, or `--no-session-persistence`
Run `claude --version`, update with `claude update`, and try the setup check again. This project uses modern Claude Code CLI flags for isolated scripted calls and structured output.

### No `structured_output` in the JSON response
Update Claude Code, then run the two-question smoke test again. The CLI response envelope may include diagnostic text on stderr; inspect `logs/errors.log` and rerun `claude --version`. The code deliberately avoids free-form parsing when it cannot validate a response.

### The agents sound repetitive or agree too quickly
Read `logs/transcript.md`, then adjust `prompts/agent_a.txt`, `prompts/agent_b.txt`, or the stage guidance in `config.json`. Change one variable at a time. Do not increase the number of questions merely to make a weak debate longer.

### Responses are too long
Lower the requested word range in both agent prompt files. The controller warns after an overlong response but keeps it as returned to avoid silently discarding a completed model call.

## 12. Design rationale

- **One call per turn:** simple and debuggable; no hidden multi-agent orchestration.
- **Python-owned question counter:** an agent cannot decide to continue after the limit.
- **Structured output:** separates the answer from the single next question and memory updates.
- **Bounded memory:** agents receive recent exchanges and capped notes, not an indefinitely growing transcript.
- **Save after every complete response:** interruption recovery is possible without resending prior exchanges.
- **Manual usage checkpoints:** the project does not claim access to a live subscription-usage API or promise the 200-question run will fit in a quota.
- **Tools disabled:** agents can debate without shell, file, browser, or MCP permissions.

## Official references

- Claude Code CLI reference: <https://code.claude.com/docs/en/cli-reference>
- Claude Code structured outputs: <https://code.claude.com/docs/en/agent-sdk/structured-outputs>
- Claude Code costs and `/usage`: <https://code.claude.com/docs/en/costs>
- Claude Code getting started: <https://code.claude.com/docs/en/getting-started>
