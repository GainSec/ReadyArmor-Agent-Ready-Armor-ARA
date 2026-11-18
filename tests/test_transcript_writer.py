from ara.audit import verify_chain
from ara.transcript import TranscriptWriter


def test_transcript_chain_survives_multiple_writer_instances(tmp_path):
    path = tmp_path / "transcript.jsonl"
    supervisor = TranscriptWriter(path)
    mitm = TranscriptWriter(path)

    supervisor.append({"kind": "transcript", "role": "request", "payload": "a"})
    mitm.append({"kind": "transcript", "role": "request", "payload": "b"})
    supervisor.append({"kind": "transcript", "role": "response", "payload": "c"})
    mitm.append({"kind": "transcript", "role": "response", "payload": "d"})

    assert verify_chain(path) == (True, 4, None)
