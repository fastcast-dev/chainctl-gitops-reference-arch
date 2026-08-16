# Chainguard Image Governance (chainctl GitOps)

A vendor-neutral, ticket-governed GitOps workflow for managing your
organization's Chainguard image catalog with `chainctl` — for teams that
can't (or won't) use Terraform. Plain Python + YAML; runs identically from
GitHub Actions, Bitbucket Pipelines, Jenkins, cron, or a laptop.

## How it works

![Architecture](docs/architecture.svg)

A team opens a ticket-linked PR against the tiered YAML config. CI lints
offline and posts a read-only `chainctl --dry-run` plan as a PR comment.
CODEOWNERS (Chainguard org admins + security) approve — the PR approval *is*
the change approval. Merge to `main` triggers `apply`, which authenticates
via OIDC and reconciles in two phases: repo creation, then Custom Assembly
builds (certs, APK endpoints, packages, ticket annotations). A nightly plan
run detects drift, e.g. someone editing an image in the Console.

The diagram source is [`docs/architecture.d2`](docs/architecture.d2);
regenerate the SVG with `make diagram` ([D2](https://d2lang.com) required).

## Getting started

Prerequisites: Python 3.10+, [`chainctl`](https://edu.chainguard.dev/chainguard/chainctl-usage/how-to-install-chainctl/),
and a Chainguard organization you can administer.

1. **Fork or copy this repo** into your own org and clone it.

2. **Install the one dependency** (PyYAML):

   ```sh
   pip install -r requirements.txt
   ```

3. **Point it at your org** — edit `config/org.yaml`:

   ```yaml
   org: your-org.example.com   # your Chainguard org (cgr.dev/<org>)
   ```

   Also review the `ticket.pattern` regex (defaults accept JIRA / SNOW /
   GitHub issue URLs) and the annotation keys, which use an `acme` example
   prefix you'll want to rename.

4. **Describe your catalog** — put the images your org enables in
   `config/images.yaml`, your org-wide baseline (internal CA certs, custom
   APK endpoints) in `config/global.yaml`, and per-image overlays in
   `config/images/<name>.yaml`. Drop your CA PEMs in `certs/` (the included
   `acme-root-ca.pem` is a placeholder — replace it).

5. **Try it locally** against your real org (read-only until `apply`):

   ```sh
   chainctl auth login          # browser-based login
   ./bin/cg-sync lint           # offline validation
   ./bin/cg-sync plan           # dry-run diff vs live org (exit 2 = changes)
   ./bin/cg-sync apply          # reconcile for real
   ```

6. **Wire up CI** — create an assumable identity (see
   [Identity setup](#identity-setup-once-by-an-org-owner) below) and set its
   UIDP as the `CHAINGUARD_IDENTITY` repository variable
   (GitHub → Settings → Secrets and variables → Actions → Variables). The
   included workflows then run automatically: `validate` on PRs, `apply` on
   merge to `main` plus a nightly drift check. Update `.github/CODEOWNERS`
   with your real reviewer teams. Non-GitHub runners live in `examples/ci/`.

7. **Make your first change** via PR: add an image to `config/images.yaml`
   with a `ticket:`, watch the plan comment appear, approve, merge — the
   repo shows up at `cgr.dev/<org>/<image>`.

## The three config tiers

| Tier | File | What it governs | chainctl command |
|---|---|---|---|
| 0 | `config/org.yaml` | Org name, ticket policy, concurrency, traceability annotations | — |
| 1 | `config/images.yaml` | **Which catalog images the org enables** (`images-enabled:`) | `chainctl images repos create <name> --parent <org>` |
| 2 | `config/global.yaml` | **Org-wide Custom Assembly baseline**: internal CA certs, custom APK endpoints (e.g. your JFrog APK remote replacing `virtualapk.cgr.dev`), org-wide packages/env | `chainctl images repos build apply --file ... --with-certificates ...` |
| 3 | `config/images/<image>.yaml` | **Per-image, per-use-case customizations** (e.g. `jq` on `jdk` for JIRA-2201), optional `save-as:` variants | merged over tier 2, same `build apply` |

Tier 3 overlays are merged on top of tier 2 into a single rendered Custom
Assembly config per image (written to `rendered/`, uploaded as a CI artifact
so reviewers can see the exact YAML sent to Chainguard).

## Governance model

- **Every change cites a ticket** (SNOW / JIRA / GitHub issue). `lint` fails
  if `ticket:` is missing or doesn't match the pattern in `org.yaml`.
- **Tickets are baked into the images** as OCI annotations
  (`com.acme.governance/change-ticket`), so a running container is traceable
  back to its change record.
- **PR review is the approval gate**: `.github/CODEOWNERS` routes tier 1/2
  changes to Chainguard org owners/admins and security. On merge, CI applies.
- **Drift detection**: `plan` exits `2` when live state differs from git
  (using `chainctl`'s native `--dry-run`); a nightly scheduled run fails on
  drift, e.g. when someone edited an image in the Console.

## Usage

```sh
pip install -r requirements.txt        # PyYAML only

./bin/cg-sync lint                     # offline: schema, tickets, cert files
./bin/cg-sync plan                     # dry-run vs live org (exit 2 = changes)
./bin/cg-sync apply                    # reconcile (parallel, non-interactive)
./bin/cg-sync apply --only jdk         # scope to one image
./bin/cg-sync report --output markdown # state report, flags unmanaged repos
```

Exit codes: `0` in sync / success, `1` error, `2` (plan only) changes pending —
designed for CI gating.

`apply` runs phase 1 (repo creation) to completion first, then phase 2
(Custom Assembly builds) — both phases fan out in parallel, bounded by
`defaults.concurrency` in `org.yaml`.

## Adding an image (the everyday flow)

1. Team opens a SNOW/JIRA/GitHub ticket for the request.
2. PR adds the entry:
   ```yaml
   # config/images.yaml
   - name: redis
     ticket: JIRA-4321
   ```
3. CI lints and posts the plan (`will create (ticket: JIRA-4321)` + the
   Custom Assembly dry-run diff) as a PR comment.
4. Chainguard admins approve via CODEOWNERS; merge triggers `apply`.
5. Repo appears at `cgr.dev/<org>/redis`, already carrying org certs and
   APK endpoints from `global.yaml`.

Per-use-case customization is the same flow with a
`config/images/<name>.yaml` overlay; use `save-as:` when only one team
should get the tool-enriched variant (e.g. `jdk` stays pristine,
`jdk-tools` gets `jq`).

## Authentication (deliberately out of scope for the tool)

The Python tool assumes an already-authenticated `chainctl`; each runner
brings its own auth:

| Runner | Mechanism | Where |
|---|---|---|
| GitHub Actions | OIDC → `chainguard-dev/setup-chainctl` with an [assumable identity](https://edu.chainguard.dev/chainguard/administration/iam-organizations/assumable-ids/) | `.github/workflows/` |
| Bitbucket Pipelines | `oidc: true` → `chainctl auth login --identity ... --identity-token` | `examples/ci/bitbucket-pipelines.yml` |
| Jenkins | OIDC plugin, or credential-file token | `examples/ci/Jenkinsfile` |
| cron / VM | IdP-minted token or persisted refresh token | `examples/ci/cron.sh` |
| Workstation | `chainctl auth login` (browser) | — |

### Identity setup (once, by an org owner)

```sh
# Example: identity assumable by this GitHub repo's main branch
chainctl iam identities create github chainctl-gitops \
  --github-repo=acme/chainguard-image-governance \
  --github-ref=refs/heads/main \
  --parent=<org> \
  --role=owner   # or a custom role: repo.create, repo.update, repo.list,
                 # manifest.create, tag.list, apk.list, build_report.list
```

Create a second, `viewer`-role identity for PR `plan` runs so untrusted
branches get read-only access. Store the identity UIDPs as CI variables
(`CHAINGUARD_IDENTITY`).

## Repo layout

```
config/
  org.yaml            tier 0 — org + policy settings
  images.yaml         tier 1 — images-enabled list (ticket per image)
  global.yaml         tier 2 — certs, APK runtime repos, org-wide packages
  images/*.yaml       tier 3 — per-image overlays (ticket per use-case)
certs/                internal CA PEMs referenced by global.yaml
scripts/chainguard_sync.py   the tool (stdlib + PyYAML)
bin/cg-sync           bash entrypoint
.github/workflows/    GitHub Actions: validate (PR) + apply (merge/nightly)
examples/ci/          Bitbucket, Jenkins, cron adapters
rendered/             (gitignored) generated per-image CA configs
```

## Notes & caveats

- **Custom Assembly certificates are Beta** and require enrollment — ask
  your Chainguard Customer Success team.
- `runtime-repositories` **replace** the default `virtualapk.cgr.dev`
  entries in `/etc/apk/repositories`; list every endpoint you need, HTTPS only.
- Packages added in overlays must exist in Chainguard's APK repository
  (`chainctl` validates on apply; the PR plan surfaces failures early).
- `report` flags repos that exist in the org but aren't in `images.yaml` —
  useful for onboarding this workflow onto an org with pre-existing repos.
- Environment keys prefixed `CHAINGUARD_` and annotation keys prefixed
  `dev.chainguard` are reserved and rejected by `lint`.
