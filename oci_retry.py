import base64
import datetime
import json
import os
import re
import sys
import time

import oci

# ╔══════════════════════════════════════════════════════╗
# ║  Target : VM.Standard.A1.Flex (ARM)                  ║
# ║  Spec   : 2 OCPU / 12 GB RAM / 100 GB disk (default) ║
# ║           fallback 1 OCPU / 6 GB (Claude verdict      ║
# ║           2026-11-06: launch small first)            ║
# ║  Arch   : ARM (Ampere)                               ║
# ║  Tier   : Oracle Always Free                         ║
# ║  OS     : Ubuntu 24.04/22.04 ARM                     ║
# ╚══════════════════════════════════════════════════════╝

# ─── Configuration ───────────────────────────────────────
# The tenancy OCID is resolved AT RUNTIME from Oracle itself (see resolve_tenancy()).
# The value that used to be hardcoded here has 61 unique characters (a valid OCID
# has 60) and is the most likely cause of the 404 NotAuthorizedOrNotFound seen on
# list_vcns in run #290 (Oracle answers 404 for a compartment that does not exist,
# exactly as it does for "no permission"). It is kept ONLY so the TENANCY_CHECK log
# line can show the mismatch; it is never sent to the API.
LEGACY_TYPED_TENANCY = "ocid1.tenancy.oc1..aaaaaaaaaqq4ucttm4cp5yo6mqhcpucqfjpeosei6djfigrmqejcqbhv6mn3q"
COMPARTMENT_ID = None  # set by resolve_tenancy() at the start of main()
SSH_PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOnnfNHbkhLkUZ5svIAY74TW+slopucAqHYZ7Qo9tIAl indra-oracle-a1"  # Replace with your SSH public key (.pub file content)
INSTANCE_NAME = "agent-box"
# Shape ladder (Claude verdict 2026-11-06): alternate 2/12 and 1/6 across cycles.
# "Out of host capacity" means no host has room for that shape, and 1/6 fits on more
# hosts than 2/12. After the instance exists, resize to 2/12.
# ONE LaunchInstance call per cycle, RETRY_INTERVAL apart. Never try both rungs back to
# back: that drew a 429 on the 2nd call EVERY cycle (run #308: 7 attempts per 45 min
# instead of ~30, because each 429 costs BACKOFF_429, and the 1/6 rung never got evaluated).
SHAPE_LADDER = [(2, 12), (1, 6)]
# Fault domains (Singapore has 1 AD but 3 fault domains). None = pin no fault domain: Oracle
# then picks one itself, "optimizing for available capacity in the availability domain"
# (Oracle docs, Regions and Availability Domains), so such a call lands wherever ANY fault
# domain has room, while a pinned call only works if THAT fault domain has room. Oracle's
# "Resolving Out of Host Capacity error" page recommends not specifying one. The pinned
# entries stay as a hedge in case Oracle's own pick is poor.
# The length MUST be odd: the shape index is launch_no % 2, and with an even length some
# (shape, fault domain) pairs would never meet. 7 entries: 4 of every 7 calls are unpinned,
# each pinned fault domain gets a call every 7 (~10 min), all pairs occur within 14 calls.
FAULT_DOMAINS = [None, "FAULT-DOMAIN-1", None, "FAULT-DOMAIN-2", None, "FAULT-DOMAIN-3", None]
BOOT_VOLUME_SIZE_IN_GBS = 100
RETRY_INTERVAL = 90  # seconds
BACKOFF_429 = 300  # seconds to wait after HTTP 429 (TooManyRequests)
SETUP_TRIES = 5  # tenancy / network / AD / image lookups retry transient errors this many times
MIN_RUN_BEFORE_RELAY_S = 300  # a 401 after this long = session token expired mid-run -> relay for a fresh one
T0 = time.time()
MAX_A1_INSTANCES = 1  # Always Free A1 = 2 OCPU / 12 GB in total -> never more than one instance at launch (a second 1/6 is a resize-phase decision, not a launch decision)
# ────────────────────────────────────────────────────────

# ─── OCI Authentication ──────────────────────────────────
# [Local] Create a config file at ~/.oci/config
#   See README.md for format; key_file should point to your API private key .pem
#
# [GitHub Actions] No config file needed
#   The workflow auto-creates it from GitHub Secrets — see README.md
#
# SECURITY TOKEN (UPST) — explicit signer, NOT config auto-detection.
# The OCI Python SDK (2.190.0) does NOT detect security_token_file from the
# config: without an explicit signer every client falls back to a plain
# API-key Signer, which signs with the dummy/real user OCID + the ephemeral
# key and never attaches the UPST -> API 401 NotAuthenticated (runs #238,
# #288). The fix: build a SecurityTokenSigner from the UPST + session private
# key and pass it explicitly to every client below.
# ────────────────────────────────────────────────────────
from oci.auth.signers import SecurityTokenSigner
from oci.signer import load_private_key

config = oci.config.from_file()
with open(config["security_token_file"]) as _f:
    _UPST = _f.read().strip()
with open(config["key_file"]) as _f:
    # SecurityTokenSigner needs a LOADED key object (with .public_key()),
    # not a PEM string — passing the raw string raises AttributeError
    # 'str' object has no attribute 'public_key' (run #289).
    _PRIVATE_KEY = load_private_key(_f.read(), None)
signer = SecurityTokenSigner(_UPST, _PRIVATE_KEY)


WIB = datetime.timezone(datetime.timedelta(hours=7))


def stamp():
    """Now as 'UTC | WIB' (the runner clock is UTC, the owner reads WIB)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return f"{now:%Y-%m-%d %H:%M:%S} UTC | {now.astimezone(WIB):%H:%M:%S} WIB"


def with_retries(fn, what):
    """Run a setup call, retrying throttling / 5xx / network errors.

    Any other 4xx is a config or permission problem -> exit 2 (the workflow must NOT relay).
    Still failing after SETUP_TRIES -> exit 1 (the workflow turns the job red, so a failure
    e-mail goes out). Before this, ONE transient error during setup killed the whole relay
    chain silently: exit 1, a green step, and no next job ever dispatched.
    """
    for n in range(1, SETUP_TRIES + 1):
        try:
            return fn()
        except oci.exceptions.ServiceError as e:
            if e.status != 429 and e.status < 500:
                print(f"🛑 FATAL during {what}: {e.status} {e.code}: {e.message}")
                sys.exit(2)
            err = f"{e.status} {e.code}"
        except Exception as e:
            err = type(e).__name__
        if n == SETUP_TRIES:
            print(f"🛑 {what} failed {SETUP_TRIES} times (last: {err})")
            sys.exit(1)
        wait = min(30 * n, 120)
        print(f"⚠️ {what} failed ({err}); retry {n}/{SETUP_TRIES - 1} in {wait}s")
        time.sleep(wait)


def tenancy_from_token(token):
    """Best-effort cross-check: tenancy OCID embedded in the UPST JWT payload (or None)."""
    try:
        part = token.split(".")[1]
        payload = json.dumps(json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))))
        hits = sorted(set(re.findall(r"ocid1\.tenancy\.[a-z0-9]+\.[a-z0-9-]*\.[a-z0-9]+", payload)))
        return hits[0] if len(hits) == 1 else None
    except Exception:
        return None


def resolve_tenancy():
    """Tenancy OCID as reported by Oracle itself: a user's compartment_id IS the tenancy OCID.

    Typed/pasted OCIDs are not trusted (see LEGACY_TYPED_TENANCY above).
    """
    user_id = os.environ.get("OCI_USER", "").strip()
    if not user_id:
        sys.exit("FATAL: env OCI_USER (service-user OCID) is not set for this step.")
    identity = oci.identity.IdentityClient(config, signer=signer)
    real = (identity.get_user(user_id).data.compartment_id or "").strip()
    if not real.startswith("ocid1.tenancy."):
        sys.exit(f"FATAL: unexpected compartment_id for the service user: {real!r}")
    print(f"TENANCY_CHECK oracle   : {real} (len={len(real)})")
    for label, typed in (("hardcoded", LEGACY_TYPED_TENANCY), ("config", config.get("tenancy", ""))):
        verdict = "same as Oracle" if typed == real else "DIFFERENT from Oracle (ignored)"
        print(f"TENANCY_CHECK {label:<9}: len={len(typed)} -> {verdict}")
    from_token = tenancy_from_token(_UPST)
    if from_token and from_token != real:
        print(f"TENANCY_CHECK WARNING: UPST claim says {from_token}, user.compartment_id says {real}")
    return real


def existing_a1_instance(quiet=False):
    """Return a live A1 instance if one already exists (guard against a second A1).

    Matches on SHAPE only, deliberately NOT on display_name: the Always Free A1
    allowance is one 2 OCPU / 12 GB instance in total, so an A1 made by anyone (this
    script, the VM CLI loop, the Console) under any name must stop the retry loop.
    (VM.Standard.A1.Flex is the shape name; 1/6 and 2/12 are both that shape.)
    """
    try:
        compute = oci.core.ComputeClient(config, signer=signer)
        live = [
            i
            for i in compute.list_instances(COMPARTMENT_ID).data
            if i.shape == "VM.Standard.A1.Flex"
            and i.lifecycle_state not in ("TERMINATING", "TERMINATED")
        ]
        if live:
            return live[0]
    except Exception as e:
        if not quiet:
            print(f"⚠️ Pre-flight ListInstances failed ({type(e).__name__}); continuing without the guard")
    return None


def get_public_ip(instance_id, wait_s=150, poll_s=5):
    """Return the instance's public IP, or None. Cosmetic only: never raises.

    Right after a launch the primary VNIC attachment is still ATTACHING, and per the SDK
    docs its vnic_id is only set once the attachment completes (get_vnic(None) raises
    ValueError). The VNIC itself can also 404 for a moment or have no public_ip yet. So
    every poll has its OWN try/except: one hiccup costs one poll, not the whole lookup.
    get_vnic lives on VirtualNetworkClient, not ComputeClient (oci SDK 2.190.0).
    """
    try:
        compute = oci.core.ComputeClient(config, signer=signer)
        vcn = oci.core.VirtualNetworkClient(config, signer=signer)
    except Exception:
        return None
    deadline = time.time() + wait_s
    while True:
        try:
            attachments = compute.list_vnic_attachments(COMPARTMENT_ID, instance_id=instance_id).data
            for a in attachments:
                if a.lifecycle_state in ("DETACHING", "DETACHED") or not a.vnic_id:
                    continue
                ip = vcn.get_vnic(a.vnic_id).data.public_ip
                if ip:
                    return ip
        except Exception:
            pass  # not there yet / transient error: keep polling until the deadline
        if time.time() >= deadline:
            return None
        time.sleep(poll_s)


def print_public_ip(instance_id):
    """Print the public IP + SSH command (or a Console hint). Never raises."""
    ip = get_public_ip(instance_id)
    if ip:
        print(f"   Public IP: {ip}")
        print(f"   SSH: ssh ubuntu@{ip}")
    else:
        print("   Public IP not available yet: check the Oracle Cloud Console.")


def get_availability_domain():
    identity = oci.identity.IdentityClient(config, signer=signer)
    ads = identity.list_availability_domains(COMPARTMENT_ID).data
    return ads[0].name


def get_ubuntu_arm_image():
    compute = oci.core.ComputeClient(config, signer=signer)
    for version in ("24.04", "22.04"):
        images = compute.list_images(
            COMPARTMENT_ID,
            operating_system="Canonical Ubuntu",
            operating_system_version=version,
            shape="VM.Standard.A1.Flex",
            sort_by="TIMECREATED",
            sort_order="DESC",
        ).data
        if images:
            print(f"Using Ubuntu {version} ARM image: {images[0].display_name}")
            return images[0].id
    raise Exception("No Ubuntu 22.04/24.04 ARM image found")


def _covers(rule, port):
    """True if this ingress rule already opens TCP `port` to 0.0.0.0/0."""
    if rule.source != "0.0.0.0/0" or rule.protocol not in ("6", "all"):
        return False
    rng = rule.tcp_options.destination_port_range if rule.tcp_options else None
    return rule.protocol == "all" or rng is None or rng.min <= port <= rng.max


def create_vcn_and_subnet():
    """Idempotent: VCN, internet gateway, default route, ingress ports and subnet are each
    checked on EVERY run and only the missing ones are created.

    The old version did the gateway / route / ports only inside the "VCN does not exist yet"
    branch. A run that died half-way through (429, 5xx, timeout) left a VCN that no later run
    would ever finish: the instance would launch fine but be unreachable. It also replaced the
    whole ingress list, dropping the default ICMP rules; existing rules are now kept.
    """
    network = oci.core.VirtualNetworkClient(config, signer=signer)
    m = oci.core.models
    gone = ("TERMINATING", "TERMINATED")

    vcns = [
        v for v in network.list_vcns(COMPARTMENT_ID, display_name="retry-vcn").data
        if v.lifecycle_state not in gone
    ]
    if vcns:
        vcn = vcns[0]
        print(f"Using existing VCN: {vcn.id}")
    else:
        vcn = network.create_vcn(
            m.CreateVcnDetails(compartment_id=COMPARTMENT_ID, display_name="retry-vcn", cidr_block="10.0.0.0/16")
        ).data
        print(f"Created VCN: {vcn.id}")

    # A VCN created a moment ago cannot have a gateway yet, so skip the lookup for it.
    igs = [
        g for g in network.list_internet_gateways(COMPARTMENT_ID, vcn_id=vcn.id).data
        if g.lifecycle_state not in gone
    ] if vcns else []
    if igs:
        ig = igs[0]
    else:
        ig = network.create_internet_gateway(
            m.CreateInternetGatewayDetails(
                compartment_id=COMPARTMENT_ID, vcn_id=vcn.id, display_name="retry-ig", is_enabled=True
            )
        ).data
        print(f"Created internet gateway: {ig.id}")

    rt = network.get_route_table(vcn.default_route_table_id).data
    if not any(r.destination == "0.0.0.0/0" and r.network_entity_id == ig.id for r in rt.route_rules):
        rules = [r for r in rt.route_rules if r.destination != "0.0.0.0/0"]
        rules.append(m.RouteRule(destination="0.0.0.0/0", network_entity_id=ig.id))
        network.update_route_table(rt.id, m.UpdateRouteTableDetails(route_rules=rules))
        print("Default route 0.0.0.0/0 -> internet gateway set")

    # Open inbound ports: SSH / HTTP / HTTPS / Streamlit (only the ones not open yet)
    sl_id = vcn.default_security_list_id or network.list_security_lists(COMPARTMENT_ID, vcn_id=vcn.id).data[0].id
    sl = network.get_security_list(sl_id).data
    missing = [p for p in (22, 80, 443, 8501) if not any(_covers(r, p) for r in sl.ingress_security_rules)]
    if missing:
        added = [
            m.IngressSecurityRule(
                protocol="6",
                source="0.0.0.0/0",
                tcp_options=m.TcpOptions(destination_port_range=m.PortRange(min=p, max=p)),
            )
            for p in missing
        ]
        network.update_security_list(
            sl.id,
            m.UpdateSecurityListDetails(
                ingress_security_rules=list(sl.ingress_security_rules) + added,
                egress_security_rules=sl.egress_security_rules,
            ),
        )
        print(f"Opened ingress ports {missing} (existing rules kept)")

    subnets = [
        s for s in network.list_subnets(COMPARTMENT_ID, vcn_id=vcn.id, display_name="retry-subnet").data
        if s.lifecycle_state not in gone
    ]
    if subnets:
        subnet = subnets[0]
        print(f"Using existing subnet: {subnet.id}")
    else:
        subnet = network.create_subnet(
            m.CreateSubnetDetails(
                compartment_id=COMPARTMENT_ID,
                vcn_id=vcn.id,
                display_name="retry-subnet",
                cidr_block="10.0.0.0/24",
                prohibit_public_ip_on_vnic=False,
            )
        ).data
        print(f"Created subnet: {subnet.id}")

    return subnet.id


def try_create_instance(subnet_id, ad_name, image_id, ocpus, memory_in_gbs, fault_domain):
    compute = oci.core.ComputeClient(config, signer=signer)
    instance = compute.launch_instance(
        oci.core.models.LaunchInstanceDetails(
            compartment_id=COMPARTMENT_ID,
            display_name=INSTANCE_NAME,
            availability_domain=ad_name,
            fault_domain=fault_domain,
            shape="VM.Standard.A1.Flex",
            shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=ocpus,
                memory_in_gbs=memory_in_gbs,
            ),
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                image_id=image_id,
                boot_volume_size_in_gbs=BOOT_VOLUME_SIZE_IN_GBS,
            ),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=subnet_id,
                assign_public_ip=True,
            ),
            metadata={"ssh_authorized_keys": SSH_PUBLIC_KEY},
        )
    ).data
    return instance


def main():
    global COMPARTMENT_ID
    COMPARTMENT_ID = with_retries(resolve_tenancy, "tenancy lookup")

    found = existing_a1_instance()
    if found:
        print(f"✅ A1 instance already exists: {found.display_name} ({found.id}, {found.lifecycle_state}) — nothing to create.")
        print_public_ip(found.id)
        return

    print("Initializing network configuration...")
    subnet_id = with_retries(create_vcn_and_subnet, "network setup")

    print("Fetching availability domain...")
    ad_name = with_retries(get_availability_domain, "availability domain lookup")
    print(f"AD: {ad_name}")

    print("Fetching Ubuntu ARM image (24.04, fallback 22.04)...")
    image_id = with_retries(get_ubuntu_arm_image, "image lookup")
    print(f"Image ID: {image_id}")

    attempt = 0
    launch_no = 0  # counts launch calls (drives the shape + fault-domain rotation)
    created = None
    while True:
        attempt += 1
        print(f"\n[{stamp()}] Attempt #{attempt} to create instance...")

        # Duplicate guard before each cycle: two retry paths run (relay + CLI),
        # so re-check every cycle, not just once at startup.
        found = existing_a1_instance(quiet=True)
        if found:
            print(f"✅ A1 instance already exists: {found.display_name} ({found.id}, {found.lifecycle_state}) — nothing to create.")
            created = found
            break

        # ONE launch call per cycle: the shape alternates 2/12 <-> 1/6 and the fault domain
        # rotates (see SHAPE_LADDER). Two calls back to back = a 429 on the 2nd, every time.
        ocpus, mem = SHAPE_LADDER[launch_no % len(SHAPE_LADDER)]
        fd = FAULT_DOMAINS[launch_no % len(FAULT_DOMAINS)]
        launch_no += 1
        where = fd or "ANY-FD"  # ANY-FD = no fault domain pinned, Oracle picks
        print(f"  Trying {ocpus} OCPU / {mem} GB on {where}...")
        try:
            created = try_create_instance(subnet_id, ad_name, image_id, ocpus, mem, fd)
            print(f"\n✅ Success! Instance created ({ocpus} OCPU / {mem} GB, {where})")
            print(f"   Fault domain: {getattr(created, 'fault_domain', None) or fd or 'unknown'}")
            print(f"   ID: {created.id}")
            print(f"   State: {created.lifecycle_state}")
            break
        except oci.exceptions.ServiceError as e:
            detail = f"{e.status} {e.code}: {e.message}"
            if e.status == 429:
                print(f"⏳ Rate limited ({detail}); backing off {BACKOFF_429}s")
                time.sleep(BACKOFF_429)
                continue
            if e.status == 401 and time.time() - T0 > MIN_RUN_BEFORE_RELAY_S:
                # The token passed the startup checks, so this is expiry, not a config error.
                # Exit 124 = "relay": the next job starts with a fresh token.
                print(f"🔄 Session token expired mid-run ({detail}); exiting so the next job can take over")
                sys.exit(124)
            if "capacity" in str(e).lower():
                print(f"❌ Out of capacity ({ocpus}/{mem}, {where}), retrying...")
            elif e.status >= 500:
                print(f"❌ Oracle server error ({detail}), retrying...")
            elif e.status in (400, 401, 403, 404) or e.code in (
                "LimitExceeded",
                "QuotaExceeded",
                "NotAuthorizedOrResourceAlreadyExists",  # CREATE calls answer 409 here instead of 404
            ):
                if e.code == "NotAuthorizedOrResourceAlreadyExists":
                    # A 409 here can mean the instance was created between
                    # the guard check and the launch: verify before dying.
                    found = existing_a1_instance(quiet=True)
                    if found:
                        print(f"✅ Instance already exists (409): {found.id} ({found.lifecycle_state})")
                        created = found
                        break
                # Config / auth / permission / quota problem: retrying cannot fix it.
                # Exit code 2 (not 124) tells the workflow NOT to relay a new job.
                print(f"🛑 FATAL, not a capacity problem: {detail}")
                sys.exit(2)
            else:
                print(f"❌ API error: {detail}, retrying...")
        except Exception as e:
            print(f"⚠️ Network timeout or other error, retrying... ({type(e).__name__})")
            # Outcome unknown: the request may have reached Oracle. Never double-create.
            found = existing_a1_instance(quiet=True)
            if found:
                print(f"✅ Instance exists after all: {found.id} ({found.lifecycle_state})")
                created = found
                break

        time.sleep(RETRY_INTERVAL)

    # Every way out of the loop sets `created` (a new instance, or one that already existed).
    # Reported AFTER the loop, outside any launch try/except: print_public_ip() never raises,
    # so nothing in it can turn a success into an error or into a second launch.
    print_public_ip(created.id)


if __name__ == "__main__":
    main()
