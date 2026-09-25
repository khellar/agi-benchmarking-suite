# Guide

How to install the harness, connect agents, run benchmarks, read the console, grade answers and publish results. The [reference](REFERENCE.md) has every command and key; [adding benchmarks](BENCHMARKS.md) covers new benchmarks.

- [Install](#install)
- [Try it without an agent](#try-it-without-an-agent)
- [Set up](#set-up)
- [Connect an OmegaClaw agent](#connect-an-omegaclaw-agent)
- [Connect any other agent](#connect-any-other-agent)
- [Run episodes](#run-episodes)
- [Hives](#hives)
- [Read the console](#read-the-console)
- [Judge answers](#judge-answers)
- [Compare and ablate](#compare-and-ablate)
- [Publish results](#publish-results)
- [Several machines](#several-machines)
- [The registry](#the-registry)
- [Troubleshooting](#troubleshooting)

## Install

Python 3.11 or later. Install from GitHub:

```bash
pip install git+https://github.com/khellar/agi-benchmarking-suite
```

Or clone it to change it, run the tests, or add benchmarks next to it:

```bash
git clone https://github.com/khellar/agi-benchmarking-suite
cd agi-benchmarking-suite
pip install -e ".[test]"
pytest
```

## Try it without an agent

Two reference agents ship with the harness: a random policy, the floor, and an oracle that knows the answers, the ceiling. They need no model and no server.

```bash
omegaeval baseline random keydoor --seeds 1-5
omegaeval baseline oracle keydoor --seeds 1-5
omegaeval export keydoor --html keydoor.html
```

Open `keydoor.html` in a browser: it is the console with those runs inside. To browse the store live instead, run `omegaeval serve` and open http://127.0.0.1:8780. The [live demo](https://khellar.github.io/agi-benchmarking-suite/) is built the same way from reference runs on every push.

Three samples are built in to exercise the harness: `keydoor`, a maze with a key and a locked door; `qa-sample`, arithmetic and multiple-choice questions; and `memory-sample`, a short chat history followed by a question.

## Set up

```bash
omegaeval init
omegaeval doctor
```

`init` writes `omegaeval.toml` with one agent block and a fresh token. Keep the file private: a token lets a process act as that agent. To keep secrets out of the file, as in CI or a container, write a token as `"${OMEGA_TOKEN}"` and it is read from that environment variable. `doctor` checks the config, the store, the gateway port, every agent's log and memory source, and every installed benchmark, and flags config keys the harness does not read.

Keep the store on a local disk. A folder synced by iCloud, Dropbox or Google Drive can split the ledger when two machines write to it; see [several machines](#several-machines) for sharing runs.

## Connect an OmegaClaw agent

OmegaClaw needs no code change. The harness speaks its websocket chat channel, follows its log, and reads its memory files without writing to them.

```bash
omegaeval serve
omegaeval connect oma-1
```

`serve` starts the gateway on port 8770 and the console on port 8780. `connect` prints the command that starts the agent pointed at the harness, for Docker with the launcher in the Omega repository and for a standalone install:

```
env WS_URL=ws://host.docker.internal:8770/agents/oma-1 WS_TOKEN=<token> ./scripts/omega start -t websocket -p <provider> -d <image>
sh run.sh run.metta commchannel=websocket WS_URL=ws://localhost:8770/agents/oma-1 WS_TOKEN=<token> 2>> omega.log
```

Fill in the provider and image, for example `-p Anthropic -d singularitynet/omega:v0.1.19`.

Each agent block says where its evidence lives:

```toml
[agents.oma-1]
token = "a-long-random-token"
label = "Omega v0.1.19, Anthropic, reasoning medium"
logs = { docker = "omega" }
memory = { docker = "omega" }
config = { provider = "Anthropic", reasoningMode = "medium" }

[agents.oma-2]
token = "another-long-random-token"
logs = { file = "/srv/agents/oma-2/omega.log" }
memory = { home = "/srv/agents/oma-2/PeTTa" }
```

- **Docker logs** are held by the Docker daemon, so the agent cannot rewrite them. A log file can be written by the agent's own process. Every manifest records which kind each run used.
- **A remote Docker host** works with `host = "ssh://user@box"` in either table.
- **One channel per agent.** While an agent points at the harness, it is not on Telegram, IRC or Slack.
- **Container names.** `scripts/omega` always names its container `omega`, so several agents on one host need their own `docker run` with distinct names.
- **Other agents** join by speaking the same four websocket frames; give them `kind = "other"`. The frames are in the [reference](REFERENCE.md#websocket-protocol).

The console's Runs view shows which agents are connected and the version each announced. A running harness notices new agent blocks in `omegaeval.toml` without a restart.

## Connect any other agent

Any agent can play: a raw model, an agent built with a framework, a script, or a service. The harness reaches it one of three ways, set in its agent block:

```toml
[agents.claude]
label = "Claude Opus 5.5, no tools"
connect = { python = "claude:Claude", cwd = "examples/agents" }

[agents.my-service]
connect = { http = "http://127.0.0.1:9100/" }

[agents.my-script]
connect = { command = ["python3", "my_agent.py"] }
```

| Transport | How it works | Good for |
| --- | --- | --- |
| `python` | the harness loads the class and calls `act(message)` for every message | agents written in Python, framework agents |
| `http` | the harness POSTs every message as JSON and reads the answer from the response | services in any language |
| `command` | the harness starts the command for each episode, writes one JSON line per message, and reads one JSON line back | scripts and command-line agents |

A command runs in, and a python module is found in, the folder that holds `omegaeval.toml`, unless `cwd` names another. The harness checks that it can reach each agent before it claims any attempt, so a typo fails at once with the reason; `omegaeval doctor` runs the same check.

- **No tags to repeat.** Each message arrives as JSON with its episode, step, text and offered actions, and the reply is bound to its step. The [reference](REFERENCE.md#agent-protocol) has the fields.
- **Information messages** are answered with `{}` once read, which tells the harness to send the next one.
- **Cognition lanes.** An agent fills the console's lanes by returning trace events with its reply: LLM calls with their tokens, tool calls, memory writes, reasoning steps and errors. Tokens also give cost, and tool calls are checked against a benchmark's tool rules.
- **Evidence grade.** What such an agent reports about itself is recorded as reported by the agent, which is weaker evidence than a Docker log.
- **Isolation.** A command agent runs in its own process group. At the end of each episode its input is closed and, if it lingers, it is stopped with every process it started. A python agent runs inside the harness and cannot be stopped if its `act` never returns, so prefer `command` for agents you do not trust to finish.

Two examples ship in `examples/agents`. `notebook.py` speaks all three transports and is the template to copy. `claude.py` is a model-only baseline: Claude through the Anthropic API, reading `ANTHROPIC_API_KEY` from the environment, to see what an architecture like OmegaClaw adds over the model it runs on.

To play episodes without starting a server, for example in a CI job, add `--local`:

```bash
omegaeval run claude qa-sample --seeds 1-10 --local
```

## Run episodes

Queue episodes from the console's Runs view, or from the command line while `serve` is running:

```bash
omegaeval run oma-1 keydoor --seeds 1-20
omegaeval run oma-1 hle --task Math --seeds 1-100 --variant reasoningMode=low
omegaeval status
```

- **Seeds** pick the episode. For a question set, seed 1 is the first question of the task.
- **Tasks** are the benchmark's difficulty levels or categories. Without `--task`, the benchmark's first task is used.
- **Variants** label the one thing you changed, so Compare can put the variants side by side.
- **Memory** carries over between episodes. `--memory fresh` records that you reset the agent's memory first; the harness does not reset it for you.
- **Budgets** are the benchmark's step limit plus `step_timeout_s`, `wall_s` and `max_llm_calls` in `[harness.episodes]`. Running out ends the episode with a typed status such as `agent_timeout`.

Each subject plays one episode at a time, and different agents play in parallel. The agent receives the benchmark's brief and first observation as a chat message, and replies with its `send` skill. This is the first message of a keydoor episode:

```
[omegaeval ep=42f7992 t=0] Benchmark episode started: Key and locked exit (reference gridworld) (task classic, seed 3).
You are in a maze. Find the key, pick it up, then reach the exit and open it. You only see the walls around your own cell. Coordinates are (row, column); N is row minus one.
How to act: reply with your send skill, one message per step, starting with the episode tag and step number exactly as shown in the Reply line.
Actions: move N | move E | move S | move W | pick | open
Observation: Last action: episode start. You are at (1,1). Walls: N, S, W. Nearby: exit to the E. Holding the key: no. Steps left: 212.
Reply: ep=42f7992 t=0 <action>
```

## Hives

A hive is several agents playing one episode together:

```toml
[hives.alpha]
members = ["oma-1", "oma-2", "oma-3"]
actor = "oma-1"
relay = true
```

Every member receives every observation. Only the actor's replies move the benchmark. The others get an `Advise:` line, and with `relay` on, their messages go to the whole hive. Each member's log is followed separately, and the console shows one strip per member. Run a hive exactly like an agent:

```bash
omegaeval run alpha keydoor --seeds 1-5
```

## Read the console

The bar at the top scopes everything:

- **Benchmark** picks one benchmark by its label and name, as set in `omegaeval.toml`, with its run count.
- **Find** narrows the runs by agent, variant, seed or run id.
- **Run** picks one attempt. With many runs it lists the 500 most recent that match, and says how many there are.
- **Same seed** stacks another run of the same benchmark, task and seed underneath, step by step.

The views:

- **Play** shows one episode. The world panel draws what the agent has seen: a map, a question card, a conversation timeline, or the transcript. Beside it, a strip has one column per loop turn and one lane per phase: observation, context sent to the LLM, the LLM call, NAL and PLN inference, belief, action, errors, memory, atoms, surprise and concepts. Click a column, or use the arrow keys, to see the exact message, prompt sections, tokens, skill calls with their results, and the benchmark's receipt.
- **Compare** puts two sets of runs on the same benchmark and task side by side, metric by metric in natural units, with a mark per seed. Ablation variants follow.
- **Progress** places every set of runs between the random floor and the oracle ceiling, and lists results by the version each agent announced.
- **Runs** lists attempts, starts episodes, and shows connected agents and installed benchmarks.
- **Sources** says where every field comes from and whether the selected run has it. A field OmegaClaw does not provide is greyed out with the reason, never shown as zero.

## Judge answers

Some answers only a judge can grade, and benchmarks such as Humanity's Last Exam are graded by a judge model. Those answers are held out of Solved until a verdict arrives:

```bash
omegaeval judge
omegaeval judge hle-3f9a1c1b2c3d/1 --verdict correct --by "reviewer-1"
omegaeval judge hle --command "python my_judge.py" --by "o3-mini, HLE judge prompt"
```

The first lists the held answers with their gold answers. The second records a person's verdict. The third runs a judge command once per held answer: it reads the case as JSON and prints a verdict, as the [reference](REFERENCE.md#judge-command-protocol) describes. `my_judge.py` stands for your own script, for example one that sends HLE's published judge prompt to a model.

- **Evidence.** Every verdict is appended to the attempt's `judgments.jsonl` with who gave it, and the result names the judge.
- **Blind.** The judge sees the question, the gold answer and the answer, never which agent gave it.
- **Latest counts.** A second verdict replaces the first in the result, and the record says when verdicts disagree.
- **Frozen releases.** An attempt in a release cannot be judged again.

## Compare and ablate

Run the same agent on the same seeds with one setting changed, and give each run a variant label:

```bash
omegaeval run oma-1 hle --seeds 1-200 --variant reasoningMode=low
omegaeval run oma-1 hle --seeds 1-200 --variant reasoningMode=high
```

Compare lines the sets up on the seeds both ran. Each metric appears in its own units, for example solved 64 of 200 against 71 of 200, with a mark per seed for better, worse or the same. The settings OmegaClaw allows changing are listed in [OMEGACLAW.md](OMEGACLAW.md#ablation-knobs).

## Publish results

```bash
omegaeval replay hle
omegaeval verify
omegaeval release hle-paper-v1 hle status=finished --note "HLE text-only, oma-1 and baselines"
omegaeval verify hle-paper-v1
omegaeval export hle subject=oma-1 --html hle-oma-1.html --redact-prompts
```

- **`replay`** re-runs each benchmark with the recorded actions and checks every receipt. It proves the benchmark is deterministic and the record complete.
- **`verify`** checks every file against its checksums.
- **`release`** freezes a set of finished attempts with their checksums. A release is never overwritten, and its attempts are never judged or rebuilt again.
- **`export --html`** writes the console as one page with the runs inside. `--redact-prompts` drops prompt text, which can carry an agent's history from other chats.

## Several machines

Give each machine its own store, and combine them when you want one history:

```bash
omegaeval merge /mnt/shared/gpu-box-store /mnt/shared/laptop-store
```

`merge` copies finished attempts that are not already there. An attempt whose number is taken by a different episode gets the next free number, and its manifest records where it came from. Nothing already in the store changes, so merging again is safe. Releases stay with the store that made them.

## The registry

The [registry](https://khellar.github.io/agi-benchmarking-suite/registry.html) on the project site lists benchmarks others have registered, with the config block that loads each one:

```bash
omegaeval add longmemeval-s
```

To share results, freeze them in a release, pack it with `omegaeval bundle`, host the file, and register it. [The registry guide](REGISTRY.md) walks through both.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| The agent shows as offline | wrong URL or token, or the agent is on another channel | print the command again with `omegaeval connect <agent>`; check `WS_URL` from inside the container |
| An http, command or python agent never answers | the server is not running, the command fails, or `act` raises | `omegaeval doctor` checks each one; the error is recorded in the run's trace |
| Episodes end as `agent_timeout` | the agent went idle after many turns without new input, or never connected | check the agent's log; raise `step_timeout_s` for slow models |
| No cognition lanes for an agent | no `logs` source, or the container name is wrong | `omegaeval doctor` shows each source |
| A benchmark is marked broken | a missing data file, a bad option or a bad id | `omegaeval conform <id>` names the failing check |
| `doctor` warns about a config key | a typo, such as `labl` for `label` | fix the key; the reference lists every valid one |
| `doctor` warns about the store location | the store is in a synced folder | move it to a local disk and merge stores instead |
| Answers stay held for a judge | the benchmark uses `judge_all` or answers did not match | `omegaeval judge` lists them |
| The console is slow on a huge store | many runs in one view | pick a benchmark and use Find; run `omegaeval reindex` if the index was deleted |
