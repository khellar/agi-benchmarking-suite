# OmegaClaw integration

How `omegaeval` plugs into an OmegaClaw agent, what it can read, and what does not exist. Other agents connect through the connectors in the [guide](GUIDE.md#connect-any-other-agent). Everything below was checked against the OmegaClaw source ([singnet/Omega](https://github.com/singnet/Omega/tree/ee0618a)) at commit ee0618a, 25 September 2026.

## The three connections

| Connection | What OmegaClaw already has | What the harness does | Code change to the agent |
| --- | --- | --- | --- |
| The game | The websocket chat channel, `channels/wschat.py`, selected with `commchannel=websocket` | Serves that protocol, one endpoint per agent, token-authenticated | None. Set `WS_URL` and `WS_TOKEN`. |
| Cognition | Python logging of every loop step to stderr, `config/logging.conf` | Follows `docker logs -f -t <container>` or a log file, and parses each loop turn | None |
| Memory | `memory/history.metta` and the Chroma store at `PeTTa/chroma_db` | Reads sizes, hashes and row counts at the start and end of each episode | None. Read-only. |

## Websocket frames

The frames, as documented in `channels/wschat.py`:

```
harness -> agent   {"type": "user_message", "seq": 7, "text": "[omegaeval ep=3f9a1c1 t=12] ..."}
agent   -> harness {"type": "resume", "last_seen_seq": 6}            after every connect
agent   -> harness {"type": "agent_message", "client_seq": "...", "text": "ep=3f9a1c1 t=12 move N"}
harness -> agent   {"type": "ack", "seq": null, "client_seq": "..."}
```

The agent's reply is whatever its `send` skill sent. Its other skills, such as `remember`, `query`, `metta` and `shell`, never reach the game. They appear only in its log.

## Log lines the parser reads

Each line has the format `YYYY-MM-DD HH:MM:SS | LEVEL | logger | message`. `docker logs -t` adds a nanosecond UTC prefix.

| Line | Written by | Gives |
| --- | --- | --- |
| `(---------iteration K)` | src/loop.metta | start of loop turn K |
| `(HUMAN-MSG: <text>)` | src/loop.metta | the new inbound message this turn, carrying the step tag |
| `(CHARS_SENT: N "<prompt>")` | src/loop.metta | the full prompt: PROMPT, SKILLS, extensions, OUTPUT_FORMAT, SAVE_PERMANENT_FILES_DIR, LAST_SKILL_USE_RESULTS, HISTORY, TIME, then the input after `:-:-:-:` |
| `[LLM_USAGE] provider=… model=… finish_reason=… prompt_tokens=… cached_tokens=… completion_tokens=… reasoning_tokens=…` | providers/lib_llm_ext.py | one LLM call and its tokens. The OpenAI Responses path logs input and output tokens instead, which the parser maps. |
| `[LLM_RAW] provider=… model=… chars=… raw='…'` | providers/lib_llm_ext.py | the raw completion. DEBUG level only. |
| `(RESPONSE: ((skill "arg") …))` | src/loop.metta | the parsed skill calls |
| `(RESPONSE: (RESULTS: ((COMMAND_RETURN: (call result)) …)))` | src/loop.metta | the result of each call |
| `LLM returned an empty response` | providers/lib_llm_ext.py | the reasoning ate the output budget |

To capture raw completions, start the agent with a logging config that sets `lib_llm_ext` to DEBUG. `scripts/omega` takes it with `-l <file>`.

## Field audit

Every field the console shows, where it comes from, and whether OmegaClaw provides it.

| Field | Source | Status |
| --- | --- | --- |
| Observation | gateway and environment adapter; `HUMAN-MSG` line shows the turn that read it | live |
| Action | `send` call in the log, and the reply the environment accepted | live |
| Receipt | environment adapter `step()` | live |
| Context in, by section | `CHARS_SENT` line | live |
| Full prompt text | raw log line, by line number | live in the local harness; exports keep an excerpt |
| LLM call, model, finish reason | `[LLM_USAGE]` | live |
| Prompt, cached, completion and reasoning tokens | `[LLM_USAGE]` | live |
| Raw completion | `[LLM_RAW]` | live at DEBUG level only |
| Reasoning text of a reasoning model | none | not in OmegaClaw. Providers return a token count only. OpenRouter is called with `exclude: true`, and the Anthropic and OpenAI paths return no reasoning text. |
| NAL inference | `metta "(\|- …)"` calls: premises from the call, conclusions with `(stv f c)` from the result | live |
| PLN inference | `metta "(\|~ …)"` calls, same | live |
| Other MeTTa | `metta` calls without `\|-` or `\|~`; `add-atom` or `remove-atom` inside is flagged as an AtomSpace write | live |
| Persistent AtomSpace, concepts formed | none | not in OmegaClaw. Each `(metta (\|- …))` call starts with a fresh AtomSpace (docs/reference-internals-memory-store.md). |
| Concepts reported over the gateway | a trace frame the agent sends | not sent by OmegaClaw |
| Chroma write | `remember` call, result `REMEMBER-SUCCESS` | live |
| Chroma read | `query` call; the result is a list of `[time, content]` pairs | live |
| Memory amended or deleted | none | not in OmegaClaw. No skill amends or deletes a memory. `remember` adds a new item with a fresh uuid every time. |
| Working memory | `pin` call; the text reaches the prompt next turn through HISTORY | live |
| Episodic history read | `episodes` call | live |
| Tool use | `shell`, file skills, `websearch`, plugin skills | live |
| Tool rules | every skill call, checked against the benchmark's forbidden or allowed skills | live when a benchmark sets tool rules |
| Errors | `ALERT_FAILED`, `UNKNOWN_SKILL_CALL`, `MULTI_COMMAND_FAILURE`, ERROR lines | live |
| Belief p(success) | the agent writes `p=<0..1>` in its reply | live only with `predictions = true` in omegaeval.toml |
| Surprise | derived from the change in stated p(success) | not in OmegaClaw. Derived when predictions are on. |
| World-model probe | none | not in OmegaClaw |
| Agent version | the `Omega version=…` message OmegaClaw sends when it starts | live |
| Memory fingerprint | history.metta size and sha256; Chroma `embeddings` rows and memories with a `time` other than `knowledge_prior` | live when `memory` is configured |
| Evidence grade | docker logs, held by the daemon, or a log file the agent's own process can write | recorded in every manifest |

## Ablation knobs

Ablation in OmegaClaw means running the same agent again with one configuration value changed, under a `--variant` label.

| Knob | How to set it | Effect |
| --- | --- | --- |
| Model | `-m` on scripts/omega, or `model=` | a different LLM |
| Provider | `-p`, or `provider=` | a different provider path |
| Reasoning effort | `reasoningMode=low\|medium\|high` | reasoning budget |
| Output budget | `maxOutputToken=` | reasoning and answer share this budget |
| History in the prompt | `maxHistory=` in characters | how much of history.metta the agent sees |
| Skill results in the prompt | `maxFeedback=` in characters | how much of the previous turn's results it sees |
| Knowledge priors | `IMPORT_KB_ON_START=0\|1` | preloaded Markdown in Chroma |
| Memory carried or fresh | `--memory fresh` after `scripts/omega clean` | whether earlier episodes are remembered |

Parameters go on the agent's command line as `name=value`. `scripts/omega` passes only its own fixed set, so for the others either append `name=value` after the image name in a direct `docker run`, or mount a `config.yaml` and pass `config=<path>`. The container scrubs `OMEGA_*` environment variables, so they do not reach the agent.

Not switchable by configuration today:

- **NAL and PLN.** The `metta` skill is always offered to the agent.
- **Chroma memory.** `maxRecallItems = 0` makes `query` raise an error in petta_lib_chromadb, so it is not a clean off-switch.
- **Concept formation.** It does not exist.

## Skills and tool rules

At commit ee0618a OmegaClaw offers these skills: `remember`, `query`, `episodes`, `pin`, `shell`, `read-file`, `write-file`, `write-file-b64`, `append-file`, `get-io-policy`, `delete-file`, `send`, `websearch`, `version` and `metta`. Plugins can add more.

For a closed-book exam, `websearch` is not the only way out. `shell` reaches the web, and `read-file` can reach the dataset when it sits inside the agent's allowed paths. An allowed list is the safer rule, because it also catches skills a plugin adds:

```toml
[environments.hle]
adapter = "omegaeval.envs.qa:QADataset"

[environments.hle.options]
path = "/data/hle.jsonl"
allow_tools = ["send", "remember", "query", "pin", "episodes", "metta"]
```

## Behaviours that matter for benchmarking

- **Repeated messages are ignored.** OmegaClaw treats a message identical to the previous one as no new input (`&prevmsg` in src/loop.metta). Every observation therefore carries a unique `[omegaeval ep=… t=…]` tag.
- **Repeated replies are dropped.** The `send` skill drops a message identical to the previous one (`&lastsend` in src/channels.metta). A run of question episodes answered "B" each time would lose every repeat, so replies start with the episode tag and step, `ep=3f9a1c1 t=0 B`.
- **Messages that arrive together are merged.** The websocket channel joins pending inbound messages with ` | `. The harness sends one observation and waits for the action, and waits for the log to show each information-only message was read before sending the next.
- **Frames over 64 KiB break the connection.** OmegaClaw's websocket client sets `max_size` to 64 KiB. The harness splits longer messages into tagged parts.
- **Log files are writable by the agent.** A file log is written by the agent's own process, so its file skills could write it too. Docker logs are held by the daemon. The manifest records the evidence grade of every run.
- **The agent can go idle.** After `maxNewInputLoops` turns, 50 by default, without new input, the agent stops calling the LLM until its wake-up interval, 600 s by default. The harness's step timeout ends such an episode as `agent_timeout`.
- **Results reach the prompt for one turn only.** Skill results reach the prompt once, through LAST_SKILL_USE_RESULTS. A query result the agent does not act on right away is gone the next turn.
- **Memory carries over.** HISTORY and Chroma persist between episodes and from other conversations. The manifest records the memory mode and fingerprints.
- **An agent has one channel.** While it points at the harness, it is not on Telegram, IRC or Slack.
- **Container names clash.** `scripts/omega` always names its container `omega`. Several agents on one host need a custom `docker run` with distinct names.
- **Clocks are coarse.** Log timestamps have one-second resolution. `docker logs -t` adds precise UTC times. The step tags align loop turns to steps exactly either way.
- **The log format may change.** Pull request [singnet/Omega#358](https://github.com/singnet/Omega/pull/358) moves the LLM to the tools API and will change the RESPONSE lines. Re-run `pytest` when it lands. `PARSER_VERSION` in `omegaeval/omega_trace.py` names the commit the parser was validated against.
