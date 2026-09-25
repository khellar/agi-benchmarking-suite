# Changelog

## 0.3.0

First public version, under the MIT License.

- **Any agent.** Besides OmegaClaw's websocket channel, agents connect over HTTP, as a command speaking JSON Lines, or as a Python class. They get structured messages with no tags to repeat, and report LLM calls, tool calls, memory, reasoning and errors as trace events that fill the console and the Result Record. `omegaeval run --local` plays episodes without a server. Examples include a model-only Claude baseline.
- **Harness faults are not scored.** An episode the harness failed or was stopped during is kept and counted, marked `not_graded`, and left out of Solved, instead of counting as the agent's failure.
- **Robust under failure.** An agent that hangs no longer holds up the queue after its episode ends, and a late answer cannot leak into the next episode. A command agent runs in its own process group and is stopped with everything it started. Stopping the harness with Ctrl-C or SIGTERM records the episode in progress as interrupted. An agent that fails to start, or whose code raises while closing, cannot leave an attempt unfinished. A torn ledger line from a crash can no longer swallow the next record, and ledger writes reach the disk before the harness moves on.
- **Checked before it runs.** `run --local`, the console's queue and `doctor` check that each agent can be reached before any attempt is claimed, so a typo fails at once with the reason. An agent's files are found next to `omegaeval.toml` by default. Tokens can be read from the environment as `"${NAME}"`. Changing an agent's settings takes effect without a restart.
- **Hardening.** The console refuses cross-site writes and DNS-rebinding requests. Bundles that unpack to more than four times their size limit are refused. The version has one source. CI tests Python 3.11 to 3.14, builds the wheel and installs it in a fresh environment, runs the source distribution's tests, lints with ruff, checks the browser scripts, and reports coverage. A security policy and contributor guide are included.
- **Human-chosen benchmark names.** A benchmark id such as `hle`, `arc-agi-3` or `freeciv` names it everywhere, including run ids such as `hle-3f9a1c1b2c3d`. `label` and `name` in `omegaeval.toml` set what the console shows. Commands accept the id or the label, and select runs by benchmark, run id prefix or `key=value` filters.
- **A query index for large stores.** A SQLite index derived from the ledger answers the console and the command line. The console loads one benchmark at a time, pages runs, and refreshes only when the ledger grows. At 20,000 attempts a refresh moves about 4 KB instead of 53 MB.
- **Merging stores.** `omegaeval merge` combines stores from several machines without losing or changing anything. `omegaeval reindex` rebuilds the index.
- **Compressed raw logs.** Agent logs are stored as `omega.log.gz`.
- **Large datasets.** Question and memory files are parsed once per process, and memory benchmarks in JSON Lines read one case per episode.
- **Judging.** `omegaeval judge` records verdicts from a person or a judge command as evidence, naming the judge. `judge_all` mirrors benchmarks graded by a judge model.
- **Tool allowlists, data identity and verification.** `allow_tools` limits the skills an agent may use. The data file's sha256 and the benchmark's options are hashed into the run id. `omegaeval verify` checks checksums, and released attempts are never changed.
- **Live configuration.** A running harness rereads `omegaeval.toml` when it changes, and finds new benchmark files without a restart. `omegaeval doctor` flags unknown config keys and stores in synced folders.
- **A registry and a project site.** GitHub Pages hosts a home page, the console demo, and a registry of benchmarks and results. Anyone registers through an issue form, and a workflow opens the pull request once the checks pass. `omegaeval add` loads a registered benchmark. `omegaeval bundle` packs a release into one verifiable file, and the site recomputes each registered release's numbers from it.
- **Documentation.** A guide, a benchmark guide, a generated reference, a data plan and a registry guide, all checked by the test suite.

## 0.2.0

Not published. Three benchmark shapes with built-in `QADataset` and `ChatMemory` adapters, benchmarks as plugins, integrity guards, and conformance with nine checks.

## 0.1.0

Not published. The gateway, the OmegaClaw log parser, reference agents, the append-only store and the console.
