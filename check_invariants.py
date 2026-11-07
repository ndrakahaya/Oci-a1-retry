#!/usr/bin/env python3
"""check_invariants.py — static enforcement of CONSTRAINTS.md for Oci-a1-retry.

Verifies the structural retry invariants in oci_retry.py (AST-based, not regex),
the Finish step in .github/workflows/oci_retry.yml, the resize workflow, and the
floor (no secrets, no stubs) across the repo.

Design this enforces (Claude's 2026-11-07 rewrite, verified by RxSpeed):
  - relay via the `api()` helper (GH_PAT first, built-in token fallback, 3 rounds)
  - one Finish step: case on SCRIPT_EXIT (0 = issue + disable, 124 = relay,
    anything else = red job, no relay)
  - success notification is a GitHub issue, not ntfy
  - watchdog restarts the chain unless the last 3 runs all failed (circuit breaker)
  - setup calls retry transient errors 5x (with_retries); token expiry mid-run relays

Exit codes (same contract as the floor-guard reference):
  0  clean
  1  at least one violation (block the change)
  2  the checker could not run (file missing, not a repo)

Usage: python check_invariants.py [--root <repo-root>]
Run from the repo root in CI. Stdlib only.
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[sys.argv.index("--root") + 1]) if "--root" in sys.argv else Path(__file__).resolve().parent


def main():
    findings = []
    script = ROOT / "oci_retry.py"
    workflow = ROOT / ".github" / "workflows" / "oci_retry.yml"

    if not script.is_file():
        print("check_invariants: oci_retry.py not found")
        return 2
    if not workflow.is_file():
        print("check_invariants: .github/workflows/oci_retry.yml not found")
        return 2

    tree = ast.parse(script.read_text(), filename=str(script))
    src = script.read_text()

    def func(name, node_tree=tree):
        for node in ast.walk(node_tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        return None

    def const_assign(name):
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == name:
                        return node.value
        return None

    def const_int(name):
        v = const_assign(name)
        return v.value if isinstance(v, ast.Constant) and isinstance(v.value, int) else None

    # --- 1. Constants: RETRY_INTERVAL 90, BACKOFF_429 300, MAX_A1_INSTANCES 1 ---
    for name, want in (("RETRY_INTERVAL", 90), ("BACKOFF_429", 300), ("MAX_A1_INSTANCES", 1)):
        got = const_int(name)
        if got != want:
            findings.append(f"{name} must be {want}, got {got}")

    # --- 2. SHAPE_LADDER: exactly 2 rungs; FAULT_DOMAINS: FD-1/2/3 + None, odd length, >= half unpinned ---
    ladder = const_assign("SHAPE_LADDER")
    if not (isinstance(ladder, ast.List) and len(ladder.elts) == 2):
        findings.append("SHAPE_LADDER must have exactly 2 rungs (2/12 and 1/6)")
    fds = const_assign("FAULT_DOMAINS")
    fd_vals = [e.value for e in fds.elts if isinstance(e, ast.Constant)] if isinstance(fds, ast.List) else None
    if fd_vals is None or len(fd_vals) != len(fds.elts):
        findings.append("FAULT_DOMAINS must be a list of string / None literals")
    else:
        for want in ("FAULT-DOMAIN-1", "FAULT-DOMAIN-2", "FAULT-DOMAIN-3"):
            if want not in fd_vals:
                findings.append(f"FAULT_DOMAINS must contain {want}")
        if None not in fd_vals:
            findings.append("FAULT_DOMAINS must contain None (an unpinned call: Oracle picks the fault domain)")
        if fd_vals.count(None) * 2 < len(fd_vals):
            findings.append("FAULT_DOMAINS: unpinned (None) entries must be at least half of the list")
        if len(fd_vals) % 2 == 0:
            findings.append(
                "FAULT_DOMAINS length must be odd (shape index is launch_no % 2; an even length "
                "leaves some shape x fault-domain pairs untested)"
            )

    # --- 3. One launch per cycle: exactly one try_create_instance call in main's loop ---
    main_fn = func("main")
    if main_fn is None:
        findings.append("main() not found")
    if main_fn is not None:
        loops = [n for n in ast.walk(main_fn) if isinstance(n, ast.While)]
        if not loops:
            findings.append("main() has no retry loop")
        else:
            loop = loops[0]
            launches = [
                n for n in ast.walk(loop)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "try_create_instance"
            ]
            if len(launches) != 1:
                findings.append(
                    f"exactly 1 try_create_instance call per cycle required, found {len(launches)}"
                )

        # --- 4. Shape alternation + FD rotation use launch_no % len(...) ---
        for var in ("SHAPE_LADDER", "FAULT_DOMAINS"):
            ok = False
            for n in ast.walk(main_fn):
                if (
                    isinstance(n, ast.Subscript)
                    and isinstance(n.value, ast.Name)
                    and n.value.id == var
                    and isinstance(n.slice, ast.BinOp)
                    and isinstance(n.slice.op, ast.Mod)
                    and isinstance(n.slice.left, ast.Name)
                    and n.slice.left.id == "launch_no"
                    and isinstance(n.slice.right, ast.Call)
                    and isinstance(n.slice.right.func, ast.Name)
                    and n.slice.right.func.id == "len"
                ):
                    ok = True
            if not ok:
                findings.append(f"{var} must be indexed as {var}[launch_no % len({var})]")

        # --- 5. 429 branch sleeps BACKOFF_429; 409 branch re-checks; generic except re-checks ---
        has_429_sleep = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "sleep"
            and n.args
            and isinstance(n.args[0], ast.Name)
            and n.args[0].id == "BACKOFF_429"
            for n in ast.walk(main_fn)
        )
        if not has_429_sleep:
            findings.append("429 branch must time.sleep(BACKOFF_429)")

        if "NotAuthorizedOrResourceAlreadyExists" not in src:
            findings.append("409 (NotAuthorizedOrResourceAlreadyExists) handling missing")
        # generic `except Exception` handler must call existing_a1_instance
        generic_handlers = [
            n for n in ast.walk(main_fn)
            if isinstance(n, ast.ExceptHandler)
            and isinstance(n.type, ast.Name)
            and n.type.id == "Exception"
        ]
        for h in generic_handlers:
            calls = [
                n for n in ast.walk(h)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "existing_a1_instance"
            ]
            if not calls:
                findings.append("generic except handler must re-check existing_a1_instance()")

    # --- 6. Duplicate guard: shape-only, never display_name ---
    # AST-based (not text): the docstring mentions display_name as a warning,
    # so a text search would false-positive. Only Attribute accesses count.
    guard = func("existing_a1_instance")
    if guard is None:
        findings.append("existing_a1_instance() not found")
    else:
        display_name_uses = [
            n for n in ast.walk(guard)
            if isinstance(n, ast.Attribute) and n.attr == "display_name"
        ]
        if display_name_uses:
            findings.append("duplicate guard must match on shape only, not display_name")
        shape_match = any(
            isinstance(n, ast.Compare)
            and any(
                isinstance(c, ast.Constant) and c.value == "VM.Standard.A1.Flex"
                for c in n.comparators
            )
            for n in ast.walk(guard)
        )
        if not shape_match:
            findings.append("duplicate guard must match shape VM.Standard.A1.Flex")

    # --- 7. get_public_ip: wait_s default 150, per-poll try/except inside the loop ---
    gip = func("get_public_ip")
    if gip is None:
        findings.append("get_public_ip() not found")
    else:
        defaults = {a.arg: d for a, d in zip(gip.args.args[-len(gip.args.defaults):], gip.args.defaults)}
        w = defaults.get("wait_s")
        if not (isinstance(w, ast.Constant) and w.value == 150):
            findings.append("get_public_ip wait_s default must be 150")
        loops = [n for n in ast.walk(gip) if isinstance(n, ast.While)]
        if not loops or not any(isinstance(n, ast.Try) for n in ast.walk(loops[0])):
            findings.append("get_public_ip loop must have a per-poll try/except")

    # --- 8. Workflow: ONE Finish step, case on SCRIPT_EXIT ---
    # 0 = issue + disable; 124 = relay; anything else = red job, no relay.
    wf = workflow.read_text()
    if 'case "${SCRIPT_EXIT:-unknown}" in' not in wf:
        findings.append("workflow Finish must use case on SCRIPT_EXIT")
    if 'echo "SCRIPT_EXIT=$?" >> "$GITHUB_ENV"' not in wf:
        findings.append("workflow must capture SCRIPT_EXIT into GITHUB_ENV")
    if "timeout 2700" not in wf:
        findings.append("workflow must wrap the script in timeout 2700 (45-min budget)")
    # success branch: notification issue + auto-disable
    if "gh issue create" not in wf:
        findings.append("workflow success branch must open a GitHub issue")
    if "actions/workflows/oci_retry.yml/disable" not in wf:
        findings.append("workflow success branch must auto-disable")
    # relay branch: dispatch + confirmation line
    if "actions/workflows/oci_retry.yml/dispatches" not in wf:
        findings.append("workflow relay branch must dispatch the next job")
    if "Next job triggered" not in wf:
        findings.append("workflow relay branch must print 'Next job triggered'")
    # failure branch: red job
    if "exit 1" not in wf:
        findings.append("workflow must exit 1 on fast failure (red job, no relay)")
    # api() helper: PAT first, built-in token fallback, 3 rounds, fail loudly
    if 'for round in 1 2 3' not in wf:
        findings.append("workflow api() helper must try 3 rounds")
    if '"$GH_PAT" "$BUILTIN_TOKEN"' not in wf:
        findings.append("workflow api() must try GH_PAT then the built-in token")
    if "BUILTIN_TOKEN: ${{ github.token }}" not in wf:
        findings.append("workflow must pass the built-in token as BUILTIN_TOKEN")
    if "actions: write" not in wf:
        findings.append("workflow needs actions: write (built-in relay/disable)")
    if "issues: write" not in wf:
        findings.append("workflow needs issues: write (success notification)")

    # --- 8b. Workflow hardening: pinned SDK; any curl must fail loudly (-f) ---
    # SDK must be pinned (an unpinned install could pull a breaking release).
    if "oci==2.190.0" not in wf:
        findings.append("oci_retry.yml must pin the SDK: pip install oci==2.190.0")
    if re.search(r"pip install oci(?!==)", wf):
        findings.append("oci_retry.yml must not install an unpinned oci package")
    # Every curl call in the workflow must fail loudly (-f) so an HTTP error
    # (e.g. expired GH_PAT -> 401) cannot silently kill the relay chain.
    for line in wf.splitlines():
        stripped = line.strip()
        if stripped.startswith("curl ") or " $(curl" in line or "RESP=$(curl" in line or "ID_TOKEN=$(curl" in line:
            if "-f" not in line:
                findings.append(f"curl without -f in oci_retry.yml: {stripped[:100]}")

    # --- 8c. oidc_exchange.sh hardening: every curl needs --max-time + --retry; secrets masked ---
    exchange = ROOT / "oidc_exchange.sh"
    if not exchange.is_file():
        findings.append("oidc_exchange.sh not found")
    else:
        ex = exchange.read_text()
        for line in ex.splitlines():
            if "curl " in line:
                if "--max-time" not in line:
                    findings.append(f"curl without --max-time in oidc_exchange.sh: {line.strip()[:100]}")
                if "--retry" not in line:
                    findings.append(f"curl without --retry in oidc_exchange.sh: {line.strip()[:100]}")
        for var in ("CLIENT_SECRET", "ID_TOKEN", "UPST"):
            if f"::add-mask::${var}" not in ex:
                findings.append(f"oidc_exchange.sh must mask {var} with ::add-mask::")

    # --- 8d. Watchdog: 30-min cron, 3-failure circuit breaker, dispatch on a dead chain ---
    watchdog = ROOT / ".github" / "workflows" / "watchdog.yml"
    if not watchdog.is_file():
        findings.append(".github/workflows/watchdog.yml missing (relay chain has no safety net)")
    else:
        wd = watchdog.read_text()
        if "cron:" not in wd and "schedule" not in wd:
            findings.append("watchdog.yml must run on a schedule")
        if "7,37" not in wd:
            findings.append("watchdog.yml must run every 30 min (cron 7,37)")
        if ".[0:3]" not in wd or "failure" not in wd or "timed_out" not in wd:
            findings.append("watchdog.yml must stop if the last 3 runs all failed/timed out (circuit breaker)")
        if "dispatches" not in wd:
            findings.append("watchdog.yml must be able to dispatch a new run")
        if '"active"' not in wd:
            findings.append("watchdog.yml must check the workflow is active before restarting")
        if "gh api" not in wd:
            findings.append("watchdog.yml must use gh api (built-in token, no PAT needed)")

    # --- 8e. Setup calls retry transient errors (SETUP_TRIES=5, with_retries) ---
    if const_int("SETUP_TRIES") != 5:
        findings.append("SETUP_TRIES must be 5")
    wr = func("with_retries")
    if wr is None:
        findings.append("with_retries() not found")
    else:
        wr_src = ast.get_source_segment(src, wr) or ""
        if "sys.exit(2)" not in wr_src:
            findings.append("with_retries must exit 2 on a non-retryable 4xx")
        if "sys.exit(1)" not in wr_src:
            findings.append("with_retries must exit 1 after SETUP_TRIES failures")
    if main_fn is not None:
        main_src = ast.get_source_segment(src, main_fn) or ""
        for call in ("resolve_tenancy", "create_vcn_and_subnet", "get_availability_domain", "get_ubuntu_arm_image"):
            if f"with_retries({call}" not in main_src:
                findings.append(f"main() must call {call} via with_retries")

    # --- 8f. Token expiry mid-run relays (401 after MIN_RUN_BEFORE_RELAY_S) ---
    if const_int("MIN_RUN_BEFORE_RELAY_S") != 300:
        findings.append("MIN_RUN_BEFORE_RELAY_S must be 300")
    if "e.status == 401 and time.time() - T0 > MIN_RUN_BEFORE_RELAY_S" not in src:
        findings.append("401 after MIN_RUN_BEFORE_RELAY_S must exit 124 (relay, fresh token)")
    if src.count("sys.exit(124)") < 1:
        findings.append("script must exit 124 for a relay (timeout or token expiry)")

    # --- 8g. Network setup is idempotent (checked every run, existing rules kept) ---
    cv = func("create_vcn_and_subnet")
    if cv is None:
        findings.append("create_vcn_and_subnet() not found")
    else:
        cv_src = ast.get_source_segment(src, cv) or ""
        for call in ("list_internet_gateways", "get_route_table", "get_security_list", "list_subnets"):
            if call not in cv_src:
                findings.append(f"create_vcn_and_subnet must check {call} on every run")
        if "list(sl.ingress_security_rules) + added" not in cv_src:
            findings.append("ingress update must keep existing rules (list(...) + added)")
    if func("_covers") is None:
        findings.append("_covers() helper not found")

    # --- 8h. Resize workflow: confirm gate, free-tier cap, capacity retries ---
    resize_py = ROOT / "resize_instance.py"
    resize_yml = ROOT / ".github" / "workflows" / "resize.yml"
    if not resize_py.is_file():
        findings.append("resize_instance.py missing")
    if not resize_yml.is_file():
        findings.append(".github/workflows/resize.yml missing")
    if resize_py.is_file():
        rsrc = resize_py.read_text()
        rtree = ast.parse(rsrc, filename=str(resize_py))
        if "CONFIRM_REBOOT" not in rsrc:
            findings.append("resize must refuse to change anything without CONFIRM_REBOOT")
        if "FREE_TIER_MAX_OCPUS, FREE_TIER_MAX_MEMORY_GBS = 2, 12" not in rsrc:
            findings.append("resize must cap the target at the Always Free allowance (2 OCPU / 12 GB)")
        if "BUDGET_S = 2580" not in rsrc:
            findings.append("resize BUDGET_S must be 2580 (43 min)")
        if "sys.exit(124)" not in rsrc:
            findings.append("resize must exit 124 when capacity never appears")
        if "update_instance" not in rsrc:
            findings.append("resize must use update_instance")
        if "import oci_retry as base" not in rsrc:
            findings.append("resize must reuse oci_retry helpers (import oci_retry as base)")
        # no stubs / swallowed errors in resize either
        for node in ast.walk(rtree):
            if isinstance(node, ast.ExceptHandler) and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                findings.append(f"empty except: pass in resize_instance.py at line {node.lineno} (swallowed error)")
        if re.search(r"raise\s+NotImplementedError|NotImplemented\b", rsrc):
            findings.append("stub (NotImplemented) in resize_instance.py")
    if resize_yml.is_file():
        ry = resize_yml.read_text()
        if "confirm_reboot" not in ry:
            findings.append("resize.yml must take a confirm_reboot input")
        if "oci==2.190.0" not in ry:
            findings.append("resize.yml must pin the SDK: pip install oci==2.190.0")
        if "timeout 2700" not in ry:
            findings.append("resize.yml must wrap the script in timeout 2700")

    # --- 8i. Log lines stamped UTC | WIB ---
    st = func("stamp")
    if st is None:
        findings.append("stamp() not found")
    else:
        st_src = ast.get_source_segment(src, st) or ""
        if "UTC | " not in st_src or "WIB" not in st_src:
            findings.append("stamp() must format lines as 'UTC | WIB'")

    # --- 9. Floor: no secrets in source ---
    secret_pats = [
        (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "PEM private key block"),
        (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "GitHub PAT"),
        (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "JWT"),
    ]
    for path in sorted(ROOT.rglob("*")):
        if ".git" in path.parts or "__pycache__" in path.parts:
            continue
        if path.is_file() and path.suffix in (".py", ".sh", ".yml", ".yaml", ".md"):
            try:
                text = path.read_text(errors="ignore")
            except OSError:
                continue
            for pat, label in secret_pats:
                if pat.search(text):
                    findings.append(f"possible secret ({label}) in {path.relative_to(ROOT)}")

    # --- 10. Floor: no stubs / swallowed errors ---
    # An `except: pass` is allowed ONLY inside a retry/poll loop (the
    # get_public_ip poll loop: one hiccup costs one poll, not the lookup).
    # Anywhere else it swallows a failure silently.
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    def inside_loop(node):
        while node in parent:
            node = parent[node]
            if isinstance(node, (ast.While, ast.For)):
                return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            if not inside_loop(node):
                findings.append(f"empty except: pass at line {node.lineno} (swallowed error)")
    if re.search(r"raise\s+NotImplementedError|NotImplemented\b", src):
        findings.append("stub (NotImplemented) in oci_retry.py")

    if not findings:
        print("check_invariants: clean")
        return 0
    print(f"check_invariants: {len(findings)} violation(s):")
    for f in findings:
        print(f"  - {f}")
    print("Each is a move that lowers the bar. Fix the code, or route it through a tracked exception.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
