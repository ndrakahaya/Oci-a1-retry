#!/usr/bin/env python3
"""check_invariants.py — static enforcement of CONSTRAINTS.md for Oci-a1-retry.

Verifies the structural retry invariants in oci_retry.py (AST-based, not regex),
the relay condition in .github/workflows/oci_retry.yml, and the floor
(no secrets, no stubs) across the repo.

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

    def func(name):
        for node in ast.walk(tree):
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

    # --- 2. SHAPE_LADDER: exactly 2 rungs; FAULT_DOMAINS: exactly 3 ---
    ladder = const_assign("SHAPE_LADDER")
    if not (isinstance(ladder, ast.List) and len(ladder.elts) == 2):
        findings.append("SHAPE_LADDER must have exactly 2 rungs (2/12 and 1/6)")
    fds = const_assign("FAULT_DOMAINS")
    if not (isinstance(fds, ast.List) and len(fds.elts) == 3):
        findings.append("FAULT_DOMAINS must have exactly 3 entries")

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

    # --- 8. Workflow: relay ONLY on exit 124, auto-disable ONLY on exit 0 ---
    wf = workflow.read_text()
    if wf.count("env.SCRIPT_EXIT == '124'") != 1:
        findings.append("workflow relay must trigger ONLY on env.SCRIPT_EXIT == '124'")
    if wf.count("env.SCRIPT_EXIT == '0'") != 1:
        findings.append("workflow auto-disable must trigger ONLY on env.SCRIPT_EXIT == '0'")

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
