# Contributing

## Set up

```bash
git clone https://github.com/khellar/agi-benchmarking-suite
cd agi-benchmarking-suite
pip install -e ".[test]"
pytest
```

The harness needs Python 3.11 or later and one runtime dependency, `websockets`.

## Before a pull request

```bash
pytest
python -m omegaeval.docgen --check
ruff check .
```

- **Tests** cover the harness end to end: real websocket episodes, the connectors, the store, the registry, and the docs themselves.
- **The reference is generated.** After changing a command, an option or a config key, run `python -m omegaeval.docgen` and commit docs/REFERENCE.md.
- **Docs are tested.** Every command shown in the docs must parse, every config snippet must load with no unknown keys, and every link must resolve.

## Where things live

| Path | What it is |
| --- | --- |
| `omegaeval/episode.py` | the episode loop: benchmark, budgets, the record |
| `omegaeval/connectors.py` | how the harness reaches agents: websocket, http, command, python |
| `omegaeval/omega_trace.py` | the OmegaClaw log parser |
| `omegaeval/store.py` | the append-only store and its query index |
| `omegaeval/envs/` | the benchmark contract and the built-in adapters |
| `omegaeval/registry.py` | registry checks, bundle verification and the site's data |
| `omegaeval/console/index.html` | the console, one self-contained page |
| `website/`, `scripts/build_site.py` | the project site |

## License

The project is under the [MIT License](LICENSE), and contributions are accepted under it too.

## Adding a benchmark or an agent

A benchmark is best shared as its own package, or registered on the site; [the benchmark guide](docs/BENCHMARKS.md) and [the registry guide](docs/REGISTRY.md) explain both. A new way to reach agents is a subclass of `Connector` in `omegaeval/connectors.py`, with tests in `tests/test_connectors.py`.
