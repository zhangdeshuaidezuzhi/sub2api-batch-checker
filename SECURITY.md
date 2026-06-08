# Security

This tool processes local account export files that may contain OAuth access
tokens, refresh tokens, or other credentials.

Please follow these rules:

- Do not commit real token JSON files.
- Do not commit generated `outputs/` files.
- Do not paste token values into issues or logs.
- If a credential is accidentally published, revoke or rotate it immediately.

The default `.gitignore` blocks common token files and generated result bundles,
but you should still review `git status` before every commit.
