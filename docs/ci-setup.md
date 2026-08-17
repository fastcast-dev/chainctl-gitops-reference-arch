# CI setup: OIDC identities, roles, and troubleshooting

Everything the GitHub workflows need to authenticate to Chainguard, set up
once by a Chainguard org owner. Total time: ~10 minutes.

Two identities keep privileges split:

| Identity | Used by | When | Role | GitHub variable |
|---|---|---|---|---|
| `chainctl-gitops-apply` | `apply` workflow | push to `main`, nightly drift check | custom write role | `CHAINGUARD_IDENTITY` |
| `chainctl-gitops-plan` | `validate` workflow | every PR | built-in `viewer` (read-only) | `CHAINGUARD_PLAN_IDENTITY` |

Untrusted branches therefore never hold write capabilities: a PR can only
*plan* (dry-run), and only a merge to `main` can *apply*.

Replace `<org>` with your Chainguard organization and `<gh-org>/<repo>`
with your GitHub repository throughout.

## 1. Create a least-privilege apply role

The built-in `owner`/`editor` roles work but grant far more than needed.
This custom role carries only what `cg-sync apply` uses:

```sh
chainctl iam roles create chainctl-gitops-apply \
  --parent=<org> \
  --capabilities=groups.list,repo.create,repo.update,repo.list,manifest.create,tag.list,apk.list,build_report.list
```

> `groups.list` is easy to miss but required: chainctl uses it to resolve
> the org *name* in `--parent <org>` to its ID. Without it every command
> fails with `No folder found for "<org>"` (see troubleshooting below).

## 2. Create the apply identity (main branch only)

```sh
chainctl iam identities create github chainctl-gitops-apply \
  --github-repo=<gh-org>/<repo> \
  --github-ref=refs/heads/main \
  --parent=<org> \
  --role=chainctl-gitops-apply \
  -o id
```

Copy the UIDP it prints (`<org-id>/<identity-id>`) and store it:

```sh
gh variable set CHAINGUARD_IDENTITY --body "<UIDP>" -R <gh-org>/<repo>
```

The `--github-ref` pin means only workflow runs on `main` can assume this
identity — a workflow run from a branch or fork presents a different OIDC
subject and is rejected.

## 3. Create the read-only plan identity (PRs)

PR runs of `validate` only need to *read* the org to compute a dry-run
diff, so bind them to the built-in `viewer` role. GitHub issues a
different OIDC subject for `pull_request` events
(`repo:<gh-org>/<repo>:pull_request` — no ref), so match it explicitly:

```sh
chainctl iam identities create chainctl-gitops-plan \
  --parent=<org> \
  --identity-issuer=https://token.actions.githubusercontent.com \
  --subject-pattern='^repo:<gh-org>(@\d+)?/<repo>(@\d+)?:pull_request$' \
  --role=viewer \
  -o id

gh variable set CHAINGUARD_PLAN_IDENTITY --body "<UIDP>" -R <gh-org>/<repo>
```

The `(@\d+)?` groups tolerate GitHub's ID-embedded subject format (next
section). To scope plan runs to specific branches instead of PR events,
use a ref-based pattern, e.g. any branch:
`^repo:<gh-org>(@\d+)?/<repo>(@\d+)?:ref:refs/heads/.+$`.

## 4. Verify

Open a PR touching `config/` — the `plan` job should post a comment.
Merge it (or `gh workflow run apply`) — the `apply` job should reconcile.
Until the variables are set, both jobs skip cleanly rather than fail.

## Troubleshooting

### `token has invalid subject: repo:<gh-org>@123456/<repo>@7891011:ref:...`

```
Error: [101] unable to exchange tokens: rpc error: code = PermissionDenied
desc = token has invalid subject: repo:acme@313680291/shop@1336454962:ref:refs/heads/main
```

GitHub can embed **immutable org/repo IDs** in the OIDC subject
(`acme@313680291` instead of `acme`). `chainctl iam identities create
github` registers the classic ID-less subject, so the exact match fails.

Fix: update the identity to the exact subject from the error message —
it is copy-pasteable, and pinning to IDs is *stronger* (the trust survives
nothing, and is spoofable by nobody, through repo rename/delete/recreate):

```sh
chainctl iam identities update <org-id>/<identity-id> \
  --subject="repo:<gh-org>@<org-id>/<repo>@<repo-id>:ref:refs/heads/main" \
  --yes
```

Or, when creating identities by hand, use a `--subject-pattern` with
optional ID groups as shown in step 3.

Find the identity ID with `chainctl iam identities list --parent <org>`.

### `No folder found for "<org>"`

Authentication succeeded but the role bound to the identity lacks
`groups.list`, so chainctl cannot resolve the org name to its ID:

```sh
chainctl iam roles update chainctl-gitops-apply --add-capabilities=groups.list --yes
```

### `plan`/`apply` job skipped

The `CHAINGUARD_PLAN_IDENTITY` / `CHAINGUARD_IDENTITY` repository variable
is unset (Settings → Secrets and variables → Actions → Variables). This is
the intended state for a fresh fork — set the variables to activate CI.

### Debugging an identity locally

Impersonate the CI path from your workstation to iterate quickly:

```sh
chainctl auth login --identity <UIDP> --identity-token <oidc-token>
chainctl images repos list --parent <org>   # what CI sees
```

## Chainguard Actions (hardened mirrors)

The workflows consume [Chainguard Actions](https://edu.chainguard.dev/chainguard/actions/overview/)
— securely rebuilt, SHA-pinned mirrors of upstream actions hosted at
[`github.com/chainguard-actions`](https://github.com/chainguard-actions)
(e.g. `actions/checkout` → `chainguard-actions/actions-checkout`). It is
in beta and gated by a per-org entitlement:

```sh
chainctl actions entitlements create --parent <org>   # enable once
chainctl actions entitlements list --parent <org>     # verify
chainctl actions discover .                           # audit this repo's action deps
```

If your GitHub org restricts allowed actions, allowlist
`chainguard-actions/*` and `chainguard-dev/*`.

## Non-GitHub runners

| Runner | Mechanism | Where |
|---|---|---|
| Bitbucket Pipelines | `oidc: true` → `chainctl auth login --identity ... --identity-token` | `examples/ci/bitbucket-pipelines.yml` |
| Jenkins | OIDC plugin, or credential-file token | `examples/ci/Jenkinsfile` |
| cron / VM | IdP-minted token or persisted refresh token | `examples/ci/cron.sh` |
