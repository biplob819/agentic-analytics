# N1.7 tasks

- [x] 1. Spec and criteria written (OWNER D-55)
- [x] 2. node_start GO
- [x] 3. Tests first (fake clickhousectl cloud, recording admin client, fake Snowflake connector, in-process MCP server, planted secrets)
- [x] 4. Build (`setup` package, CLI wiring, doctor MCP check, env inventory)
- [x] 5. Converge twice
- [x] 6. Verifier (two rounds)
- [ ] 7. Owner live run (ClickHouse done 2026-11-17; Snowflake `--run` pending): `setup clickhouse`, the two console steps, `doctor` MCP check PASS; `setup snowflake --run`
- [ ] 8. Proofs, green, commit
