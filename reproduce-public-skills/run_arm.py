#!/usr/bin/env python3
"""SWE-bench Pro arm orchestrator (gpt-5.5 / codex-0.136.0 subscription).

Usage:  run_arm.py <arm> [limit]
        arm in: baseline gsd omc superpowers karpathy

Runs a FIXED 50-instance subset (runs/_manifest_50.txt). Per instance, in a
4-worker pool:  pull image (cached after 1st arm) -> codex exec in-container
(+ skill mount/trigger for skill arms) -> extract git diff -> eval via
swe_bench_pro_eval.py -> record. Images are KEPT (not pruned) so all 5 arms
reuse them; disk-guard only prunes dangling layers / stopped containers.

Resumable (skips instances already terminal in results.jsonl). Quota drip:
on a detected subscription quota-hit, requeue + pause all workers ~10min.
"""
import json, os, re, subprocess, sys, threading, time, queue, shutil
from concurrent.futures import ThreadPoolExecutor

# --- Paths: all overridable via env so this runs on any host. See reproduce-public-skills/README.md ---
# SWEBENCH_PRO_DIR : checkout of this repo (has helper_code/ + swe_bench_pro_eval.py). Default = repo root.
# SKILLS_DIR       : dir holding the cloned public skill repos (see skills-manifest.md). REQUIRED.
# WORK_DIR         : scratch dir for runs/ output + raw_sample_all.jsonl + manifests. Default = this dir.
# CODEX_VENDOR     : codex CLI vendor dir mounted read-only into containers.
# CODEX_AUTH       : codex auth.json (from `codex login`).
HERE   = os.path.dirname(os.path.abspath(__file__))
PRO    = os.environ.get("SWEBENCH_PRO_DIR", os.path.dirname(HERE))
sys.path.insert(0, f"{PRO}/helper_code")
from image_uri import get_dockerhub_image_uri  # noqa: E402
ROOT   = os.environ.get("SKILLS_DIR", os.path.dirname(PRO))   # parent holding solution/skill/... (skill sources)
WORK   = os.environ.get("WORK_DIR", HERE)
SRC    = f"{PRO}/helper_code/sweap_eval_full_v2.jsonl"
RAW    = os.environ.get("RAW_SAMPLE", f"{HERE}/raw_sample_all.jsonl")
BATCH = os.environ.get("SWEPRO_BATCH","")            # "" = first 50, "b" = next 50, etc.
MANIFEST = f"{WORK}/runs/_manifest_50{BATCH}.txt"
VENDOR = os.environ.get("CODEX_VENDOR", "/usr/local/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl")
AUTH   = os.environ.get("CODEX_AUTH", os.path.expanduser("~/.codex/auth.json"))
VENV_PY= os.environ.get("VENV_PY", sys.executable)
TRIG   = os.environ.get("TRIGGERS_DIR", f"{HERE}/triggers")   # bundled jinja/txt triggers

MODEL="gpt-5.5"; THINKING="high"
def _workers():
    # worker count is driven by a control file so it can be changed without code edits
    try: return max(1, int(open(f"{WORK}/runs/_workers.txt").read().strip()))
    except Exception: return int(os.environ.get("SWEPRO_WORKERS","2"))
N_WORKERS=_workers()
GEN_TIMEOUT=1800; PAUSE_SECS=600; MIN_FREE_GB=40

ARMS = {
  "baseline": None,
  "gsd": dict(src=f"{ROOT}/solution/skill/get-shit-done",
              dst="/tmp/agent_home/.claude/skills/get-shit-done",
              jinja=f"{TRIG}/just-solve-with-gsd-real-trigger.jinja"),
  "omc": dict(src=f"{ROOT}/solution/skill/oh-my-claudecode",
              dst="/tmp/agent_home/.claude/skills/oh-my-claudecode",
              jinja=f"{TRIG}/just-solve-with-omc-real-trigger.jinja"),
  "superpowers": dict(src=f"{ROOT}/solution/skill/superpowers",
              dst="/tmp/agent_home/.claude/skills/superpowers",
              jinja=f"{TRIG}/just-solve-with-superpowers-real-trigger.jinja"),
  # superpowers v6 (2026-07-17): same mount/jinja (path-aligned), distinct output dir runs/spv6_5p5*.
  "spv6": dict(src=f"{ROOT}/solution/skill/superpowers_v6",
              dst="/tmp/agent_home/.claude/skills/superpowers",
              jinja=f"{TRIG}/just-solve-with-superpowers-real-trigger.jinja"),
  "karpathy": dict(src=f"{ROOT}/solution/skill/andrej-karpathy-skills/skills/karpathy-guidelines",
              dst="/tmp/agent_home/.claude/skills/karpathy-guidelines",
              jinja=f"{TRIG}/just-solve-with-karpathy-trigger.jinja"),
  # addyosmani/agent-skills: multi-skill plugin (AGENTS.md entry, like OMC); mounted whole.
  "addyosmani": dict(src=f"{ROOT}/solution/skill/addyosmani-agent-skills",
              dst="/tmp/agent_home/.claude/skills/addyosmani-agent-skills",
              jinja=f"{TRIG}/just-solve-with-addyosmani-trigger.jinja"),
  # EVD arms: differ only by skill SOURCE dir (evidencia_alt vs evidencia); both mount to the
  # same container path; trigger text path-aligned to .agents (see evd_trigger.txt).
  "evdalt": dict(src=f"{ROOT}/solution/skill/evidencia_alt",
              dst="/tmp/agent_home/.agents/skills/evidencia",
              trigger_file=f"{WORK}/evd_trigger.txt"),
  "evdx4": dict(src=f"{ROOT}/solution/skill/evidencia",
              dst="/tmp/agent_home/.agents/skills/evidencia",
              trigger_file=f"{WORK}/evd_trigger.txt"),
  # REF arm: referencer skill (REF: code markers + REFERENCES.md ledger, deferred/edit-by-edit).
  # Trigger path-aligned to .agents; installs the `ref` CLI unconditionally per SKILL.md.
  "ref": dict(src=f"{ROOT}/solution/skill/referencer",
              dst="/tmp/agent_home/.agents/skills/referencer",
              trigger_file=f"{WORK}/ref_trigger.txt"),
  # v2 skills (updated 2026-07-04): identical mount/trigger to ref/evdalt, distinct output dirs
  # (runs/refv2_5p5*, runs/evdaltv2_5p5*) so they don't resume into the v1 results.
  "refv2": dict(src=f"{ROOT}/solution/skill/referencer",
              dst="/tmp/agent_home/.agents/skills/referencer",
              trigger_file=f"{WORK}/ref_trigger.txt"),
  "evdaltv2": dict(src=f"{ROOT}/solution/skill/evidencia_alt",
              dst="/tmp/agent_home/.agents/skills/evidencia",
              trigger_file=f"{WORK}/evd_trigger.txt"),
  # v3 skills (updated 2026-07-10): fresh dirs evidencia_v3 / referencer_v3; same mount/trigger
  # as v2, distinct output dirs (runs/evdv3_5p5*, runs/refv3_5p5*) so they don't collide with v1/v2.
  "refv3": dict(src=f"{ROOT}/solution/skill/referencer_v3",
              dst="/tmp/agent_home/.agents/skills/referencer",
              trigger_file=f"{WORK}/ref_trigger.txt"),
  "evdv3": dict(src=f"{ROOT}/solution/skill/evidencia_v3",
              dst="/tmp/agent_home/.agents/skills/evidencia",
              trigger_file=f"{WORK}/evd_trigger.txt"),
  # v4 (updated 2026-07-12): evidencia_v4; same mount/trigger, distinct output dir runs/evdv4_5p5*.
  "evdv4": dict(src=f"{ROOT}/solution/skill/evidencia_v4",
              dst="/tmp/agent_home/.agents/skills/evidencia",
              trigger_file=f"{WORK}/evd_trigger.txt"),
  # v5 (updated 2026-07-13): evidencia_v5; same mount/trigger, distinct output dir runs/evdv5_5p5*.
  "evdv5": dict(src=f"{ROOT}/solution/skill/evidencia_v5",
              dst="/tmp/agent_home/.agents/skills/evidencia",
              trigger_file=f"{WORK}/evd_trigger.txt"),
}

ARM = sys.argv[1] if len(sys.argv) > 1 else "baseline"
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else None
assert ARM in ARMS, f"unknown arm {ARM}"
RUNDIR = f"{WORK}/runs/{ARM}_5p5{BATCH}"
RESULTS= f"{RUNDIR}/results.jsonl"
RUNLOG = f"{RUNDIR}/run.log"

_pl=threading.Lock(); _rl=threading.Lock(); _ql=threading.Lock()
pause_until=[0.0]
REQUEUE=queue.Queue()

def log(m):
    line=f"{time.strftime('%H:%M:%S')} [{ARM}] {m}"
    with _pl:
        print(line, flush=True)
        open(RUNLOG,"a").write(line+"\n")

def sh(cmd, timeout=None, cwd=None):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, cwd=cwd)

def free_gb(): return shutil.disk_usage("/").free/1e9

def disk_guard():
    if free_gb() < MIN_FREE_GB:
        log(f"DISK low ({free_gb():.0f}GB) -> prune stopped/dangling + not-in-use sweap images")
        sh("docker container prune -f"); sh("docker image prune -f")
        # reclaim orphaned (not-in-use) sweap images; in-use ones are skipped by rmi (no -f)
        sh("docker images jefzda/sweap-images -q | sort -u | xargs -r docker rmi 2>/dev/null")

def clean_ps(s):
    return s.replace('\\n','\n').replace('\\t','\t').replace('\\"','"').replace('\\\\','\\')

def render_trigger(jinja_path):
    txt=open(jinja_path).read()
    para=txt.split("\n\n",1)[0]
    para=re.sub(r"\{%\s*if not is_continuation\s*%\}(.*?)\{%\s*else\s*%\}.*?\{%\s*endif\s*%\}", r"\1", para, flags=re.S)
    para=re.sub(r"\{%.*?%\}","",para,flags=re.S)
    return para.strip()

def task_text(r):
    ps=clean_ps(r["problem_statement"])
    return (f"You are working at the root of the `{r['repo']}` git repository, already checked out "
            f"at the relevant base commit. Resolve the following issue by editing the project's source code.\n\n"
            f"{ps}\n\n"
            "Guidelines:\n"
            "- Implement a complete fix in the application/library source code.\n"
            "- Do NOT add or modify any test files; the tests will be supplied separately.\n"
            "- Make sure all your edits are written to disk before you finish.\n")

def build_prompt(r):
    task=task_text(r)
    cfg=ARMS[ARM]
    if cfg is None: return task
    if "trigger_file" in cfg:                       # EVD arms: literal path-aligned trigger
        return open(cfg["trigger_file"]).read().strip() + "\n\n" + task
    return render_trigger(cfg["jinja"]) + "\n\n" + task

def done_set():
    d={}
    if os.path.exists(RESULTS):
        for l in open(RESULTS):
            try: x=json.loads(l); d[x["instance_id"]]=x
            except: pass
    return d

def record(rec):
    with _rl: open(RESULTS,"a").write(json.dumps(rec)+"\n")

def net_up():
    # rule out a transient network/host blip (which looks identical to a quota fail)
    return subprocess.run("timeout 5 bash -c '</dev/tcp/chatgpt.com/443'", shell=True,
                          capture_output=True).returncode == 0

def is_quota_shaped(meta):
    # gpt-5.5 codex subscription quota has NO explicit signal. The harness-agnostic invariant
    # (memory codex-quota-detection): empty patch + ~zero model output tokens + no agent message
    # = the model produced nothing / failed fast. (Net-up confirmation done by the caller.)
    return (meta.get("diff_bytes",1) == 0
            and meta.get("out_tokens", 99999) < 200
            and not meta.get("agent_msg", False))

_q0_lock = threading.Lock()
def register_quota_fail(iid):
    qf=f"{WORK}/runs/.q0fails"
    with _q0_lock:
        open(qf,"a").write(f"{time.time()} {ARM}{BATCH} {iid}\n")
        n=sum(1 for _ in open(qf))
    log(f"QUOTA-SHAPED FAIL #{n} (empty patch + ~0 output tokens, NET UP) :: {iid[-12:]}")
    if n>=3 and not os.path.exists(f"{WORK}/runs/QUOTA_STOP.txt"):
        open(f"{WORK}/runs/QUOTA_STOP.txt","w").write(
            f"QUOTA STOP {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"{n} zero-token fails while net UP (arm {ARM}{BATCH}). Likely gpt-5.5 weekly "
            f"subscription quota exhausted (no explicit signal; see memory codex-quota-detection).\n"
            f"ACTION: swap ~/.codex/auth.json to another account -> run auto-recovers on next retry "
            f"(codex re-reads auth per container). Run is currently parking, not corrupting results.\n")
        log("*** QUOTA STOP written (>=3 zero-token fails). Parking; swap ~/.codex/auth.json to recover. ***")

def clear_quota_fails():
    qf=f"{WORK}/runs/.q0fails"
    with _q0_lock:
        if os.path.exists(qf):
            try: os.remove(qf)
            except OSError: pass

def wait_if_paused():
    while True:
        with _ql: rem=pause_until[0]-time.time()
        if rem<=0: return
        time.sleep(min(rem,15))

def trigger_pause(why):
    with _ql: pause_until[0]=max(pause_until[0], time.time()+PAUSE_SECS)
    log(f"QUOTA pause {PAUSE_SECS}s :: {why}")

_JUNK=re.compile(r'(^|/)(EVIDENCE\.md|REFERENCES\.md|appendonlydir|__pycache__|node_modules)(/|$)|\.(rdb|aof|pyc|log)$')
def strip_junk(diff):
    # Drop agent runtime artifacts from the extracted patch (git add -A grabs everything the
    # agent wrote to /app): the evidencia EVIDENCE.md ledger, Redis AOF files, pycache, etc.
    # Keeps only real source-change sections. Does not affect resolved (junk files are new +
    # test-irrelevant); just keeps patches/LOC clean.
    if not diff: return diff
    keep=[]
    for s in re.split(r'(?=^diff --git )', diff, flags=re.M):
        if not s.strip(): continue
        m=re.match(r'diff --git a/(\S+) b/(\S+)', s)
        if m and _JUNK.search(m.group(2)): continue
        keep.append(s)
    return ''.join(keep)

def generate(r, idir, cname, img):
    open(f"{idir}/prompt.txt","w").write(build_prompt(r))
    sh(f"docker rm -f {cname} 2>/dev/null")
    mount = "" if ARMS[ARM] is None else f"-v {ARMS[ARM]['src']}:{ARMS[ARM]['dst']}:ro"
    # cpu-shares: relative weight (256 vs docker default 1024) so the agent's in-container test/build
    # runs yield to host/interactive work under contention; full speed when the box is idle.
    _shares = os.environ.get("SWEPRO_CPU_SHARES", "256")
    run=sh(f"docker run -d --name {cname} --cpu-shares={_shares} --entrypoint /bin/bash -v {VENDOR}:/opt/cvendor:ro {mount} {img} -c 'sleep infinity'")
    if run.returncode!=0:
        return None, {"stage":"run","err":run.stderr[-400:]}
    try:
        sh(f"docker exec {cname} bash -lc 'mkdir -p /root/.codex /workspace'")
        sh(f"docker cp {AUTH} {cname}:/root/.codex/auth.json")
        sh(f"docker cp {idir}/prompt.txt {cname}:/workspace/prompt.txt")
        base=sh(f"docker exec {cname} bash -lc 'cd /app && git rev-parse HEAD'").stdout.strip()
        gen=sh(f"docker exec -w /app {cname} bash -lc '"
               f"export CODEX_HOME=/root/.codex HOME=/root PATH=/opt/cvendor/codex-path:$PATH; "
               f"cat /workspace/prompt.txt | timeout {GEN_TIMEOUT} /opt/cvendor/bin/codex exec "
               f"--skip-git-repo-check --json --dangerously-bypass-approvals-and-sandbox "
               f'--model {MODEL} --config model_reasoning_effort=\"{THINKING}\" '
               f"> /workspace/o.jsonl 2> /workspace/e.log; echo EXIT=$?'", timeout=GEN_TIMEOUT+120)
        sh(f"docker cp {cname}:/workspace/o.jsonl {idir}/codex_stdout.jsonl 2>/dev/null")
        sh(f"docker cp {cname}:/workspace/e.log {idir}/codex_stderr.log 2>/dev/null")
        out=open(f"{idir}/codex_stdout.jsonl").read() if os.path.exists(f"{idir}/codex_stdout.jsonl") else ""
        err=open(f"{idir}/codex_stderr.log").read() if os.path.exists(f"{idir}/codex_stderr.log") else ""
        ec=0
        for t in (gen.stdout or "").split():
            if t.startswith("EXIT="):
                try: ec=int(t.split("=")[1])
                except: pass
        had_msg=('"agent_message"' in out)
        out_tokens=0                                  # primary quota tell: zero model output tokens
        for line in out.splitlines():
            if '"turn.completed"' in line:
                try: out_tokens+=json.loads(line).get("usage",{}).get("output_tokens",0)
                except Exception: pass
        diff=strip_junk(sh(f"docker exec {cname} bash -lc 'cd /app && git add -A && git diff --cached {base}'").stdout)
        return diff, {"exit":ec,"agent_msg":had_msg,"diff_bytes":len(diff),
                      "out_tokens":out_tokens,"blob":(out[-2500:]+"\n"+err[-1500:])}
    finally:
        sh(f"docker rm -f {cname} 2>/dev/null")

def evaluate(iid, idir, patch):
    preds=f"{idir}/pred.json"
    json.dump([{"instance_id":iid,"patch":patch,"prefix":ARM}], open(preds,"w"))
    eo=f"{idir}/eval"; os.makedirs(eo, exist_ok=True)
    sh(f"{VENV_PY} swe_bench_pro_eval.py --raw_sample_path={RAW} --patch_path={preds} "
       f"--output_dir={eo} --scripts_dir=run_scripts --num_workers=1 "
       f"--dockerhub_username=jefzda --use_local_docker", timeout=3600, cwd=PRO)
    rf=f"{eo}/eval_results.json"
    if os.path.exists(rf):
        try: return bool(json.load(open(rf)).get(iid,False))
        except: return None
    return None

def process(iid, idx, total, rows):
    wait_if_paused()
    r=rows[iid]; idir=f"{RUNDIR}/inst/{iid}"; os.makedirs(idir, exist_ok=True)
    img=get_dockerhub_image_uri(iid,"jefzda",r["repo"])
    cname=f"prokgen_{ARM}_{abs(hash(iid))%100000}"
    t0=time.time(); disk_guard()
    log(f"[{idx}/{total}] START {r['repo']:22s} {iid[-12:]} free={free_gb():.0f}GB")
    pulled=False
    for attempt in range(3):                       # retry transient pull blips (Docker Hub rate-limits big images)
        if sh(f"docker pull {img}", timeout=2400).returncode==0:
            pulled=True; break
        log(f"[{idx}/{total}] pull attempt {attempt+1} failed {iid[-12:]}, retrying"); time.sleep(30)
    if not pulled:
        if not net_up():
            # network outage (e.g. wifi down) -> PARK + requeue; do NOT record a false pull_fail.
            # (This is what would have prevented the 2026-06-28 wifi-outage corruption.)
            log(f"[{idx}/{total}] PULL net-DOWN {iid[-12:]} -> park 120s + requeue (not recorded)")
            time.sleep(120); REQUEUE.put(iid); return
        log(f"[{idx}/{total}] PULL-FAIL {iid[-12:]} (3 attempts, net up)")
        record({"instance_id":iid,"repo":r["repo"],"resolved":False,"stage":"pull_fail","ts":time.time()}); return
    try:
        patch,meta=generate(r, idir, cname, img)
        if patch is None:
            log(f"[{idx}/{total}] GEN-ERR {iid[-12:]} :: {meta.get('err','')[:120]}")
            record({"instance_id":iid,"repo":r["repo"],"resolved":False,"stage":"gen_err","ts":time.time()}); return
        diff_empty=len(patch.strip())==0
        meta["diff_bytes"]=0 if diff_empty else meta["diff_bytes"]
        # --- quota / network handling: empty patch + ~0 output tokens = quota-shaped ---
        if is_quota_shaped(meta):
            if net_up():                                   # quota = empty + zero tokens + NET UP
                register_quota_fail(iid)                   # counts; writes QUOTA_STOP.txt at >=3
                trigger_pause(f"quota-shaped {iid[-12:]} out_tok={meta.get('out_tokens')} exit={meta['exit']}")
                REQUEUE.put(iid); return                   # park + retry (auto-recovers on auth swap)
            else:                                          # net down = transient blip, not quota
                log(f"[{idx}/{total}] NET-DOWN empty {iid[-12:]} -> requeue (network noise, not quota)")
                time.sleep(30); REQUEUE.put(iid); return
        clear_quota_fails()                                # a productive generation clears the streak
        open(f"{idir}/patch.diff","w").write(patch)
        if diff_empty:                                     # spent tokens but no patch = genuine no-fix
            log(f"[{idx}/{total}] EMPTY-DIFF {iid[-12:]} (out_tok={meta.get('out_tokens')}, not quota)")
            record({"instance_id":iid,"repo":r["repo"],"resolved":False,"stage":"empty_diff",
                    "gen_exit":meta["exit"],"ts":time.time()}); return
        resolved=evaluate(iid, idir, patch); dt=time.time()-t0
        log(f"[{idx}/{total}] DONE {iid[-12:]} resolved={resolved} {dt:.0f}s diff={meta['diff_bytes']}B")
        record({"instance_id":iid,"repo":r["repo"],"resolved":bool(resolved),"stage":"eval",
                "secs":round(dt),"diff_bytes":meta["diff_bytes"],"gen_exit":meta["exit"],"ts":time.time()})
    finally:
        sh(f"docker rmi {img} 2>/dev/null")   # prune-after-eval: bound disk (images are 2-20GB each)

def main():
    os.makedirs(f"{RUNDIR}/inst", exist_ok=True)
    rows={json.loads(l)["instance_id"]:json.loads(l) for l in open(SRC)}
    manifest=[l.strip() for l in open(MANIFEST) if l.strip()]
    if LIMIT is not None: manifest=manifest[:LIMIT]
    done=done_set()
    pending=[i for i in manifest if i not in done]
    total=len(manifest)
    log(f"=== START arm={ARM} model={MODEL} think={THINKING} workers={N_WORKERS} "
        f"done={len(done)} pending={len(pending)} total={total} free={free_gb():.0f}GB ===")
    work=queue.Queue()
    for i in pending: work.put(i)
    def worker():
        while True:
            try: iid=REQUEUE.get_nowait()
            except queue.Empty:
                try: iid=work.get_nowait()
                except queue.Empty:
                    if REQUEUE.empty(): return
                    time.sleep(5); continue
            idx=manifest.index(iid)+1
            try: process(iid, idx, total, rows)
            except subprocess.TimeoutExpired:
                log(f"[{idx}/{total}] TIMEOUT {iid[-12:]} -> requeue"); REQUEUE.put(iid)
            except Exception as e:
                log(f"[{idx}/{total}] EXC {iid[-12:]} :: {repr(e)[:160]}")
                record({"instance_id":iid,"repo":rows[iid]["repo"],"resolved":False,
                        "stage":"exception","err":repr(e)[:200],"ts":time.time()})
    with ThreadPoolExecutor(max_workers=N_WORKERS) as ex:
        for f in [ex.submit(worker) for _ in range(N_WORKERS)]: f.result()
    done=done_set(); ev=[d for d in done.values() if d.get("stage")=="eval"]
    nr=sum(1 for d in ev if d.get("resolved"))
    log(f"=== END finished={len(done)} evaluated={len(ev)} resolved={nr} "
        f"rate={(nr/len(ev)*100) if ev else 0:.1f}% ===")

if __name__=="__main__":
    main()
