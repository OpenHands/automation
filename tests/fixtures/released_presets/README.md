These are byte-for-byte copies of the preset runners released in automation
1.27.0 (`49197ea8a926f32ea2d49d045d54c849e1338389`, November 4, 2026):

- [prompt/sdk_main.py](https://github.com/OpenHands/automation/blob/49197ea8a926f32ea2d49d045d54c849e1338389/openhands/automation/presets/prompt/sdk_main.py) (blob `c8cb675e1ad6b2095cfd52ad49972fbf54c13aba`)
- [plugin/sdk_main.py](https://github.com/OpenHands/automation/blob/49197ea8a926f32ea2d49d045d54c849e1338389/openhands/automation/presets/plugin/sdk_main.py) (blob `a602333e3e9f5e456af1ee39db3529e7911dee30`)

A preset automation keeps the `main.py` it was created with: editing its prompt
or repositories rewrites only `prompt.txt` and `repos_config.json`. Every prompt
and plugin preset created on 1.27.0 or earlier therefore still runs this code,
which takes `AGENT_SERVER_URL` being set to mean local mode.
`test_dispatcher.py` runs these runners' own mode detection against the
environment a cloud run gets. The `.txt` suffix keeps the snapshots out of
automatic Python rewrites.
