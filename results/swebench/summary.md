# SWE-bench Verified: cheap, strong and cascade

25 tasks from princeton-nlp/SWE-Bench_Verified (test). Cheap: `together_ai/openai/gpt-oss-120b` (cheap-v1). Strong: `together_ai/moonshotai/Kimi-K3.1-Pro` (strong-v1).

| Strategy | Resolved | Resolve rate | Total cost | Cost / task | Cost / resolved | Escalated |
|---|---:|---:|---:|---:|---:|---:|
| cheap | 5/25 | 20% | $2.7737 | $0.1109 | $0.5547 | - |
| strong | 22/25 | 88% | $3.7504 | $0.1500 | $0.1705 | - |
| cascade | 18/25 | 72% | $4.7227 | $0.1889 | $0.2624 | 14 |
| oracle (upper bound)* | 22/25 | 88% | $2.9927 | $0.1197 | $0.1360 | 17 |

**Cascade rule** (run-time signals only): escalate to the strong model when the cheap run hit a limit, errored, or submitted an empty patch; otherwise keep the cheap patch. Cascade cost per task = cheap cost + strong cost if escalated.
Escalation reasons: empty_patch 1, limit 13.

\* The oracle routes each task with hindsight to the cheaper model that resolves it (cheap if neither does), paying only that model. It uses grading results, so it is an upper bound, not a strategy.

Graded with swebench 4.1.0. Grader check (gold.gold-410.json): 1/1 gold patches resolved, passed. Generated at commit `35a1aea67da3f7059f9315a30e99f5ea221d7333`.
