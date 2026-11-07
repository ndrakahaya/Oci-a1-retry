"""Raise the A1 instance to 2 OCPU / 12 GB, the size the retry loop could not get at launch.

Run by .github/workflows/resize.yml. Login and helpers come from oci_retry.py (importing it
only builds the signer; its main() does not run).

Changing the shape REBOOTS a running instance. Oracle: "If the instance is running when you
change the shape, it is rebooted as part of the change shape operation." Public IP, private IP
and volume / VNIC attachments stay the same. Because of the reboot, this script changes
nothing unless CONFIRM_REBOOT=true; without it, it only reports the current size.

Resizing can hit "Out of host capacity" just like a launch, so it retries every 90 s, for 43
minutes at most.

Exit codes: 0   = the instance is at (or above) the target size
            124 = still no capacity when the 43 minutes ran out: run the workflow again later
            2   = not confirmed, no A1 instance, or a config / auth / limit problem
            1   = lookups kept failing, or the resize was accepted but never showed up
"""
import os
import sys
import time

import oci

import oci_retry as base

TARGET_OCPUS = float(os.environ.get("TARGET_OCPUS", "2"))
TARGET_MEMORY_GBS = float(os.environ.get("TARGET_MEMORY_GBS", "12"))
FREE_TIER_MAX_OCPUS, FREE_TIER_MAX_MEMORY_GBS = 2, 12  # Always Free A1 allowance since 2026-06-15 (was 4 / 24); above it is billed or rejected
BUDGET_S = 2580  # 43 min; the workflow also wraps the script in `timeout 2700`
CONFIRMED = os.environ.get("CONFIRM_REBOOT", "").strip().lower() == "true"


def size_of(inst):
    return float(inst.shape_config.ocpus), float(inst.shape_config.memory_in_gbs)


def is_target(inst):
    ocpus, memory = size_of(inst)
    return ocpus >= TARGET_OCPUS and memory >= TARGET_MEMORY_GBS


def label(ocpus, memory):
    return f"{ocpus:g} OCPU / {memory:g} GB"


def find_instance(compute):
    live = [
        i
        for i in compute.list_instances(base.COMPARTMENT_ID).data
        if i.shape == "VM.Standard.A1.Flex" and i.lifecycle_state not in ("TERMINATING", "TERMINATED")
    ]
    return live[0] if live else None


def wait_until_resized(compute, instance_id, was_running, timeout_s=600, poll_s=15):
    """Poll until the new size shows (and the instance is RUNNING again if it was running)."""
    end = time.time() + timeout_s
    while time.time() < end:
        try:
            cur = compute.get_instance(instance_id).data
            if is_target(cur) and (cur.lifecycle_state == "RUNNING" or not was_running):
                return cur
            print(f"  ... {cur.lifecycle_state}, now {label(*size_of(cur))}")
        except Exception as e:
            print(f"  ... poll failed ({type(e).__name__}), still waiting")
        time.sleep(poll_s)
    return None


def main():
    target = label(TARGET_OCPUS, TARGET_MEMORY_GBS)
    if TARGET_OCPUS > FREE_TIER_MAX_OCPUS or TARGET_MEMORY_GBS > FREE_TIER_MAX_MEMORY_GBS:
        print(
            f"🛑 Target {target} is above the Always Free A1 allowance "
            f"({FREE_TIER_MAX_OCPUS} OCPU / {FREE_TIER_MAX_MEMORY_GBS} GB); the excess would be billed. Refusing."
        )
        sys.exit(2)

    base.COMPARTMENT_ID = base.with_retries(base.resolve_tenancy, "tenancy lookup")
    compute = oci.core.ComputeClient(base.config, signer=base.signer)

    inst = base.with_retries(lambda: find_instance(compute), "instance lookup")
    if inst is None:
        print("🛑 No A1 instance found. Run the OCI ARM Retry workflow first.")
        sys.exit(2)
    print(f"Instance {inst.display_name} ({inst.id}) is {inst.lifecycle_state}, now {label(*size_of(inst))}; target {target}")

    if is_target(inst):
        print("✅ Already at the target size; nothing to do.")
        base.print_public_ip(inst.id)
        return
    if not CONFIRMED:
        print(
            "⏸️ Not confirmed, nothing changed. Changing the shape reboots a running instance "
            "(IP and disk stay). Run the workflow again with confirm_reboot ticked."
        )
        sys.exit(2)

    was_running = inst.lifecycle_state == "RUNNING"
    details = oci.core.models.UpdateInstanceDetails(
        shape_config=oci.core.models.UpdateInstanceShapeConfigDetails(
            ocpus=TARGET_OCPUS, memory_in_gbs=TARGET_MEMORY_GBS
        )
    )
    deadline = time.time() + BUDGET_S
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        print(f"\n[{base.stamp()}] Resize attempt #{attempt} to {target}...")
        try:
            compute.update_instance(inst.id, details)
            print("✅ Oracle accepted the resize" + (" (the instance reboots now)." if was_running else "."))
            break
        except oci.exceptions.ServiceError as e:
            detail = f"{e.status} {e.code}: {e.message}"
            if e.status == 429:
                print(f"⏳ Rate limited ({detail}); waiting {base.BACKOFF_429}s")
                time.sleep(base.BACKOFF_429)
            elif "capacity" in str(e).lower() or e.status >= 500 or e.status == 409:
                # 409 = the instance is busy (still PROVISIONING, or mid-change)
                print(f"❌ {detail}; retrying in {base.RETRY_INTERVAL}s")
                time.sleep(base.RETRY_INTERVAL)
            else:
                print(f"🛑 FATAL {detail}")
                sys.exit(2)
        except Exception as e:
            print(f"⚠️ {type(e).__name__}: {e}; retrying in {base.RETRY_INTERVAL}s")
            time.sleep(base.RETRY_INTERVAL)
    else:
        print(f"⏳ Still no capacity after {BUDGET_S // 60} min and {attempt} attempts. Run the workflow again later.")
        sys.exit(124)

    done = wait_until_resized(compute, inst.id, was_running)
    if done is None:
        print("⚠️ The resize was accepted but the new size has not shown up after 10 min. NOT retrying: check the instance in the Oracle Console.")
        sys.exit(1)
    print(f"✅ Done: {done.display_name} is {done.lifecycle_state}, now {label(*size_of(done))}")
    if not was_running:
        print("ℹ️ The instance was not running before, so it stays stopped. Start it from the Console when you want it.")
    base.print_public_ip(done.id)


if __name__ == "__main__":
    main()
