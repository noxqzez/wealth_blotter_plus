# SQL Standards

- Tables and columns: `snake_case`
- Explicit foreign key constraints on relations
- `ENABLE ROW LEVEL SECURITY` on all client/portfolio/trade tables; ship matching policies
- Prefer DDL in `/database` as the contract for schema changes
