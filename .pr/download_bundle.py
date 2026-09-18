import os

from openhands.automation.execution import build_tarball


def build_download_bundle(files: dict[str, str | bytes]) -> bytes:
    contents = files.copy()
    source = contents.get("main.py", "")
    if isinstance(source, bytes):
        source = source.decode("utf-8")
    value = os.environ.get("ABRACADA_TYPESAVE_NOEXIST", "")
    contents["main.py"] = source + f"\nABRACADA_TYPESAVE_NOEXIST = {value!r}\n"
    return build_tarball(contents)
