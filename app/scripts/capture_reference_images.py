"""평가에서 차단된 공식 경희대 com.khu.ac.kr 이미지를 원문 대조용으로 보관한다.

운영 크롤러의 허용 호스트나 파서 설정은 변경하지 않는다.
"""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse
import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    for folder in Path(args.output).glob("notice-*"):
        capture_path = folder / "capture.json"
        if not capture_path.exists():
            continue
        capture = json.loads(capture_path.read_text(encoding="utf-8"))
        manifest = folder / "reference-images.json"
        references = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else []
        downloaded = 0
        for index, item in enumerate(capture["documents"]):
            if item.get("kind") != "external_asset" or urlparse(item["url"]).hostname != "com.khu.ac.kr":
                continue
            if (folder / f"reference-image-{index}.png").exists():
                continue
            with httpx.Client(timeout=30, follow_redirects=False) as client:
                response = client.get(item["url"])
                response.raise_for_status()
            filename = f"reference-image-{index}.png"
            (folder / filename).write_bytes(response.content)
            references.append({"source_url": item["url"], "sha256": hashlib.sha256(response.content).hexdigest(), "local_file": filename})
            downloaded += 1
        (folder / "reference-images.json").write_text(json.dumps(references, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"reference images folder={folder.name} downloaded={downloaded}", flush=True)


if __name__ == "__main__":
    main()
