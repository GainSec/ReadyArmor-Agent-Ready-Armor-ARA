# Troubleshooting

## Armory does not start

1. Examine the startup output.
2. Make sure that the selected port is available.
3. Make sure that the state directory is writable.
4. Examine the Python version.
5. Start Armory with an unused port.

```sh
ara armory --state-root .ara --bind 127.0.0.1 --port 4771
```

## The browser reports unauthorized access

1. Read the token from the selected state directory.
2. Use the complete address printed at startup.
3. Use the administrator token for configuration changes.
4. Use the viewing token for observation.

## Armory does not show a worker session

1. Examine the worker's `--state-root` value.
2. Examine the Armory `--state-root` value.
3. Select the same directory for both processes.
4. Examine the session list.

```sh
ara session list --state-root .ara
```

## The native guard is absent

1. Make sure that Make and a C compiler are installed.
2. Build the native guard.

   ```sh
   make -C src/guard/libc_shim
   ```

3. Examine the build output for errors.
4. Start a new contained session.

## A policy rule has no effect

1. Verify the policy syntax.
2. Examine the rule scope.
3. Examine higher-priority matching rules.
4. Make sure that the worker uses the expected interception path.
5. Examine the session audit events.

```sh
ara bulwark verify --state-root .ara
ara bulwark list --state-root .ara
```

## A task stops at a chunk boundary

1. Open the task detail.
2. Read the last channel error.
3. Examine unresolved evidence or finding requirements.
4. Complete the necessary channel operation.
5. Use the task recovery control.

## Material extraction is incomplete

1. Examine the manifest and coverage records.
2. Identify the unsupported file format.
3. Install the corresponding optional extractor.
4. Install Tesseract when OCR is necessary.
5. Run extraction again for the unresolved material.

## Collect diagnostic data

Record the host system, Python version, worker version, and ARA version.
Record the exact command and its result.
Remove tokens and private content from diagnostic excerpts.

```sh
ara --version
ara debug-log --help
```
