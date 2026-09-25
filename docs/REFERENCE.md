# Reference

Every command, configuration key, file and protocol the harness has. Sections marked generated come straight from the code by `python -m omegaeval.docgen`, and the test suite fails when they drift. For how to use them, start with the [guide](GUIDE.md).

- [Commands](#commands)
- [Selecting runs](#selecting-runs)
- [Configuration file](#configuration-file)
- [Benchmark adapter](#benchmark-adapter)
- [Result Record](#result-record)
- [The store](#the-store)
- [HTTP API](#http-api)
- [Agent protocol](#agent-protocol)
- [Websocket protocol](#websocket-protocol)
- [Judge command protocol](#judge-command-protocol)
- [Registry entries](#registry-entries)

## Commands

Every command that reads the configuration takes `-c` for its path, `omegaeval.toml` by default. A benchmark can be named by its id or by its label in any case, so `hle` and `HLE` both work.

<!-- generated:cli -->
### omegaeval init

Write an omegaeval.toml template.

```
omegaeval init [--dir DIR]
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `--dir` | Folder to write it in. | `.` |

### omegaeval connect

Print the command that points an agent at the harness.

```
omegaeval connect agent [--config CONFIG]
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `agent` | An agent id from omegaeval.toml. |  |
| `-c`, `--config` | The config file. | `omegaeval.toml` |

### omegaeval serve

Run the gateway, episode queue and console.

```
omegaeval serve [--config CONFIG] [--no-baselines]
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `-c`, `--config` | The config file. | `omegaeval.toml` |
| `--no-baselines` | Do not start the random and oracle reference agents. |  |

### omegaeval run

Queue episodes for an agent or hive on a running harness.

```
omegaeval run agent-or-hive benchmark [--config CONFIG] [--task TASK] [--seeds SEEDS] [--variant VARIANT] [--memory carry|fresh] [--note NOTE] [--local]
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `agent-or-hive` | An agent or hive id from omegaeval.toml. |  |
| `benchmark` | A benchmark id, or its label. |  |
| `-c`, `--config` | The config file. | `omegaeval.toml` |
| `--task` | Default: the benchmark's first task. |  |
| `--seeds` | Seeds to play, e.g. 1-5 or 1,3,7. | `1` |
| `--variant` | Free text naming the one thing changed, e.g. reasoningMode=low. |  |
| `--memory` | Keep the agent's memory from earlier episodes, or record that it was reset. One of: carry, fresh. | `carry` |
| `--note` | Free text kept in the manifest. |  |
| `--local` | Play the episodes in this process and wait for them, instead of queuing them on a harness. |  |

### omegaeval baseline

Run a reference policy without a server.

```
omegaeval baseline random|oracle benchmark [--config CONFIG] [--task TASK] [--seeds SEEDS] [--note NOTE]
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `policy` | Random is the floor, oracle the ceiling. One of: random, oracle. |  |
| `benchmark` | A benchmark id, or its label. |  |
| `-c`, `--config` | The config file. | `omegaeval.toml` |
| `--task` | Default: the benchmark's first task. |  |
| `--seeds` | Seeds to play, e.g. 1-5 or 1,3,7. | `1` |
| `--note` | Free text kept in the manifest. |  |

### omegaeval status

Agents, connections and queue.

```
omegaeval status [--config CONFIG]
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `-c`, `--config` | The config file. | `omegaeval.toml` |

### omegaeval import-log

Turn an Omega log file into a console bundle.

```
omegaeval import-log log-file [--agent AGENT] [--out OUT]
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `log-file` | An OmegaClaw log file. |  |
| `--agent` | The agent name to show. | `imported` |
| `--out` | Where to write the bundle; default <log-file>.bundle.json. |  |

### omegaeval export

Write attempts to a JSON file, or into a self-contained console page.

```
omegaeval export [attempts ...] [--config CONFIG] [--max MAX] [--out OUT] [--html HTML] [--redact-prompts]
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `attempts` | Attempts: run_id/attempt, a run id or its start, a benchmark id, or key=value filters such as subject=oma-1; default all. |  |
| `-c`, `--config` | The config file. | `omegaeval.toml` |
| `--max` | Refuse to export more attempts than this. | `1000` |
| `--out` | A JSON file the console can open. |  |
| `--html` | A self-contained console page with the runs embedded. |  |
| `--redact-prompts` | Drop prompt text and raw completions. |  |

### omegaeval rebuild

Re-parse raw logs with this parser version; evidence is untouched.

```
omegaeval rebuild [attempts ...] [--config CONFIG]
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `attempts` | Attempts: run_id/attempt, a run id or its start, a benchmark id, or key=value filters such as subject=oma-1; default all. |  |
| `-c`, `--config` | The config file. | `omegaeval.toml` |

### omegaeval replay

Re-run the environment with the recorded actions and check every receipt.

```
omegaeval replay [attempts ...] [--config CONFIG]
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `attempts` | Attempts: run_id/attempt, a run id or its start, a benchmark id, or key=value filters such as subject=oma-1; default all. |  |
| `-c`, `--config` | The config file. | `omegaeval.toml` |

### omegaeval conform

The checks a benchmark adapter must pass.

```
omegaeval conform benchmark [--config CONFIG] [--seeds SEEDS] [--task TASK]
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `benchmark` | A benchmark id, or its label. |  |
| `-c`, `--config` | The config file. | `omegaeval.toml` |
| `--seeds` | Seeds to check. | `1-3` |
| `--task` | Default: the benchmark's first task. |  |

### omegaeval new-benchmark

Write a working adapter template to start from.

```
omegaeval new-benchmark name [--config CONFIG] [--kind interactive|qa|memory] [--dir DIR]
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `name` | The benchmark id: lowercase letters, digits, - or _. |  |
| `-c`, `--config` | The config file. | `omegaeval.toml` |
| `--kind` | The benchmark shape. One of: interactive, qa, memory. | `interactive` |
| `--dir` | Folder to write the file in. | `benchmarks` |

### omegaeval doctor

Check the config, agents, log sources, memory paths and benchmarks.

```
omegaeval doctor [--config CONFIG]
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `-c`, `--config` | The config file. | `omegaeval.toml` |

### omegaeval benchmarks

List installed benchmarks and where each comes from.

```
omegaeval benchmarks [--config CONFIG]
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `-c`, `--config` | The config file. | `omegaeval.toml` |

### omegaeval judge

List answers held for a judge, or record a verdict from a person or a command.

```
omegaeval judge [attempts ...] [--config CONFIG] [--verdict correct|wrong] [--command COMMAND] [--by BY] [--reason REASON] [--timeout TIMEOUT]
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `attempts` | Attempts: run_id/attempt, a run id or its start, a benchmark id, or key=value filters such as subject=oma-1; default every held answer. |  |
| `-c`, `--config` | The config file. | `omegaeval.toml` |
| `--verdict` | A person's verdict on the named attempts. One of: correct, wrong. |  |
| `--command` | An external judge: reads the case as JSON on stdin, prints a JSON verdict. |  |
| `--by` | Who judged: a person, or the model and prompt behind the command. |  |
| `--reason` | Why, kept with the verdict. |  |
| `--timeout` | Seconds the judge command may take per answer. | `300` |

### omegaeval verify

Check every file against its checksums, or the named releases.

```
omegaeval verify [release ...] [--config CONFIG]
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `release` | Release names; default every attempt and release. |  |
| `-c`, `--config` | The config file. | `omegaeval.toml` |

### omegaeval merge

Copy the finished attempts of other stores into this one.

```
omegaeval merge store [store ...] [--config CONFIG]
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `store` | Paths of other omegaeval stores. |  |
| `-c`, `--config` | The config file. | `omegaeval.toml` |

### omegaeval reindex

Rebuild the query index from the ledger and result files.

```
omegaeval reindex [--config CONFIG]
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `-c`, `--config` | The config file. | `omegaeval.toml` |

### omegaeval bundle

Pack a release into one file anyone can verify, for the registry.

```
omegaeval bundle name [--config CONFIG] [--out OUT] [--withhold-logs]
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `name` | The release name. |  |
| `-c`, `--config` | The config file. | `omegaeval.toml` |
| `--out` | The file to write; default <name>.tar.gz. |  |
| `--withhold-logs` | Leave out raw agent logs, whose prompts can carry memory from other conversations. |  |

### omegaeval add

Add a registered benchmark's config block to omegaeval.toml.

```
omegaeval add benchmark [--config CONFIG] [--registry REGISTRY]
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `benchmark` | The benchmark id or label in the registry. |  |
| `-c`, `--config` | The config file. | `omegaeval.toml` |
| `--registry` | A registry.json URL or file; default the project's site. |  |

### omegaeval release

Freeze a set of finished attempts with checksums, for citation.

```
omegaeval release name [attempts ...] [--config CONFIG] [--where WHERE] ... [--note NOTE]
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `name` | The release name; a release is never overwritten. |  |
| `attempts` | Attempts: run_id/attempt, a run id or its start, a benchmark id, or key=value filters such as subject=oma-1; default all. |  |
| `-c`, `--config` | The config file. | `omegaeval.toml` |
| `--where` | A key=value filter, e.g. env=hle; repeatable. |  |
| `--note` | Free text kept in the release. |  |
<!-- /generated:cli -->

## Selecting runs

`export`, `replay`, `rebuild`, `judge` and `release` take attempts in any mix of these forms:

| Form | Selects |
| --- | --- |
| `hle-3f9a1c1b2c3d/2` | one attempt |
| `hle-3f9a1c1b2c3d` | every attempt of that run |
| `hle-3f9a` | every attempt of every run whose id starts with that text |
| `hle` | every attempt of the benchmark with that id |
| `key=value` | only attempts whose field matches. Filters apply to every name given |

With no names, every attempt that matches the filters is selected.

<!-- generated:filters -->
Filter keys: `run_id`, `attempt`, `env`, `task`, `seed`, `subject`, `label`, `variant`, `memory_mode`, `status`, `terminal`, `success`, `needs_judge`, `judged`, `void`, `steps`, `started_at`, `ended_at`, `harness`, `agent_version`, `path`, `note`, `exact`. Values match exactly; `success`, `needs_judge`, `judged`, `void` and `exact` take true or false.
<!-- /generated:filters -->

```bash
omegaeval export hle subject=oma-1 --html hle-oma-1.html
omegaeval judge hle needs_judge=true
omegaeval release hle-paper-v1 hle status=finished
```

## Configuration file

`omegaeval.toml` lives where you run the commands, or wherever `-c` points. Relative paths in it resolve against the file itself. `omegaeval init` writes a commented template, and `omegaeval doctor` flags any key that is not in these tables.

<!-- generated:config -->
#### [harness]

| Key | Default | Meaning |
| --- | --- | --- |
| `store` | `"omegaeval-store"` | Folder where runs are kept. Relative paths resolve against this file. |
| `benchmark_dirs` | `["benchmarks"]` | Folders searched for benchmark .py files. |

#### [harness.gateway]

| Key | Default | Meaning |
| --- | --- | --- |
| `host` | `"0.0.0.0"` | Address the gateway listens on for agents. |
| `port` | `8770` | Port the gateway listens on. |
| `agent_url` | `"ws://host.docker.internal:8770"` | Address agents dial, as printed by omegaeval connect. |

#### [harness.console]

| Key | Default | Meaning |
| --- | --- | --- |
| `host` | `"127.0.0.1"` | Address the console listens on. |
| `port` | `8780` | Port the console listens on. |

#### [harness.episodes]

| Key | Default | Meaning |
| --- | --- | --- |
| `step_timeout_s` | `600` | Seconds an agent has to reply before the episode ends as agent_timeout. |
| `wall_s` | `10800` | Seconds an episode may run in total. |
| `grace_s` | `15` | Seconds to keep reading an agent's log after the episode ends. |
| `read_timeout_s` | `600` | Seconds to wait for the log to show an information-only message was read. |
| `pace_s` | `0.2` | Pause between information-only messages to agents with no log source. |
| `max_llm_calls` | `0` | End an episode when the agent's log shows more LLM calls. 0 means no limit. |
| `require_tick` | `true` | Accept only replies that name the current episode and step. |
| `predictions` | `false` | Ask the agent for p=<0..1> with each action. |

#### [agents.<id>]

| Key | Default | Meaning |
| --- | --- | --- |
| `connect` |  | How the harness reaches the agent: { http = "URL" }, { command = ["python", "agent.py"] } or { python = "module:Class" }. Without it, the agent dials the websocket gateway. cwd is the folder a command runs in and a python module is found in, this file's folder by default; options are keyword arguments for a python class. |
| `token` |  | The WS_TOKEN a websocket agent presents to the gateway; required for those. An http agent receives it as a bearer token. "${NAME}" reads it from the environment variable NAME. |
| `label` |  | Display name, recorded in every manifest. |
| `kind` | `"omega"` | omega for OmegaClaw, whose log and memory the harness can read, or other. |
| `logs` |  | Where its log is: { docker = "name" } or { file = "path" }, plus host = "ssh://...". |
| `memory` |  | Where its memory is: { docker = "name" } or { home = "PeTTa root" }, plus host. |
| `config` |  | Its configuration, recorded verbatim in every manifest. |

#### [hives.<id>]

| Key | Default | Meaning |
| --- | --- | --- |
| `members` | required | Agent ids in the hive. |
| `actor` | (the first member) | The member whose replies move the benchmark. |
| `relay` | `true` | Relay advisors' messages to the whole hive. |
| `label` |  | Display name. |

#### [environments.<id>]

| Key | Default | Meaning |
| --- | --- | --- |
| `adapter` |  | module:Class of the adapter. Optional when <id> is already installed. |
| `options` |  | Keyword arguments for the adapter, such as path, judge_all or allow_tools. |
| `label` |  | Short display name, such as HLE. Defaults to the id. |
| `name` |  | Full display name, such as Humanity's Last Exam. |

#### [prices."<model>"]

| Key | Default | Meaning |
| --- | --- | --- |
| `input` | `0.0` | USD per million uncached prompt tokens. |
| `cached_input` | (the input price) | USD per million cached prompt tokens. |
| `output` | `0.0` | USD per million completion tokens. |
<!-- /generated:config -->

A benchmark id is the key of its `[environments.<id>]` block, or the `id` of its adapter class. It must be 1 to 40 lowercase letters, digits, `-` or `_`, starting with a letter or digit, because it becomes part of every run id. Put display text in `label` and `name`.

## Benchmark adapter

A benchmark is a subclass of `omegaeval.envs.base.Environment`. [Adding benchmarks](BENCHMARKS.md) walks through writing one.

### Methods

| Method | Side | Returns |
| --- | --- | --- |
| `describe()` | public | a dict declaring the benchmark, below. Called often: keep it cheap |
| `reset(task, seed)` | public | the first `Observation`. The same task and seed must give the same episode |
| `parse_action(text)` | public | one canonical action or answer from the agent's free text, or `None` |
| `step(action)` | public | a `Receipt`. `action` is canonical, or `None` when the reply did not parse |
| `delivered()` | public | a `Receipt` for an observation that expected no reply, once the agent read it |
| `render()` | public | a frame of what the agent has been shown, or `None` for the transcript |
| `score()` | privileged | success, utility, terminal, steps, metrics. With `needs_judge`, success and utility are `None` |
| `floor()` | privileged | the utility of the declared trivial policy on this instance |
| `ceiling()` | privileged | `{utility, steps}` of an oracle on this instance, or `None` |
| `max_steps()` | privileged | the step budget for this instance |
| `solution()` | privileged | the oracle's replies, one per observation that expects one, or `None` |
| `fingerprint()` | privileged | what episodes depend on besides code and options, such as a data file's sha256, or `None`. Hashed into the run id |

### describe() keys

| Key | Required | Meaning |
| --- | --- | --- |
| `id` | yes | the benchmark id |
| `version` | yes | the adapter's version. Bump it when behaviour or scoring changes |
| `name` | yes | full display name |
| `kind` | yes | `interactive`, `qa` or `memory` |
| `tasks` | yes | task names, such as difficulty levels or categories |
| `observation` | yes | one line on what the agent reads |
| `actions` | yes | one line on what the agent replies |
| `utility` | yes | `{meaning, direction}`: what utility measures, and whether `higher` or `lower` is better |
| `default_task` | no | the task used when none is given. Default: the first task |
| `label` | no | short display name. The config's `label` wins |
| `brief` | no | the instructions sent at the start of every episode |
| `renderer` | no | the frame type `render()` returns |
| `oracle` | no | whether `solution()` exists |
| `floor_policy` | no | one line describing the trivial policy behind `floor()` |
| `scoring` | no | `deterministic`, `judge` or `mixed` |
| `tool_policy` | no | `{forbid: [skills], allow: [skills] or null, void_on_violation}` |

### Observation and Receipt

| `Observation` field | Meaning |
| --- | --- |
| `text` | what the agent reads |
| `actions` | the canonical actions offered now. Empty for a free-text answer |
| `public` | structured public state, kept in the record |
| `expects_action` | `False` for information that needs no reply |
| `free_text` | `True` when any non-empty reply is an answer |

| `Receipt` field | Meaning |
| --- | --- |
| `observation` | the next `Observation` |
| `valid` | whether the reply was an acceptable action or answer |
| `effect` | a short word for what happened, such as `moved`, `hint` or `answer recorded` |
| `done` | whether the episode ended |
| `info` | extra public facts about the step |

### Frames

`render()` returns one of these, and the console draws it beside the loop strip.

| Type | Fields | Drawn as |
| --- | --- | --- |
| `grid` | `rows` [str], `agent` [row, col], `legend` {char: label} | a map of what the agent has seen |
| `tiles` | `rows` [str], `legend` {char: label}, `colors` {char: css color}, `marks` [{r, c, label}] | a coloured tile map with marks |
| `pixels` | `rows` [[int]] or [hex str], `palette` [css color] | the frame itself, in the ARC palette unless one is given |
| `card` | `title`, `question`, `choices` [str], `answer` | the question and the agent's answer; the gold answer after the episode |
| `timeline` | `items` [{label, date, kind, delivered}], `current`, `question` | conversations delivered, then the question |

A frame shows only what the agent has been shown. Gold answers and evidence locations go in `score()["metrics"]`, which the console reveals after the episode ends.

### Built-in adapters

`omegaeval.envs.qa:QADataset` serves question sets, and `omegaeval.envs.chatmemory:ChatMemory` serves memory benchmarks. Their options go in the `options` table of an `[environments.<id>]` block.

| QADataset option | Default | Meaning |
| --- | --- | --- |
| `path` | | JSON or JSON Lines file of questions |
| `name` | | full display name |
| `sample` | `false` | use generated arithmetic questions instead of a file |
| `forbid_tools` | `[]` | skills the agent must not use |
| `allow_tools` | | the only skills the agent may use; `send` is always allowed |
| `void_on_violation` | `true` | void the episode when a tool rule is broken |
| `judge_on_mismatch` | `true` | hold a short answer that does not match for a judge, instead of marking it wrong |
| `judge_all` | `false` | hold every answer for a judge, keeping the deterministic grade as a pregrade |
| `skip_images` | `true` | skip questions that have an image |

| ChatMemory option | Default | Meaning |
| --- | --- | --- |
| `path` | | JSON or JSON Lines file of cases. JSON Lines is read one case per episode |
| `name` | | full display name |
| `sample` | `false` | use generated cases instead of a file |
| `forbid_tools` | `[]` | skills the agent must not use |
| `allow_tools` | | the only skills the agent may use; `send` is always allowed |
| `void_on_violation` | `true` | void the episode when a tool rule is broken |
| `judge_on_mismatch` | `true` | hold a reply without the gold answer for a judge, instead of marking it wrong |
| `judge_all` | `false` | hold every answer for a judge, keeping the containment check as a pregrade |

## Result Record

`result.json` in every attempt. A block the evidence cannot support is `{"available": false, "reason": ...}`, never a zero.

| Key | Contents |
| --- | --- |
| `schema`, `harness`, `parser` | format and versions that computed the record |
| `run_id`, `attempt` | which attempt |
| `outcome` | `success`, `utility`, `terminal`, `floor`, `ceiling`, `normalized`, `metrics`; `needs_judge`, `judged`, `void` or `not_graded` when they apply |
| `efficiency` | `steps`, `max_steps`, `invalid_actions`, `wall_seconds`, `agent_messages`, `accepted_actions`, `messages_delivered` |
| `regret` | `oracle_steps` and `extra_steps` for solved episodes |
| `prediction` | `n`, `brier`, `mean_p`, `final_p` when the agent stated predictions |
| `cognition` | per agent: iterations, LLM calls, tokens, models, skills, reasoning by engine, atoms, memory operations, errors |
| `tool_policy` | forbidden or allowed skills, violations found, whether the episode is void |
| `cost` | USD from tokens and the `[prices]` table |
| `world_model` | not available on OmegaClaw |
| `concepts` | counts of concepts an agent reported over the gateway |

## The store

```
<store>/store.json                       store id and format version
<store>/index.jsonl                      ledger: one line per event, never rewritten (the source of truth)
<store>/index.sqlite                     query index, rebuilt from the ledger when missing (safe to delete)
<store>/runs/<run_id>/<attempt>/
    manifest.json                        inputs and provenance
    env.jsonl                            every message sent and received (evidence)
    judgments.jsonl                      verdicts, when a judge graded the answer (evidence)
    agents/<agent>/omega.log.gz          raw log slice, compressed when the episode ends (evidence)
    agents/<agent>/trace.jsonl           parsed loop turns (derived)
    agents/<agent>/trace-meta.json       log source, line count and parser summary (derived)
    agents/<agent>/stderr.log            what a command agent wrote to standard error (evidence)
    result.json                          the Result Record (derived)
    checksums.json                       sha256 of every file above
<store>/releases/<name>/release.json     frozen attempts with checksums, for citation
<store>/releases/<name>/release.sha256   checksum of release.json
```

A run id is the benchmark id and 12 hex digits of the hash of the episode's inputs, such as `hle-3f9a1c1b2c3d`. The ledger records these events: `started`, `finished`, `judged`, `interrupted`, `replayed`, `rebuilt` and `merged`. [The data plan](DATA.md) explains how the pieces fit, what can be deleted, and how far it scales.

## HTTP API

`omegaeval serve` answers these on the console port. Responses are JSON, compressed with gzip when the client accepts it.

<!-- generated:api -->
| Method | Path | Returns |
| --- | --- | --- |
| GET | `/` | the console page |
| GET | `/api/state` | harness version and links, store, ledger_rev, run counts per benchmark, agents, hives, installed benchmarks with labels, the job queue |
| GET | `/api/runs` | attempts, newest first: {total, limit, offset, rows}. Query: any filter key, q=text, limit (default 500), offset |
| GET | `/api/results` | compact Result Records of finished attempts. Query: any filter key, variant_is=, limit |
| GET | `/api/progress` | solve counts per set of runs and per agent version. Query: any filter key |
| GET | `/api/runs/<run_id>/<attempt>` | everything about one attempt: manifest, result, messages, parsed traces |
| GET | `/api/runs/<run_id>/<attempt>/raw/<agent>/<line>` | one line of an agent's raw log |
| POST | `/api/episodes` | queue episodes. Body: {subject, env, task, seeds, variant, memory_mode, note} |
| POST | `/api/import-log` | parse an OmegaClaw log sent as the body into a console bundle. Query: agent= |
<!-- /generated:api -->

## Agent protocol

Agents reached over `http`, `command` or `python` receive every message as one JSON object:

| Field | Meaning |
| --- | --- |
| `protocol` | `omegaeval.agent/1` |
| `agent` | the agent's id |
| `episode` | the episode tag, the same for every message of an episode |
| `step` | the step this message belongs to |
| `kind` | `brief` for the first message, then `observation`, `info`, `end`, or `hive` for a message from another hive member |
| `role` | `actor`, or `advisor` in a hive |
| `text` | the observation, the information, the end notice or the relayed message |
| `instructions` | the benchmark's brief and tool rules, on the first message |
| `actions` | the actions offered now, empty for a free-text answer |
| `expects_reply` | whether an answer is expected |
| `free_text` | whether any text counts as the answer |
| `predictions` | whether to add a probability of solving, as `p` |
| `benchmark`, `task`, `seed` | which episode this is, on the first message |
| `actor` | for an advisor, the member who acts |
| `sender` | for a relayed message, the member who wrote it |

The answer is a JSON object. A plain string is taken as the reply, and `null` or `{}` as no reply:

```json
{"reply": "move N", "p": 0.6, "trace": [
  {"kind": "llm", "provider": "anthropic", "model": "claude-opus-5-5", "input_tokens": 1200, "output_tokens": 30},
  {"kind": "tool", "name": "websearch", "args": "saturn moons", "result": "..."},
  {"kind": "memory", "store": "vector", "op": "add", "text": "The door needs the red key."},
  {"kind": "reasoning", "engine": "pln", "premises": ["..."], "conclusions": [{"term": "...", "f": 0.9, "c": 0.8}]},
  {"kind": "context", "chars": 5400, "sections": {"system": 1200, "history": 4200}},
  {"kind": "error", "message": "..."}
]}
```

| Trace event | Fields | Fills |
| --- | --- | --- |
| `llm` | `model`, `provider`, `input_tokens`, `output_tokens`, `cached_tokens`, `reasoning_tokens`, `finish_reason` | the LLM lane, token counts and cost |
| `tool` | `name`, `args`, `result` | tool calls, checked against the benchmark's tool rules |
| `memory` | `store`, `op`, `text`, `result` | the memory lane |
| `reasoning` | `engine`, `premises`, `conclusions` | the reasoning and premises lanes |
| `context` | `chars`, `sections`, `input` | the context lane |
| `error` | `type`, `message` | errors |

A command agent may also write `{"type": "trace", "step": 3, "events": [...]}` lines between answers. Anything it writes to standard error is kept in the attempt as `stderr.log`.

## Websocket protocol

OmegaClaw, and any agent that prefers a chat channel, connects to the gateway at `ws://<host>:<port>/agents/<agent-id>` with `Authorization: Bearer <token>`. These are the frames OmegaClaw's `channels/wschat.py` speaks:

```
harness -> agent   {"type": "user_message", "seq": 7, "text": "[omegaeval ep=3f9a1c1 t=12] ..."}
agent   -> harness {"type": "resume", "last_seen_seq": 6}            after every connect
agent   -> harness {"type": "agent_message", "client_seq": "...", "text": "ep=3f9a1c1 t=12 move N"}
harness -> agent   {"type": "ack", "seq": null, "client_seq": "..."}
agent   -> harness {"type": "trace", "step": 12, "events": [...]}                     optional trace events
agent   -> harness {"type": "trace", "kind": "concepts", "step": 12, "concepts": [{"id": "key"}]}   optional
```

Every message to the agent starts with a tag, and every reply names the episode and step:

```
[omegaeval ep=3f9a1c1 t=12]            an observation that expects a reply
[omegaeval ep=3f9a1c1 t=12 info]       information to read, no reply expected
[omegaeval ep=3f9a1c1 t=12 part 1/3]   one part of a message split under the 64 KiB frame limit
[omegaeval ep=3f9a1c1 END]             the episode is over
ep=3f9a1c1 t=12 move N p=0.6           a reply; p only when predictions are on
```

## Judge command protocol

`omegaeval judge --command CMD` runs `CMD` once per held answer, with one case as JSON on standard input:

```json
{"benchmark": "hle", "benchmark_version": "0.2.0", "task": "all", "seed": 42,
 "run_id": "hle-3f9a1c1b2c3d", "attempt": 1, "question_id": "66f2b2a8", "question": "...",
 "gold": "B", "answer": "B. 53", "answer_type": "multipleChoice", "pregrade": "match",
 "grading": "every answer goes to a judge on this benchmark"}
```

It prints one verdict as JSON on standard output. Anything besides `verdict` and `reason` is kept as detail:

```json
{"verdict": "correct", "reason": "the answer names option B", "model": "o3-mini"}
```

`verdict` is `correct` or `wrong`; `yes`, `no`, `true` and `false` are accepted too. The case never says which agent answered.

## Registry entries

Registry entries are TOML files in `registry/benchmarks/` and `registry/results/`. [The registry guide](REGISTRY.md#entry-fields) lists their fields, and `python -m omegaeval.registry check` validates them. A bundle made by `omegaeval bundle` holds `bundle.json`, which names the release and any files withheld, then `store.json`, `releases/<name>/release.json` and `release.sha256`, and every file of the release's attempts under `runs/`.
