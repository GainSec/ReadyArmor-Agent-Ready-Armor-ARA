# blacksmith-hammer — Channel catalog

This seed weapon supplies its channel catalog directly.
Forge generates channel catalogs when it seals derived weapons.

## Write channels (agent-callable during EXHAUST)

| Channel | Sink | Function |
|---|---|---|
| `extract.proc` | `forge.extractions` | Procedure / crumb extraction (PROC alphabet). |
| `extract.conc` | `forge.extractions` | Conceptual shape extraction (CONC alphabet). |
| `extract.fact_shape` | `forge.extractions` | Fact-shape registration. |
| `extract.store` | `forge.extractions` | Resource / store entry extraction (FACT alphabet). |
| `extract.line` | `forge.extractions` | Scope / out-of-scope line extraction (META alphabet). |
| `fact.assert` | `facts.jsonl` | Direct fact assertion during extraction. |

## Read channels (agent-callable any time)

- `mats.search`
- `mats.get`
- `evidence.search`
- `evidence.get`
- `facts.query`

## Active tool surface

The active MCP mode selects the available tools.
Use the tool list returned by the server for the current job.
The Forge MCP interface also provides `extract.resource`, `extract.rhema_rule`, `extract.bulwark_rule`, and `extract.walls_path`.
