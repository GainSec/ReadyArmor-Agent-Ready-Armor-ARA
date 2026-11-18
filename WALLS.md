# WALLS

WALLS contains resource rules for the compatibility interface.
A path rule applies to the resource accessed by a tool or process.
`*` matches within one path segment.
`?` matches one character within a segment.
`**` matches across path segments.

## deny_path
~/.ssh/**
~

## deny_command
# Add command patterns to this section when a command restriction is necessary.
