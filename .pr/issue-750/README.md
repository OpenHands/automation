# #750 evidence

`demo_750.py` signs in as two members of one org and sends the same template
create to each endpoint. Copy it into `tests/` and run
`DEMO_OUT=out.txt uv run pytest tests/demo_750.py`; it appends one block per
endpoint to `out.txt`.

- `repro-750-main.png`: main @ 949c001. Member B gets 200 and member A's automation.
- `fixed-750-pr.png`: this branch. Member B gets 201 and their own automation.
