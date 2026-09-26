# portfolio_suggestions — run audits

Manual audit trail for the advisory / portfolio-suggestions crew
(`quant_analyst` → `compliance_officer` → `trade_staging_agent`).

Each kickoff writes a timestamped file here:

```text
local_audit_YYYYMMDDHHMM.md
```

Open the latest `local_audit_*.md` after a run to review each agent’s raw output in
sequence (workaround when CrewAI Desktop is unavailable).

Other workflows get sibling folders under `audits/<workflow_name>/`.
