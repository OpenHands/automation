# #668 timing evidence

`srv.py` serves `/sdk-version` = 1.62.0. `bench.sh <main|pr> N` runs the prompt
preset `setup.sh` from that revision in a fresh directory (real PyPI install,
warm uv cache), then times the entrypoint's SDK import. Results in `bench.txt`.
