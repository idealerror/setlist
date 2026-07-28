"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path

from . import __version__, capture, config, sessionize, storage, sync


def prepare_console() -> None:
    """Windows consoles default to cp1252 and track titles frequently are not.
    line_buffering keeps output flowing when stdout is redirected to a log,
    which is otherwise block-buffered and loses the tail on restart."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)


def silence_proactor_shutdown_noise() -> None:
    """aiohttp on the Windows ProactorEventLoop raises 'Event loop is closed'
    from __del__ during interpreter shutdown. Harmless, but it buries real
    output. Swallow only that specific case."""
    if sys.platform != "win32":
        return
    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport as T
    except Exception:
        return

    original = T.__del__

    def quiet_del(self, _original=original):
        try:
            _original(self)
        except RuntimeError as exc:
            if "Event loop is closed" not in str(exc):
                raise

    T.__del__ = quiet_del


def load_config(args) -> config.Config:
    path = args.config
    if path is None:
        # A config.toml sitting next to the package root is the normal
        # deployment; fall back to defaults when there isn't one.
        candidate = Path.cwd() / "config.toml"
        path = candidate if candidate.is_file() else None
    cfg = config.load(path)
    if args.db:
        cfg.storage.db_path = args.db
        cfg.root = Path.cwd()
    if path:
        print(f"Config: {Path(path).resolve()}")
    else:
        print("Config: built-in defaults (no config.toml found)")
    return cfg


# ----------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------

def cmd_list_devices(args) -> int:
    capture.quiet_warnings()
    print(capture.format_sources(capture.list_sources()))
    return 0


async def _run_with_sync(cfg, conn, source, recognizer, max_runtime):
    from .loop import Runner

    syncer = sync.build(cfg, conn, __version__)
    if syncer is None:
        print("Sync: disabled (no server.url/server.token configured)")
    else:
        print(f"Sync: {cfg.server.url} as venue '{cfg.venue.slug}'")

    runner = Runner(cfg, conn, source, recognizer, __version__, syncer=syncer)
    task = asyncio.create_task(syncer.run_forever()) if syncer else None
    try:
        await runner.run(max_runtime=max_runtime)
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if syncer is not None:
            # One last flush so a clean stop does not leave a queue behind.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(syncer.drain(), timeout=30)
            await syncer.aclose()


def cmd_run(args) -> int:
    from .recognize import ShazamRecognizer

    cfg = load_config(args)
    source = capture.open_source(cfg.audio)
    print(f"Source: {source.name}")
    print(f"  {source.info.kind}, {source.backend}, {source.samplerate} Hz, "
          f"{source.channels} ch")

    conn = storage.connect(cfg.db_path)
    print(f"Database: {cfg.db_path}")
    pending = storage.unsynced_count(conn)
    if pending:
        print(f"  {pending} detections pending sync")

    recognizer = ShazamRecognizer()

    silence_proactor_shutdown_noise()
    try:
        asyncio.run(_run_with_sync(cfg, conn, source, recognizer,
                                   args.max_runtime))
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        conn.close()
    return 0


def cmd_sync(args) -> int:
    """One-shot drain. Useful for verifying credentials, and for pushing a
    backlog by hand after an outage without starting capture."""
    cfg = load_config(args)
    conn = storage.connect(cfg.db_path)

    async def _go():
        syncer = sync.build(cfg, conn, __version__)
        if syncer is None:
            print("No server configured (set server.url and server.token).")
            return 2
        try:
            report = await syncer.drain()
            print(f"Sent {report['sent']}: {report['accepted']} accepted, "
                  f"{report['duplicates']} already held, "
                  f"{report['pending']} still pending")
            await syncer.heartbeat()
            print("Heartbeat delivered.")
            return 0
        finally:
            await syncer.aclose()

    silence_proactor_shutdown_noise()
    try:
        return asyncio.run(_go())
    except Exception as exc:
        print(f"Sync failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("Detections remain queued locally; nothing was lost.",
              file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_sessionize(args) -> int:
    cfg = load_config(args)
    conn = storage.connect(cfg.db_path)
    try:
        gap = args.gap_minutes or cfg.sessionize.play_gap_minutes
        count = sessionize.rebuild(conn, gap, since=args.since, until=args.until)
        scope = "all detections"
        if args.since or args.until:
            scope = f"{args.since or 'start'} .. {args.until or 'now'}"
        print(f"Derived {count} plays from {scope} (gap {gap} min)")
    finally:
        conn.close()
    return 0


def cmd_export(args) -> int:
    cfg = load_config(args)
    conn = storage.connect(cfg.db_path)
    try:
        if args.table == "plays":
            count = storage.export_plays_csv(conn, args.out)
        else:
            count = storage.export_detections_csv(conn, args.out)
        print(f"Wrote {count} {args.table} rows to {args.out}")
    finally:
        conn.close()
    return 0


def cmd_stats(args) -> int:
    cfg = load_config(args)
    conn = storage.connect(cfg.db_path)
    try:
        stats = storage.cache_stats(conn)
        total = storage.detection_count(conn)
        plays = conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
        served = stats["cache_hits"] + stats["api_detections"]
        rate = (100.0 * stats["cache_hits"] / served) if served else 0.0
        print(f"detections      {total}")
        print(f"  via cache     {stats['cache_hits']}")
        print(f"  via API       {stats['api_detections']}")
        print(f"cache hit rate  {rate:.1f}%")
        print(f"plays (derived) {plays}")
        print(f"cached tracks   {stats['tracks']} ({stats['hashes']} hashes)")
        print(f"API calls today {storage.api_calls_today(conn)}"
              f" / {cfg.api.daily_ceiling}")
        print(f"pending sync    {storage.unsynced_count(conn)}")
    finally:
        conn.close()
    return 0


# ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="setlist",
        description="Venue setlist logger -- capture client.")
    p.add_argument("--version", action="version", version=f"setlist {__version__}")
    p.add_argument("--config", metavar="TOML",
                   help="config file (default: ./config.toml if present)")
    p.add_argument("--db", metavar="PATH", help="override storage.db_path")

    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="listen and log (the main command)")
    run.add_argument("--max-runtime", type=float, metavar="SECONDS",
                     help="exit cleanly after this long so a scheduled task "
                          "can restart the process (spec 9.2)")
    run.set_defaults(func=cmd_run)

    devices = sub.add_parser("list-devices", help="show capture sources")
    devices.set_defaults(func=cmd_list_devices)

    syncer = sub.add_parser(
        "sync", help="push queued detections to the server once and exit")
    syncer.set_defaults(func=cmd_sync)

    sess = sub.add_parser("sessionize", help="rebuild plays from detections")
    sess.add_argument("--since", metavar="ISO8601")
    sess.add_argument("--until", metavar="ISO8601")
    sess.add_argument("--gap-minutes", type=float,
                      help="override sessionize.play_gap_minutes")
    sess.set_defaults(func=cmd_sessionize)

    exp = sub.add_parser("export", help="write a CSV")
    exp.add_argument("table", choices=["plays", "detections"])
    exp.add_argument("out", metavar="CSV")
    exp.set_defaults(func=cmd_export)

    stats = sub.add_parser("stats", help="cache and API counters")
    stats.set_defaults(func=cmd_stats)
    return p


def main(argv: list[str] | None = None) -> int:
    prepare_console()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except config.ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
