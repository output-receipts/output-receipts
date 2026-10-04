# validate/: accuracy checks, the INS comparison and the review tools

Nothing here is needed to run the tool. These scripts measure it, compare it with INS, and support human or
AI review. Results for the FY2024 NCI R01 run are in `out/` and summarized in `../VALIDATION.md`.

## Scripts

| Script | What it does | Needs |
|---|---|---|
| `ins_compare.py` | Compares a run with NCI's public INS-Data tables; writes the rows INS does not list | INS-Data tables (download from github.com/CBIIT/INS-Data, `data/02_output/`) |
| `ins_benchmark.py` | Runs the rules on papers INS already lists and compares with the datasets INS attributes to them: how many does the tool find, and what does it add? No AI | INS-Data tables; network (about an hour) |
| `id_audit.py` | Blind check of a sample of candidate rows: is the deposit the paper's own? Samples by tier (`--strata tier1=50,tier2=40,tier3=40`) and reports per tier | A model (Claude CLI by default; any model via `--cmd`) |
| `compare_reviewers.py` | Row-by-row agreement between two reviewers on the same audit rows, overall and per tier | Two `id_audit*.jsonl` files |
| `ai_judge.py` | A blind reviewer gives papers a category: the review queue, and/or a stratified audit sample | A model, as above |
| `rescore.py` | Re-scores saved per-paper audit answers against the current rules (no model calls; use after any rule change) | Saved answers in `out/` |
| `human_check.py` | Builds a review page: each row shows the sentence containing the identifier, one-click answers, timed | Nothing; `--rows FILE --n 200` for a curator sample |
| `attribution.py` | How many awards each paper cites; papers published before their award started | Network (RePORTER) |
| `scale.py` | Awards with deposits, candidate rows per tier, NCI's yearly award volume | Network (RePORTER) |
| `adapters/` | Two ready-made `--cmd` adapters: Codex command line, any OpenAI-compatible endpoint | See `../AI_REVIEW.md` |

## Files in out/

**Published** (aggregate results, or deposits that were found):

| File | Contents |
|---|---|
| `ins_compare_report.txt`, `ins_compare.json` | INS comparison summary (the JSON feeds `site/build.py`) |
| `ins_benchmark_report.txt`, `ins_benchmark.json`, `ins_benchmark_added.tsv` | Benchmark against INS's own paper-to-dataset records, and the deposits found for those papers that INS does not list |
| `scale_report.txt`, `attribution_report.txt` | Scale and attribution figures |

**Written locally when you run the scripts, not published** (see `../.gitignore`): `ins_missing_datasets.tsv` (the
candidate rows INS does not list, including rows whose link does not resolve; its published subset is
`../public/candidates_not_in_ins.tsv`), the reviewers' row-by-row answers and reports (`id_audit*.jsonl`,
`id_audit_report*.txt`, `reviewer_agreement*.txt`, `answers*.jsonl`, `audit_report*.txt`), review-queue labels, and
the review pages. Their aggregate results are in `../VALIDATION.md` and `../public/summary.json`.
