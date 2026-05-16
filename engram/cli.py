"""Engram CLI — minimal surface for v0.2.0.

  engram init    — create config, prepare data dir
  engram status  — graph stats
  engram serve   — start the MCP server
  engram assess  — debug entrypoint for blast_radius from the terminal
"""

from __future__ import annotations

import json
import logging
import sys

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from engram.config import load_config, write_default_user_config
from engram.db import open_db, integrity_check, schema_version, has_vector_support

console = Console()
logger = logging.getLogger(__name__)


@click.group()
@click.option("--debug", is_flag=True, help="Verbose logging")
@click.version_option()
def cli(debug: bool) -> None:
    """Engram — Local-first DevOps context and blast-radius layer for AI agents."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


@cli.command()
def init() -> None:
    """Initialize ~/.engram/, create config, bootstrap the DB."""
    cfg_path = write_default_user_config()
    cfg = load_config()
    conn = open_db(cfg)
    vec = has_vector_support(conn)
    ver = schema_version(conn)

    console.print(Panel(
        f"[bold green]Engram initialized.[/bold green]\n\n"
        f"  Config:           {cfg_path}\n"
        f"  Data dir:         {cfg.data_dir}\n"
        f"  Database:         {cfg.db_path}\n"
        f"  Schema version:   {ver}\n"
        f"  Vector support:   {'yes (sqlite-vec)' if vec else 'no (lexical-only mode)'}\n\n"
        "[bold]Next:[/bold]\n"
        "  1. Edit ~/.engram/config.yaml — add your repo paths to watch_paths.\n"
        "  2. Run [bold]engram index[/bold] (lands in next commit).\n"
        "  3. Run [bold]engram serve[/bold] and wire into Claude Code / Cursor.\n",
        title="Engram",
        border_style="green",
    ))


@cli.command()
def status() -> None:
    """Show database stats and integrity."""
    from engram.graph import graph_stats

    cfg = load_config()
    if not cfg.db_path.exists():
        console.print(
            "[yellow]No database yet. Run [bold]engram init[/bold] first.[/yellow]"
        )
        return
    conn = open_db(cfg)
    stats = graph_stats(conn)

    table = Table(title="Engram Status", border_style="green")
    table.add_column("Table", style="bold")
    table.add_column("Rows", justify="right")
    for k, v in stats.items():
        table.add_row(k, str(v))
    console.print(table)

    ok = integrity_check(conn)
    console.print(f"  Integrity: [{'green' if ok else 'red'}]{ 'ok' if ok else 'CORRUPTED'}[/{'green' if ok else 'red'}]")
    console.print(f"  Vector:    {'yes' if has_vector_support(conn) else 'no — running lexical-only'}")
    console.print(f"  Schema:    v{schema_version(conn)}")
    console.print(f"  Database:  {cfg.db_path}")


@cli.command()
@click.option("--path", "-p", "paths", multiple=True, type=click.Path(exists=True, file_okay=False),
              help="Path(s) to index. Repeatable. Overrides watch_paths.")
@click.option("--force", is_flag=True, help="Re-index files regardless of content hash.")
def index(paths: tuple[str, ...], force: bool) -> None:
    """Crawl filesystem, extract resources/entities, build the graph."""
    import time
    from pathlib import Path
    from engram.crawler import index_paths

    cfg = load_config()
    conn = open_db(cfg)
    target_paths = [Path(p) for p in paths] if paths else cfg.watch_paths
    if not target_paths:
        console.print("[yellow]No paths to index. Pass --path or edit ~/.engram/config.yaml.[/yellow]")
        sys.exit(1)

    console.print(f"\n[bold]Indexing[/bold] {len(target_paths)} path(s)...")
    for p in target_paths:
        console.print(f"  - {p}")
    t0 = time.time()
    with console.status("[bold green]Crawling..."):
        stats = index_paths(conn, cfg, paths=target_paths, force=force)
    elapsed = time.time() - t0

    table = Table(title="Indexing Complete", border_style="green", show_header=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Files scanned",       str(stats.files_scanned))
    table.add_row("Files indexed",       str(stats.files_indexed))
    table.add_row("Files skipped",       str(stats.files_skipped))
    table.add_row("Projects found",      str(stats.projects_found))
    table.add_row("Resources extracted", str(stats.resources_extracted))
    table.add_row("Entities extracted",  str(stats.entities_extracted))
    table.add_row("Edges created",       str(stats.edges_created))
    table.add_row("Technologies",        str(len(stats.technologies)))
    table.add_row("Time",                f"{elapsed:.1f}s")
    console.print(table)

    if stats.extractor_hits:
        ext_table = Table(title="Extractor Coverage", border_style="cyan")
        ext_table.add_column("Extractor", style="bold")
        ext_table.add_column("Files", justify="right")
        for name, n in sorted(stats.extractor_hits.items(), key=lambda x: -x[1]):
            ext_table.add_row(name, str(n))
        console.print(ext_table)


@cli.command()
@click.confirmation_option(prompt="This wipes the Engram database. Continue?")
def clear() -> None:
    """Drop and recreate the database. Destructive."""
    cfg = load_config()
    if cfg.db_path.exists():
        cfg.db_path.unlink()
        console.print(f"[green]Dropped {cfg.db_path}[/green]")
    conn = open_db(cfg)
    console.print(f"[green]Fresh DB initialized at {cfg.db_path}[/green]")


@cli.command(name="emit-agents-md")
@click.option("--target", "-t", "target_path", type=click.Path(),
              default="AGENTS.md", show_default=True,
              help="Path to write/update. AGENTS.md / CLAUDE.md / INFRA.md.")
@click.option("--force", is_flag=True, help="Overwrite even if existing section was tampered.")
def emit_agents_md(target_path: str, force: bool) -> None:
    """Write or update the engram-managed section of AGENTS.md."""
    from pathlib import Path
    from engram.output.codified import upsert_agents_md

    cfg = load_config()
    conn = open_db(cfg)
    result = upsert_agents_md(conn, cfg, Path(target_path), force=force)
    if result["action"] == "tampered":
        console.print(
            f"[red]REFUSED:[/red] {result['path']} — existing engram section was modified "
            "after Engram wrote it. Pass --force to overwrite, or review the diff first."
        )
        sys.exit(2)
    color = {"created": "green", "updated": "green",
             "unchanged": "yellow"}.get(result["action"], "white")
    console.print(f"[{color}]{result['action']}[/{color}]: {result['path']}")
    if result.get("signature"):
        console.print(f"  signature: {result['signature']}")


@cli.command(name="check-drift")
@click.option("--target", "-t", "target_path", type=click.Path(exists=True),
              default="AGENTS.md", show_default=True)
@click.option("--diff", is_flag=True, help="Show the diff between on-disk and current.")
def check_drift(target_path: str, diff: bool) -> None:
    """Compare an emitted AGENTS.md against the current graph state."""
    from pathlib import Path
    from engram.output.codified import detect_drift, render_drift_diff

    cfg = load_config()
    conn = open_db(cfg)
    result = detect_drift(conn, cfg, Path(target_path))
    if result.get("drift"):
        console.print(f"[yellow]DRIFT[/yellow]: {result['path']} — "
                      f"{result.get('reason', 'on-disk section does not match current graph')}")
        if diff:
            console.print(render_drift_diff(conn, Path(target_path)))
        sys.exit(1)
    else:
        console.print(f"[green]up to date[/green]: {result['path']}")


@cli.command()
@click.option("--target", "-t", "target_kind",
              type=click.Choice(["k8s", "terraform", "all"]),
              default="all", show_default=True,
              help="Which source-file format to annotate.")
@click.option("--apply", "apply_flag", is_flag=True,
              help="Actually write to source files. Without this, runs as dry-run.")
@click.option("--only-red", is_flag=True,
              help="Only annotate resources in production (risk_tier=red).")
def label(target_kind: str, apply_flag: bool, only_red: bool) -> None:
    """Write engram.io/* labels onto source files (K8s) and tags (Terraform).

    By default DRY RUN. Add --apply to write to disk. Use `engram unlabel`
    to reverse every change.
    """
    from engram.output.annotate import plan_annotations, apply_plan

    cfg = load_config()
    conn = open_db(cfg)
    plan = plan_annotations(conn, target_kind=target_kind, only_red=only_red)
    if not plan.ops and not plan.skipped:
        console.print("[yellow]No resources to annotate. Did you `engram index` first?[/yellow]")
        return
    summary = plan.summary()
    console.print(f"Plan: [bold]{len(plan.ops)}[/bold] resource(s) "
                  f"({', '.join(f'{k}={v}' for k, v in summary.items())})")
    if plan.skipped:
        console.print(f"[dim]Skipped {len(plan.skipped)} resource(s) (untaggable types):[/dim]")
        for kind, fp, reason in plan.skipped[:10]:
            console.print(f"  [dim]skip[/dim]  {kind}  [dim]{reason}[/dim]")
        if len(plan.skipped) > 10:
            console.print(f"  [dim]... and {len(plan.skipped) - 10} more[/dim]")
    for op in plan.ops[:20]:
        console.print(
            f"  {op.target_kind:9}  {op.target_path}  "
            f"[dim]{op.rationale}[/dim]"
        )
    if len(plan.ops) > 20:
        console.print(f"  ... and {len(plan.ops) - 20} more")

    result = apply_plan(conn, plan, dry_run=not apply_flag)
    mode = "DRY RUN" if result["dry_run"] else "APPLIED"
    color = "yellow" if result["dry_run"] else "green"
    console.print(f"\n[{color}]{mode}[/{color}] — files would change: "
                  f"{len(result['changed'])}, no-op: {len(result['noop'])}, "
                  f"errors: {len(result['errors'])}")
    for fp in result["changed"][:20]:
        console.print(f"  changed: {fp}")
    for fp, err in result["errors"]:
        console.print(f"  [red]error: {fp} — {err}[/red]")


@cli.command()
@click.option("--apply", "apply_flag", is_flag=True,
              help="Actually remove the labels. Without this, runs as dry-run.")
def unlabel(apply_flag: bool) -> None:
    """Remove every engram.io/* label/tag Engram has applied."""
    from engram.output.annotate import unlabel_all
    cfg = load_config()
    conn = open_db(cfg)
    result = unlabel_all(conn, dry_run=not apply_flag)
    mode = "DRY RUN" if result["dry_run"] else "APPLIED"
    color = "yellow" if result["dry_run"] else "green"
    console.print(f"[{color}]{mode}[/{color}] — would change "
                  f"{len(result['changed'])} file(s), no-op {len(result['noop'])}.")
    for fp in result["changed"]:
        console.print(f"  {fp}")


@cli.command()
def serve() -> None:
    """Start the MCP stdio server."""
    console.print("[bold]Starting Engram MCP server (stdio)...[/bold]", file=sys.stderr)
    console.print("[dim]Use Ctrl+C to stop.[/dim]\n", file=sys.stderr)
    try:
        from engram.mcp_server import run_server
        run_server()
    except KeyboardInterrupt:
        console.print("\n[yellow]Server stopped.[/yellow]", file=sys.stderr)


@cli.command()
@click.argument("target", required=True)
@click.option("--env", "environment", default=None,
              help="Set environment (production / staging / dev / ...).")
@click.option("--owner", default=None, help="Owning team / person.")
@click.option("--runbook", default=None, help="URL of the on-call runbook.")
@click.option("--note", default=None, help="Free-text note shown in assess output.")
@click.option("--key", "extra_key", default=None, help="Custom annotation key.")
@click.option("--value", "extra_value", default=None,
              help="Custom annotation value (paired with --key).")
@click.option("--remove", is_flag=True, default=False,
              help="Remove all user annotations for this target instead of setting.")
def annotate(target: str, environment: str | None, owner: str | None,
             runbook: str | None, note: str | None,
             extra_key: str | None, extra_value: str | None,
             remove: bool) -> None:
    """Bolt your knowledge onto a discovered resource.

    For everything Engram can't auto-detect (click-ops ECS services, an EC2
    that "just runs prod", an RDS with no tags), tell Engram once. Every
    future `assess` and `infra_context` call uses the annotation.

    The TARGET can be:

        \b
        resource UID:               46c1f3e828e25c97f2cf
        kind + name:                aws:rds:DBInstance:payments-prod
        kind + namespace + name:    k8s:Deployment:prod:payments
        bare name (if unique):      payments-prod

    Examples:

        \b
        engram annotate aws:rds:DBInstance:payments-prod --env production --owner platform
        engram annotate k8s:Deployment:prod:payments --runbook https://example.com/run
        engram annotate ec2-12345 --note "legacy box, ssh as ubuntu, redeploy via fab"
    """
    from engram.graph import get_user_annotations, remove_user_annotation, upsert_user_annotation

    cfg = load_config()
    if not cfg.db_path.exists():
        console.print("[yellow]No database. Run [bold]engram init[/bold] first.[/yellow]")
        sys.exit(1)
    conn = open_db(cfg)

    target_uid = _resolve_annotation_target(conn, target)
    if not target_uid:
        console.print(f"[red]No resource matched '{target}'.[/red]")
        console.print(
            "Try `engram status` to confirm the resource exists, or one of the "
            "TARGET shapes shown in --help."
        )
        sys.exit(1)

    if remove:
        n = remove_user_annotation(conn, target_uid)
        console.print(f"[green]Removed {n} annotation(s) from {target_uid}.[/green]")
        return

    updates: list[tuple[str, str]] = []
    if environment:
        updates.append(("environment", environment))
    if owner:
        updates.append(("owner", owner))
    if runbook:
        updates.append(("runbook", runbook))
    if note:
        updates.append(("note", note))
    if extra_key and extra_value:
        updates.append((extra_key, extra_value))

    if not updates:
        existing = get_user_annotations(conn, target_uid)
        if not existing:
            console.print(
                "[yellow]No annotation flags given and no existing annotations to show.[/yellow]\n"
                "Use --env, --owner, --runbook, --note, or --key/--value."
            )
            return
        table = Table(title=f"Annotations on {target_uid}", border_style="blue", show_header=True)
        table.add_column("Key", style="bold")
        table.add_column("Value")
        for k, v in sorted(existing.items()):
            table.add_row(k, v)
        console.print(table)
        return

    for key, value in updates:
        upsert_user_annotation(conn, target_uid=target_uid, key=key, value=value)
    console.print(
        f"[green]Annotated[/green] {target_uid} with: "
        + ", ".join(f"{k}={v!r}" for k, v in updates)
    )


def _resolve_annotation_target(conn, target: str) -> str | None:
    """Resolve a user-facing TARGET string to a resource UID.

    Tries (in order):
      1. literal UID match
      2. `kind:name`  or  `kind:namespace:name`
      3. unique `name` match
    """
    # 1. literal UID.
    row = conn.execute("SELECT uid FROM resource WHERE uid = ?", (target,)).fetchone()
    if row:
        return row["uid"]

    # 2. structured kind[:namespace]:name.
    if ":" in target:
        parts = target.split(":")
        # Try as kind:name (kind has its own colons like aws:rds:DBInstance:foo).
        # We greedily consume from the start: try the last token as `name`, the rest as `kind`.
        if len(parts) >= 2:
            name = parts[-1]
            kind_or_ns = ":".join(parts[:-1])
            # Try `kind = kind_or_ns, name = name` directly.
            row = conn.execute(
                "SELECT uid FROM resource WHERE kind = ? AND name = ? LIMIT 1",
                (kind_or_ns, name),
            ).fetchone()
            if row:
                return row["uid"]
            # Try `kind = kind_or_ns minus last segment as namespace, namespace = that, name = name`.
            if ":" in kind_or_ns:
                kind = ":".join(kind_or_ns.split(":")[:-1])
                namespace = kind_or_ns.split(":")[-1]
                row = conn.execute(
                    "SELECT uid FROM resource WHERE kind = ? AND namespace = ? AND name = ? LIMIT 1",
                    (kind, namespace, name),
                ).fetchone()
                if row:
                    return row["uid"]

    # 3. unique bare name.
    rows = conn.execute(
        "SELECT uid FROM resource WHERE name = ? LIMIT 2", (target,),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["uid"]
    if len(rows) > 1:
        console.print(
            f"[yellow]Name '{target}' is ambiguous ({len(rows)} matches). "
            "Use kind:name or kind:namespace:name.[/yellow]"
        )
        return None
    return None


@cli.command(name="infer")
@click.option("--min-score", default=0.6, show_default=True,
              help="Minimum match score (0..1) to accept inferred edges.")
def infer(min_score: float) -> None:
    """Re-run the value-match inference pass.

    Walks every env_var / env_ref / secret_ref entity in the graph and
    matches its value against the endpoint / DNS / ARN / bucket-name of
    every discovered cloud Resource. New matches become DEPENDS_ON edges
    tagged `inferred_from: value_match`.

    Idempotent. Safe to re-run anytime.
    """
    from engram.inference.value_match import infer_value_matches

    cfg = load_config()
    if not cfg.db_path.exists():
        console.print("[yellow]No database. Run [bold]engram init[/bold] first.[/yellow]")
        sys.exit(1)
    conn = open_db(cfg)
    with console.status("[bold green]Inferring value matches..."):
        stats = infer_value_matches(conn, min_score=min_score)

    table = Table(title="Value-Match Inference", border_style="green", show_header=False)
    table.add_column("Metric", style="bold")
    table.add_column("Count", justify="right")
    table.add_row("Entities scanned", str(stats.entities_scanned))
    table.add_row("Resources indexed", str(stats.resources_indexed))
    table.add_row("Hostname/ARN candidates extracted", str(stats.candidates_extracted))
    table.add_row("Edges inferred", str(stats.edges_inferred))
    table.add_row("Skipped: too short", str(stats.skipped_too_short))
    table.add_row("Skipped: score below threshold", str(stats.skipped_low_score))
    console.print(table)


@cli.command()
@click.argument("operation")
@click.argument("target")
def assess(operation: str, target: str) -> None:
    """Run blast_radius from the terminal. Debug aid."""
    from engram.safety.blast_radius import assess as _assess

    cfg = load_config()
    if not cfg.db_path.exists():
        console.print("[yellow]No database. Run [bold]engram init[/bold] first.[/yellow]")
        sys.exit(1)
    conn = open_db(cfg)
    result = _assess(conn, operation, target)

    color = {"green": "green", "orange": "yellow", "red": "red"}.get(result.risk_tier, "white")
    action_emoji = {"proceed": "OK", "confirm": "ASK", "block": "STOP"}.get(result.action, "?")

    console.print(Panel(
        f"[bold]{action_emoji}  Action: [{color}]{result.action.upper()}[/{color}]  "
        f"Tier: [{color}]{result.risk_tier}[/{color}][/bold]\n\n"
        f"  Operation:    {result.operation}  ({result.operation_class})\n"
        f"  Target:       {result.target}\n"
        f"  Environment:  {result.environment or 'unknown'}\n"
        f"  Resources:    {len(result.resolved_resources)} resolved\n"
        f"  Dependents:   {len(result.dependents)}\n"
        f"  Incidents:    {len(result.incident_refs)}\n",
        title="Blast Radius",
        border_style=color,
    ))
    console.print("[bold]Reasons:[/bold]")
    for r in result.reasons:
        console.print(f"  - {r}")
    if result.resolved_resources:
        console.print("\n[bold]Resolved resources:[/bold]")
        for r in result.resolved_resources[:10]:
            console.print(f"  - {r['kind']}  [bold]{r['name']}[/bold]  "
                          f"[dim]{r['file_path']}[/dim]")
    if result.dependents:
        console.print("\n[bold]Dependents:[/bold]")
        for d in result.dependents[:10]:
            console.print(f"  - {d['kind']}:{d['id']}  [dim]via {d['rel_type']}[/dim]")


@cli.command()
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the report as JSON.")
@click.option("--max-rows", default=30, show_default=True,
              help="Max rows per section in the text output.")
def drift(as_json: bool, max_rows: int) -> None:
    """Show resources in cloud but not in IaC, and vice versa.

    The drift report compares cloud-discovered resources (from
    `engram import-cloud` / `engram import-cluster`) against IaC-defined
    resources (from `engram index`). Production-tier untracked resources
    are highlighted first.

    This is the headline command for click-ops shops: "show me every
    production resource that was clicked into existence and has no
    Terraform / Helm / K8s manifest behind it."
    """
    from engram.drift import detect_drift, render_drift

    cfg = load_config()
    if not cfg.db_path.exists():
        console.print("[yellow]No database. Run `engram init` first.[/yellow]")
        sys.exit(1)
    conn = open_db(cfg)
    report = detect_drift(conn)

    if as_json:
        import json as _json
        click.echo(_json.dumps({
            "untracked_cloud": report.untracked_cloud,
            "stale_iac": report.stale_iac,
            "untracked_prod_count": report.untracked_prod_count,
        }, indent=2))
        return

    click.echo(render_drift(report, max_rows=max_rows))

    # Exit code semantics: 0 = no drift, 1 = drift exists.
    # Lets people wire `engram drift || alert-prod-team` into cron.
    if report.untracked_cloud or report.stale_iac:
        sys.exit(1)


@cli.command(name="hook")
def hook_cmd() -> None:
    """Read a Claude Code PreToolUse payload from stdin and exit 0/1/2.

    This is the enforcement primitive — the harness-layer gate that
    turns Engram from advice into blocking safety. Not normally invoked
    directly; instead, install it into Claude Code with:

        engram hook-install --target claude-code

    Exit codes:
      0  = green / proceed (or non-Bash tool call → allow)
      1  = orange / confirm (Claude Code shows user the warning)
      2  = red / block (Claude Code refuses the tool call)

    Override: ENGRAM_HOOK_ALLOW=red forces red operations through (audited).
    Strict mode: ENGRAM_HOOK_STRICT=1 makes parser/DB failures block.
    """
    from engram.hook.main import run
    sys.exit(run())


@cli.command(name="hook-install")
@click.option("--target", default="claude-code",
              type=click.Choice(["claude-code"]),
              show_default=True,
              help="Agent harness to install the hook into.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Show what would change without writing.")
def hook_install_cmd(target: str, dry_run: bool) -> None:
    """Install the Engram PreToolUse hook into an agent harness.

    Mirrors `engram mcp install`: idempotent JSON merge. Only touches the
    `hooks.PreToolUse` matcher entry for engram; other entries are left
    alone.

    After install, every `Bash` tool call in Claude Code is intercepted by
    `engram hook` and gated by blast_radius.
    """
    from engram.hook.installer import install_hook
    result = install_hook(target, dry_run=dry_run)
    if "error" in result:
        console.print(f"[red]{result['error']}[/red]")
        sys.exit(1)
    verb = "would " if dry_run else ""
    console.print(
        f"[green]{result['action']}[/green] ({verb}wrote): "
        f"{result['config_path']}"
    )
    console.print(f"  matcher: {result.get('matcher_summary', '?')}")
    if dry_run:
        console.print("\n[dim]Re-run without --dry-run to actually write.[/dim]")
    else:
        console.print(
            "\n[bold]Done.[/bold] Restart Claude Code so it picks up the hook.\n"
            "Test with: [bold]engram hook < /dev/null[/bold]  (should exit 0).\n"
            "Demo blocking: pipe a destructive tool-call JSON into [bold]engram hook[/bold]."
        )


@cli.command()
def diagnose() -> None:
    """Quick diagnostic: where's the DB, what does it know, what's missing."""
    cfg = load_config()
    console.print(f"  Data dir:    {cfg.data_dir}")
    console.print(f"  Database:    {cfg.db_path}  ({'exists' if cfg.db_path.exists() else 'NOT YET CREATED'})")
    console.print(f"  Watch paths: {cfg.watch_paths or '[none — edit ~/.engram/config.yaml]'}")
    console.print(f"  Embeddings:  {'enabled' if cfg.embeddings_enabled else 'disabled (lexical-only)'}")
    if cfg.db_path.exists():
        conn = open_db(cfg)
        from engram.graph import graph_stats
        stats = graph_stats(conn)
        nonzero = {k: v for k, v in stats.items() if v}
        console.print(f"  Graph:       {nonzero or 'empty — run `engram index` (lands next commit)'}")


@cli.command(name="import-cloud")
@click.option("--provider", type=click.Choice(["aws", "gcp", "azure"]),
              default="aws", show_default=True,
              help="Cloud provider to discover (aws, gcp, or azure).")
@click.option("--kinds", "-k",
              default="rds,ec2,s3,eks,lambda,elb,ecs,sqs,sns,dynamodb,"
                      "vpc,subnet,sg,iam,secrets,route53,"
                      "cloudfront,apigateway,asg,ebs,elasticache,"
                      "logs,alarms,events,stepfunctions,kms,acm,cognito,"
                      "kinesis,opensearch,redshift,waf",
              show_default=True,
              help=("Comma-separated kinds. v0.2 (10): rds, ec2, s3, eks, "
                    "lambda, elb, ecs, sqs, sns, dynamodb. v0.3 (+6): vpc, "
                    "subnet, sg, iam, secrets, route53. v0.4 (+16): "
                    "cloudfront, apigateway, asg, ebs, elasticache, logs, "
                    "alarms, events, stepfunctions, kms, acm, cognito, "
                    "kinesis, opensearch, redshift, waf. 32 services total."))
@click.option("--region", default=None,
              help="AWS region (default: read from AWS CLI config).")
@click.option("--profile", default=None,
              help="AWS profile name (default: read from AWS CLI config).")
def import_cloud(provider: str, kinds: str, region: str | None,
                 profile: str | None) -> None:
    """Discover click-ops cloud resources via the provider CLI.

    Closes the gap for resources that were created in the cloud console
    and never described in IaC. Engram never authenticates to your cloud;
    your `aws` CLI does that with your existing credentials. Engram only
    parses the JSON output.

    Example:

        engram import-cloud --provider aws --kinds rds,s3,eks
    """
    cfg = load_config()
    conn = open_db(cfg)
    kind_list = [k.strip() for k in kinds.split(",") if k.strip()]

    console.print(f"\n[bold]Importing {provider.upper()} resources[/bold] ({', '.join(kind_list)})")
    if region:
        console.print(f"  Region:  {region}")
    if profile:
        console.print(f"  Profile: {profile}")

    if provider == "aws":
        from engram.cloud.aws_import import import_aws
        with console.status("[bold green]Running AWS CLI..."):
            stats = import_aws(conn, kinds=kind_list, region=region, profile=profile)
    elif provider == "gcp":
        from engram.cloud.gcp_import import import_gcp
        with console.status("[bold green]Running gcloud..."):
            stats = import_gcp(conn, kinds=kind_list, project=profile, region=region)
    elif provider == "azure":
        from engram.cloud.azure_import import import_azure
        with console.status("[bold green]Running az..."):
            stats = import_azure(conn, kinds=kind_list, subscription=profile)
    else:
        console.print(f"[red]Provider '{provider}' not supported.[/red]")
        sys.exit(1)

    if stats.errors:
        for kind, reason in stats.errors:
            console.print(f"  [red]error[/red] {kind}: {reason}")

    table = Table(title="Cloud Import Complete", border_style="green", show_header=False)
    table.add_column("Kind", style="bold")
    table.add_column("Discovered", justify="right")
    for kind, count in sorted(stats.per_kind.items(), key=lambda x: -x[1]):
        table.add_row(kind, str(count))
    table.add_row("", "")
    table.add_row("Total inserted", str(stats.inserted))
    console.print(table)
    if stats.inserted == 0 and not stats.errors:
        console.print(
            "[yellow]No resources found. If you expected some, check `aws sts "
            "get-caller-identity` and the --region.[/yellow]"
        )

    # Auto-run value-match inference so newly-discovered cloud resources
    # immediately get linked to env-var references in source.
    if stats.inserted > 0:
        from engram.inference.value_match import infer_value_matches
        vm = infer_value_matches(conn)
        if vm.edges_inferred:
            console.print(
                f"\n[bold]Inferred {vm.edges_inferred} new DEPENDS_ON edge(s)[/bold] "
                f"by matching env-var values to discovered resource endpoints."
            )


@cli.command(name="import-cluster")
@click.option("--context", default=None,
              help="kubeconfig context name (default: current-context).")
@click.option("--namespace", "-n", default=None,
              help="Restrict to one namespace (default: all namespaces).")
@click.option("--kinds", "-k", default=None,
              help="Comma-separated kinds (default: workloads + RBAC).")
def import_cluster(context: str | None, namespace: str | None,
                   kinds: str | None) -> None:
    """Discover the live state of a Kubernetes cluster via kubectl.

    Resources discovered here merge naturally with resources extracted from
    YAML manifests — same (kind, name, namespace) tuple. Useful for clusters
    where some objects are kubectl-applied without source-of-truth manifests.

    Example:

        engram import-cluster --context prod --namespace api
    """
    from engram.cloud.kubectl_import import import_cluster as _import_cluster

    cfg = load_config()
    conn = open_db(cfg)
    kind_list = [k.strip() for k in kinds.split(",") if k.strip()] if kinds else None

    console.print(f"\n[bold]Importing live Kubernetes cluster[/bold]")
    console.print(f"  Context:   {context or '(current)'}")
    console.print(f"  Namespace: {namespace or 'all'}")

    with console.status("[bold green]Running kubectl..."):
        stats = _import_cluster(conn, context=context, namespace=namespace,
                                kinds=kind_list)

    if stats.errors:
        for kind, reason in stats.errors:
            console.print(f"  [red]error[/red] {kind}: {reason}")

    table = Table(title="Cluster Import Complete", border_style="green",
                  show_header=False)
    table.add_column("Kind", style="bold")
    table.add_column("Discovered", justify="right")
    for kind, count in sorted(stats.per_kind.items(), key=lambda x: -x[1]):
        if count:
            table.add_row(kind, str(count))
    table.add_row("", "")
    table.add_row("Total inserted", str(stats.inserted))
    console.print(table)


@cli.group()
def mcp() -> None:
    """Manage the MCP-server install in Claude Code / Cursor / Cline / Codex."""


@mcp.command("install")
@click.option("--target", "-t",
              type=click.Choice(["claude-code", "cursor", "cline", "codex", "all"]),
              required=True)
@click.option("--dry-run", is_flag=True, help="Show what would change without writing.")
def mcp_install(target: str, dry_run: bool) -> None:
    """Register engram as an MCP server for the given client."""
    from engram.install import install, install_all
    results = install_all(dry_run=dry_run) if target == "all" else [install(target, dry_run=dry_run)]
    for r in results:
        if "error" in r:
            console.print(f"[red]error[/red] ({r.get('target', '?')}): {r['error']}")
            continue
        color = {"created": "green", "updated": "green",
                 "unchanged": "yellow"}.get(r["action"], "white")
        mode = " (dry-run)" if r["dry_run"] else ""
        console.print(f"[{color}]{r['action']}[/{color}]{mode}: "
                      f"{r['target']} -> {r['config_path']}")


@mcp.command("uninstall")
@click.option("--target", "-t",
              type=click.Choice(["claude-code", "cursor", "cline", "codex"]),
              required=True)
def mcp_uninstall(target: str) -> None:
    """Remove engram's MCP entry from a client's config."""
    from engram.install import uninstall
    r = uninstall(target)
    if "error" in r:
        console.print(f"[red]error[/red]: {r['error']}")
        sys.exit(1)
    console.print(f"{r['action']}: {target} -> {r['config_path']}")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
