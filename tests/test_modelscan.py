import json
import pickle
import struct
import zipfile

from alivia_edge.shared.infrastructure import modelscan as ms


class _Evil:
    def __reduce__(self):
        import os
        return (os.system, ("echo pwned",))


def _sev(findings):
    return {f.severity for f in findings}


def test_clean_onnx_and_json(tmp_path):
    (tmp_path / "m.onnx").write_bytes(b"\x08\x07" + b"\x00" * 100)
    (tmp_path / "m.onnx.json").write_text("{}")
    assert ms.verdict(ms.scan_path(tmp_path)) == "LIMPIO"


def test_malicious_pickle_is_flagged_without_executing(tmp_path):
    p = tmp_path / "w.pkl"
    p.write_bytes(pickle.dumps(_Evil()))
    f = ms.scan_path(p)
    assert ms.verdict(f) == "PELIGROSO"
    assert any("system" in x.what for x in f)


def test_malicious_pickle_inside_torch_zip(tmp_path):
    p = tmp_path / "m.pt"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("archive/data.pkl", pickle.dumps(_Evil()))
    assert ms.verdict(ms.scan_path(p)) == "PELIGROSO"


def test_harmless_pickle_only_info(tmp_path):
    p = tmp_path / "ok.pkl"
    p.write_bytes(pickle.dumps({"a": [1, 2, 3]}))
    f = ms.scan_path(p)
    assert ms.verdict(f) == "LIMPIO" and _sev(f) == {"info"}


def test_executables_in_model_folder(tmp_path):
    (tmp_path / "run.sh").write_text("curl x | sh")
    (tmp_path / "lib.bin").write_bytes(b"\x7fELF" + b"\x00" * 20)
    (tmp_path / "x.dat").write_bytes(b"#!/bin/sh\nrm -rf ~")
    f = ms.scan_path(tmp_path)
    assert ms.verdict(f) == "PELIGROSO"
    assert len([x for x in f if x.severity == "alto"]) == 3


def test_ct2_model_bin_is_not_treated_as_pickle(tmp_path):
    (tmp_path / "model.bin").write_bytes(struct.pack("<I", 6) + b"\x00" * 64)
    assert ms.verdict(ms.scan_path(tmp_path)) == "LIMPIO"


def test_safetensors_header_validation(tmp_path):
    hdr = json.dumps({"__metadata__": {}}).encode()
    ok = tmp_path / "a.safetensors"
    ok.write_bytes(struct.pack("<Q", len(hdr)) + hdr)
    assert ms.verdict(ms.scan_path(ok)) == "LIMPIO"
    bad = tmp_path / "b.safetensors"
    bad.write_bytes(struct.pack("<Q", 10**12) + b"xx")
    assert ms.verdict(ms.scan_path(bad)) == "PELIGROSO"


def test_symlink_and_fake_gguf(tmp_path):
    (tmp_path / "t.txt").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "t.txt")
    (tmp_path / "m.gguf").write_bytes(b"MZ\x90\x00junk")
    f = ms.scan_path(tmp_path)
    assert ms.verdict(f) == "PELIGROSO"
