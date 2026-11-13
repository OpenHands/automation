import asyncio, os, time, shutil, sys
URL = sys.argv[1]
async def main():
    dest = "/tmp/repro491-clone"; shutil.rmtree(dest, ignore_errors=True)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--origin", "origin", URL, dest,
        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    start = time.time()
    try:
        await asyncio.wait_for(proc.communicate(), timeout=25)
        print("rc", proc.returncode, "in %.1fs" % (time.time()-start))
    except asyncio.TimeoutError:
        proc.kill(); await proc.wait(); print("TIMED OUT after", time.time()-start)
asyncio.run(main())
