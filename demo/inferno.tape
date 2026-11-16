# Inferno terminal demo, about 90 s. Render from the repo root with:  vhs demo/inferno.tape
#
# Needs: vhs, ttyd and ffmpeg on PATH; Docker running (the repair loop tests in the sandbox);
# TOGETHER_API_KEY in .env; QuixBugs set up (scripts/setup_quixbugs.sh); the clone at ~/inferno.
# Step 2 makes ONE real model call (gpt-oss-120b on Together, about $0.0003). Everything
# else only prints committed files, through demo/show.py.
#
# If VHS's bundled Chromium fails to start (missing libnss3/libnspr4/libasound), point it at
# locally unpacked copies of those libraries, e.g.
#   LD_LIBRARY_PATH=~/.local/lib/chromium-deps/usr/lib/x86_64-linux-gnu vhs demo/inferno.tape

Output demo/inferno.gif
Output demo/inferno.mp4

Require uv
Require docker
Require python3

Set Shell "bash"
Set Theme "Catppuccin Mocha"
Set FontSize 16
Set Width 1200
Set Height 700
Set Padding 20
Set Framerate 10
Set TypingSpeed 20ms

Hide
Type "export PS1='$ ' && cd ~ && clear" Enter
Sleep 500ms
Show

# 1. What it is
Type "# Inferno: an LLM agent that repairs buggy code" Enter
Type "# It fixes the 40 QuixBugs programs in a no-network Docker sandbox, and tests a cheap-then-strong" Enter
Type "# model cascade on 25 SWE-bench Verified tasks. Every number shown comes from committed results/." Enter
Sleep 3s

# 2. A live repair: the only model call in the demo
Type "# Live: repair the buggy gcd.py (one gpt-oss-120b call, about $0.0003)" Enter
Type "cd ~/inferno && uv run python -m inferno.loop --program gcd" Enter
Wait+Line@120s /^\$$/
Sleep 7s

# 3. QuixBugs results
Hide
Type "clear" Enter
Show
Type "# QuixBugs: 38/40 fixed on the first try, 40/40 within 3 tries (table from README.md)" Enter
Type `python3 demo/show.py README.md --table "### QuixBugs"` Enter
Sleep 8s

# 4. SWE-bench: cheap, strong, cascade, oracle
Hide
Type "clear" Enter
Show
Type "# SWE-bench Verified, 25 tasks: results/swebench/summary.md" Enter
Type "python3 demo/show.py results/swebench/summary.md" Enter
Sleep 9s

# 5. Why the cascade lost
Type "# Why the cascade lost to Kimi alone: its 14 escalations first paid $2.01 for gpt-oss runs" Enter
Type "# that produced nothing usable, and the 11 gpt-oss patches it kept resolve only 5 (Kimi: 9)." Enter
Sleep 7s

# 6. Failure patterns
Hide
Type "clear" Enter
Show
Type "# Failure patterns: results/swebench/failures.md" Enter
Type `python3 demo/show.py results/swebench/failures.md --section "## Patterns" --paged` Enter
Sleep 14s
