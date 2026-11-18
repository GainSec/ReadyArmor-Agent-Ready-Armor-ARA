# Contributing to ARA

## Prepare the development environment

1. Create a Python virtual environment.
2. Activate the environment.
3. Install the development dependencies.

   ```sh
   python -m pip install -e '.[dev]' pytest-asyncio
   ```

4. Build the native guard.

   ```sh
   make -C src/guard/libc_shim
   ```

## Verify a change

1. Do the relevant tests.

   ```sh
   python -m pytest -q
   ```

2. Do the Armory smoke check.

   ```sh
   bash scripts/smoke.sh
   ```

3. Examine the output for failed assertions and server errors.
4. Update the product documents for changed behavior.

The smoke script starts a temporary Armory instance on port `4870`.
It checks major routes and token rejection.
Use `ARA_SMOKE_PORT` to select another port.

## Development conventions

Place tests in `tests/` beside the related feature tests.
Use the `short_tmp` fixture for socket paths when the test module provides it.
Unix socket paths have host-specific length limits.
Keep runtime policy and seed data consistent with their parsers.
Keep optional extraction libraries in the `extract` dependency group.

## Report a problem

Include the exact command or request.
Include the observed result and expected result.
Include the host system and relevant version numbers.
Remove credentials and private evidence from examples.

See the [architecture guide](docs/architecture.md) for the source layout.
