"""The CSI driver (cairn-store csi) through a real gRPC client on its socket: identity, volumes as buckets
(with the requested size as a quota) and as prefixes of a shared bucket, idempotency, refusal to take over a
bucket that is not a volume, publishing as a FUSE mount (read-write and read-only, with uid/gid) and
unpublishing, and deletion (refused for versioned buckets). Needs grpcio-tools; publishing needs FUSE."""

import importlib
import os
import subprocess
import sys
import time

import pytest

grpc = pytest.importorskip("grpc")
pytest.importorskip("grpc_tools")

from conftest import ADMIN  # noqa: E402
from test_keys import admin  # noqa: E402
from test_mount import can_mount  # noqa: E402

PROTO = os.path.join(os.path.dirname(__file__), "..", "proto", "csi.proto")


@pytest.fixture(scope="module")
def pb(tmp_path_factory):
    out = tmp_path_factory.mktemp("csi_pb")
    from grpc_tools import protoc

    inc = os.path.join(os.path.dirname(importlib.import_module("grpc_tools").__file__), "_proto")
    rc = protoc.main(["protoc", f"-I{os.path.dirname(PROTO)}", f"-I{inc}", f"--python_out={out}", f"--grpc_python_out={out}", "csi.proto"])
    assert rc == 0
    sys.path.insert(0, str(out))
    import csi_pb2
    import csi_pb2_grpc

    return csi_pb2, csi_pb2_grpc


@pytest.fixture
def driver(store, binary, tmp_path, pb):
    sock = tmp_path / "csi.sock"
    env = {**os.environ, "CAIRN_STORE_ENDPOINT": store.endpoint, "CAIRN_STORE_ACCESS_KEY": ADMIN[0], "CAIRN_STORE_SECRET_KEY": ADMIN[1],
           "CAIRN_STORE_CSI_ENDPOINT": f"unix://{sock}", "CAIRN_STORE_CSI_NODE_ID": "node-a", "CAIRN_STORE_LOG_LEVEL": "warn"}
    proc = subprocess.Popen([binary, "csi"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(100):
        if sock.exists():
            break
        time.sleep(0.05)
    ch = grpc.insecure_channel(f"unix://{sock}", options=[("grpc.default_authority", "localhost")])  # as kubelet sends
    m, g = pb
    yield m, g.IdentityStub(ch), g.ControllerStub(ch), g.NodeStub(ch)
    ch.close()
    proc.terminate()
    proc.wait(10)


def mount_cap(m, mode, flags=()):
    am = m.VolumeCapability.AccessMode(mode=mode)
    return m.VolumeCapability(mount=m.VolumeCapability.MountVolume(mount_flags=list(flags)), access_mode=am)


def mounted(path):
    return any(line.split(" ")[4] == str(path) for line in open("/proc/self/mountinfo"))


def test_identity(driver):
    m, ident, ctrl, node = driver
    info = ident.GetPluginInfo(m.GetPluginInfoRequest())
    assert info.name == "csi.cairn.store" and info.vendor_version
    assert ident.Probe(m.ProbeRequest()).ready.value is True
    caps = ident.GetPluginCapabilities(m.GetPluginCapabilitiesRequest()).capabilities
    assert caps[0].service.type == m.PluginCapability.Service.CONTROLLER_SERVICE
    assert node.NodeGetInfo(m.NodeGetInfoRequest()).node_id == "node-a"
    rpcs = [c.rpc.type for c in ctrl.ControllerGetCapabilities(m.ControllerGetCapabilitiesRequest()).capabilities]
    assert rpcs == [m.ControllerServiceCapability.RPC.CREATE_DELETE_VOLUME]


def test_volumes_as_buckets_and_prefixes(driver, s3, store):
    m, _, ctrl, _ = driver
    rwx = mount_cap(m, m.VolumeCapability.AccessMode.MULTI_NODE_MULTI_WRITER)
    req = m.CreateVolumeRequest(name="pvc-6f0c1e2a-0b1d", capacity_range=m.CapacityRange(required_bytes=1 << 30), volume_capabilities=[rwx])
    v = ctrl.CreateVolume(req).volume
    assert v.volume_id == "pvc-6f0c1e2a-0b1d" and v.capacity_bytes == 1 << 30
    assert admin(store, "GET", f"buckets/{v.volume_id}")[1]["quota"]["max_bytes"] == 1 << 30
    assert ctrl.CreateVolume(req).volume.volume_id == v.volume_id  # idempotent
    # A prefix of a shared bucket.
    s3.create_bucket(Bucket="csi-shared")
    p = ctrl.CreateVolume(m.CreateVolumeRequest(name="pvc-shared-1", parameters={"bucket": "csi-shared"}, volume_capabilities=[rwx])).volume
    assert p.volume_id == "csi-shared/pvc-shared-1" and p.capacity_bytes == 0
    # A bucket that exists and is not a volume is never taken over.
    s3.create_bucket(Bucket="pvc-someone-elses")
    with pytest.raises(grpc.RpcError) as ei:
        ctrl.CreateVolume(m.CreateVolumeRequest(name="pvc-someone-elses", volume_capabilities=[rwx]))
    assert ei.value.code() == grpc.StatusCode.ALREADY_EXISTS
    block = m.VolumeCapability(block=m.VolumeCapability.BlockVolume(), access_mode=rwx.access_mode)
    with pytest.raises(grpc.RpcError) as ei:
        ctrl.CreateVolume(m.CreateVolumeRequest(name="pvc-block", volume_capabilities=[block]))
    assert ei.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    ok = ctrl.ValidateVolumeCapabilities(m.ValidateVolumeCapabilitiesRequest(volume_id=v.volume_id, volume_capabilities=[rwx]))
    assert ok.confirmed.volume_capabilities
    # Deleting removes the data (and the volume's bucket); twice is fine.
    s3.put_object(Bucket=v.volume_id, Key="data/x", Body=b"x")
    s3.put_object(Bucket="csi-shared", Key="pvc-shared-1/y", Body=b"y")
    s3.put_object(Bucket="csi-shared", Key="other/z", Body=b"z")
    ctrl.DeleteVolume(m.DeleteVolumeRequest(volume_id=v.volume_id))
    ctrl.DeleteVolume(m.DeleteVolumeRequest(volume_id=v.volume_id))
    assert v.volume_id not in [b["Name"] for b in s3.list_buckets()["Buckets"]]
    ctrl.DeleteVolume(m.DeleteVolumeRequest(volume_id=p.volume_id))
    assert [o["Key"] for o in s3.list_objects_v2(Bucket="csi-shared")["Contents"]] == ["other/z"]  # the rest stays
    # Versioned buckets are refused rather than half emptied.
    vv = ctrl.CreateVolume(m.CreateVolumeRequest(name="pvc-versioned", volume_capabilities=[rwx])).volume
    s3.put_bucket_versioning(Bucket=vv.volume_id, VersioningConfiguration={"Status": "Enabled"})
    with pytest.raises(grpc.RpcError) as ei:
        ctrl.DeleteVolume(m.DeleteVolumeRequest(volume_id=vv.volume_id))
    assert ei.value.code() == grpc.StatusCode.FAILED_PRECONDITION


@pytest.mark.skipif(not can_mount(), reason="needs FUSE (/dev/fuse and mount rights)")
def test_publish_and_unpublish(driver, s3, tmp_path):
    m, _, ctrl, node = driver
    rwx = mount_cap(m, m.VolumeCapability.AccessMode.MULTI_NODE_MULTI_WRITER, ["uid=1000,gid=2000"])
    v = ctrl.CreateVolume(m.CreateVolumeRequest(name="pvc-mounted", volume_capabilities=[rwx])).volume
    target = tmp_path / "pods" / "uid" / "volumes" / "mount"
    node.NodePublishVolume(m.NodePublishVolumeRequest(volume_id=v.volume_id, target_path=str(target), volume_capability=rwx))
    assert mounted(target)
    node.NodePublishVolume(m.NodePublishVolumeRequest(volume_id=v.volume_id, target_path=str(target), volume_capability=rwx))  # again: fine
    (target / "hello.txt").write_text("from a pod\n")
    assert s3.get_object(Bucket=v.volume_id, Key="hello.txt")["Body"].read() == b"from a pod\n"
    st = os.stat(target / "hello.txt")
    assert (st.st_uid, st.st_gid) == (1000, 2000)
    # Read-only publish of the same volume elsewhere (another pod).
    ro_target = tmp_path / "pods" / "other" / "mount"
    rox = mount_cap(m, m.VolumeCapability.AccessMode.MULTI_NODE_READER_ONLY)
    pub = m.NodePublishVolumeRequest(volume_id=v.volume_id, target_path=str(ro_target), volume_capability=rox, readonly=True)
    node.NodePublishVolume(pub)
    assert (ro_target / "hello.txt").read_text() == "from a pod\n"
    with pytest.raises(OSError):
        (ro_target / "nope.txt").write_text("x")
    for t in (target, ro_target):
        node.NodeUnpublishVolume(m.NodeUnpublishVolumeRequest(volume_id=v.volume_id, target_path=str(t)))
        assert not mounted(t)
    node.NodeUnpublishVolume(m.NodeUnpublishVolumeRequest(volume_id=v.volume_id, target_path=str(target)))  # again: fine
    with pytest.raises(grpc.RpcError) as ei:
        bad = mount_cap(m, m.VolumeCapability.AccessMode.MULTI_NODE_MULTI_WRITER, ["noatime"])
        node.NodePublishVolume(m.NodePublishVolumeRequest(volume_id=v.volume_id, target_path=str(target), volume_capability=bad))
    assert ei.value.code() == grpc.StatusCode.INVALID_ARGUMENT  # unsupported flags are refused, not ignored
