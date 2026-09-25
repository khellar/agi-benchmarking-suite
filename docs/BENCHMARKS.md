# Adding a benchmark

A benchmark is anything the harness can play against an agent: a question set, a memory test, a game, a live environment. This page shows how to load one, name it, check it and run it.

- [Pick the way in](#pick-the-way-in)
- [Name it](#name-it)
- [A question set from a file](#a-question-set-from-a-file)
- [A memory benchmark from a file](#a-memory-benchmark-from-a-file)
- [A game or live environment](#a-game-or-live-environment)
- [Share it as a package](#share-it-as-a-package)
- [Register it](#register-it)
- [Check it, then run it](#check-it-then-run-it)
- [Grading, judges and tool rules](#grading-judges-and-tool-rules)
- [Checklist](#checklist)

## Pick the way in

| You have | Do this | Example |
| --- | --- | --- |
| Questions with gold answers, in JSON or JSON Lines | point the built-in `QADataset` adapter at the file | Humanity's Last Exam |
| Chat histories followed by a question | point the built-in `ChatMemory` adapter at the file | LongMemEval |
| A game, simulator or anything interactive | write an adapter class in `./benchmarks` | FreeCiv, ARC-AGI-3, AGI Maze |
| An adapter several teams will use | publish it as a Python package | a shared FreeCiv wrapper |

Every benchmark has one of three shapes. `interactive`: every observation offers actions and expects one back. `qa`: one question per episode, answered in free text. `memory`: several messages that need no reply, then a question.

A running harness picks up new benchmarks without a restart. It rereads `omegaeval.toml` when the file changes, and new files in the benchmark folder are found the next time the benchmark list is read.

## Name it

The benchmark id is how people select it everywhere: in the console, on the command line, and in every run id, such as `hle-3f9a1c1b2c3d`. Choose a short lowercase id and give it a label and a full name for display.

```toml
[environments.hle]
adapter = "omegaeval.envs.qa:QADataset"
label = "HLE"
name = "Humanity's Last Exam (text-only)"
```

- **The id** is 1 to 40 lowercase letters, digits, `-` or `_`: `hle`, `arc-agi-3`, `freeciv`, `longmemeval-s`. `omegaeval doctor` rejects anything else.
- **The label** is what the console's benchmark selector and run lists show, such as `HLE` or `ARC-AGI-3`. It defaults to the id.
- **The name** is the full title, shown beside the label.

For a benchmark written as a class, the id and label are class attributes, and the name comes from `describe()`. A config block with the same id can override the label and name without touching the code. Commands accept the label as well as the id, in any case, so `omegaeval baseline oracle HLE` works.

## A question set from a file

`QADataset` plays one question per episode. Seed 1 is the first question of the task, seed 2 the second, and so on. It reads JSON, a JSON array or JSON Lines, and accepts common field names:

| Field | Names accepted |
| --- | --- |
| id | `id`, `question_id`, `uid` |
| question | `question`, `prompt`, `problem` |
| choices | `choices`, `options` (a list, optional) |
| answer | `answer`, `gold`, `target`, `solution` |
| type | `answer_type`, `type`: `multipleChoice` or `exactMatch` |
| category | `category`, `subject`, `raw_subject`; each category becomes a task |
| image | `image`, `image_url`; questions with an image are skipped, because a chat channel carries text |

A multiple-choice question with no choices field may carry its options in the text, the way Humanity's Last Exam does: an `Answer Choices:` line, then `A. ...`, `B. ...`. The adapter reads them from the text and grades the option letter.

**Humanity's Last Exam.** The [dataset](https://huggingface.co/datasets/cais/hle) is gated on Hugging Face: accept its terms there first, then export the test split to JSON Lines:

```bash
pip install datasets
python -c "from datasets import load_dataset; load_dataset('cais/hle', split='test').to_json('/data/hle.jsonl')"
```

Then add it to `omegaeval.toml`:

```toml
[environments.hle]
adapter = "omegaeval.envs.qa:QADataset"
label = "HLE"
name = "Humanity's Last Exam (text-only)"

[environments.hle.options]
path = "/data/hle.jsonl"
judge_all = true
allow_tools = ["send", "remember", "query", "pin", "episodes", "metta"]
```

`judge_all` holds every answer for a judge, because the published HLE scores are graded by a judge model, as the [HLE evaluation code](https://github.com/centerforaisafety/hle) shows. `allow_tools` keeps the exam closed-book: `websearch`, `shell` and the file skills are not on the list, so using them voids the episode. The [reference](REFERENCE.md#built-in-adapters) lists every option.

## A memory benchmark from a file

`ChatMemory` sends earlier conversations one message at a time, waits until the agent's log shows it read each one, then asks the question. It reads its own field names and LongMemEval's: `question_id`, `question_type`, `question`, `answer`, `question_date`, `haystack_sessions`, `haystack_dates`, `haystack_session_ids` and `answer_session_ids`. Only each turn's role and content reach the agent. Which sessions hold the evidence is revealed after the episode.

**LongMemEval.** Download the cleaned files listed in the [LongMemEval repository](https://github.com/xiaowu0162/LongMemEval), then:

```toml
[environments.longmemeval-s]
adapter = "omegaeval.envs.chatmemory:ChatMemory"
label = "LongMemEval S"
name = "LongMemEval (S, cleaned)"

[environments.longmemeval-s.options]
path = "/data/longmemeval_s_cleaned.json"
judge_all = true
```

A file of several gigabytes, such as LongMemEval M, should be JSON Lines: one case per line. The adapter then keeps only an index in memory and reads one case per episode. Convert a JSON array once:

```bash
python -c "import json, sys; [print(json.dumps(c)) for c in json.load(open(sys.argv[1]))]" longmemeval_m_cleaned.json > longmemeval_m.jsonl
```

## A game or live environment

Write an adapter: a Python class with a public side that produces what the agent sees, and a privileged side that only the harness calls. Start from a template that already passes every check:

```bash
omegaeval new-benchmark freeciv --kind interactive
```

That writes `benchmarks/freeciv.py`. Files in `./benchmarks` are found on their own; list other folders in `[harness] benchmark_dirs`. For a live game, `reset` joins or starts a game, `step` sends one action and reads the new state, and `render` returns a map or frame for the console.

Here is a complete small benchmark. The agent looks for a hidden number and is told to go higher or lower after each guess. The test suite runs this exact code through all nine checks.

```python
# benchmarks/hotcold.py
import random

from omegaeval.envs.base import Environment, Observation, Receipt


class HotCold(Environment):
    id = "hotcold"                 # the benchmark id: lowercase, used in run ids such as hotcold-3f9a1c1b2c3d
    label = "Hot/cold"             # what people see in the console
    version = "1.0.0"              # bump when behaviour or scoring changes
    SIZES = {"small": 16, "large": 256}

    # -- public: what the agent is shown ------------------------------------------
    def describe(self):
        return {
            "id": self.id, "version": self.version, "name": "Find the hidden number",
            "kind": "interactive", "tasks": list(self.SIZES), "default_task": "small",
            "observation": "text: the range still possible and the last hint",
            "actions": "guess N",
            "utility": {"meaning": "1 if the number is found within the step budget, else 0",
                        "direction": "higher"},
            "brief": "Find the hidden number. After each guess you are told to go higher or lower.",
            "renderer": "tiles", "oracle": True, "scoring": "deterministic",
            "floor_policy": "a random guess each step that ignores the hints",
            "tool_policy": {"forbid": []},
        }

    def reset(self, task, seed):
        self.n = self.SIZES[task]
        self.target = random.Random(f"{self.id}:{task}:{seed}").randint(1, self.n)
        self.lo, self.hi, self.last = 1, self.n, None
        self.steps, self.invalid, self.found = 0, 0, False
        return self._obs(f"The number is between 1 and {self.n}.")

    def parse_action(self, text):
        words = str(text).lower().replace("guess", " ").split()
        return f"guess {int(words[0])}" if words and words[0].isdigit() else None

    def step(self, action):
        self.steps += 1
        out_of_steps = self.steps >= self.max_steps()
        if action is None:
            self.invalid += 1
            return Receipt(self._obs("Reply with: guess N"), False, "unparseable", out_of_steps, {})
        guess = int(action.split()[1])
        self.last = guess
        if guess == self.target:
            self.found = True
            return Receipt(self._obs(f"{guess} is right."), True, "found", True, {})
        if guess < self.target:
            self.lo = max(self.lo, guess + 1)
            hint = f"{guess} is too low: go higher."
        else:
            self.hi = min(self.hi, guess - 1)
            hint = f"{guess} is too high: go lower."
        return Receipt(self._obs(hint), True, "hint", out_of_steps, {})

    def render(self):
        row = "".join("." if self.lo <= i <= self.hi else "x" for i in range(1, self.n + 1))
        marks = [{"r": 0, "c": self.last - 1, "label": "last guess"}] if self.last else []
        return {"type": "tiles", "rows": [row], "legend": {".": "still possible", "x": "ruled out"},
                "colors": {".": "#cfe0ff", "x": "#9aa3ad"}, "marks": marks}

    # -- privileged: only the harness calls these ------------------------------------
    def score(self):
        terminal = "solved" if self.found else ("budget_exhausted" if self.steps >= self.max_steps() else "running")
        return {"success": self.found, "utility": 1.0 if self.found else 0.0, "terminal": terminal,
                "steps": self.steps, "metrics": {"target": self.target, "invalid_actions": self.invalid}}

    def floor(self):
        return round(1 - (1 - 1 / self.n) ** self.max_steps(), 4)

    def ceiling(self):
        return {"utility": 1.0, "steps": len(self.solution())}

    def max_steps(self):
        return 2 * self.n.bit_length()

    def solution(self):
        lo, hi, plan = 1, self.n, []
        while True:
            mid = (lo + hi) // 2
            plan.append(f"guess {mid}")
            if mid == self.target:
                return plan
            lo, hi = (mid + 1, hi) if mid < self.target else (lo, mid - 1)

    # -- internals --------------------------------------------------------------------
    def _obs(self, text):
        actions = [f"guess {i}" for i in range(self.lo, self.hi + 1)] if not self.found else []
        return Observation(text + f" Still possible: {self.lo} to {self.hi}.", actions,
                           {"lo": self.lo, "hi": self.hi, "steps": self.steps})
```

What each part is for:

- **`describe()`** declares the benchmark once. The one sentence in `utility` about what utility means, and which direction is better, is the only benchmark-specific meaning the harness carries.
- **`reset()` and `step()`** must be deterministic for a task and seed: derive every random choice from them, as `random.Random(f"{self.id}:{task}:{seed}")` does. Exact replay depends on it.
- **An unparseable reply** returns a receipt with `valid=False`. It never raises.
- **`render()`** shows only what the agent has seen. The hidden number goes in `score()["metrics"]`, which the console reveals after the episode.
- **`floor()` and `ceiling()`** place every run between a trivial policy and an oracle, so a score means the same distance in every benchmark.
- **`solution()`** drives the oracle reference agent. Leave it out, and return `None` from `ceiling()`, when there is no oracle.
- **`fingerprint()`** is only needed when episodes depend on something outside the code, such as a data file. The built-in adapters hash their data file with it, so new data never shares a run id with old data.

The world panel draws whatever frame `render()` returns: `grid` for mazes, `tiles` for strategy maps, `pixels` for ARC-style frames, `card` for questions and `timeline` for conversations. With no renderer, the console shows the transcript. The [reference](REFERENCE.md#frames) lists each frame's fields.

## Share it as a package

Put the adapter in a Python package and declare an entry point. Anyone who installs the package gets the benchmark, with no file to copy and no config block:

```toml
# pyproject.toml of your package
[project.entry-points."omegaeval.environments"]
freeciv = "freeciv_omegaeval:FreeCiv"
```

## Register it

Register the benchmark on the project site so others can load it with `omegaeval add <id>`. Fill in the [benchmark form](https://github.com/khellar/agi-benchmarking-suite/issues/new?template=register-benchmark.yml), and a pull request that adds it is opened once the checks pass. The [registry guide](REGISTRY.md) lists what an entry needs.

## Check it, then run it

```bash
omegaeval conform hotcold
omegaeval benchmarks
omegaeval baseline random hotcold --seeds 1-5
omegaeval baseline oracle hotcold --seeds 1-5
omegaeval run oma-1 hotcold --seeds 1-5
```

`conform` runs nine checks, and a benchmark's runs count only when all of them pass:

1. `describe()` is complete, including what utility means and which direction is better.
2. Reset, step and deliveries are deterministic for a seed.
3. Different seeds give different episodes.
4. An unparseable reply is a typed failure, not an exception.
5. Offered actions and free-text answers survive `parse_action`.
6. Floor and ceiling references are declared.
7. `score()` reports success, utility, terminal status and steps, and a judge-held answer leaves success empty.
8. `render()` returns a known frame type or nothing.
9. The agent's view does not contain the privileged solution.

`omegaeval doctor` runs the checks on every installed benchmark at once. The random and oracle baselines give the floor and the ceiling that Progress draws every agent between. `run` needs a harness started with `omegaeval serve` and an agent from `omegaeval.toml`; the [guide](GUIDE.md#run-episodes) covers both.

## Grading, judges and tool rules

- **Deterministic first.** Multiple choice compares the option letter. Short answers compare after normalization, so `3/4` matches `0.75` and `1,088` matches `1088`.
- **Never wrong by default.** A short answer that does not match is held for a judge rather than marked wrong. Set `judge_on_mismatch = false` when a mismatch is simply wrong.
- **Judge-graded benchmarks.** `judge_all = true` holds every answer, keeping the deterministic grade as a pregrade. `omegaeval judge` then records verdicts from a person or from a judge command; the [guide](GUIDE.md#judge-answers) shows how.
- **Tool rules.** `forbid_tools` lists skills the agent must not use. `allow_tools` lists the only skills it may use, which also catches skills a plugin adds. A violation found in the agent's log voids the episode.
- **Versions.** Bump `version` whenever behaviour or scoring changes. It is recorded in every manifest and hashed into the run id.

## Checklist

- [ ] The id is short and lowercase, and the label and name read well in the console.
- [ ] `omegaeval conform <id>` passes all nine checks on every task.
- [ ] Random and oracle baselines have run, so Progress shows a floor and a ceiling.
- [ ] Gold answers and hidden state live only on the privileged side.
- [ ] Data files are where the harness can read them and the agent cannot.
- [ ] Tool rules and judging match how the benchmark is published.
