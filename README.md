# SessionLock

A focused native 4D plug-in for observing **the current process's user-session lock state**.
It is a likely-away signal, not inactivity detection or a license-management policy.
It never disconnects users, releases licenses, captures screens, intercepts quitting,
polls the mouse, or creates 4D processes/callbacks. A server cannot inspect remote
4D clients' locks: install and poll on each local client.

## Requirements and status

- 4D 21.1; the test project uses `compatibilityVersion: 2101`.
- macOS 14+ on Apple Silicon or Intel; universal build supported. The lock
  backend is **best effort and undocumented**, not a compatibility guarantee.
- Windows 11 x64 (current 4D 21.1 desktop baseline). Windows 7's reversed WTS
  flags are deliberately unsupported. Interactive desktop sessions, including
  the process's own RDP session, are targeted, not the active console session.
- CMake 3.20+, Xcode command-line tools or Visual Studio with Desktop C++.
  No third-party native runtime dependencies.

The deployment target is not a tested-OS matrix. See [validation](#validation)
for what has actually been exercised. Windows services, Session 0, headless
agents and absent GUI sessions may be unavailable or unknown. Do not equate
successful registration with a known lock state.

## Commands and contract

All commands take **no arguments** and return an **Object** (`:J`), including
`SessionLock Events`, which returns a diagnostic envelope rather than a bare
collection. Because Start and Stop return values, their manifest names follow
the guide's value-returning title-case convention.

| Command | Result | Effect |
|---|---|---|
| `SessionLock Start` | `{success; state}` | Register native monitoring, wait for initialization, query baseline |
| `SessionLock State` | State object | Nondestructive cached read; never starts monitoring or queries the OS |
| `SessionLock Events` | `{events; dropped; state}` | Atomically drain the application-wide queue and capture current state |
| `SessionLock Stop` | `{success; state}` | Unregister, stop native work and join thread; invalidate cached lock |

`success` means registration/cleanup succeeded, **not** that the lock state is
known. Query diagnostics may be present even after successful Start. Repeating
Start while healthy is idempotent and preserves the queue/generation. Repeating
Stop is safe. A failed native monitor can be retried with Start, which first
cleans up the previous backend. Unload stops monitoring automatically.

### State object

| Property | Type | Meaning |
|---|---|---|
| `lock` | Text | `"unknown"`, `"locked"` or `"unlocked"` |
| `running` | Boolean | Start lifecycle active, until Stop or failed startup |
| `available` | Boolean | Native observer registered and no fatal monitor failure |
| `source` | Text | Last observation/query source, or `"none"` |
| `observedAt` | Number or Null | UTC Unix milliseconds of observation **or failed observation attempt**, not OS event time |
| `generation` | Text | Decimal run identifier, initially `"0"` |
| `sequence` | Text | Last queued event's application-lifetime sequence, initially `"0"` |
| `droppedTotal` | Text | Number of queue overflows in the current generation |
| `error` | Object | `{code: Number; message: Text}`, zero/empty means no current diagnostic |

Initial and stopped state is unknown, not running, unavailable, source none and
timestamp null. Stop retains generation/sequence/drop counters and pending
events; the next fresh Start clears the queue and drop counters, increments
generation and preserves sequence. Native snapshots and notifications have a
revision guard so an in-flight query cannot overwrite a newer processed signal.

State is **last observed**, not a guarantee of current OS state. Notifications
can be missed, delayed or unavailable, especially over sleep or session changes.
An error or absent snapshot never becomes unlocked. Activity, disconnection,
screen sleep and screensaver are not lock states; no fabricated activity Boolean
is exposed. Stopped-state invalidation is not an event.

### Events envelope

```json
{
  "events": [{
    "previous": "unlocked",
    "lock": "locked",
    "source": "wts-notification",
    "observedAt": 1793740000000,
    "sequence": "1",
    "generation": "1"
  }],
  "dropped": "0",
  "state": {}
}
```

`state` has the complete shape above. Events are **observed lock/unlock
notifications whose value differs from the cache**. The first notification
after an unknown state can have `previous: "unknown"`; this is an observation,
not proof that an unknown-to-known physical transition just occurred. Duplicate
same-value notifications update observation time/source but do not queue events.
Initial and reconciliation snapshots never queue transitions.

The ring holds **128 events**, drops the oldest on overflow, and retains latest
state independently. `dropped` counts lost events since the previous drain and
resets on drain; `state.droppedTotal` does not. Sequence gaps expose loss.
Counters are decimal Text to avoid double-precision loss of 64-bit integers.
Sequence is monotonic within the application's lifetime, not across launches.
Timestamp wall clocks may move backwards; use sequence for ordering.

Draining is thread-safe but **globally consuming**: designate one 4D consumer,
then distribute its returned collection in application code. State readers do
not consume events. SDK conversion occurs only during the invoking PluginMain
cycle, never in native callbacks. A catastrophic native command exception
returns `{success:false; error:{code:10002; message:...}}` and logs to stderr;
it does not masquerade as an empty successful drain. Conversion failures in the
4D runtime log to stderr and can return Null. Draining has already consumed its
batch if subsequent allocation/runtime conversion fails.

### Simple caller-driven polling

```4d
var $result; $state; $batch; $event : Object
$result:=SessionLock Start
If ($result.success)
    $state:=SessionLock State
    // Unknown must remain unknown in application policy.
    If ($state.available) & ($state.lock="locked")
        // Update application-specific presence UI only.
    End if
End if
```

Call the following from an **existing application's periodic mechanism**, not
a plug-in callback or newly created process:

```4d
$batch:=SessionLock Events
If ($batch.dropped#"0")
    // History is incomplete. Use $batch.state as the last-observed snapshot.
End if
For each ($event; $batch.events)
    // Consume this observed lock/unlock change in application code.
End for each
```

At application shutdown or when monitoring is no longer needed:

```4d
$result:=SessionLock Stop
```

No constants are defined; compare the documented textual states.

## Platform behavior and limits

### Windows

Uses documented `ProcessIdToSessionId`, `WTSRegisterSessionNotification` with
`NOTIFY_FOR_THIS_SESSION`, and `WM_WTSSESSION_CHANGE` on an owned hidden native
window. Only the matching process session's lock/unlock messages affect events.
`WTSQuerySessionInformationW(WTSSessionInfoEx)` supplies the initial/reconciled
baseline; buffer size, Level, session identity and flags are validated and
WTS memory is freed. Connection/activity values are not interpreted as lock.

Disconnect, reconnect and suspend/resume invalidate/reconcile the cache without
inventing transitions during an unobserved gap. Startup registers before its
baseline query and processes queued session messages before committing that
snapshot; revision changes discard the stale snapshot. A native event wakes
the message-loop thread for shutdown; no host window subclass or shutdown
blocking is used. WTS/Win32 failures have nonzero diagnostics.

Windows source values are `wts-notification`, `wts-snapshot`,
`wts-session-change`, `windows-suspend`, `windows-resume`, and
`windows-backend` (fatal diagnostics).

### macOS (accepted best effort)

Uses Objective-C++ with an owned native thread/run loop and managed
`NSDistributedNotificationCenter` observers for
`com.apple.screenIsLocked` / `com.apple.screenIsUnlocked`. These notification
names are **undocumented**, can change without notice and can be spoofed.
`CGSessionCopyCurrentDictionary` is public, but its
`CGSSessionScreenIsLocked` key is **undocumented**. Only an actual CFBoolean
key value is accepted; absence, wrong type or no GUI dictionary yields unknown
with a diagnostic. In particular **an absent key is not false**.

Observers register before the baseline query; already queued notifications are
serviced before its guarded commit. There is no OS-supplied generation/time
ordering, so delayed notifications remain a limitation. Registration has no
OS acknowledgement that these undocumented names are still produced.
`available:true` indicates an installed observer, not proven delivery.
macOS does not reconstruct missed events or promise post-wake freshness;
Stop/Start can request a new baseline. A missing baseline remains unknown until
a credible notification arrives. It supports starting while already locked
only insofar as that snapshot key is available.

No use of session resign-active, `kCGSessionOnConsoleKey`, screen sleep,
screensaver or idle time as a substitute for lock. No Accessibility or Screen
Recording permission is requested. **Not security-authoritative**, and not a
sufficient basis for punitive actions or automatic license reclamation.

## Build and installation

```bash
git clone --recurse-submodules https://github.com/miyako/4d-plugin-session-lock.git
cd 4d-plugin-session-lock
```

macOS universal:

```bash
cmake -S SessionLock -B SessionLock/build -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_OSX_ARCHITECTURES="arm64;x86_64" -DCMAKE_OSX_DEPLOYMENT_TARGET=14.0
cmake --build SessionLock/build --parallel
ctest --test-dir SessionLock/build --output-on-failure
```

Windows x64 (PowerShell; let CMake select the installed Visual Studio version):

```powershell
cmake -S SessionLock -B SessionLock/build -A x64
cmake --build SessionLock/build --config Release --parallel
ctest --test-dir SessionLock/build -C Release --output-on-failure
```

Output is under `SessionLock/SessionLock-test/Plugins/`:
`SessionLock.bundle` on macOS, or `SessionLock/Contents/Windows64/SessionLock.4DX`
and `SessionLock/Contents/Resources/manifest.json` on Windows. Copy that entire
platform folder into the application's/database's Plugins folder and restart
4D. macOS Info.plist uses `BNDL`. The SDK source is compiled into the library
and its `FourDPackex` entry point is exported.

For private cross-platform staging, copy the Windows `Contents/Windows64`
directory into the macOS bundle's `Contents`, retaining the shared manifest.
Do not install both platform containers as duplicate plug-ins in the same
Plugins directory. Local output is unsigned/ad-hoc development output, **not
Developer ID signed or notarized distribution**. There are no published
releases, signing workflows, version-bump/tag pushes, or signing secrets.

## Validation

`core_tests` exercises the tri-state cache, snapshot race guard, fixed ring,
overflow accounting, lossless serialization, injected backend errors,
idempotent lifecycle and concurrent producers/consumers. `backend_smoke` only
checks real observer lifecycle; it never locks the desktop or spoofs native
notifications. Passing synthetic tests is not OS lock-integration evidence.

Run the real host project with matching tool4d (free, no license activation):

```bash
/path/to/tool4d --dataless --startup-method=test_all \
  --project="$(pwd)/SessionLock/SessionLock-test/Project/SessionLock.4DProject"
```

Require exit zero **and** an exact `PASS` stdout line. The host test checks
native load, object/collection shapes, stopped defaults, idempotent commands,
polling and cleanup without assuming a desktop lock state.
`.github/workflows/build.yml` performs read-only macOS universal and Windows
x64 builds, native tests and tool4d tests on push, PR and manual dispatch.
It does not sign, publish artifacts, create releases, tags, or push commits.

Manual desktop checklist (perform yourself; automated tests do not alter
session settings):

1. Start while unlocked, compare State with the actual desktop; accept unknown
   on macOS when the key is absent. State reads must not drain Events.
2. Lock and unlock normally, then drain from one consumer. Check sources,
   sequence order, duplicate suppression and timestamp meaning.
3. Start while locked (using an already configured application path), including
   missing-key behavior on macOS. Initial snapshot must not be a transition.
4. On Windows RDP, confirm the process's session is monitored, not another
   console session. Disconnect/reconnect and sleep/wake must not fabricate
   missing transitions. On macOS explicitly check best-effort wake behavior.
5. Stop, unload/quit, restart monitoring repeatedly; verify no native observer
   or thread remains after Stop. Exercise an unavailable/noninteractive host.

### Local evidence (2026-11-04)

On macOS 26.7 arm64 with AppleClang 21 / SDK 26.5, the universal bundle builds
with both architectures, expected SDK exports and only system runtime imports.
Native core and real-backend lifecycle tests pass; the core concurrency suite
also passes ThreadSanitizer. These are not minimum-OS or physical lock tests.

Real tool4d 21.1 build 21.100562 loads the plug-in and completes all host command
assertions (`PASS`) on both arm64 and x86_64 under existing Rosetta. On this
machine **both processes subsequently exit with SIGSEGV (-11)** and emit xbox
error 1150. An isolated project with **no plug-in**, executing only a log line,
reproduces the same post-method exit failure on both architectures. Therefore
the **whole-process host gate is blocked, not passed**, despite successful
command assertions. No host security/attach settings were changed.

The local macOS snapshot key was absent: Start correctly returned success,
running/available true, lock unknown and diagnostic 20002. The agent did not
perform physical lock/unlock, RDP or sleep/wake tests. Windows SDK compilation,
linking and runtime remain unexecuted locally; CI is provided but has not been
run or pushed. Do not infer platform support validation from source or stub
syntax checks.

### User-reported interactive acceptance (2026-11-04)

After receiving instructions for an interactive local 4D 21.1 macOS test
covering Start, cached State, manual lock/unlock, Events and Stop, the user
reported **"all tests pass"**. This records user-reported local interactive
acceptance, not an independently observed automated result. Exact macOS
version, hardware and per-step outputs were not supplied.

This report does not clear the separate automated tool4d shutdown failure
above or verify Windows, other macOS configurations, RDP, sleep/wake or the
remaining manual checklist cases.

## Provenance and license

MIT; see [LICENSE](LICENSE). Scaffold and conventions derive from
[miyako/4d-plugin-tutorial](https://github.com/miyako/4d-plugin-tutorial/tree/example).
SDK gitlink is pinned to
`cd97d1876d4258b56d2516e8ad2816f4ee93e0b5`; initialize with
`git submodule update --init --recursive`. Its own notice is
[`4D-Plugin-SDK/LICENSE.md`](4D-Plugin-SDK/LICENSE.md); retain both notices when
redistributing a built plug-in. No third-party backend source is copied.

Context and API references:
[original 4D discussion](https://discuss.4d.com/t/bildschirm-gesperrt/21199),
[related broader plug-in](https://github.com/miyako/4d-plugin-system-notification),
[WTS notifications](https://learn.microsoft.com/en-us/windows/win32/api/wtsapi32/nf-wtsapi32-wtsregistersessionnotification),
[WTSINFOEX level 1](https://learn.microsoft.com/en-us/windows/win32/api/wtsapi32/ns-wtsapi32-wtsinfoex_level1_w),
[CGSessionCopyCurrentDictionary](https://developer.apple.com/documentation/coregraphics/cgsessioncopycurrentdictionary()),
[4D 21.1 requirements](https://co.4d.com/product-download/4D-21-LTS).
