# Forge

Forge converts source materials into a sealed task method.
A source material is called a Mat.
A manifest identifies the material elements that extraction must process.
Coverage records connect extracted items to those elements.

## Workflow

The main phases are deposit, manifest, review, exhaust, synthesis, and seal.
A session preserves progress between phase operations.
The source directory `src/ara/forge/phases/` contains the phase handlers.

1. Create a Forge session.

   ```sh
   ara forge --state-root .ara new example-method
   ```

2. Record the returned session identifier.
3. Examine the deposit options.

   ```sh
   ara forge deposit --help
   ```

4. Deposit the source materials.
5. Finalize the deposit.
6. Generate the manifest.
7. Examine the manifest entries.
8. Approve the manifest.
9. Process the manifest through extraction.
10. Examine the coverage results.
11. Generate the weapon draft.
12. Examine the draft.
13. Seal the weapon.

Use each command's `--help` output for its session and file arguments.

```sh
ara forge finalize-deposit --help
ara forge manifest --help
ara forge approve-manifest --help
ara forge run-exhaust --help
ara forge coverage --help
ara forge synthesize --help
ara forge seal --help
```

## Extraction

`auto-extract` processes supported report sections through the selected pack.
`run-exhaust` drives an agent through unresolved manifest entries.
Its default agent path uses contained Hermes runs.
Each iteration selects a fresh batch of unresolved elements.
Coverage thresholds and iteration limits control completion.

Install optional text extractors when the materials use their formats:

```sh
python -m pip install -e '.[extract]'
```

PDF, Word, Excel, and image extraction use optional libraries.
Image OCR also needs the Tesseract executable.

## Packs and alphabets

An alphabet defines structured item types and their operations.
A pack groups domain definitions for extraction and runtime use.
Pack Forge creates a pack draft from source materials.
Review, reconciliation, and promotion determine the installed result.

```sh
ara forge new example-pack --kind pack
ara forge pack --help
```

## Reforge

Reforge proposes a change against a predecessor weapon.
The proposal preserves the predecessor relationship.
Examine the proposed differences before you accept the new sealed result.

```sh
ara forge reforge --help
```

See [Task lifecycle](tasks.md) for execution after equipment.

## Inspect the sealed result

The weapon directory contains the files consumed by the runtime.

| Artifact | Function |
|---|---|
| `weapon.json` | Structured method components and metadata |
| `policy/` | Resource and model policy fragments |
| `chunks/` | Procedure graph nodes and their channel surfaces |
| `alphabets/` | Projected domain vocabulary and validator data |
| `channel_catalog.json` | Structured channel catalog |
| `channel_catalog.md` | Human-readable channel reference |
| `search/fts.db` | Indexed component retrieval |
| `seal.json` | Identity, hash, validation, and seal metadata |
| `forge_audit.jsonl` | Copied authoring history when available |

Examine the files that matter to the intended task before equipment.
The phase graph, policy, vocabulary, and source citations have separate responsibilities.

## Calibrate retrieval

Author representative queries with their expected component identifiers.
Use a `query`, `expected_top_ids`, optional `kind`, and desired `min_precision_at_5` for each case.

```json
[
  {
    "query": "authentication procedure",
    "expected_top_ids": ["EXPECTED_COMPONENT_ID"],
    "kind": "crumb",
    "min_precision_at_5": 0.2
  }
]
```

Replace the expected identifier with the actual component identity from the sealed weapon.
Precision at five divides matching returned items by five.
Select the threshold for the expected result set.

```sh
ara weapon --state-root .ara calibrate WEAPON_ID --queries calibration_queries.json --min-precision 0.2
```

The command writes `calibration_report.json` under the weapon directory.
The report includes per-query results, mean precision, mean recall, and threshold failures.
Calibration is an explicit operator verification step.
Seal copies an available query file into the weapon for this later command.

## Connect creation to execution

The [end-to-end guide](end-to-end.md) traces a sealed weapon into equipment, preflight, the Hermes loop, evidence, and review.
The [cognitive mechanism guide](cognitive-mechanisms.md) explains retrieval, vocabulary, procedure chunks, and freshness in more detail.
