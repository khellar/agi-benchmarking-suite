# Data plan

How the harness keeps its history: what is evidence, what is derived, how things are named, how far it scales, and how several machines share results. The layout itself is in the [reference](REFERENCE.md#the-store).

## Principles

- **Files are the evidence.** Every message sent and received, every raw log slice and every verdict is a plain file, sealed with sha256 checksums.
- **The ledger is the source of truth.** `index.jsonl` gets one line per event and is never rewritten. Everything else about an attempt's state can be rebuilt from it and the attempt's files.
- **Derived means disposable.** Parsed traces, Result Records and the query index are computed from the evidence. A better parser or a new metric means rebuilding them, never editing evidence.
- **Nothing is overwritten.** Running the same inputs again adds an attempt. Releases freeze attempts for citation, and frozen attempts are never changed again.

## Names

| Thing | Example | Where it comes from |
| --- | --- | --- |
| Benchmark id | `hle` | chosen by a person, in `omegaeval.toml` or the adapter class |
| Label and name | `HLE`, `Humanity's Last Exam` | display only; can change at any time |
| Run id | `hle-3f9a1c1b2c3d` | the benchmark id, then 12 hex digits of the hash of the episode's inputs |
| Attempt | `hle-3f9a1c1b2c3d/2` | numbered from 1 each time the same inputs run again |
| Episode tag | `ep=3f9a1c2` | six digits of the run hash plus the attempt, as the agent sees it |

The inputs hashed into a run id are the harness version, the benchmark id, version and options, the sha256 of its data file, the task and seed, the agent or hive with its members and label, the variant, the memory mode, the budgets and the reply protocol. A changed dataset, option or budget therefore always gets a new run id, and the same inputs always get the same one. A benchmark's label and name are not hashed, so renaming a benchmark for display never splits its history. An agent's label is hashed, because it describes the agent being measured.

## What can be deleted

| File | Kind | If it is lost |
| --- | --- | --- |
| `index.jsonl` | ledger | the history of events is gone: back it up |
| `env.jsonl`, `omega.log.gz`, `judgments.jsonl`, `stderr.log` | evidence | that attempt can no longer be checked or rebuilt: back them up |
| `manifest.json` | inputs and provenance | that attempt loses its identity: back it up |
| `trace.jsonl`, `trace-meta.json`, `result.json` | derived | `omegaeval rebuild` recomputes them from the evidence |
| `checksums.json` | seal | `rebuild` writes it again, but a release still holds the original checksums |
| `index.sqlite` | query index | rebuilt from the ledger and result files the next time the store is opened, or by `omegaeval reindex` |

A backup is a copy of the store folder. The ledger only grows, so incremental tools such as rsync copy just the new attempts and the end of the ledger. The index can be left out of backups.

## How far it scales

Queries go through a SQLite index derived from the ledger, not through the files. The console asks for one benchmark at a time and at most 500 runs per page, and asks again only when the ledger grows. `python scripts/scale_check.py 20000` measures a synthetic store of 20,000 attempts copied from real reference runs:

| Query on 20,000 attempts | Time |
| --- | --- |
| Build the index from the ledger, once | 2.8 s |
| Reopen the store and read a page of 500 runs | 20 ms |
| A page of 500 runs of one benchmark | 12 ms |
| Count one benchmark's runs | under 1 ms |
| Progress summary for every set of runs | 75 ms |

What the console transfers at that size:

| Moment | Transfer |
| --- | --- |
| A refresh when nothing changed | about 4 KB |
| Switching benchmark: a page of runs and the progress summary | about 8 KB compressed |
| Opening Compare on two sets of about 5,000 runs each | about 60 KB compressed per set |

Before the index, the same console downloaded 53 MB on every refresh at this size. The query cost now depends on the page size and the number of sets, not on the size of the store.

Disk grows with the evidence. A baseline attempt takes tens of kilobytes. An OmegaClaw attempt adds its raw log, which carries the full prompt of every loop turn; the history section alone is up to about 30 KB per turn. Logs are compressed with gzip when the episode ends, and prompt text compresses several times over.

## Several machines

A store belongs to one machine. Processes on that machine share it safely: a running harness, `baseline` runs, `judge` and `rebuild` can all write at once, because ledger appends are atomic and the index takes a database lock. Different machines should not share one store through a synced folder, where two writers can split the ledger; `omegaeval doctor` warns when the store sits in iCloud, Dropbox, Google Drive or OneDrive.

To combine histories, give each machine its own store and merge:

```bash
omegaeval merge /mnt/shared/gpu-box-store
```

A merge copies finished attempts that are not already present, judged by their evidence checksums. When an attempt number is taken by a different episode, the copy gets the next free number and its manifest records the store and attempt it came from. Merging the same store twice changes nothing.

## Bundles

`omegaeval bundle <release>` packs a release into one `.tar.gz` file: the release's record and checksums, and every file of its attempts. Anyone can check it without the store it came from, which is how the registry verifies results. `--withhold-logs` leaves out the agents' raw logs, whose prompts can carry memory from other conversations; the rest still verifies, and the bundle records that logs were withheld.

## Checks

| Check | Command | Proves |
| --- | --- | --- |
| Checksums | `omegaeval verify` | no file changed since it was sealed |
| A release | `omegaeval verify <release>` | the release's files are exactly what it froze |
| Exact replay | `omegaeval replay` | the benchmark is deterministic and the record complete |
| Rebuild | `omegaeval rebuild` | derived records follow from the evidence with the current parser |

## Formats and versions

| Version | Where | Changes when |
| --- | --- | --- |
| `omegaeval.store/1` | `store.json` | the store layout changes |
| `omegaeval.run/0` | bundles and exports | the attempt bundle changes |
| `omegaeval.result/0` | `result.json` | the Result Record changes meaning |
| index version | inside `index.sqlite` | the index layout changes; the index then rebuilds itself |
| parser version | `trace.jsonl`, `result.json` | the OmegaClaw log parser changes; run `omegaeval rebuild` |

## Limits

- **One folder per store.** There is no central server or object storage backend yet. Stores combine by merging.
- **Long episodes load whole.** Play loads one attempt at a time, all of it. An episode of many thousands of steps with large observations will be slow to open.
- **Large JSON arrays load whole.** A question or memory file that is one JSON array is parsed in full, once per process. Use JSON Lines above about a gigabyte, and memory benchmarks then read one case per episode.
