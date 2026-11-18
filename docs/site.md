# Documentation site

The technical atlas contains the product guides, capability catalog, architecture, operating traces, and source excerpts.
All styles, screenshots, diagrams, scripts, search data, and document content are embedded in `index.html`.
The page works without a product server or an internet connection.

## Publish with GitHub Pages

The published HTML is already included in `docs/index.html`.
GitHub Pages serves the committed `docs/` directory.

1. Push the `main` branch to GitHub.
2. Open the repository's **Settings**.
3. Select **Pages**.
4. Under **Source**, select **Deploy from a branch**.
5. Select the `main` branch.
6. Select the `/docs` folder.
7. Select **Save**.
8. Select **Visit site** when the address is available.

The `docs/.nojekyll` file selects plain static serving.
The Markdown guides, source metadata, and screenshot originals remain repository files.
The publication uses the checked-in HTML rather than a repository deployment workflow.

## Open the atlas locally

Open `docs/index.html` from the repository in a browser.
Use the left navigation to select a guide.
Use Search to find a feature, procedure, concept, or implementation symbol.
Use Control in Depth to examine each layer's boundary, activation, decision scope, and code-path example.
Select a source reference to read its code excerpt.
Select a product screenshot to open the full image.
Use the gallery category links to find a group of views.
Use the arrow buttons or arrow keys to change the view.
Select Actual size to inspect the image at its original resolution.
Press Escape to close the image.
Use the color control to select a light or dark theme.
Use the browser print command to print the full technical edition.

## Update the published content

For future content changes, update the Markdown guides and site profile.
Then run the local builder from the repository directory:

```sh
python3 scripts/build_docs.py
```

The builder reads `docs/site.json` and the referenced Markdown chapters.
It refreshes the generated source file index from the configured implementation directories.
It converts the guides to HTML and embeds screenshots, diagrams, search data, and source excerpts.
Screenshot originals remain in `docs/assets/screenshots/` for the Markdown guides.
The HTML publication embeds each original image once as a data URL.
Repeated chapter images reuse the embedded gallery image.
The builder writes `docs/index.html` and `docs/.nojekyll` beside the existing documentation.
It preserves the hand-authored guides, site profile, and assets.
The build uses the Python standard library.

Commit the updated HTML with the content changes, then push `main`.
GitHub Pages publishes that directory through the selected branch source.

## Verify the builder

```sh
python3 -m unittest discover -s scripts -p test_build_docs.py
```

The test checks links, unique anchors, embedded assets, escaped source, interactive controls, and preserved input files.
It also checks that the builder creates the publication in `docs/`.

## Share one file

Copy `docs/index.html` to another location or device.
Open the copied file in a browser.
The guides, screenshots, diagrams, search, operating traces, and source excerpts remain available in that file.
