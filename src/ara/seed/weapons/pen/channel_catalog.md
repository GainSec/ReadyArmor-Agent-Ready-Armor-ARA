# pen — Channel catalog

This seed weapon supplies its channel catalog directly.
Forge generates channel catalogs when it seals derived weapons.

## Write channels (agent-callable during EXHAUST)

| Channel | Sink | Function |
|---|---|---|
| `propose.alphabet` | `forge.extractions` | Declare an alphabet for the new pack (name, shape, fields). |
| `extract.conc` | `forge.extractions` | A starter entry for a declared alphabet (keyed by alphabet_name). |
| `propose.binding` | `forge.extractions` | A validator binding: finding field -> rule -> alphabet. |
| `propose.chunk` | `forge.extractions` | A phase-graph node (ChunkDef) for the pack's chunk_templates. |
| `propose.detection` | `forge.extractions` | A manifest_detection keyword for finding-section detection. |
| `extract.bulwark_rule` | `forge.extractions` | A bulwark rule for the pack's policy_defaults (baseline: hermes-default armor). |
| `extract.rhema_rule` | `forge.extractions` | A rhema rule for the pack's policy_defaults. |
| `extract.walls_path` | `forge.extractions` | A WALLS entry for the pack's policy_defaults. |

## Read channels (agent-callable any time)

- `mats.search`
- `mats.get`

## Active tool surface

The active MCP mode selects the available tools.
Use the tool list returned by the server for the current job.
Pack mode also provides `extract.bulwark_rule`, `extract.rhema_rule`, and `exhaust.status`.
Reconciliation mode provides `reconcile.merge`, `reconcile.split`, `reconcile.resolve_cell`, and `reconcile.drop`.
Workflow mode provides `propose.chunk` with material read tools.
