# Public skills manifest

Clone each into `$SKILLS_DIR/solution/skill/<name>` (default `$SKILLS_DIR` = the dir above this repo).
`run_arm.py` mounts these read-only into the generation container and prepends the matching trigger
(in `triggers/`). Commit pins are the versions used for the runs on this branch.

| arm | dir name | upstream | pinned commit | trigger |
|-----|----------|----------|---------------|---------|
| gsd | `get-shit-done` | https://github.com/open-gsd/get-shit-done-redux | `de73ad9` | `triggers/just-solve-with-gsd-real-trigger.jinja` |
| omc | `oh-my-claudecode` | https://github.com/Yeachan-Heo/oh-my-claudecode | `a172043` | `triggers/just-solve-with-omc-real-trigger.jinja` |
| superpowers | `superpowers` | https://github.com/obra/superpowers | `f2cbfbe` | `triggers/just-solve-with-superpowers-real-trigger.jinja` |
| spv6 | `superpowers_v6` | https://github.com/obra/superpowers | `d884ae0` | `triggers/just-solve-with-superpowers-real-trigger.jinja` |
| karpathy | `andrej-karpathy-skills` | https://github.com/multica-ai/andrej-karpathy-skills | `2c60614` | `triggers/just-solve-with-karpathy-trigger.jinja` |
| addyosmani | `addyosmani-agent-skills` | https://github.com/addyosmani/agent-skills | `70b7506` | `triggers/just-solve-with-addyosmani-trigger.jinja` |
| baseline | — (no skill) | — | — | built-in task prompt |

```bash
mkdir -p "$SKILLS_DIR/solution/skill" && cd "$SKILLS_DIR/solution/skill"
git clone https://github.com/open-gsd/get-shit-done-redux get-shit-done && git -C get-shit-done checkout de73ad9
git clone https://github.com/Yeachan-Heo/oh-my-claudecode           && git -C oh-my-claudecode checkout a172043
git clone https://github.com/obra/superpowers                       && git -C superpowers checkout f2cbfbe
git clone https://github.com/obra/superpowers superpowers_v6         && git -C superpowers_v6 checkout d884ae0
git clone https://github.com/multica-ai/andrej-karpathy-skills      && git -C andrej-karpathy-skills checkout 2c60614
git clone https://github.com/addyosmani/agent-skills addyosmani-agent-skills && git -C addyosmani-agent-skills checkout 70b7506
```

## Not included (in-house research skills)
The `evidencia*` and `referencer*` arms in `run_arm.py` reference private in-development skills
(Hyper-Int `EVIDENCE.md` / `REFERENCES.md` and local variants). Their source is **intentionally not
vendored here.** Those arms will simply be skipped unless you supply the dirs yourself. This branch
reproduces the **public** skills only.
