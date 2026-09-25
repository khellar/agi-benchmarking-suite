# agi-benchmarking-suite

[![tests](https://github.com/khellar/agi-benchmarking-suite/actions/workflows/ci.yml/badge.svg)](https://github.com/khellar/agi-benchmarking-suite/actions/workflows/ci.yml)
[![console demo](https://github.com/khellar/agi-benchmarking-suite/actions/workflows/pages.yml/badge.svg)](https://khellar.github.io/agi-benchmarking-suite/)

`omegaeval` is one harness that every benchmark sits behind. Point it at an [OmegaClaw](https://github.com/singnet/Omega) agent, any other agent, or a hive of them, pick a benchmark such as HLE, ARC-AGI-3 or FreeCiv, and it plays episodes through the agent's own chat channel. It records what the agent saw, thought, remembered and did, and keeps an append-only history you can cite.

The question it answers: did this change to the agent make it better, worse, or both, and in which respects.

**[Visit the project site](https://khellar.github.io/agi-benchmarking-suite/)** for the live console demo and the registry of benchmarks and verified results. It is rebuilt on every push.

## Try it

```bash
pip install git+https://github.com/khellar/agi-benchmarking-suite
omegaeval baseline random qa-sample --seeds 1-10
omegaeval baseline oracle qa-sample --seeds 1-10
omegaeval export qa-sample --html report.html
```

Open `report.html`: it is the console with those runs inside. The two reference agents need no model and no server. They give every benchmark a floor and a ceiling.

## Connect an agent

```bash
omegaeval init
omegaeval doctor
omegaeval serve
omegaeval connect oma-1
```

`init` writes `omegaeval.toml`, `doctor` checks it, and `serve` starts the gateway and the console at http://127.0.0.1:8780. `connect` prints the command that starts an OmegaClaw agent on its websocket channel, pointed at the harness. OmegaClaw needs no code change: the harness speaks its chat channel, follows its log, and reads its memory files without writing to them. Queue episodes from the console's Runs view, or with `omegaeval run oma-1 keydoor --seeds 1-5`.

Any other agent connects by one line in its agent block: a Python class, an HTTP endpoint, or a command that speaks JSON Lines. It receives each message as JSON and answers with its reply, plus trace events that fill the console's lanes. `examples/agents` has a template that speaks all three, and a model-only Claude baseline:

```toml
[agents.claude]
connect = { python = "claude:Claude", cwd = "examples/agents" }
```

```bash
omegaeval run claude qa-sample --seeds 1-10 --local
```

## Load a new benchmark

Give it a short lowercase id and a display label, then load it one of three ways:

1. **A data file.** Point a built-in adapter at it in `omegaeval.toml`:

   ```toml
   [environments.hle]
   adapter = "omegaeval.envs.qa:QADataset"
   label = "HLE"
   name = "Humanity's Last Exam (text-only)"

   [environments.hle.options]
   path = "/data/hle.jsonl"
   judge_all = true
   ```

2. **A Python file in `./benchmarks`.** Start from a template that already passes every check with `omegaeval new-benchmark freeciv --kind interactive`.
3. **An installed package** that declares an `omegaeval.environments` entry point.

Then check it and run it:

```bash
omegaeval conform hle
omegaeval baseline random hle --seeds 1-20
omegaeval run oma-1 hle --seeds 1-20
```

A running harness picks up new benchmarks without a restart. The id names the benchmark everywhere: in the console, on the command line, and in every run id, such as `hle-3f9a1c1b2c3d`. [Adding benchmarks](docs/BENCHMARKS.md) covers question sets, memory tests, games, grading and judges.

## Registry

The [registry](https://khellar.github.io/agi-benchmarking-suite/registry.html) lists benchmarks anyone can load and result releases anyone can verify. Load a registered benchmark with one command:

```bash
omegaeval add hle
```

Anyone with a GitHub account can register a benchmark or a release through an issue form, and a pull request is opened once the checks pass. A registered release's numbers are recomputed from its sealed files before they are shown. [The registry guide](docs/REGISTRY.md) explains how it works.

## Documentation

| Page | For |
| --- | --- |
| [Guide](docs/GUIDE.md) | installing, connecting agents, running episodes, reading the console, judging, publishing |
| [Adding benchmarks](docs/BENCHMARKS.md) | loading and writing benchmarks, with a complete example |
| [Reference](docs/REFERENCE.md) | every command, config key, file format and protocol |
| [Data plan](docs/DATA.md) | naming, evidence, the index, scale, merging stores, backups |
| [Registry](docs/REGISTRY.md) | loading registered benchmarks, and registering benchmarks and results |
| [Specification](docs/SPEC.md) | the design and what v0 includes |
| [OmegaClaw integration](docs/OMEGACLAW.md) | what the harness reads from OmegaClaw, field by field |
| [Changelog](CHANGELOG.md) | what changed in each version |
| [Contributing](CONTRIBUTING.md) | setting up, testing, and where the code lives |
| [Security](SECURITY.md) | reporting a vulnerability, and what the harness protects |

## Status

Version 0.3. Validated against OmegaClaw at commit ee0618a, and tested with reference agents, example agents and test doubles; not yet run against a live OmegaClaw agent. The Humanity's Last Exam and LongMemEval mappings follow their published formats and have not been run on the full datasets.

## License

[MIT](LICENSE).
