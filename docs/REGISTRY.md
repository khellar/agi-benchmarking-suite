# Registry

The project site lists benchmarks anyone can load, and result releases anyone can verify. Anyone with a GitHub account can register either one. This page explains how, what the checks look at, and how maintainers run it.

- [Use a registered benchmark](#use-a-registered-benchmark)
- [Register a benchmark](#register-a-benchmark)
- [Register results](#register-results)
- [What the checks do](#what-the-checks-do)
- [Entry fields](#entry-fields)
- [For maintainers](#for-maintainers)

## Use a registered benchmark

The [registry page](https://khellar.github.io/agi-benchmarking-suite/registry.html) shows each benchmark with the commands that load it:

```bash
omegaeval add hle
omegaeval conform hle
```

`add` copies the benchmark's config block from the registry into `omegaeval.toml`, with its label and name. If the benchmark reads a data file, point `options.path` at your copy. If it lives in a package, the registry shows the install command to run first.

## Register a benchmark

Open the [benchmark form](https://github.com/khellar/agi-benchmarking-suite/issues/new?template=register-benchmark.yml) and fill in the id, label, full name, shape, a short description, a homepage, the maintainers and the config block that loads it. When the checks pass, a pull request that adds the entry is opened for you, and a maintainer merges it. When they fail, a comment says what to fix; edit the issue and they run again.

You can also open a pull request yourself that adds `registry/benchmarks/<id>.toml`. For example:

```toml
# registry/benchmarks/arc-agi-3.toml
id = "arc-agi-3"
label = "ARC-AGI-3"
name = "ARC-AGI-3 interactive games"
kind = "interactive"
description = "Turn-based puzzle games where the rules have to be discovered by playing."
homepage = "https://arcprize.org"
maintainers = ["your-github-handle"]
scoring = "deterministic"
install = "pip install arc-omegaeval"
tags = ["abstraction", "games"]
config = '''
[environments.arc-agi-3]
adapter = "arc_omegaeval:ArcGames"
label = "ARC-AGI-3"
'''
```

The config block can only name an adapter class and its options. The adapter must be one of the built-in ones, or come from the package that `install` installs, and the harness refuses anything that is not a benchmark class. Importing a package still runs its code, which is why maintainers review every entry, and why `add` prints the block it adds.

## Register results

Results are a release: a frozen set of attempts with their checksums. Make one, pack it, host the file, then register it:

```bash
omegaeval release hle-oma-1-2026-10 hle subject=oma-1
omegaeval bundle hle-oma-1-2026-10 --withhold-logs
```

`bundle` writes `hle-oma-1-2026-10.tar.gz` and prints its sha256. Host the file at an https address, such as an asset of a GitHub release in your own repository. Then open the [results form](https://github.com/khellar/agi-benchmarking-suite/issues/new?template=register-results.yml) with the release name, benchmark, agent, date, file address and sha256.

- **Raw logs.** An agent's raw log holds every prompt it was sent, and a prompt can carry memory from other conversations. `--withhold-logs` leaves the logs out. The results still verify, and the registry marks them as having logs withheld.
- **Your numbers are not typed in.** The registry downloads the bundle, checks every checksum, and computes the solve counts from the result files.
- **Listed, not ranked.** The registry lists releases newest first, with each one's own numbers, judges and agent versions.

## What the checks do

| Entry | Checked on every pull request | Checked when the site is built |
| --- | --- | --- |
| Benchmark | required fields, the id rules, the file name, a unique id, an https homepage, and a config block that parses, holds only `[environments.<id>]`, uses only keys omegaeval reads, and names a built-in adapter or one from the package in `install` | the same, so a broken entry is left off the page |
| Results | required fields, the release name rules, a registered benchmark, the date, an https bundle address, and a sha256 | the bundle is downloaded, its sha256 compared, every checksum verified, and the numbers recomputed |

The site is rebuilt on every change and once a week, so a bundle that disappears shows as not verified. Run the same checks locally before submitting:

```bash
python -m omegaeval.registry check registry/benchmarks/arc-agi-3.toml
python -m omegaeval.registry check --fetch
```

## Entry fields

A benchmark entry, `registry/benchmarks/<id>.toml`:

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | The benchmark id: 1 to 40 lowercase letters, digits, - or _. It names the file too. |
| `label` | yes | Short display name, such as HLE. At most 24 characters. |
| `name` | yes | Full name. At most 100 characters. |
| `kind` | yes | interactive, qa or memory. |
| `description` | yes | One or two sentences on what it measures. At most 400 characters. |
| `homepage` | yes | https URL of the benchmark, its paper or its dataset. |
| `maintainers` | yes | GitHub handles of the people who answer for the entry. |
| `config` | yes | The omegaeval.toml block that loads it: one [environments.<id>] table and its options. |
| `scoring` | no | deterministic, judge or mixed. |
| `install` | no | The command that installs the adapter, when it lives in a package. |
| `license` | no | The license of the benchmark's data or code. |
| `tags` | no | Keywords, such as reasoning or memory. |

A results entry, `registry/results/<release>.toml`:

| Field | Required | Meaning |
| --- | --- | --- |
| `release` | yes | The release name given to omegaeval release. It names the file too. |
| `benchmark` | yes | The id of the benchmark the release covers: registered, or built in. |
| `agent` | yes | The agent or hive id in the release. |
| `date` | yes | The day the release was made, as YYYY-MM-DD. |
| `bundle` | yes | https URL of the file made by omegaeval bundle, such as a GitHub release asset. |
| `bundle_sha256` | yes | The sha256 omegaeval bundle printed for that file. |
| `maintainers` | yes | GitHub handles of the people who answer for the entry. |
| `agent_description` | no | What the agent is: version, model and settings. |
| `judged_by` | no | Who or what judged the answers, when a judge graded them. |
| `notes` | no | Anything a reader needs to interpret the results. |

For example:

```toml
# registry/results/hle-oma-1-2026-10.toml
release = "hle-oma-1-2026-10"
benchmark = "hle"
agent = "oma-1"
date = "2026-10-01"
bundle = "https://github.com/your-github-handle/your-repo/releases/download/v1/hle-oma-1-2026-10.tar.gz"
bundle_sha256 = "0f3c2a9b8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a"
maintainers = ["your-github-handle"]
agent_description = "Omega v0.1.19, Anthropic, reasoning medium"
judged_by = "o3-mini with the HLE judge prompt"
```

## For maintainers

Registration runs on GitHub Actions and GitHub Pages. Once, in the repository's settings:

1. **Pages.** Set the source to GitHub Actions. The site workflow then publishes the home page, the registry and the console demo.
2. **Actions.** Under workflow permissions, allow GitHub Actions to create pull requests. Without it, the intake workflow pushes a branch and comments a link that opens the pull request in one click.
3. **Label.** Optionally create a `registry` label, which the forms apply to registration issues. The workflows do not depend on it.

For each registration pull request, check that the maintainers are real people who answer for the entry, that the homepage describes the benchmark, and, for a benchmark in a package, that the package is what it claims to be. Merging publishes the entry at the next site build.
