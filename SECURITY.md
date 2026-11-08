# Security

Do not open a public issue for a credential leak or another exploitable
problem. Report it privately through GitHub's security advisory flow:

<https://github.com/8cylinder/tesla/security/advisories/new>

Include the affected version, a minimal reproduction, and any relevant
logs with secrets removed. Never paste Tesla client secrets, refresh
tokens, private keys, or ntfy tokens into an issue.

The CLI stores secrets only in the platform config directory and never
logs configuration values.
