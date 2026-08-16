# Organization CA certificates

Place your internal CA certificate bundle(s) here as PEM files and reference
them from `config/global.yaml` under `certificates:`. They are public keys —
safe to commit — and are merged into every image's trust bundle by Custom
Assembly (`--with-certificates`).

> Custom Assembly certificate support is a Beta feature that requires
> enrollment; contact your Chainguard Customer Success team to enable it.

Example:

```
certs/acme-root-ca.pem
certs/acme-intermediate.pem
```
