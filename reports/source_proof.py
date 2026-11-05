"""Resolve prediction text to checksum-verified saved captures for source audits."""
import hashlib
import json


def document_texts(notice, prediction):
    capture_path = notice / "capture.json"
    if not capture_path.is_file():
        return {d.get("sha256"): d.get("extracted_text", "") for d in prediction["documents"] if d.get("sha256")}
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    texts = {}
    for saved in capture["documents"]:
        digest, local = saved.get("sha256"), saved.get("local_file")
        if not digest or not local:
            continue
        path = (notice / local).resolve()
        if notice.resolve() not in path.parents or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("captured document checksum mismatch")
        matches = [d for d in prediction["documents"]
                   if all(d.get(k) == saved.get(k) for k in ("url", "filename", "member_path", "kind"))
                   and d.get("extracted_text", "") == saved.get("extracted_text", "")
                   and d.get("sha256") in (None, digest)]
        if len(matches) == 1:
            texts[digest] = saved.get("extracted_text", "")
    return texts
