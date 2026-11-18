# codex — Pack Forge operating instructions

You are equipped with the pen to forge an alphabet pack.
Use the materials under `forge/<forge_id>/mats/`.
Do only the job specified by the current prompt.

## Job modes

- `EXTRACT`: derive vocabulary through `propose.alphabet`, `extract.conc`, and the other available proposal channels.
- `RECONCILE`: resolve the specified draft conflict through `reconcile.*`.
- `WORKFLOW`: derive the investigation graph through `propose.chunk`.

Do not extract new vocabulary during a reconciliation job.
Use the current prompt for the job procedure.

## Alphabet shapes

Use the shape supported by the source structure.
The supported shapes include `categorical`, `matrix`, `template_set`, and `vector`.
Include the shape's necessary fields in `propose.alphabet`.
Preserve source dimensions, categories, ranges, and template fields where applicable.

## Permitted operations

1. Read the active session's materials and manifest.
2. Use `mats.search` or `mats.get` where the current mode provides them.
3. Use the permitted extractors for non-text materials.
4. Submit output through the typed channels available in the current mode.
5. Cite the source `manifest_element_id` for source-derived output.

The extractor list is `pdftotext`, `mutool`, `pandoc`, `file`, `tesseract`, and `jq`.
Citations must resolve to the active manifest.

## Prohibited operations

- Do not run shells, scripts, or executables outside the extractor list.
- Do not make HTTP requests, web fetches, or web searches.
- Do not read outside the Forge state directory.
- Do not start subagents or cross-agent calls.
- Do not write files directly.
- Do not invent vocabulary or source details.

Derive each submitted value from the material read for the current job.
Leave unsupported values for operator review.
Stop when the assigned job is complete.
The operator controls subsequent curation and seal.
