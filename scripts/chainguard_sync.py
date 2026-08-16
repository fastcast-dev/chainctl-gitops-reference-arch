#!/usr/bin/env python3
"""chainguard_sync — governed, vendor-neutral wrapper around chainctl.

Reads the tiered YAML config in ./config and reconciles a Chainguard org:

  Phase 1 (repos):  ensure every image in images.yaml exists in the org
                    (`chainctl images repos create`)
  Phase 2 (builds): apply Custom Assembly — org certs, custom APK runtime
                    repositories (global.yaml) merged with per-image
                    overlays (config/images/*.yaml) — via
                    `chainctl images repos build apply`

Subcommands:
  lint    validate YAML structure, tickets, cert files, reserved prefixes
  plan    dry-run reconcile; exit 0 = in sync, 2 = changes pending, 1 = error
  apply   reconcile for real (parallel, non-interactive)
  report  markdown/json state report for PR comments or ticket attachments

No CI-specific logic lives here: authentication is ambient (chainctl auth
login must have happened — OIDC assumable identity in CI, browser login on
a workstation). Only dependency beyond the stdlib is PyYAML.
"""

import argparse
import concurrent.futures
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("error: PyYAML is required (pip install pyyaml)")

REPO_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
RESERVED_ENV_PREFIX = "CHAINGUARD_"
RESERVED_ANNOTATION_PREFIX = "dev.chainguard"

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass
class ImageEntry:
    name: str
    ticket: str = ""
    description: str = ""
    source: str = ""


@dataclass
class Overlay:
    image: str
    path: Path
    ticket: str = ""
    approved_by: str = ""
    packages: list = field(default_factory=list)
    environment: dict = field(default_factory=dict)
    annotations: dict = field(default_factory=dict)
    accounts: dict = field(default_factory=dict)
    save_as: str = ""


@dataclass
class Config:
    root: Path
    org: str
    concurrency: int
    render_dir: Path
    ticket_required: bool
    ticket_pattern: str
    ann_ticket_key: str
    ann_managed_key: str
    ann_managed_value: str
    images: list
    global_certs: list
    global_runtime_repos: list
    global_packages: list
    global_env: dict
    global_annotations: dict
    global_exclude: list
    overlays: dict  # name -> Overlay
    chainctl: str = "chainctl"


@dataclass
class Result:
    target: str
    action: str
    ok: bool
    changed: bool = False
    detail: str = ""
    cmd: str = ""


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _read_yaml(path: Path):
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}")
    except OSError as e:
        raise ConfigError(f"{path}: {e}")


def load_config(config_dir: Path, chainctl_bin: str) -> Config:
    config_dir = config_dir.resolve()
    root = config_dir.parent
    org_cfg = _read_yaml(config_dir / "org.yaml")
    images_cfg = _read_yaml(config_dir / "images.yaml")
    global_path = config_dir / "global.yaml"
    global_cfg = _read_yaml(global_path) if global_path.exists() else {}

    org = str(org_cfg.get("org") or "").strip()
    if not org:
        raise ConfigError("config/org.yaml: `org` is required")

    defaults = org_cfg.get("defaults") or {}
    ticket = org_cfg.get("ticket") or {}
    ann = org_cfg.get("annotations") or {}

    images = []
    for raw in images_cfg.get("images-enabled") or []:
        if isinstance(raw, str):
            images.append(ImageEntry(name=raw.strip()))
        elif isinstance(raw, dict):
            images.append(
                ImageEntry(
                    name=str(raw.get("name") or "").strip(),
                    ticket=str(raw.get("ticket") or ""),
                    description=str(raw.get("description") or ""),
                    source=str(raw.get("source") or ""),
                )
            )
        else:
            raise ConfigError(f"config/images.yaml: bad entry: {raw!r}")

    overlays = {}
    overlay_dir = config_dir / "images"
    if overlay_dir.is_dir():
        for path in sorted(overlay_dir.glob("*.yaml")) + sorted(overlay_dir.glob("*.yml")):
            data = _read_yaml(path)
            cust = data.get("customizations") or {}
            ov = Overlay(
                image=str(data.get("image") or "").strip(),
                path=path,
                ticket=str(data.get("ticket") or ""),
                approved_by=str(data.get("approved-by") or ""),
                packages=list(cust.get("packages") or []),
                environment=dict(cust.get("environment") or {}),
                annotations=dict(cust.get("annotations") or {}),
                accounts=dict(cust.get("accounts") or {}),
                save_as=str(cust.get("save-as") or ""),
            )
            overlays[ov.image] = ov

    return Config(
        root=root,
        org=org,
        concurrency=int(defaults.get("concurrency", 8)),
        render_dir=root / str(defaults.get("render-dir", "rendered")),
        ticket_required=bool(ticket.get("required", True)),
        ticket_pattern=str(ticket.get("pattern") or ""),
        ann_ticket_key=str(ann.get("ticket-key") or ""),
        ann_managed_key=str(ann.get("managed-by-key") or ""),
        ann_managed_value=str(ann.get("managed-by-value") or "chainctl-gitops"),
        images=images,
        global_certs=[str(c) for c in (global_cfg.get("certificates") or [])],
        global_runtime_repos=[str(r) for r in (global_cfg.get("runtime-repositories") or [])],
        global_packages=list(global_cfg.get("packages") or []),
        global_env=dict(global_cfg.get("environment") or {}),
        global_annotations=dict(global_cfg.get("annotations") or {}),
        global_exclude=[str(x) for x in (global_cfg.get("exclude") or [])],
        overlays=overlays,
        chainctl=chainctl_bin,
    )


# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------


def _check_ticket(cfg: Config, ticket: str, where: str, errors: list):
    if not ticket:
        if cfg.ticket_required:
            errors.append(f"{where}: missing required `ticket` reference")
        return
    if cfg.ticket_pattern and not re.match(cfg.ticket_pattern, ticket):
        errors.append(f"{where}: ticket {ticket!r} does not match ticket.pattern")


def lint(cfg: Config):
    errors, warnings = [], []
    enabled = set()

    for img in cfg.images:
        where = f"config/images.yaml [{img.name or '?'}]"
        if not img.name:
            errors.append(f"{where}: entry has no name")
            continue
        if not REPO_NAME_RE.match(img.name):
            errors.append(f"{where}: invalid repo name (must match {REPO_NAME_RE.pattern})")
        if img.name in enabled:
            errors.append(f"{where}: duplicate image entry")
        enabled.add(img.name)
        if len(img.description) > 255:
            errors.append(f"{where}: description exceeds 255 characters")
        _check_ticket(cfg, img.ticket, where, errors)

    for cert in cfg.global_certs:
        if not (cfg.root / cert).is_file():
            errors.append(f"config/global.yaml: certificate file not found: {cert}")
    for repo in cfg.global_runtime_repos:
        if not repo.startswith("https://"):
            errors.append(f"config/global.yaml: runtime repository must be HTTPS: {repo}")
    for name in cfg.global_exclude:
        if name not in enabled:
            warnings.append(f"config/global.yaml: exclude lists unknown image: {name}")

    for name, ov in cfg.overlays.items():
        where = str(ov.path.relative_to(cfg.root))
        if not ov.image:
            errors.append(f"{where}: missing `image` key")
            continue
        if ov.path.stem != ov.image:
            errors.append(f"{where}: filename must match image name ({ov.image})")
        if ov.image not in enabled:
            errors.append(
                f"{where}: customizes {ov.image!r} which is not in images-enabled "
                "(add it to config/images.yaml first)"
            )
        _check_ticket(cfg, ov.ticket, where, errors)
        if ov.save_as and not REPO_NAME_RE.match(ov.save_as):
            errors.append(f"{where}: invalid save-as repo name: {ov.save_as}")
        if ov.save_as and ov.save_as in enabled:
            errors.append(f"{where}: save-as {ov.save_as!r} collides with an enabled image")
        for pkg in ov.packages:
            if not isinstance(pkg, str) or not pkg.strip():
                errors.append(f"{where}: packages must be non-empty strings: {pkg!r}")
        for env_map, src in ((ov.environment, where), (cfg.global_env, "config/global.yaml")):
            for key in env_map:
                if str(key).startswith(RESERVED_ENV_PREFIX):
                    errors.append(f"{src}: environment key {key!r} uses reserved prefix")
        for ann_map, src in ((ov.annotations, where), (cfg.global_annotations, "config/global.yaml")):
            for key in ann_map:
                if str(key).startswith(RESERVED_ANNOTATION_PREFIX):
                    errors.append(f"{src}: annotation key {key!r} uses reserved prefix")

    return errors, warnings


# ---------------------------------------------------------------------------
# Rendering (merge tiers 2 + 3 into a Custom Assembly config per image)
# ---------------------------------------------------------------------------


def desired_build(cfg: Config, img: ImageEntry):
    """Return (ca_config dict, cert_paths, save_as) or None if no customization applies."""
    ov = cfg.overlays.get(img.name)
    use_global = img.name not in cfg.global_exclude

    packages = list(dict.fromkeys(
        ([*cfg.global_packages] if use_global else []) + (ov.packages if ov else [])
    ))
    runtime_repos = cfg.global_runtime_repos if use_global else []
    certs = cfg.global_certs if use_global else []
    env = {**(cfg.global_env if use_global else {}), **(ov.environment if ov else {})}
    annotations = {
        **(cfg.global_annotations if use_global else {}),
        **(ov.annotations if ov else {}),
    }
    ticket = (ov.ticket if ov and ov.ticket else img.ticket)
    if cfg.ann_ticket_key and ticket:
        annotations[cfg.ann_ticket_key] = ticket
    if cfg.ann_managed_key:
        annotations[cfg.ann_managed_key] = cfg.ann_managed_value

    if not any([packages, runtime_repos, certs, env, ov and ov.accounts]):
        return None  # nothing to assemble for this image

    ca = {}
    contents = {}
    if packages:
        contents["packages"] = packages
    if runtime_repos:
        contents["runtime_repositories"] = runtime_repos
    if contents:
        ca["contents"] = contents
    if env:
        ca["environment"] = {str(k): str(v) for k, v in env.items()}
    if annotations:
        ca["annotations"] = {str(k): str(v) for k, v in annotations.items()}
    if ov and ov.accounts:
        ca["accounts"] = ov.accounts
    return ca, [str(cfg.root / c) for c in certs], (ov.save_as if ov else "")


def render_all(cfg: Config, only=None):
    """Write merged CA configs to render_dir. Returns [(image, yaml_path, certs, save_as)]."""
    cfg.render_dir.mkdir(parents=True, exist_ok=True)
    rendered = []
    for img in cfg.images:
        if only and img.name not in only:
            continue
        desired = desired_build(cfg, img)
        if desired is None:
            continue
        ca, certs, save_as = desired
        path = cfg.render_dir / f"{img.name}.yaml"
        with open(path, "w") as f:
            f.write("# GENERATED by chainguard_sync — do not edit; edit config/ instead.\n")
            yaml.safe_dump(ca, f, sort_keys=True, default_flow_style=False)
        rendered.append((img.name, path, certs, save_as))
    return rendered


# ---------------------------------------------------------------------------
# chainctl plumbing
# ---------------------------------------------------------------------------


def run_chainctl(cfg: Config, args, json_out=False, timeout=900):
    cmd = [cfg.chainctl, *args]
    if json_out:
        cmd += ["-o", "json"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout), " ".join(
        shlex.quote(a) for a in cmd
    )


def existing_repos(cfg: Config):
    proc, cmd = run_chainctl(
        cfg, ["images", "repos", "list", "--parent", cfg.org], json_out=True
    )
    if proc.returncode != 0:
        raise ConfigError(
            f"could not list repos for {cfg.org} (is chainctl authenticated?):\n"
            f"$ {cmd}\n{proc.stderr.strip()}"
        )
    data = json.loads(proc.stdout or "null")
    items = data.get("items", []) if isinstance(data, dict) else (data or [])
    names = set()
    for item in items:
        if isinstance(item, dict) and item.get("name"):
            names.add(str(item["name"]).rsplit("/", 1)[-1])
    return names


def build_apply_args(cfg: Config, repo: str, rendered: Path, certs, save_as, existing):
    """Assemble the `build apply` argv. If the save-as variant already exists,
    target it directly instead of re-creating it from the base repo."""
    target = repo
    args = ["images", "repos", "build", "apply", "--parent", cfg.org, "--file", str(rendered)]
    if save_as:
        if save_as in existing:
            target = save_as
        else:
            args += [f"--save-as={save_as}"]
    args += [f"--repo={target}"]
    for cert in certs:
        args += [f"--with-certificates={cert}"]
    return target, args


def _tail(text: str, lines=6):
    return "\n".join([ln for ln in text.strip().splitlines() if ln.strip()][-lines:])


# ---------------------------------------------------------------------------
# Plan / Apply
# ---------------------------------------------------------------------------


def compute_plan(cfg: Config, only=None):
    existing = existing_repos(cfg)
    results = []

    to_create = [
        img for img in cfg.images
        if (not only or img.name in only) and img.name not in existing
    ]
    for img in cfg.images:
        if only and img.name not in only:
            continue
        if img.name in existing:
            results.append(Result(img.name, "repo", ok=True, changed=False, detail="exists"))
        else:
            results.append(Result(img.name, "repo", ok=True, changed=True,
                                   detail=f"will create (ticket: {img.ticket or 'n/a'})"))

    rendered = render_all(cfg, only=only)

    def dry_run(item):
        name, path, certs, save_as = item
        if name in {i.name for i in to_create}:
            return Result(name, "build", ok=True, changed=True,
                          detail="will apply after repo creation")
        if save_as and save_as not in existing:
            return Result(name, "build", ok=True, changed=True,
                          detail=f"will create variant repo {save_as!r}")
        target, args = build_apply_args(cfg, name, path, certs, save_as, existing)
        proc, cmd = run_chainctl(cfg, args + ["--dry-run"])
        if proc.returncode == 0:
            return Result(target, "build", ok=True, changed=False, detail="in sync", cmd=cmd)
        # chainctl exits non-zero from --dry-run when changes are pending;
        # genuine failures also land here, so keep the output tail visible.
        return Result(target, "build", ok=True, changed=True,
                      detail=_tail(proc.stdout + "\n" + proc.stderr), cmd=cmd)

    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
        results.extend(pool.map(dry_run, rendered))
    return results


def apply_changes(cfg: Config, only=None):
    existing = existing_repos(cfg)
    results = []

    # Phase 1 — create missing repos (parallel, barrier before builds).
    missing = [
        img for img in cfg.images
        if (not only or img.name in only) and img.name not in existing
    ]

    def create(img: ImageEntry):
        args = ["images", "repos", "create", img.name, "--parent", cfg.org]
        if img.description:
            args += ["--description", img.description]
        if img.source:
            args += ["--source", img.source]
        proc, cmd = run_chainctl(cfg, args)
        ok = proc.returncode == 0
        return Result(img.name, "repo-create", ok=ok, changed=ok,
                      detail="created" if ok else _tail(proc.stderr), cmd=cmd)

    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
        results.extend(pool.map(create, missing))
    existing |= {r.target for r in results if r.ok}

    # Phase 2 — Custom Assembly builds (parallel).
    rendered = render_all(cfg, only=only)

    def build(item):
        name, path, certs, save_as = item
        if name not in existing:
            return Result(name, "build-apply", ok=False,
                          detail="skipped: repo does not exist (creation failed?)")
        target, args = build_apply_args(cfg, name, path, certs, save_as, existing)
        proc, cmd = run_chainctl(cfg, args + ["--yes"])
        ok = proc.returncode == 0
        return Result(target, "build-apply", ok=ok, changed=ok,
                      detail="applied" if ok else _tail(proc.stdout + "\n" + proc.stderr),
                      cmd=cmd)

    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
        results.extend(pool.map(build, rendered))
    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def emit(results, fmt: str, title: str):
    if fmt == "json":
        print(json.dumps([r.__dict__ for r in results], indent=2))
        return
    rows = [(r.target, r.action,
             ("OK" if r.ok else "FAILED") + (" (change)" if r.changed and r.ok else ""),
             r.detail.replace("\n", " ")[:100]) for r in results]
    if fmt == "markdown":
        print(f"### {title}\n")
        print("| target | action | status | detail |")
        print("|---|---|---|---|")
        for row in rows:
            print("| " + " | ".join(row) + " |")
    else:
        print(title)
        for row in rows:
            print("  {:<24} {:<14} {:<14} {}".format(*row))
    changed = sum(1 for r in results if r.changed)
    failed = sum(1 for r in results if not r.ok)
    print(f"\n{len(results)} targets, {changed} with changes, {failed} failed")


def cmd_lint(cfg: Config, args):
    errors, warnings = lint(cfg)
    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"error: {e}")
    print(f"\nlint: {len(errors)} error(s), {len(warnings)} warning(s) "
          f"across {len(cfg.images)} image(s), {len(cfg.overlays)} overlay(s)")
    return 1 if errors else 0


def cmd_plan(cfg: Config, args):
    rc = cmd_lint(cfg, args)
    if rc:
        return rc
    results = compute_plan(cfg, only=set(args.only) if args.only else None)
    emit(results, args.output, f"Plan for {cfg.org}")
    if any(not r.ok for r in results):
        return 1
    return 2 if any(r.changed for r in results) else 0


def cmd_apply(cfg: Config, args):
    rc = cmd_lint(cfg, args)
    if rc:
        return rc
    results = apply_changes(cfg, only=set(args.only) if args.only else None)
    emit(results, args.output, f"Apply for {cfg.org}")
    return 1 if any(not r.ok for r in results) else 0


def cmd_report(cfg: Config, args):
    existing = existing_repos(cfg)
    results = []
    for img in cfg.images:
        ov = cfg.overlays.get(img.name)
        parts = []
        if img.name not in cfg.global_exclude and (cfg.global_certs or cfg.global_runtime_repos):
            parts.append("global certs/apk")
        if ov:
            parts.append(f"overlay ({ov.ticket or 'no ticket'})")
        results.append(Result(
            img.name, "state",
            ok=img.name in existing,
            detail=("present" if img.name in existing else "MISSING")
                   + (", customized: " + ", ".join(parts) if parts else ", vanilla"),
        ))
    managed = {i.name for i in cfg.images} | {o.save_as for o in cfg.overlays.values() if o.save_as}
    for orphan in sorted(existing - managed):
        results.append(Result(orphan, "state", ok=True,
                              detail="exists in org but NOT in images.yaml (unmanaged)"))
    emit(results, args.output, f"State report for {cfg.org}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="chainguard-sync", description=__doc__.split("\n")[0])
    parser.add_argument("--config-dir", default="config", type=Path)
    parser.add_argument("--chainctl", default=os.environ.get("CHAINCTL_BIN", "chainctl"))
    sub = parser.add_subparsers(dest="command", required=True)

    for name, fn in (("lint", cmd_lint), ("plan", cmd_plan),
                     ("apply", cmd_apply), ("report", cmd_report)):
        p = sub.add_parser(name)
        p.add_argument("--output", choices=["text", "json", "markdown"], default="text")
        p.add_argument("--only", action="append", metavar="IMAGE",
                       help="limit to specific image(s); repeatable")
        p.set_defaults(fn=fn)

    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config_dir, args.chainctl)
        return args.fn(cfg, args)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
