#!/usr/bin/env python3
"""test_retry_simulation.py — simulation evidence for oci_retry.py.

Stubs the OCI SDK in sys.modules, then runs the real main() loop against
canned API responses (Out of capacity, 429, success, 409, fatal 401, network
error). Proves the CONSTRAINTS.md invariants by behaviour, not by reading logs:

  - exactly 1 launch per cycle, shapes alternate 2/12 <-> 1/6
  - fault domains rotate across 3 FDs
  - 429 costs a 300s backoff; capacity costs a 90s retry
  - 409 / uncertain network error re-checks for an existing instance
  - fatal 401 exits 2 (never relay); existing instance stops the loop

Stdlib only. No network, no credentials, no real OCI calls.
Usage: python test_retry_simulation.py   (exit 0 = all scenarios pass)
"""
import contextlib
import io
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Fake OCI SDK (installed in sys.modules before importing oci_retry)
# ---------------------------------------------------------------------------


class FakeServiceError(Exception):
    def __init__(self, status=None, code=None, message=""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class FakeModel:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _obj(**kwargs):
    return FakeModel(**kwargs)


class FakeIdentityClient:
    def __init__(self, state):
        self._s = state

    def get_user(self, user_id):
        return _obj(data=_obj(compartment_id=self._s["tenancy"], name="github-actions-svc"))

    def list_availability_domains(self, compartment_id):
        return _obj(data=[_obj(name="ViQK:AP-SINGAPORE-1-AD-1")])


class FakeComputeClient:
    def __init__(self, state):
        self._s = state

    def list_instances(self, compartment_id):
        return _obj(data=list(self._s["instances"]))

    def list_images(self, compartment_id=None, **kwargs):
        return _obj(data=[_obj(display_name="Canonical-Ubuntu-24.04", id="ocid1.image.oc1..fake")])

    def list_vnic_attachments(self, compartment_id, instance_id=None):
        return _obj(data=[_obj(lifecycle_state="ATTACHED", vnic_id="ocid1.vnic.oc1..fake")])

    def launch_instance(self, details):
        self._s["launches"].append(
            (details.shape_config.ocpus, details.shape_config.memory_in_gbs, details.fault_domain)
        )
        outcome = self._s["launch_script"].pop(0) if self._s["launch_script"] else ("capacity",)
        kind = outcome[0]
        if kind == "capacity":
            raise FakeServiceError(500, "OutOfHostCapacity", "Out of host capacity")
        if kind == "429":
            raise FakeServiceError(429, "TooManyRequests", "Too many requests")
        if kind == "409":
            raise FakeServiceError(409, "NotAuthorizedOrResourceAlreadyExists", "already exists")
        if kind == "fatal":
            raise FakeServiceError(401, "NotAuthenticated", "Not authenticated")
        if kind == "network":
            raise ConnectionError("simulated network drop")
        if kind == "success":
            # SDK returns a Response whose .data is the instance.
            return _obj(data=_obj(id="ocid1.instance.oc1..fake", lifecycle_state="RUNNING"))
        raise AssertionError(f"unknown launch outcome {kind!r}")


class FakeNetworkClient:
    def __init__(self, state):
        self._s = state

    def list_vcns(self, compartment_id, display_name=None):
        return _obj(data=[_obj(
            id="ocid1.vcn.oc1..fake",
            lifecycle_state="AVAILABLE",
            default_route_table_id="ocid1.routetable.oc1..fake",
            default_security_list_id="ocid1.securitylist.oc1..fake",
        )])

    def list_internet_gateways(self, compartment_id, vcn_id=None):
        return _obj(data=[_obj(id="ocid1.internetgateway.oc1..fake", lifecycle_state="AVAILABLE")])

    def get_route_table(self, route_table_id):
        return _obj(data=_obj(
            id=route_table_id,
            route_rules=[_obj(destination="0.0.0.0/0", network_entity_id="ocid1.internetgateway.oc1..fake")],
        ))

    def get_security_list(self, security_list_id):
        # One "all" rule covers every port, so create_vcn_and_subnet adds nothing.
        return _obj(data=_obj(
            id=security_list_id,
            ingress_security_rules=[_obj(source="0.0.0.0/0", protocol="all", tcp_options=None)],
            egress_security_rules=[],
        ))

    def list_security_lists(self, compartment_id, vcn_id=None):
        return _obj(data=[_obj(id="ocid1.securitylist.oc1..fake")])

    def list_subnets(self, compartment_id, vcn_id=None, display_name=None):
        return _obj(data=[_obj(id="ocid1.subnet.oc1..fake", lifecycle_state="AVAILABLE")])

    def create_vcn(self, details):
        return _obj(data=_obj(
            id="ocid1.vcn.oc1..new",
            default_route_table_id="ocid1.routetable.oc1..new",
            default_security_list_id="ocid1.securitylist.oc1..new",
        ))

    def create_internet_gateway(self, details):
        return _obj(data=_obj(id="ocid1.internetgateway.oc1..new", lifecycle_state="AVAILABLE"))

    def update_route_table(self, route_table_id, details):
        return _obj(data=_obj(id=route_table_id))

    def update_security_list(self, security_list_id, details):
        return _obj(data=_obj(id=security_list_id))

    def create_subnet(self, details):
        return _obj(data=_obj(id="ocid1.subnet.oc1..new", lifecycle_state="AVAILABLE"))

    def get_vnic(self, vnic_id):
        return _obj(data=_obj(public_ip="1.2.3.4"))


def install_fake_oci(state):
    oci = types.ModuleType("oci")
    oci.config = types.ModuleType("oci.config")
    oci.config.from_file = lambda: dict(state["config"])
    oci.signer = types.ModuleType("oci.signer")
    oci.signer.load_private_key = lambda pem, pw: _obj()
    oci.auth = types.ModuleType("oci.auth")
    oci.auth.signers = types.ModuleType("oci.auth.signers")
    oci.auth.signers.SecurityTokenSigner = lambda token, key: _obj()
    oci.identity = types.ModuleType("oci.identity")
    oci.identity.IdentityClient = lambda config, signer=None: FakeIdentityClient(state)
    oci.core = types.ModuleType("oci.core")
    oci.core.ComputeClient = lambda config, signer=None: FakeComputeClient(state)
    oci.core.VirtualNetworkClient = lambda config, signer=None: FakeNetworkClient(state)
    oci.core.models = types.ModuleType("oci.core.models")
    for name in (
        "CreateVcnDetails", "CreateInternetGatewayDetails", "UpdateRouteTableDetails",
        "RouteRule", "IngressSecurityRule", "TcpOptions", "PortRange",
        "UpdateSecurityListDetails", "CreateSubnetDetails", "LaunchInstanceDetails",
        "LaunchInstanceShapeConfigDetails", "InstanceSourceViaImageDetails",
        "CreateVnicDetails", "UpdateInstanceDetails", "UpdateInstanceShapeConfigDetails",
    ):
        setattr(oci.core.models, name, FakeModel)
    oci.exceptions = types.ModuleType("oci.exceptions")
    oci.exceptions.ServiceError = FakeServiceError
    for name, mod in (
        ("oci", oci), ("oci.config", oci.config), ("oci.signer", oci.signer),
        ("oci.auth", oci.auth), ("oci.auth.signers", oci.auth.signers),
        ("oci.identity", oci.identity), ("oci.core", oci.core),
        ("oci.core.models", oci.core.models), ("oci.exceptions", oci.exceptions),
    ):
        sys.modules[name] = mod


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

FAKE_TENANCY = "ocid1.tenancy.oc1..aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def fresh_state(launch_script, instances=None):
    tmp = tempfile.mkdtemp(prefix="oci-sim-")
    token_file = os.path.join(tmp, "token")
    key_file = os.path.join(tmp, "key.pem")
    Path(token_file).write_text("fake-upst-token")
    Path(key_file).write_text("fake-pem")
    return {
        "tenancy": FAKE_TENANCY,
        "config": {
            "security_token_file": token_file,
            "key_file": key_file,
            "tenancy": FAKE_TENANCY,
        },
        "instances": list(instances or []),
        "launch_script": list(launch_script),
        "launches": [],
        "sleeps": [],
    }


def run_main(state):
    """Import oci_retry fresh (module-level config load runs once per import),
    run main() with sleep mocked, return (exit_code, stdout)."""
    for mod in [m for m in list(sys.modules) if m == "oci_retry"]:
        del sys.modules[mod]
    install_fake_oci(state)
    os.environ["OCI_USER"] = "ocid1.user.oc1..aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    import oci_retry  # noqa: E402  (import after stub install is the point)

    buf = io.StringIO()
    with mock.patch("time.sleep", side_effect=lambda s: state["sleeps"].append(s)):
        with contextlib.redirect_stdout(buf):
            try:
                oci_retry.main()
                code = 0
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
    return code, buf.getvalue()


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError(f"{name}: {detail}")
    print(f"  ok: {name}")


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def scenario_alternation():
    print("scenario: alternation + FD rotation + capacity (6 fail, then success)")
    state = fresh_state([("capacity",)] * 6 + [("success",)])
    code, out = run_main(state)
    check("exit 0 on success", code == 0, f"got {code}")
    check("7 launch calls", len(state["launches"]) == 7, f"got {len(state['launches'])}")
    shapes = [(o, m) for o, m, _ in state["launches"]]
    check("shapes alternate 2/12,1/6,...", shapes == [(2, 12), (1, 6)] * 3 + [(2, 12)], f"got {shapes}")
    fds = [f for _, _, f in state["launches"]]
    check("FDs rotate 1,2,3,1,2,3,1", fds == ["FAULT-DOMAIN-1", "FAULT-DOMAIN-2", "FAULT-DOMAIN-3"] * 2 + ["FAULT-DOMAIN-1"], f"got {fds}")
    check("all sleeps are 90s retry", all(s == 90 for s in state["sleeps"]), f"got {state['sleeps']}")
    check("no 300s backoff", 300 not in state["sleeps"])
    check("success printed", "Success! Instance created" in out)


def scenario_429_backoff():
    print("scenario: 429 costs a 300s backoff, loop continues alternating")
    state = fresh_state([("capacity",), ("429",), ("capacity",), ("success",)])
    code, out = run_main(state)
    check("exit 0 on success", code == 0, f"got {code}")
    check("4 launch calls", len(state["launches"]) == 4, f"got {len(state['launches'])}")
    check("shapes still alternate", [(o, m) for o, m, _ in state["launches"]] == [(2, 12), (1, 6), (2, 12), (1, 6)])
    check("sleeps are 90,300,90", state["sleeps"] == [90, 300, 90], f"got {state['sleeps']}")


def scenario_409_existing():
    print("scenario: 409 re-checks and stops on the existing instance")
    existing = _obj(
        shape="VM.Standard.A1.Flex", lifecycle_state="RUNNING",
        display_name="agent-box", id="ocid1.instance.oc1..existing",
    )
    state = fresh_state([("409",)])
    calls = {"n": 0}

    def list_instances(self, compartment_id):
        calls["n"] += 1
        # first calls (startup guard + pre-cycle guard): none yet; after 409: exists
        return _obj(data=[existing] if calls["n"] >= 3 else [])

    with mock.patch.object(FakeComputeClient, "list_instances", list_instances):
        code, out = run_main(state)
    check("exit 0 (found existing)", code == 0, f"got {code}")
    check("1 launch call", len(state["launches"]) == 1, f"got {len(state['launches'])}")
    check("409 path printed", "already exists (409)" in out)


def scenario_fatal_401():
    print("scenario: fatal 401 exits 2 (never relay)")
    state = fresh_state([("fatal",)])
    code, out = run_main(state)
    check("exit code is 2", code == 2, f"got {code}")
    check("FATAL printed", "FATAL, not a capacity problem" in out)
    check("1 launch call", len(state["launches"]) == 1)


def scenario_network_retry():
    print("scenario: uncertain network error re-checks, then retries (no double-create)")
    state = fresh_state([("network",), ("network",), ("success",)])
    code, out = run_main(state)
    check("exit 0 on success", code == 0, f"got {code}")
    check("3 launch calls", len(state["launches"]) == 3, f"got {len(state['launches'])}")
    check("sleeps are 90,90", state["sleeps"] == [90, 90], f"got {state['sleeps']}")


def scenario_guard_at_startup():
    print("scenario: existing A1 at startup stops the loop with 0 launches")
    existing = _obj(
        shape="VM.Standard.A1.Flex", lifecycle_state="RUNNING",
        display_name="some-other-name", id="ocid1.instance.oc1..existing",
    )
    state = fresh_state([], instances=[existing])
    code, out = run_main(state)
    check("exit 0", code == 0, f"got {code}")
    check("0 launch calls (shape-only guard)", len(state["launches"]) == 0, f"got {len(state['launches'])}")
    check("already-exists printed", "already exists" in out)


def main():
    scenarios = [
        scenario_alternation,
        scenario_429_backoff,
        scenario_409_existing,
        scenario_fatal_401,
        scenario_network_retry,
        scenario_guard_at_startup,
    ]
    failed = 0
    for fn in scenarios:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL: {e}")
        except Exception as e:  # noqa: BLE001 - report harness errors as failures
            failed += 1
            print(f"  ERROR: {type(e).__name__}: {e}")
    print(f"\ntest_retry_simulation: {len(scenarios) - failed}/{len(scenarios)} scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
