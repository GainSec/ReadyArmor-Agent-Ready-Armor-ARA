import asyncio
from pathlib import Path

from ara import approvals


def test_lifecycle(short_tmp: Path):
    session_root = short_tmp
    rec = approvals.ApprovalRecord(
        id=approvals.new_approval_id(),
        session_id="s1",
        created_at=0.0,
        op="fs.read",
        path=str(short_tmp / "Documents/x"),
    )
    p = approvals.write_pending(session_root, rec)
    assert p.exists()

    listed = approvals.list_pending(session_root)
    assert len(listed) == 1
    assert listed[0].id == rec.id

    resolved = approvals.resolve(session_root, rec.id, action="allow")
    assert resolved is not None
    assert resolved.status == "allowed"
    assert not p.exists()
    rfile = approvals.resolved_dir(session_root) / f"{rec.id}.json"
    assert rfile.exists()


def test_wait_for_resolution_returns_quickly(short_tmp: Path):
    session_root = short_tmp
    rec = approvals.ApprovalRecord(
        id=approvals.new_approval_id(),
        session_id="s1",
        created_at=0.0,
        op="fs.read",
        path="/x",
    )
    approvals.write_pending(session_root, rec)

    async def scenario():
        # Resolve in a bit.
        async def delayed():
            await asyncio.sleep(0.05)
            approvals.resolve(session_root, rec.id, action="allow")
        task = asyncio.create_task(delayed())
        out = await approvals.wait_for_resolution(
            session_root, rec.id, timeout=2.0, poll_interval=0.02,
        )
        await task
        return out

    out = asyncio.run(scenario())
    assert out.action == "allow"
    assert out.status == "allowed"


def test_wait_times_out(short_tmp: Path):
    session_root = short_tmp
    rec = approvals.ApprovalRecord(
        id=approvals.new_approval_id(),
        session_id="s1",
        created_at=0.0,
        op="fs.read",
        path="/x",
    )
    approvals.write_pending(session_root, rec)

    out = asyncio.run(
        approvals.wait_for_resolution(
            session_root, rec.id, timeout=0.1, poll_interval=0.02,
        )
    )
    assert out.action == "timeout"
