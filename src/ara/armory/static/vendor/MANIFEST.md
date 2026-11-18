# Vendored static assets

Armory and Forge use these local JavaScript libraries.
The application serves these files without a content delivery network.

| File | Version | License | sha256 | Source |
|---|---|---|---|---|
| cytoscape.min.js | 3.30.2 | MIT | 83e8c54a6bec655bfd81df07df605649c268af69aeca67a5ea2da54ea42dac81 | https://unpkg.com/cytoscape@3.30.2/dist/cytoscape.min.js |
| marked.min.js | 14.1.3 | MIT | 1092c3ce33dd737edd02e780263f1476bf18278d81817af6403df6840f8034ab | https://cdn.jsdelivr.net/npm/marked@14.1.3/marked.min.js |

## Update a library

1. Download the selected library version from its source.
2. Calculate the downloaded file's SHA-256 hash.
3. Update the version and hash in this table.
4. Examine the affected Armory and Forge pages.
