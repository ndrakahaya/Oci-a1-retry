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
# ║  OS     : Canonical Ubuntu 22.04                     ║
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
# Shape ladder (Claude verdict 2026-11-06): try 2/12 first, then 1/6 if out of
# capacity. "Out of host capacity" means no host has room for that shape, and
# 1/6 fits on more hosts than 2/12. After the instance exists, resize to 2/12.
SHAPE_LADDER = [(2, 12), (1, 6)]
# Fault domains (Singapore has 1 AD but 3 fault domains). Rotate per attempt.
FAULT_DOMAINS = ["FAULT-DOMAIN-1", "FAULT-DOMAIN-2", "FAULT-DOMAIN-3"]
BOOT_VOLUME_SIZE_IN_GBS = 100
RETRY_INTERVAL = 90  # seconds
BACKOFF_429 = 300  # seconds to wait after HTTP 429 (TooManyRequests)
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
    """Return a live 'agent-box' A1 instance if one already exists (guard against double-create).

    Checks by display name (not just shape): with two retry paths running
    (GitHub relay + CLI), the guard must catch an instance created by either
    path, whatever shape it was launched with.
    """
    try:
        compute = oci.core.ComputeClient(config, signer=signer)
        live = [
            i
            for i in compute.list_instances(COMPARTMENT_ID).data
            if i.display_name == INSTANCE_NAME
            and i.shape == "VM.Standard.A1.Flex"
            and i.lifecycle_state not in ("TERMINATING", "TERMINATED")
        ]
        if live:
            return live[0]
    except Exception as e:
        if not quiet:
            print(f"⚠️ Pre-flight ListInstances failed ({type(e).__name__}); continuing without the guard")
    return None


def get_public_ip(instance_id, wait_s=60):
    """Return the instance's public IP, or None. Cosmetic only: never raises.

    The VNIC attachment can lag a few seconds behind a fresh launch, so
    poll briefly, then give up quietly and let the caller fall back to
    "check the Console". get_vnic lives on VirtualNetworkClient, not
    ComputeClient (verified against oci SDK 2.190.0).
    """
    try:
        compute = oci.core.ComputeClient(config, signer=signer)
        vcn = oci.core.VirtualNetworkClient(config, signer=signer)
        deadline = time.time() + wait_s
        while True:
            attachments = compute.list_vnic_attachments(
                COMPARTMENT_ID, instance_id=instance_id
            ).data
            live = [
                a
                for a in attachments
                if a.lifecycle_state not in ("TERMINATING", "TERMINATED", "DETACHING")
            ]
            for a in live:
                vnic = vcn.get_vnic(a.vnic_id).data
                if vnic.public_ip:
                    return vnic.public_ip
            if time.time() >= deadline:
                return None
            time.sleep(5)
    except Exception:
        return None

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


def create_vcn_and_subnet():
    network = oci.core.VirtualNetworkClient(config, signer=signer)

    # Check if VCN already exists
    vcns = network.list_vcns(COMPARTMENT_ID, display_name="retry-vcn").data
    if vcns:
        vcn = vcns[0]
        print(f"Using existing VCN: {vcn.id}")
    else:
        vcn = network.create_vcn(
            oci.core.models.CreateVcnDetails(
                compartment_id=COMPARTMENT_ID,
                display_name="retry-vcn",
                cidr_block="10.0.0.0/16",
            )
        ).data
        print(f"Created VCN: {vcn.id}")

        # Create Internet Gateway
        ig = network.create_internet_gateway(
            oci.core.models.CreateInternetGatewayDetails(
                compartment_id=COMPARTMENT_ID,
                vcn_id=vcn.id,
                display_name="retry-ig",
                is_enabled=True,
            )
        ).data

        # Update route table to allow outbound traffic
        network.update_route_table(
            vcn.default_route_table_id,
            oci.core.models.UpdateRouteTableDetails(
                route_rules=[
                    oci.core.models.RouteRule(
                        destination="0.0.0.0/0",
                        network_entity_id=ig.id,
                    )
                ]
            ),
        )

        # Open inbound ports: SSH / HTTP / HTTPS / Streamlit
        security_lists = network.list_security_lists(COMPARTMENT_ID, vcn_id=vcn.id).data
        if security_lists:
            existing_egress = security_lists[0].egress_security_rules
            new_ingress = []
            for port in [22, 80, 443, 8501]:
                new_ingress.append(
                    oci.core.models.IngressSecurityRule(
                        protocol="6",
                        source="0.0.0.0/0",
                        tcp_options=oci.core.models.TcpOptions(
                            destination_port_range=oci.core.models.PortRange(
                                min=port, max=port
                            )
                        ),
                    )
                )
            network.update_security_list(
                security_lists[0].id,
                oci.core.models.UpdateSecurityListDetails(
                    ingress_security_rules=new_ingress,
                    egress_security_rules=existing_egress,
                ),
            )

    # Check if subnet already exists
    subnets = network.list_subnets(
        COMPARTMENT_ID, vcn_id=vcn.id, display_name="retry-subnet"
    ).data
    if subnets:
        subnet = subnets[0]
        print(f"Using existing subnet: {subnet.id}")
    else:
        subnet = network.create_subnet(
            oci.core.models.CreateSubnetDetails(
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
    COMPARTMENT_ID = resolve_tenancy()

    found = existing_a1_instance()
    if found:
        print(f"✅ A1 instance already exists: {found.display_name} ({found.id}, {found.lifecycle_state}) — nothing to create.")
        public_ip = get_public_ip(found.id)
        if public_ip:
            print(f"   Public IP: {public_ip}")
        return

    print("Initializing network configuration...")
    subnet_id = create_vcn_and_subnet()

    print("Fetching availability domain...")
    ad_name = get_availability_domain()
    print(f"AD: {ad_name}")

    print("Fetching Ubuntu ARM image (24.04, fallback 22.04)...")
    image_id = get_ubuntu_arm_image()
    print(f"Image ID: {image_id}")

    attempt = 0
    launch_no = 0  # counts individual launch tries (drives fault-domain rotation)
    while True:
        attempt += 1
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{now}] Attempt #{attempt} to create instance...")

        # Duplicate guard before each cycle: two retry paths run (relay + CLI),
        # so re-check every cycle, not just once at startup.
        found = existing_a1_instance(quiet=True)
        if found:
            print(f"✅ A1 instance already exists: {found.display_name} ({found.id}, {found.lifecycle_state}) — nothing to create.")
            public_ip = get_public_ip(found.id)
            if public_ip:
                print(f"   Public IP: {public_ip}")
            break

        # Shape ladder: try 2/12, then 1/6 if out of capacity (same cycle).
        created = None
        for (ocpus, mem) in SHAPE_LADDER:
            fd = FAULT_DOMAINS[launch_no % len(FAULT_DOMAINS)]
            launch_no += 1
            print(f"  Trying {ocpus} OCPU / {mem} GB on {fd}...")
            try:
                created = try_create_instance(subnet_id, ad_name, image_id, ocpus, mem, fd)
                print(f"\n✅ Success! Instance created ({ocpus} OCPU / {mem} GB, {fd})")
                print(f"   ID: {created.id}")
                print(f"   State: {created.lifecycle_state}")
                public_ip = get_public_ip(created.id)
                if public_ip:
                    print(f"   Public IP: {public_ip}")
                    print(f"   SSH: ssh ubuntu@{public_ip}")
                else:
                    print(f"   Check Oracle Cloud Console for the public IP")
                break
            except oci.exceptions.ServiceError as e:
                detail = f"{e.status} {e.code}: {e.message}"
                if e.status == 429:
                    print(f"⏳ Rate limited ({detail}); backing off {BACKOFF_429}s")
                    time.sleep(BACKOFF_429)
                    continue
                if "capacity" in str(e).lower():
                    print(f"❌ Out of capacity ({ocpus}/{mem}, {fd}), trying next shape...")
                    continue
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
                            public_ip = get_public_ip(found.id)
                            if public_ip:
                                print(f"   Public IP: {public_ip}")
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
                    public_ip = get_public_ip(found.id)
                    if public_ip:
                        print(f"   Public IP: {public_ip}")
                    created = found
                    break
        if created:
            break

        time.sleep(RETRY_INTERVAL)


if __name__ == "__main__":
    main()
