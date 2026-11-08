# Command reference

```{click} tesla.cli:cli
:prog: tesla
:nested: full
```

## FILES

The command and its state live in the platform per-user config directory.
Run `tesla --config` to print the exact path of `config.toml`.

- `config.toml`: configuration file. Missing means built-in defaults.
- `tokens.json`: OAuth refresh token, mode 0600.
- `private_key.pem`: EC private key for signed commands, mode 0600.
- `notify_state.json`: notification situation and cooldown state.
- `auto-charge.lock`: single-instance lock for `tesla auto-charge`.

Typical paths:

- Linux: `~/.config/tesla/config.toml`
- macOS: `~/Library/Application Support/tesla/config.toml`
- Windows: `%LOCALAPPDATA%\tesla\config.toml`

`tesla --completions` writes
`${BASH_COMPLETION_USER_DIR:-${XDG_DATA_HOME:-~/.local/share}}/bash-completion/completions/tesla.bash`.

## ENVIRONMENT

- `TESLA_CLIENT_ID`, `TESLA_CLIENT_SECRET`: Tesla application credentials.
- `TESLA_REDIRECT_URI`: OAuth redirect URI; defaults to
  `http://localhost:8085/callback`.
- `TESLA_NTFY_TOPIC`, `TESLA_NTFY_TOKEN`, `TESLA_NTFY_URL`: ntfy settings.
- `NO_COLOR`, `FORCE_COLOR`, `TERM`: color detection when
  `--color=auto`. `--color` takes precedence over both variables.
- `BASH_COMPLETION_USER_DIR`, `XDG_DATA_HOME`, `HOME`: completion
  destination.
- `XDG_CONFIG_HOME`, `HOME`, `APPDATA`: platform config directory.

## NOTES

Bash completions require bash >= 4.4. Automatic loading requires
bash-completion >= 2.18 and the `.bash` suffix. On macOS, `/bin/bash` is
3.2; install a newer bash (for example `brew install bash`). zsh is not
supported.

## SEE ALSO

`tesla --help`, `tesla --version`, and
<https://github.com/8cylinder/tesla>.
