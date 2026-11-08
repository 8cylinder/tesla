# Contributing

Issues and pull requests are welcome at
<https://github.com/8cylinder/tesla>.

## Development

```sh
make sync
make fmt
make lint
make type
make test
make check
```

`make check` must pass without modifying source files. Add tests for
behavior changes and keep the CLI in `src/tesla/cli.py` thin: put logic in
the other modules.

## Commit style

Use a short imperative subject line. Mention user-visible behavior and
configuration changes in the body when relevant.
