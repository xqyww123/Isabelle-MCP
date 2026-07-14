# Plan — ship the Scala component, drop the patch dependency

**Status:** IMPLEMENTED (rev. 3, after two adversarial reviews). Kept as the design record —
every claim below is cited to the Isabelle sources or to a test in `tests/test_component.py`.
**Goal:** `pip install isabelle-mcp` is the *only* installation step. No Isabelle patch, no heap
rebuild, no second package, no compile on the user's machine, no manual `isabelle components -u`.

> **Revision history.**
> **Rev. 1** built the component from source on the user's machine. Killed: the compile (measured
> **25 s wall / 83 s CPU on 14 cores**) would land inside `bin/isabelle`'s implicit `scala_build`,
> i.e. inside our own `isabelle mcp_server` spawn, under `initialize`'s hard 30 s deadline — an
> unbreakable timeout loop on any 2–4-core laptop.
> **Rev. 2** shipped a prebuilt jar with `no_build = true`. That is right, and its one load-bearing
> claim is now **proven end to end** (§1.2). But its threat model was inverted, its central
> verification was destructive, and the one line the whole design rests on was ungated.
> **Rev. 3** fixes those three and pins down when `ensure_component()` runs.

---

## 0. Why this exists

Isabelle-MCP used to require `my-better-isabelle-prover` to patch the user's Isabelle:
`pide_control`'s Scala half gave the stock `vscode_server` the PIDE requests we need; its ML half
added `Document.cancel_execution`; `perspective_eof_clamp` fixed an off-by-EOF in the caret
perspective.

All three are gone. The first and third are now **our own code**, in `isabelle mcp_server` (a fork of
Isabelle2025-2's `src/Tools/VSCode/src/*`, package `isabelle.mcp`). The second is replaced by an **ML
prelude injected at prover startup** (`ML_Process` `use_prelude`) built from the public `EXECUTION`
API alone — see [`../scala/docs/CANCELLATION.md`](../scala/docs/CANCELLATION.md), verified end to end
on a **fully un-patched** Isabelle (pristine sources *and* pristine heaps): a runaway proof in an
imported theory falls from 3.45 cores to 0.01 on cancel.

So **Isabelle-MCP needs zero Isabelle patches** — which removes the old design's worst cost: patching
`src/Pure/**.ML` invalidated every session heap on the machine.

It trades one prerequisite for another: **`isabelle mcp_server` exists only if our Scala component is
registered.** Today it is registered by a path hand-appended to `~/.isabelle/…/etc/components` — i.e.
it works **on the author's machine only**. This plan closes that gap.

---

## 1. Locked decisions

### 1.1 The component is a **package asset**, registered in place. Nothing is copied.

```python
files("isabelle_mcp") / "scala" / isabelle_identifier    # -> a real pathlib.Path
```

- wheel install → `…/site-packages/isabelle_mcp/scala/Isabelle2025-2/`
- editable install (this repo) → `…/Isabelle-MCP/src/isabelle_mcp/scala/Isabelle2025-2/`

The same expression yields a real, existing directory in both (verified: editable →
`PosixPath('/…/Isabelle-MCP/src/isabelle_mcp')`, `is_dir()` True; pip unzips wheels, so the wheel case
is a real directory too). **No dev-mode branch, no override env var.** The development tree *is* the
asset.

> **Never wrap it in `importlib.resources.as_file()`.** For a zip resource that materialises a temp
> copy which is deleted on context exit — leaving a permanently dangling registration.

Isabelle does not require a component to live anywhere in particular: `etc/components` is a list of
absolute paths, and `init_component` (`lib/scripts/getfunctions:244-268`) merely sources
`$DIR/etc/settings` and appends to `ISABELLE_COMPONENTS`.

### 1.2 We ship a **prebuilt jar** and declare `no_build = true`. The user's machine never compiles.

```
etc/build.props:  module   = lib/isabelle_mcp.jar
                  no_build = true              # <- the single line everything rests on
                  sources  = src/*.scala       # kept for audit / release rebuild
                  services = isabelle.mcp.Tools
etc/settings:     ISABELLE_MCP_SCALA_HOME="$COMPONENT"
                  classpath "$ISABELLE_MCP_SCALA_HOME/lib/isabelle_mcp.jar"
lib/isabelle_mcp.jar                            # tracked in git, shipped in the wheel
```

**Proven end to end** (scratch `USER_HOME`, a copy of the component, real distribution untouched):

```
$ isabelle getenv -b ISABELLE_CLASSPATH | tr : '\n' | grep mcp
  …/isabelle_mcp/scala/Isabelle2025-2/lib/isabelle_mcp.jar
$ isabelle -?                    →  mcp_server - PIDE language server for Isabelle-MCP
$ isabelle mcp_server            →  Usage: isabelle mcp_server [OPTIONS] …
$ isabelle scala_build           →  rc=0, no banner, jar mtime unchanged
```

The chain: `getfunctions`' `classpath` → `ISABELLE_CLASSPATH` → `lib/Tools/java:12-13`'s `-classpath`
→ `Classpath.services` (`classpath.scala:82-92`) → `isabelle.setup.Build.get_services(jar)` →
**`META-INF/isabelle/services` inside the jar** (`Build.java:358-378`) → `Class.forName` →
`Isabelle_Tool.find`.

> Note the mechanism precisely: under `no_build`, **nothing reads `etc/build.props` at runtime**. The
> services come from `META-INF/isabelle/services` *inside the jar*. (Stock precedent for the pattern:
> `contrib/naproche-*` and `contrib/Semantic_Embedding`; neither ships an `Isabelle_Tool`, so the tool
> case is proven by the run above, not by precedent.)

**Why `no_build` and not a source build.** `Build.build()` returns at its first statement when
`module_result()` is `""` (`Build.java:146`, `:457-458`) — *before* the `fresh` flag (`scala_build -f`)
is ever read. So `scala_build`, **with or without `-f`**, never touches our component. Consequences:

| Rev.-1 blocker | Why it is gone |
|---|---|
| a 25 s compile deferred into `mcp_server`'s spawn, killed by `initialize`'s 30 s deadline | there is no compile |
| the component dir must be **writable forever** (`-f` unconditionally rewrites every component's jar, `Build.java:396, 491`) | `scala_build` never touches us → `sudo pip install`, Docker, Nix, read-only `site-packages`: all fine |
| a prebuilt jar's shasum pins the *builder's* `isabelle.jar` digest, and jar builds are not byte-reproducible (`Build.java:401-414`) | no shasum is ever compared |
| a component that fails to compile bricks every `isabelle` Scala tool (`bin/isabelle:48`) | we cannot make `scala_build` fail |

**What we give up, and what replaces it.** `scala_build`'s automatic "does this jar match this
Isabelle?" check. Replaced by **version keying**: the component lives under the exact Isabelle
identifier and `ensure_component()` refuses to register on any other. Within one release the sources
are fixed, so the API is fixed, so the jar links — including for users who rebuilt their own
`isabelle.jar` (different bytes, same API), whom a shasum check would have **falsely rejected**.

> **The invariant, stated honestly:** the jar links as long as the host's Pure Scala API is
> binary-compatible with stock Isabelle2025-2. Every Scala patch in `my_better_isabelle_prover` is
> purely additive, so a patched host is fine — but this is an invariant, not a guarantee.

**Residual risk we accept, deliberately** (decision taken by the maintainer):

| jar state | consequence |
|---|---|
| **absent** | harmless — `isabelle build` / `doc` / `version` all fine; only `isabelle mcp_server` reports *Unknown Isabelle tool* (`Build.java:365` returns `List.of()` for a non-regular file) |
| **corrupt, or does not link** | `Classpath.services` is a strict `val` that eagerly opens every jar and `Class.forName`s every service → **every Isabelle Scala tool exits non-zero** (`*** I/O error: zip END header not found`, or `*** Bad Isabelle/Scala service …`), naming nothing useful |

We do **not** guard the second case (a corrupt jar means a corrupt install; `pip` is responsible for
delivery integrity). What we **must** do instead, because it costs nothing:

- `ensure_component()` still refuses to register when the jar is **absent** (cheap, and it turns a
  confusing "unknown tool" into a clear error);
- the cure is **documented** in the README *and* in every error message we raise:
  **`isabelle components -x <path>`** — verified to work even with a broken jar registered (`rc=0`;
  `lib/Tools/components` reaches `isabelle.Components` without going through `Classpath.services`).

**`no_build = true` is gated, three ways** — it is the one line the design rests on, and rev. 2 had
nothing checking it:

1. the release build works from a **copy** of the component with `no_build` removed; the shipped
   `etc/build.props` is **never** mutated (§7);
2. the release gate parses the shipped `build.props` **the way Isabelle does** and requires
   `no_build` to be exactly `true` (§6.6);
3. `ensure_component()` refuses to register a component whose `no_build` is not exactly `true`.

> **"Exactly" is not pedantry.** Isabelle reads this file with `java.util.Properties`, which keeps a
> value's *trailing* whitespace, and `Build.get_bool` switches on the literal string. `no_build =
> true·` — one invisible space, on the one line §7 has you delete and retype by hand — is therefore
> not `true`. It aborts **every** `isabelle` command on the user's machine with `*** Bad boolean
> property`. Both checks above parse rather than grep, because a substring test passes on
> `# no_build = true` and on `no_build = true·` alike: it says OK to the two states that hurt most.

### 1.3 Registration goes through **`isabelle components -u/-x`**. We never write `etc/components`.

Rev. 1 planned to edit that file in Python, because `lib/Tools/components:130-131` runs
`isabelle scala_build || exit $?` *before* `isabelle java isabelle.Components` — so a component that
fails to compile cannot be removed by `-x`. With `no_build = true` **that** chicken-and-egg cannot
arise, and the reason to bypass Isabelle's own API goes with it.

> But note what `no_build` does **not** buy: a component whose `build.props` fails to *parse* is
> just as unremovable. Verified — with `no_build = true·` registered, `isabelle components -x` is
> itself among the commands that exit 2, and the user must hand-edit `etc/components` to escape.
> Registration is a one-way door onto every Isabelle command on the machine, which is why
> `ensure_component()` validates **before** it opens that door and not after.

`Components.update_components` (`components.scala:305-318`) is exactly right: it normalises
(`path0.expand.absolute`), requires the directory to exist for `-u` (`Directory(path).check`), drops
any existing line for that path before appending, preserves blank and `#` lines, creates the file if
missing, and **does not write when nothing changes** (`if (lines1 != lines3) write_components(lines3)`).
`-x` works on a **dangling** path (verified: rc = 0, line removed).

**No lock.** `-u` is idempotent; the worst outcome of two concurrent `ensure_component()` calls is a
lost update, healed by the next call. It is Isabelle's file and Isabelle's read-modify-write.

We do **read** `$ISABELLE_HOME_USER/etc/components` (a plain text file) — that is what makes the fast
path free.

### 1.4 Stale entries: pruned **by path shape**, on every check.

We register a path inside the venv, so ordinary events leave it behind: `rm -rf .venv`,
`pip uninstall`, a Python minor-version bump, moving the project.

**A dangling entry is *not* silent.** Every `isabelle` command then prints, on stderr, forever
(`getfunctions:255-268`):

```
### Missing Isabelle component: "/home/u/proj/.venv/…/isabelle_mcp/scala/Isabelle2025-2"
```
Exit code stays 0, so nothing breaks — but the noise accumulates, one line per abandoned venv.

A fingerprint that *reads* the directory cannot work: the directory is gone. Blanket-deleting every
non-existent path is unacceptable — another component may sit on a temporarily unmounted volume. So:
**prune by the shape of the path string.** A line matching

```
…/isabelle_mcp/scala/Isabelle<something>
```
is ours by construction. Every such line that is **not** the current target is removed
(`isabelle components -x`), whether or not the directory still exists. Blank and `#` lines are
skipped. Any other line is never touched.

That makes **re-install / upgrade / venv change self-cleaning**. A user who deletes a venv without
uninstalling keeps the stderr line until their next install (which prunes it) or an explicit
`isabelle-mcp uninstall`. That is their affair.

**Convergence must be checked.** `-x` removes a line only if `File.eq(Path.explode(line), target)`; a
line we classify as ours whose spelling it cannot match (a symlinked venv, a hand-edited line) yields
`Unchanged component` with **rc = 0** and the line still present. Without a re-check, `stale` is
non-empty forever and the fast path never engages again. So: **after mutating, re-read; if a line we
declared stale survives, log a warning naming it** (do not hard-fail — a `#`-commented line is inert
to Isabelle).

**Duplicates matter, but not for the reason rev. 1 gave.** `Isabelle_Tool.find_internal` is a
`collectFirst` (`isabelle_tool.scala:37-42`): two components providing `mcp_server` do **not** raise —
**the first silently wins.** Worse, `ISABELLE_MCP_SCALA_HOME` is won by the *last* component sourced
while the jar is won by the *first* on the classpath, so a divergent pair can pair one install's jar
with another's `ML/mcp_prelude.ML`. Pruning is what prevents this.

### 1.5 When `ensure_component()` runs — and when it must not

**Hard constraint: the Python process must start fast and must run *zero* `isabelle` commands at
boot.** `server_lifespan` only constructs the client today ("the prover is NOT started here"), and it
stays that way.

`ensure_component()` is called from exactly two places:

- **`IsabelleLSPClient.start()`** — i.e. once per `isabelle_launch`, immediately before we spawn
  `isabelle mcp_server`. That launch already costs 15–25 s (JVM + HOL heap), so our cost is noise.
- **`isabelle-mcp install` / `isabelle-mcp uninstall`** — a CLI command, with a human waiting.

**Memoise the stable half, re-read the volatile half.** These are different things and rev. 2 wrongly
cached them together:

| part | changes during the process's life? | cost |
|---|---|---|
| the `isabelle` binary, `isabelle version`, `ISABELLE_HOME_USER`, the component dir | no | **expensive** — two `isabelle` subprocesses, ~0.6 s |
| the content of `etc/components` | **yes — another install can rewrite it at any moment** | **free** — one read of a few-hundred-byte file, tens of µs |

So: memoise the first, **re-read the second on every call**. In the steady state (our line present, no
other line of our shape) the check returns immediately — **no JVM, no subprocess, no disk write**.
With a single install, after the first registration `ensure_component()` **never writes again**.

This is also what makes two live installs safe: a server that was evicted by another install
**re-registers before its next spawn**, instead of silently running the other install's jar and
prelude. (Isabelle itself has the same guard on the write side: `components.scala:315` only writes
when the content actually differs.)

### 1.6 `isabelle` comes from `PATH`. No new env var.

Registration needs `isabelle version` and `isabelle getenv ISABELLE_HOME_USER`; and the server's whole
job is to spawn `isabelle mcp_server`. "isabelle not on PATH" is a hard prerequisite of the product,
not a component problem.

`isabelle-mcp install --isabelle-bin /path/to/bin/isabelle` already prepends that directory to `PATH`
and passes `-e PATH=…` into the MCP client registration (`install.py:198`), so the **server process**
has `isabelle` on `PATH` at runtime. All five `isabelle` call sites in `lsp_client.py` use the bare
name and stay that way.

### 1.7 Registering a component **cannot invalidate any heap** — if we respect four rules.

The whole staleness decision is `store.scala:549-560`: three digests — `sources_shasum`,
`input_heaps`, `output_heap`. The persisted schema (`store.scala:136-142`) has **no options column, no
environment column, no components column**, and `ISABELLE_COMPONENTS` / `ISABELLE_CLASSPATH` /
`ISABELLE_SCALA_SERVICES` appear **nowhere** under `src/Pure/Build/`.

Hard constraints on the component, forever:

| Never | Why |
|---|---|
| set an env var some ROOT uses as `condition = X` (`ISABELLE_GHC`, `Z3_INSTALLED`, `ISABELLE_MLTON`, …) | folded into `meta_digest` (`sessions.scala:549-561, 677`) and *not* trimmed → those sessions rebuild |
| set `ML_HOME` / `POLYML_HOME` / `ML_PLATFORM` / `ML_OPTIONS` | Pure's `input_shasum` digests `polyml_exe` (`store.scala:385`) → **Pure rebuilds, cascading into everything** |
| re-declare an existing option in `etc/options` | hard `error("Duplicate declaration of option …")` (`options.scala:440`) — breaks every `isabelle` command |
| add a session that becomes an ancestor of an existing one | changes that session's `meta_digest` |

Our `etc/settings` sets one fresh variable plus the `classpath` line; we ship no `etc/options`, no
`ROOT`, no theories. Safe. Also: `use_prelude` never touches a heap (heaps are written only by
`ML_Heap.save_child`, `build_job.scala:260` — inside a build job, never in a PIDE session).

---

## 2. Layout

```
src/isabelle_mcp/
    scala/
        Isabelle2025-2/                  # keyed by ISABELLE_IDENTIFIER, not a parsed year
            etc/build.props              # incl. no_build = true
            etc/settings                 # ISABELLE_MCP_SCALA_HOME + classpath <jar>
            src/*.scala                  # 14 files
            ML/mcp_prelude.ML
            lib/isabelle_mcp.jar         # TRACKED in git, shipped in the wheel
            docs/CANCELLATION.md         # travels with the component
```

Packaging:

```toml
[tool.setuptools.package-data]
isabelle_mcp = ["py.typed", "scala/**/*"]
```
plus `MANIFEST.in` for the sdist. The jar is **tracked** — `no_build = true` means nothing regenerates
it on the user's machine.

---

## 3. `src/isabelle_mcp/component.py`

```python
def ensure_component() -> Path:
    """Idempotent. Guarantee that `isabelle mcp_server` resolves; return the component dir.

    Steady state: one small file read. No subprocess, no JVM, no write.
    """
```

**Memoised once per process (stable):**

1. Resolve `isabelle` on `PATH`. Absent → `IsabelleToolError` naming
   `isabelle-mcp install --isabelle-bin /path/to/Isabelle/bin/isabelle`.
2. `isabelle version` → the Isabelle identifier → `files("isabelle_mcp")/"scala"/<id>`.
   No such directory → `IsabelleToolError`: *"Isabelle-MCP does not support `<id>` (supported:
   Isabelle2025-2). If you set ISABELLE_IDENTIFIER yourself, that is why."*
3. Assert `lib/isabelle_mcp.jar` exists **and** `etc/build.props` contains `no_build = true`.
   Either missing → refuse (§1.2). Never register a build-enabled component.
4. `isabelle getenv -b ISABELLE_HOME_USER` → the path of `<home_user>/etc/components`.

**Re-done on every call (volatile):**

5. Read `etc/components`. Skipping blank and `#` lines, let
   `ours = [l for l in lines if shape(l)]`.
   If `ours == [target]` → **return** (fast path: one file read).
6. Otherwise: `isabelle components -x <each l in ours if l != target>`, then
   `isabelle components -u <target>`. Non-zero exit → raise with the captured output, distinguishing
   the two common causes: an **I/O error** on `$ISABELLE_HOME_USER/etc` (unwritable home, Docker image
   built as root, read-only/quota-exhausted cluster home) versus **some other broken Scala component**
   on the machine (`components` runs `scala_build` first). In both cases print the exact line to add to
   or remove from `etc/components` by hand — we deliberately own no other way to touch that file.
7. Re-read `etc/components`; if a line we declared stale survives, **log a warning naming it** (`-x`
   silently no-ops on a spelling it cannot match). Do not hard-fail.
8. Log, at INFO, the component path that ended up in force.

There is **no rollback machinery**: with `no_build = true` a registration cannot make `scala_build`
fail, and the corrupt-jar case is an accepted, documented risk (§1.2).

**`isabelle-mcp uninstall`** (new console entry point): `isabelle components -x <our path>` + remove
the MCP registration from the client(s).

---

## 4. Changes, file by file

| File | Change |
|---|---|
| `src/isabelle_mcp/component.py` | **new** (§3) |
| `src/isabelle_mcp/lsp_client.py` | delete `check_isabelle_patched()` and the `skip_patch_check` parameter; call `ensure_component()` in `start()` — **not** at import, **not** in `server_lifespan` |
| `src/isabelle_mcp/lsp_client.py` | **diagnostics:** the clean-EOF branch of `_read_loop` must call `_fail_pending_waiters`, and `_STDERR_ERROR_RE` must match `unknown isabelle tool` — otherwise a prover that dies before the handshake yields a blind 30 s `initialize` timeout naming nothing |
| `src/isabelle_mcp/lsp_client.py` | **dead code:** the three `isabelle_year()` pre-2025 branches (`:157`, `:423`, `:1516`). With 2024 dropped they are unreachable — but if version detection ever *fails* they silently send a 2025 server the 2024 option name, which aborts it. Delete them, or make an undetectable version a hard error |
| `src/isabelle_mcp/server.py` | delete `_skip_patch_check`, the `--skip-patch-check` flag, its plumbing. **`server_lifespan` must remain free of any `isabelle` invocation** |
| `src/isabelle_mcp/install.py` | delete `_check_patches()` and `--skip-patch-check`; call `ensure_component()`; add `uninstall` |
| `scripts/install.sh` | delete the patch check and `--skip-patch-check` |
| `pyproject.toml` | **remove** `my-better-isabelle-prover` from `dependencies`; add `package-data`; add the `uninstall` console script |
| `MANIFEST.in` | new — carry `scala/` into the sdist |
| `README.md`, `AGENTS.md`, `.claude/README.md`, `docs/ARCHITECTURE.md`, `CHANGELOG.md`, `examples/README.md` | remove every "apply the patches first" instruction — **they are now false**. Drop the `isabelle vscode_server --help` troubleshooting step: it is a **false green** (it still succeeds while `mcp_server` is missing). Document `isabelle components -x <path>` as the escape hatch |
| `tests/` | delete the patch-check tests; add `component.py` tests (§6) |

---

## 5. Failure modes

| # | Failure | Response |
|---|---|---|
| 1 | `isabelle` not on `PATH` | error naming `--isabelle-bin`; nothing written |
| 2 | unsupported Isabelle identifier (2024, a Mercurial checkout, a user-set `ISABELLE_IDENTIFIER`) | error naming the identifier and the supported set; nothing written |
| 3 | `lib/isabelle_mcp.jar` absent, or `build.props` lacks `no_build = true` | refuse to register; the wheel is broken |
| 4 | jar present but **corrupt / does not link** | **accepted, unguarded risk**: every Isabelle Scala tool exits non-zero. Cure, documented in the README and in our error text: `isabelle components -x <path>` |
| 5 | `isabelle components -u/-x` exits non-zero | two causes: an I/O error on `$ISABELLE_HOME_USER/etc` (unwritable home), or **some other** broken Scala component. Distinguish them, and print the manual edit to make |
| 6 | dangling entry from a deleted venv | `### Missing Isabelle component: "<path>"` on the stderr of every `isabelle` command (exit 0). Pruned by shape on the next check; or `isabelle-mcp uninstall`; or `isabelle components -x` |
| 7 | duplicate / divergent registration (second venv, upgrade) | **no error** — `collectFirst` lets the first silently win. Pruned by shape on **every** `ensure_component()`; a server evicted by another install re-registers before its next spawn (§1.5) |
| 8 | a line we declared stale that `-x` cannot match | warn, naming it; do not hard-fail (§1.4) |
| 9 | two `ensure_component()` concurrently | `-u` is idempotent; a lost update is healed by the next call |

There is deliberately **no** row for "the component fails to compile" or "the component dir is not
writable": with `no_build = true` neither can happen.

---

## 6. Verification (all must pass)

1. **Fresh user.** `USER_HOME=$(mktemp -d)` — no `etc/components` at all → `ensure_component()` →
   `isabelle mcp_server` prints our banner → a session launches. *This is the test the current code
   fails.* **Assert on output** (`Usage: isabelle mcp_server` present, `Unknown Isabelle tool` absent),
   **never on rc**: `isabelle mcp_server --help` and `-?` both exit 1 while resolving correctly.
2. **Fast path is inert.** With the component already registered: `ensure_component()` must leave
   `etc/components` byte-identical, touch nothing in the component dir, and **start no subprocess at
   all** (patch `subprocess.run` and assert it is never called after the memoised prologue).
3. **`scala_build` never touches us.** In a scratch `USER_HOME` with our component registered, run
   plain `isabelle scala_build`: assert rc = 0, no `### Building Isabelle-MCP` banner, and
   `lib/isabelle_mcp.jar`'s mtime unchanged.
   **Do NOT run `scala_build -f`.** `-f` rebuilds *every* component, including `$ISABELLE_HOME`'s own
   `isabelle.jar` — a scratch `USER_HOME` does not isolate that, so the test would rewrite the shared
   distribution's jar under other agents, and would abort mid-rebuild on the read-only Isabelle it is
   meant to certify. The "`-f` cannot touch us" property is proven by reading `Build.java:457-458`
   (the function returns before `fresh` is read), and the `no_build` line itself is gated by §6.6 and
   by `ensure_component()` — not by a destructive test.
4. **Stale pruning.** Register `<tmp>/x/isabelle_mcp/scala/Isabelle2025-2`, delete the directory, run
   `ensure_component()` → the entry is gone and no `### Missing Isabelle component` line remains on a
   subsequent `isabelle version`.
5. **Foreign entries survive.** A registered component on a non-existent path *not* shaped like ours is
   left alone. Blank and `#` lines survive untouched.
6. **The release gate** (`scripts/check_component.py`, run by CI on the source tree *and* on both
   built artefacts — §8). Everything it checks is derived from `etc/build.props`, parsed as Isabelle
   parses it, because that is the file Isabelle actually obeys:
   - `no_build` is exactly `true`;
   - the jar's `META-INF/isabelle/services` is what `build.props` declares;
   - the jar's `META-INF/isabelle/shasum` records **exactly** `<meta_info>` + the declared
     requirements + the declared sources, and every source still hashes to what the jar recorded.
     Checking only the entries the jar lists would be blind to a source *added* since it was built —
     the jar cannot testify about a file it has never heard of;
   - `build.props` declares no property the gate does not model (a new `scalac_options` feeds the
     build, and nothing will ever rebuild the jar to honour it);
   - `ML/mcp_prelude.ML` is present.

   A silent `package-data`/`MANIFEST.in` miss, a forgotten `no_build` flip-back, and a stale jar are
   the three ways to ship a broken release, and none of them shows up on the author's machine.
7. **Startup diagnostics.** With `mcp_server` deliberately unregistered, `start()` fails in seconds with
   a message containing `Unknown Isabelle tool` — not a 30 s content-free `initialize` timeout.
8. **Regression.** `pytest -m integration` (6/6) and the multi-node cancel test (prover CPU falls from
   ~3 cores to ~0) on the un-patched distribution.

---

## 7. Release recipe for the jar

The jar is a build artefact that we **commit**, so it needs a recipe that cannot silently ship a stale
or build-enabled component:

1. Copy the component to a scratch directory and **remove the `no_build` line from the copy**.
2. Register the copy in a scratch `USER_HOME`; run `isabelle scala_build` (**not** `-f`); it compiles
   and writes `lib/isabelle_mcp.jar` in the copy.
3. Copy that jar back into `src/isabelle_mcp/scala/Isabelle2025-2/lib/` and commit it.
4. **Never mutate the shipped `etc/build.props`.** Step 1 says *copy*, and this is why: a `no_build`
   flipped in place and forgotten restores every rev.-1 blocker, invisibly, because the shasum still
   matches on the author's machine. And retyping the line by hand is how you get `no_build = true·`,
   a trailing space that reads as `true` to a human and aborts every `isabelle` command on the user's
   machine (§1.2). Editing a copy costs nothing; editing the original is the trap.
5. Running `isabelle scala_build` against the *shipped* component is a **no-op**, not a rebuild — it
   would silently ship a stale jar. §6.6 is what catches that.

`scripts/check_component.py` enforces all of this (§6.6), and CI runs it on every push *and* against
the built wheel and sdist before publishing (§8). Run it before releasing:

```bash
python scripts/check_component.py             # the source tree
uv build && python scripts/check_component.py dist/*      # and both artefacts that ship
```

The jar is **tracked**, so this recipe only runs when a `.scala` source changes — not on every
release. You do not have to remember when: the gate compares the jar's record against what
`build.props` declares today, so it tells you.

The jar is pure JVM bytecode (150 `.class` + 16 `.tasty`, no native code), so **one jar serves every
platform** — which is what lets the wheel stay `py3-none-any`. Its only binding is the Isabelle
release, which is exactly what the `scala/<identifier>/` key expresses.

---

## 8. Out of scope

- **Isabelle2024.** The fork is cut from 2025-2's VSCode sources; three of its files do not exist in
  2024 and its Pure Scala API differs. The last supporting commit is tagged
  `last-isabelle2024-support` in **both** repositories. `ensure_component()` rejects it by name.
- **`sledgehammer` / `auto` cancellation.** Every cancellation experiment so far used an allocating ML
  loop. See `CANCELLATION.md` §8.
- **`my_better_isabelle_prover`** keeps `expose_foreign` (*user*, for `Semantic_Embedding`'s SIMD FFI)
  and the three *dev* features (Isa-REPL, Isa-Mini). Isabelle-MCP simply stops depending on it.

---

## 9. Order of work

1. **First**, add `no_build = true` + the `classpath` line to the component **where it stands today**
   (`contrib/Isabelle-MCP/scala/`), and rebuild the jar per §7. This makes the migration
   order-independent — the currently-registered path keeps working throughout.
2. Move the component into the package; `git add` (incl. the jar); re-point the author's existing
   `~/.isabelle/Isabelle2025-2/etc/components` line in the same step, or every other agent on this
   shared checkout starts seeing `### Missing Isabelle component`.
3. `pyproject.toml` / `MANIFEST.in` / package-data → verify the wheel (§6.6).
4. `component.py` + its tests (§6.1–6.5 run against a scratch `USER_HOME`; none needs a prover).
5. Rip out the patch dependency (`lsp_client`, `server`, `install.py`, `install.sh`, `pyproject.toml`).
6. Startup diagnostics; delete the pre-2025 dead branches.
7. `isabelle-mcp uninstall`.
8. Docs.
9. Full verification (§6), then commit.
