# The optional AI component

Output Receipts has two separate parts:

1. **The tool** (`receipts/`): transparent rules and public lookups. It runs without any AI and produces every
   own-versus-reused decision, every link and record check, the tiers, the INS candidate rows and the receipts.
   Papers whose category its rules cannot decide are labelled `NEEDS_REVIEW` and listed in
   `data/review/needs_review.jsonl`.
2. **AI review** (`validate/`): optional. A capable language model can (a) give the `NEEDS_REVIEW` papers a category
   (about 8% of the FY2024 R01 cohort; this does not change any dataset row) and (b) measure accuracy by judging a
   random sample blind. A human reviewer can do either job instead, using the same instructions.

AI is not part of the tool or of what it proposes; it was used to check the tool and to label the category of
papers the rules left undecided. The answers the models gave in
this prototype are kept locally and are not published; their aggregate results are in `VALIDATION.md`.

This repository supplies the AI part as **instructions and plumbing**, not as a dependency: the prompts, the expected
answer formats, the scripts that send them, and evidence of how well capable models perform. An adopting team
plugs in whatever AI setup it already has.

## Plugging in a model

Every AI call goes through one function, `ask_model` in `validate/ai_judge.py`. By default it calls the Claude Code
command line with all tools switched off; pass `--cmd` to use anything else. The command receives the prompt on stdin
and must print the answer. It is run in an empty temporary folder, so a command-line agent that can read files sees
none of this repository's outputs: the reviewer stays blind to the tool's answers.

Any OpenAI-compatible endpoint (hosted API, enterprise AI gateway, or a local open-weight server such as vLLM or
Ollama) takes two environment variables. Linux / macOS (bash, zsh):

```
export OR_BASE_URL=http://localhost:11434/v1
export OR_MODEL="<model name your endpoint expects>"
python validate/ai_judge.py --skip-review --audit 100 --cmd "python validate/adapters/openai_compatible.py"
```

Windows PowerShell: `$env:OR_BASE_URL = "http://localhost:11434/v1"` and `$env:OR_MODEL = "<model name>"`, then the
same `python` line (in cmd.exe: `set OR_BASE_URL=...`).

OpenAI models through the Codex command line (one line; works in any shell):

```
python validate/id_audit.py --population validate/out/ins_missing_datasets.tsv --sample-from validate/out/YOUR_FIRST_AUDIT.jsonl --tag _second_reviewer --cmd "python validate/adapters/codex_cli.py gpt-6.1-sol medium"
```

## The tasks and their answer formats

The full prompt texts are the `PROMPT` constants in `validate/ai_judge.py` and `validate/id_audit.py`. Each gives the
model the paper's title and the passages of its open full text that mention availability, deposits, accession
numbers, repositories, code, supplements or data sources (selected by `packet()`, about 14,000 characters at most).
For the deposit question, the passages that mention the identifier are always included. The model is **never shown
the tool's answer**.

| Task | Script | The model answers with |
|---|---|---|
| Label a paper | `ai_judge.py` | `LABEL:` one of the labels in `HOW_IT_WORKS.md`; `MIXED: yes/no`; `CODE_SHARED: yes/no`; `CONFIDENCE: high/medium/low`; `REASON:` one sentence |
| Is this deposit the paper's own? | `id_audit.py` | `ROLE: OWN/REUSED/NOT_A_DEPOSIT/UNCLEAR`; `KIND: DATA/CODE/OTHER`; `CONFIDENCE`; `REASON` |

Answers are parsed line by line, so any model that follows a fixed format works. Low-confidence labels are not
applied; those papers are reported as ambiguous (`validate/out/ambiguous.csv`).

## Models tested

| Model | Developer | Used for | Result |
|---|---|---|---|
| Claude Opus 5.5 (medium effort) | Anthropic | category labels for undecided papers; per-paper audits; deposit audits | see `VALIDATION.md` |
| GPT-6.1 (Sol, medium effort, via `adapters/codex_cli.py`) | OpenAI | deposit audits, on the same rows as Claude | agrees with Claude on 121 of 130 rows of the tier audit (Cohen's kappa 0.65); see `VALIDATION.md` |

Only these two models were tested; re-run the audit scripts to measure any other model before relying on it.
