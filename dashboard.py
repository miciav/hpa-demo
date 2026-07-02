#!/usr/bin/env python3
"""HPA Demo Dashboard -- real-time TUI using Rich.

Shows CPU utilization, pod status, and HPA scaling in a live dashboard.
Start/stop a load generator to trigger autoscaling.
"""

from __future__ import annotations

# Keep the demo script self-contained, but avoid hiding installation failures.
try:
    import rich  # noqa: F401
except ImportError:
    import subprocess as _sp
    import sys as _sys

    install = _sp.run(
        [_sys.executable, "-m", "pip", "install", "rich", "-q"],
        capture_output=True,
        text=True,
    )
    if install.returncode != 0:
        print("Rich is required. Install it with: python -m pip install rich", file=_sys.stderr)
        print(install.stderr, file=_sys.stderr)
        raise

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


# ============================================================
# Configuration
# ============================================================


CONFIG: dict[str, Any] = {
    "namespace": os.getenv("HPA_DASHBOARD_NAMESPACE", "default"),
    "deploy_label": os.getenv("HPA_DASHBOARD_DEPLOY_LABEL", "run=php-apache"),
    "hpa_name": os.getenv("HPA_DASHBOARD_HPA_NAME", "php-apache"),
    "load_gen_name": os.getenv("HPA_DASHBOARD_LOAD_GEN", "load-generator"),
    "target_cpu_pct": int(os.getenv("HPA_DASHBOARD_TARGET_CPU", "50")),
    "interval": float(os.getenv("HPA_DASHBOARD_INTERVAL", "1.0")),
    "max_pods": int(os.getenv("HPA_DASHBOARD_MAX_PODS", "6")),
    "max_log_lines": int(os.getenv("HPA_DASHBOARD_MAX_LOG_LINES", "6")),
    "ascii_boxes": os.getenv("HPA_DASHBOARD_ASCII", "0") not in ("0", "false", "False", "no"),
}


@dataclass
class CommandResult:
    """Result of a kubectl invocation."""

    command: list[str]
    stdout: str = ""
    stderr: str = ""
    returncode: int = 1
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def short_error(self) -> str:
        if self.timed_out:
            return "timeout"
        text = (self.stderr or self.stdout or "command failed").strip()
        return text.splitlines()[-1] if text else "command failed"


# ============================================================
# Pure / Testable Functions
# ============================================================


def parse_top_pods(output):
    """Parse kubectl top pods --no-headers output into {pod_name: cpu_millicores}."""
    if not output or not output.strip():
        return {}
    pods = {}
    for line in output.strip().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            name = parts[0]
            cpu_str = parts[1]
            try:
                if cpu_str.endswith("m"):
                    pods[name] = int(cpu_str[:-1])
                elif cpu_str == "0":
                    pods[name] = 0
                else:
                    pods[name] = int(float(cpu_str) * 1000)  # cores → millicores
            except (ValueError, IndexError):
                pods[name] = 0
    return pods


def parse_pods(json_str):
    """Parse kubectl get pods -o json into a list of pod dicts."""
    if not json_str:
        return []
    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        return []

    pods = []
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        status = item.get("status", {})
        container_statuses = status.get("containerStatuses") or []
        ready_count = sum(1 for cs in container_statuses if cs.get("ready"))
        total_count = len(container_statuses)
        creation_ts = meta.get("creationTimestamp")
        pods.append({
            "name": meta.get("name", "?"),
            "status": status.get("phase", "Unknown"),
            "ready": ready_count > 0 if total_count else False,
            "ready_count": ready_count,
            "total_count": total_count,
            "age": _human_age(creation_ts),
        })
    return pods


def parse_hpa(json_str, target_name=None):
    """Parse kubectl HPA JSON into a dict, or None."""
    if not json_str:
        return None
    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        return None

    if "items" in data:
        items = data.get("items", [])
        if not items:
            return None
        item = _select_hpa(items, target_name)
    else:
        item = data

    if not item:
        return None

    meta = item.get("metadata", {})
    spec = item.get("spec", {})
    status = item.get("status", {})
    target_ref = spec.get("scaleTargetRef", {}) or {}

    # Extract CPU utilization from currentMetrics (autoscaling/v2)
    cpu_pct = 0
    for metric in status.get("currentMetrics", []):
        resource = metric.get("resource", {})
        if resource.get("name") == "cpu":
            current = resource.get("current", {})
            cpu_pct = current.get("averageUtilization", 0)
            break

    return {
        "name": meta.get("name", "?"),
        "target": target_ref.get("name", "?"),
        "current": status.get("currentReplicas", 0),
        "desired": status.get("desiredReplicas", 0),
        "min": spec.get("minReplicas", 0),
        "max": spec.get("maxReplicas", 0),
        "cpu_pct": cpu_pct,
    }


def detect_scale(old_count, new_count):
    """Return a scale-event string, or None if unchanged."""
    if old_count is None:
        return f"Initial pod count: {new_count}"
    if new_count == old_count:
        return None
    direction = "up" if new_count > old_count else "down"
    return f"Scaling {direction}: {old_count} -> {new_count} pods"


def render_cpu_bar(cpu_pct, target_pct, bar_width=28):
    """Render a CPU utilization bar using unicode block characters."""
    from rich.text import Text

    try:
        cpu_pct = max(0, int(cpu_pct))
    except (TypeError, ValueError):
        cpu_pct = 0

    # Scale bar: 0 to 2x target
    max_visible = max(target_pct * 2, 50)
    ratio = min(cpu_pct / max_visible, 1.0) if max_visible > 0 else 0
    filled_width = ratio * bar_width
    full_blocks = int(filled_width)
    partial = filled_width - full_blocks

    bar = "█" * full_blocks

    if partial > 0 and full_blocks < bar_width:
        idx = min(int(partial * 8), 7)
        bar += ["▏", "▎", "▍", "▌", "▋", "▊", "▉", "█"][idx]
        full_blocks += 1

    empty = bar_width - full_blocks
    if empty > 0:
        bar += "░" * empty

    # Color: green under target, yellow approaching, red over
    if cpu_pct <= target_pct:
        style = "green"
    elif cpu_pct <= target_pct * 1.5:
        style = "yellow"
    else:
        style = "red"

    return Text.assemble((bar, style), "  ", (f"{cpu_pct}%", "bold " + style))


def _select_hpa(items, target_name=None):
    if target_name:
        for item in items:
            spec = item.get("spec", {})
            meta = item.get("metadata", {})
            if spec.get("scaleTargetRef", {}).get("name") == target_name:
                return item
            if target_name in meta.get("name", ""):
                return item
    return items[0] if items else None


def _human_age(creation_timestamp):
    if not creation_timestamp:
        return "—"
    try:
        created = datetime.fromisoformat(creation_timestamp.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - created
    except Exception:
        return "—"

    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


# ============================================================
# Data Collection (subprocess wrappers)
# ============================================================


def _kubectl_cmd(args):
    return ["kubectl", "-n", CONFIG["namespace"], *args]


def _run_kubectl_result(args, timeout=5):
    """Run kubectl and return a CommandResult."""
    command = _kubectl_cmd(args)
    if shutil.which("kubectl") is None:
        return CommandResult(command=command, stderr="kubectl not found in PATH", returncode=127)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return CommandResult(
            command=command,
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            command=command,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            returncode=124,
            timed_out=True,
        )
    except Exception as exc:
        return CommandResult(command=command, stderr=str(exc), returncode=1)


def _run_kubectl_ok(args, timeout=5):
    """Run kubectl, return (stdout, True) or (diagnostic, False)."""
    result = _run_kubectl_result(args, timeout=timeout)
    return (result.stdout if result.ok else result.short_error(), result.ok)


def get_hpa_info():
    """Fetch HPA info."""
    result = _run_kubectl_result(
        ["get", "hpa", CONFIG["hpa_name"], "-o", "json"], timeout=5,
    )
    return parse_hpa(result.stdout, target_name=CONFIG["hpa_name"]) if result.ok else None


def get_pods():
    """Fetch pod list for the deployment."""
    result = _run_kubectl_result(
        ["get", "pods", "-l", CONFIG["deploy_label"], "-o", "json"], timeout=5,
    )
    return parse_pods(result.stdout) if result.ok else []


def get_top_pods():
    """Fetch per-pod CPU metrics."""
    result = _run_kubectl_result(
        ["top", "pods", "-l", CONFIG["deploy_label"], "--no-headers"], timeout=5,
    )
    return parse_top_pods(result.stdout) if result.ok else {}


def is_load_running():
    """Check if the load generator pod is running."""
    result = _run_kubectl_result(
        ["get", "pod", CONFIG["load_gen_name"], "-o", "jsonpath={.status.phase}"],
        timeout=4,
    )
    return result.ok and result.stdout.strip() == "Running"


def collect_snapshot():
    """Collect all live metrics and return (snapshot, errors)."""
    errors = []

    hpa_result = _run_kubectl_result(
        ["get", "hpa", CONFIG["hpa_name"], "-o", "json"], timeout=5,
    )
    hpa = parse_hpa(hpa_result.stdout, target_name=CONFIG["hpa_name"]) if hpa_result.ok else None
    if not hpa_result.ok:
        errors.append(f"HPA: {hpa_result.short_error()}")

    pods_result = _run_kubectl_result(
        ["get", "pods", "-l", CONFIG["deploy_label"], "-o", "json"], timeout=5,
    )
    pods = parse_pods(pods_result.stdout) if pods_result.ok else []
    if not pods_result.ok:
        errors.append(f"Pods: {pods_result.short_error()}")

    top_result = _run_kubectl_result(
        ["top", "pods", "-l", CONFIG["deploy_label"], "--no-headers"], timeout=5,
    )
    top_pods = parse_top_pods(top_result.stdout) if top_result.ok else {}
    # Don't error on top failure — it can take time after deployment

    load_running = is_load_running()

    snapshot = {
        "connected": hpa_result.ok and pods_result.ok,
        "hpa_info": hpa,
        "pods": pods,
        "top_pods": top_pods,
        "load_running": load_running,
        "last_update": datetime.now().strftime("%H:%M:%S"),
    }
    return snapshot, errors


# ============================================================
# Actions
# ============================================================


def start_load():
    """Start the load generator pod."""
    # Delete old pod if exists (leftover from crash)
    _run_kubectl_result(["delete", "pod", CONFIG["load_gen_name"]], timeout=5)
    result = _run_kubectl_result([
        "run", CONFIG["load_gen_name"],
        "--image=busybox:1.28",
        "--restart=Never",
        "--", "/bin/sh", "-c",
        "while sleep 0.01; do wget -q -O- http://php-apache; done",
    ], timeout=10)
    return result.ok, result.short_error() if not result.ok else "Load generator started"


def stop_load():
    """Stop the load generator pod."""
    result = _run_kubectl_result(
        ["delete", "pod", CONFIG["load_gen_name"]], timeout=10,
    )
    return result.ok, result.short_error() if not result.ok else "Load generator stopped"


# ============================================================
# TUI Helpers
# ============================================================


def _panel_box():
    from rich import box
    return box.ASCII if CONFIG.get("ascii_boxes", False) else box.SQUARE


def _bounded(value, default, minimum=1):
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def add_log(state, style, message):
    """Append a timestamped message to the rolling activity log."""
    ts = datetime.now().strftime("%H:%M:%S")
    state.setdefault("log", []).append((style, f"[{ts}] {message}"))
    if len(state["log"]) > 20:
        state["log"] = state["log"][-20:]


def _build_header(state):
    from rich.panel import Panel
    from rich.text import Text

    connected = state.get("connected", False)
    error_count = len(state.get("errors", []))
    dot_style = "bold green" if connected and error_count == 0 else "bold red"
    hpa = state.get("hpa_info") or {}
    cpu_pct = hpa.get("cpu_pct", 0)
    current = hpa.get("current", "?")
    max_r = hpa.get("max", "?")
    load_status = "on" if state.get("load_running") else "off"
    load_style = "bold yellow" if state.get("load_running") else "dim"

    header = Text.assemble(
        ("●", dot_style),
        "  ",
        (f"cpu={cpu_pct}%", "bold"),
        "   ",
        (f"pods={current}/{max_r}", "blue"),
        "   ",
        (f"load={load_status}", load_style),
        "   ",
        (f"ns={CONFIG['namespace']}", "dim"),
        "   ",
        (f"updated={state.get('last_update', '—')}", "dim"),
    )
    return Panel(header, border_style="blue", padding=(0, 1), box=_panel_box(), height=3)


def _build_cpu_panel(state, height=12):
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    hpa = state.get("hpa_info") or {}
    cpu_pct = hpa.get("cpu_pct", 0)
    target = CONFIG["target_cpu_pct"]
    current = hpa.get("current", 0)
    desired = hpa.get("desired", 0)
    min_r = hpa.get("min", 0)
    max_r = hpa.get("max", 0)

    items = []

    # CPU bar
    items.append(render_cpu_bar(cpu_pct, target, bar_width=28))

    # Target marker
    items.append(Text(f"Target: {target}%  |  Request: 200m per pod", style="dim"))

    # Scale event banner
    banner = state.get("scale_event_banner")
    if banner:
        items.append(Text(banner, style="bold yellow"))
    else:
        items.append(Text("No recent scale event", style="dim"))

    items.append(Text(""))

    # HPA info
    if hpa:
        items.append(Text(f"HPA: {current} → {desired} replicas", style="bold magenta"))
        items.append(Text(f"Min: {min_r}  Max: {max_r}", style="dim"))

    items.append(Text(""))
    items.append(Text("1 start load   2 stop load   q quit", style="dim"))

    return Panel(
        Group(*items),
        title="CPU + HPA",
        border_style="cyan",
        padding=(0, 1),
        box=_panel_box(),
        height=height,
    )


def _build_pod_panel(state, height=None):
    from rich.panel import Panel
    from rich.table import Table

    pods = state.get("pods", [])
    top_pods = state.get("top_pods", {})
    max_pods = _bounded(CONFIG.get("max_pods"), default=6)
    visible = pods[:max_pods]
    hidden = max(0, len(pods) - len(visible))

    table = Table(box=None, expand=True, show_header=True, header_style="bold")
    table.add_column("Pod", style="cyan", no_wrap=False, overflow="fold", ratio=3)
    table.add_column("CPU", justify="right", ratio=1)
    table.add_column("Ready", ratio=1)
    table.add_column("Age", justify="right", ratio=1)

    for pod in visible:
        name = pod["name"]
        cpu_str = "—"
        if name in top_pods:
            cpu_str = f"{top_pods[name]}m"

        phase = pod["status"]
        phase_style = "green" if phase == "Running" else "yellow"
        ready_style = "green" if pod["ready"] else "red"
        ready_text = (
            f"{pod.get('ready_count', 0)}/{pod.get('total_count', 0)}"
            if pod.get("total_count")
            else "No"
        )

        table.add_row(
            name,
            cpu_str,
            f"[{ready_style}]{ready_text}[/]",
            pod.get("age", "—"),
        )

    if hidden:
        table.add_row(
            f"[dim]… {hidden} more pod(s)[/]",
            "[dim]—[/]",
            "[dim]—[/]",
            "[dim]—[/]",
        )

    if not pods:
        table.add_row(
            f"[dim]No pods match {CONFIG['deploy_label']}[/]",
            "[dim]—[/]",
            "[dim]—[/]",
            "[dim]—[/]",
        )

    return Panel(
        table,
        title=f"Pods ({len(pods)})",
        border_style="blue",
        padding=(0, 1),
        box=_panel_box(),
        height=height if height is not None else max_pods + 5,
    )


def _build_log_panel(state):
    from rich.panel import Panel
    from rich.text import Text

    max_lines = _bounded(CONFIG.get("max_log_lines"), default=6)
    log_text = Text("", no_wrap=True, overflow="ellipsis")
    entries = state.get("log", [])
    errors = state.get("errors", [])

    if len(errors) > max_lines:
        if max_lines == 1:
            error_rows = [("red", f"{len(errors)} active errors")]
        else:
            visible_errors = errors[-(max_lines - 1):]
            hidden_count = len(errors) - len(visible_errors)
            error_rows = [("red", f"+{hidden_count} more active errors")]
            error_rows.extend(("red", err) for err in visible_errors)
    else:
        error_rows = [("red", err) for err in errors]

    if not entries and not error_rows:
        log_text.append("No activity yet\n", style="dim")
    else:
        log_slots = max(0, max_lines - len(error_rows))
        rows = entries[-log_slots:] + error_rows if log_slots else error_rows[-max_lines:]
        for style, msg in rows[-max_lines:]:
            log_text.append(f"{msg}\n", style=style)
    return Panel(
        log_text,
        title=f"Activity (last {max_lines})",
        border_style="green",
        padding=(0, 1),
        box=_panel_box(),
        height=max_lines + 3,
    )


def render(state):
    """Build the full dashboard layout."""
    from rich.console import Group
    from rich.table import Table

    max_pods = _bounded(CONFIG.get("max_pods"), default=6)
    row_height = max(12, max_pods + 5)

    main = Table.grid(expand=True)
    main.add_column(ratio=2)
    main.add_column(ratio=1)
    main.add_row(
        _build_cpu_panel(state, height=row_height),
        _build_pod_panel(state, height=row_height),
    )
    return Group(
        _build_header(state),
        main,
        _build_log_panel(state),
    )


# ============================================================
# Background Threads
# ============================================================


def _keyboard_reader(action_queue, state):
    """Background thread: read single keys from stdin."""
    if not sys.stdin.isatty():
        state["keyboard_enabled"] = False
        return

    import atexit
    import termios
    import tty

    fd = sys.stdin.fileno()
    try:
        old = termios.tcgetattr(fd)
    except Exception:
        state["keyboard_enabled"] = False
        return

    def restore():
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass

    atexit.register(restore)
    state["keyboard_enabled"] = True

    try:
        tty.setraw(fd)
        while not state.get("quit"):
            ch = sys.stdin.read(1)
            if ch == "1":
                action_queue.put(("start_load", None))
            elif ch == "2":
                action_queue.put(("stop_load", None))
            elif ch in ("q", "Q", "\x03"):
                state["quit"] = True
                action_queue.put(("quit", None))
                break
    except Exception:
        state["keyboard_enabled"] = False
    finally:
        restore()


def _background_worker(action_queue, log_queue):
    """Background thread: process actions, report via log_queue."""
    while True:
        action, _value = action_queue.get()
        if action == "quit":
            break
        try:
            if action == "start_load":
                ok, msg = start_load()
                log_queue.put(("magenta" if ok else "red", msg))
            elif action == "stop_load":
                ok, msg = stop_load()
                log_queue.put(("magenta" if ok else "red", msg))
        except Exception as exc:
            log_queue.put(("red", f"Error: {exc}"))


# ============================================================
# Main
# ============================================================


def _build_arg_parser():
    parser = argparse.ArgumentParser(description="HPA CPU autoscaling demo dashboard")
    parser.add_argument("--namespace", default=CONFIG["namespace"])
    parser.add_argument("--deploy-label", default=CONFIG["deploy_label"])
    parser.add_argument("--hpa-name", default=CONFIG["hpa_name"])
    parser.add_argument("--load-gen-name", default=CONFIG["load_gen_name"])
    parser.add_argument("--target-cpu", type=int, default=CONFIG["target_cpu_pct"])
    parser.add_argument("--interval", type=float, default=CONFIG["interval"])
    parser.add_argument("--screen", action="store_true", help="Use Rich alternate screen mode")
    parser.add_argument("--max-pods", type=int, default=CONFIG["max_pods"])
    parser.add_argument("--max-log-lines", type=int, default=CONFIG["max_log_lines"])
    parser.add_argument(
        "--ascii-boxes", action="store_true",
        help="Use plain ASCII borders (fallback for terminals without Unicode support)",
    )
    parser.add_argument("--once", action="store_true", help="Collect once, render, and exit")
    return parser


def main(argv=None):
    from rich.console import Console
    from rich.live import Live

    args = _build_arg_parser().parse_args(argv)
    CONFIG.update({
        "namespace": args.namespace,
        "deploy_label": args.deploy_label,
        "hpa_name": args.hpa_name,
        "load_gen_name": args.load_gen_name,
        "target_cpu_pct": args.target_cpu,
        "interval": max(0.25, args.interval),
        "max_pods": max(1, args.max_pods),
        "max_log_lines": max(1, args.max_log_lines),
        "ascii_boxes": args.ascii_boxes,
    })

    state: dict[str, Any] = {
        "hpa_info": None,
        "pods": [],
        "top_pods": {},
        "load_running": False,
        "prev_pod_count": None,
        "scale_events": 0,
        "scale_event_banner": None,
        "scale_event_banner_ttl": 0,
        "log": [],
        "errors": [],
        "connected": False,
        "quit": False,
        "keyboard_enabled": False,
        "last_update": "—",
    }

    action_queue: queue.Queue = queue.Queue()
    log_queue: queue.Queue = queue.Queue()

    add_log(state, "green", "Dashboard started")
    add_log(state, "dim", f"Target: {CONFIG['target_cpu_pct']}% CPU, label: {CONFIG['deploy_label']}")

    if args.once:
        snapshot, errors = collect_snapshot()
        state.update(snapshot)
        state["errors"] = errors
        Console().print(render(state))
        return 0 if not errors else 1

    kbd_thread = threading.Thread(
        target=_keyboard_reader, args=(action_queue, state), daemon=True,
    )
    kbd_thread.start()

    worker_thread = threading.Thread(
        target=_background_worker, args=(action_queue, log_queue), daemon=True,
    )
    worker_thread.start()

    try:
        with Live(render(state), refresh_per_second=4, screen=args.screen) as live:
            while not state["quit"]:
                try:
                    while True:
                        style, msg = log_queue.get_nowait()
                        add_log(state, style, msg)
                except queue.Empty:
                    pass

                snapshot, errors = collect_snapshot()
                state.update(snapshot)
                state["errors"] = errors

                new_count = len(state["pods"])
                event = detect_scale(state["prev_pod_count"], new_count)
                if event:
                    if state["prev_pod_count"] is not None:
                        state["scale_events"] += 1
                    state["scale_event_banner"] = event
                    state["scale_event_banner_ttl"] = max(2, int(4 / CONFIG["interval"]))
                    add_log(state, "yellow", event)
                state["prev_pod_count"] = new_count

                if state.get("scale_event_banner_ttl", 0) > 0:
                    state["scale_event_banner_ttl"] -= 1
                else:
                    state["scale_event_banner"] = None

                live.update(render(state))
                time.sleep(CONFIG["interval"])

    except KeyboardInterrupt:
        state["quit"] = True
    finally:
        try:
            action_queue.put(("quit", None))
        except Exception:
            pass
        Console().print("[green]Dashboard stopped.[/]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
