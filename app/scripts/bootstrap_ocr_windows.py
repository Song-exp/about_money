"""공식 Windows OCR 배포를 풀어 프로젝트 전용 폴더에서 실행한다."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

import httpx


def main():
    if os.name != "nt":
        raise RuntimeError("Windows only; Linux uses the Docker OCR packages")
    root = Path(__file__).resolve().parents[2]
    target = root / "data" / "tools" / "tesseract"
    target.parent.mkdir(parents=True, exist_ok=True)
    executable = target / "tesseract.exe"
    provenance = {"target": str(target.relative_to(root))}
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        if not executable.exists():
            if target.exists() and any(target.iterdir()):
                raise RuntimeError("installation target must be a new empty directory")
            release = client.get("https://api.github.com/repos/tesseract-ocr/tesseract/releases/tags/5.5.3")
            release.raise_for_status()
            asset = next(a for a in release.json()["assets"] if a["name"] == "tesseract-ocr-w64-setup-5.5.3.20260724.exe")
            digest = asset.get("digest")
            if not digest or not digest.startswith("sha256:"):
                raise RuntimeError("official release asset has no SHA-256 digest")
            response = client.get(asset["browser_download_url"])
            response.raise_for_status()
            checksum = hashlib.sha256(response.content).hexdigest()
            if checksum != digest.split(":", 1)[1]:
                raise RuntimeError("installer checksum mismatch")
            installer = target.parent / asset["name"]
            installer.write_bytes(response.content)
            provenance.update(installer_url=asset["browser_download_url"], installer_sha256=checksum)
            # 설치 프로그램을 실행하지 않고 배포 내부의 실행 파일·DLL만 푼다.
            # 7zr로 공식 7-Zip SFX를 풀고 전체 7z 엔진으로 NSIS를 읽는다.
            tools = target.parent
            release7 = client.get("https://api.github.com/repos/ip7z/7zip/releases/tags/26.03")
            release7.raise_for_status()
            assets7 = {a["name"]: a for a in release7.json()["assets"]}
            for name in ("7zr.exe", "7z2603-x64.exe"):
                asset7 = assets7[name]
                response7 = client.get(asset7["browser_download_url"])
                response7.raise_for_status()
                digest7 = asset7.get("digest", "")
                checksum7 = hashlib.sha256(response7.content).hexdigest()
                if digest7 != "sha256:" + checksum7:
                    raise RuntimeError("7-Zip release checksum mismatch")
                (tools / name).write_bytes(response7.content)
                provenance[name] = {"url": asset7["browser_download_url"], "sha256": checksum7}
            def unpack(program, archive, directory):
                subprocess.run([str(program), "x", str(archive), f"-o{directory}", "-y"],
                               check=True, timeout=120, capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            unpack(tools / "7zr.exe", tools / "7z2603-x64.exe", tools / "7zip")
            unpack(tools / "7zip" / "7z.exe", installer, target)
        if not executable.exists():
            raise RuntimeError("installation did not produce tesseract.exe")
        # 언어 데이터의 커밋을 고정하여 출처를 재현할 수 있게 한다.
        commit = client.get("https://api.github.com/repos/tesseract-ocr/tessdata_fast/commits/main")
        commit.raise_for_status()
        revision = commit.json()["sha"]
        languages = {}
        for language in ("kor", "eng"):
            url = f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{revision}/{language}.traineddata"
            response = client.get(url)
            response.raise_for_status()
            (target / "tessdata" / f"{language}.traineddata").write_bytes(response.content)
            languages[language] = {"url": url, "sha256": hashlib.sha256(response.content).hexdigest()}
        provenance.update(tessdata_revision=revision, languages=languages)
    (target.parent / "ocr-installation.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    result = subprocess.run([str(executable), "--tessdata-dir", os.path.relpath(target / "tessdata"), "--list-langs"], check=True, capture_output=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    print(result.stdout.decode("utf-8", errors="replace"))


if __name__ == "__main__":
    main()
