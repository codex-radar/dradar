# Automatic update bootstrap (0.5.203 candidate)

0.5.202 and older do not automatically discover signed releases. An existing
user must finish their current work and migrate once to this bootstrap version.
Updating the server cannot inject discovery code into an old running process.
The website's pinned uvx source must be updated separately after publication.
No account re-login is required by the updater. Do not terminate old workers to
perform that initial migration; old workers do not participate in the new
invocation-lock protocol.

The console entry and newly built PYZ share `dradar.launcher`. On launch,
discovery reads only the fixed HTTPS stable pointer, verifies the existing
embedded public root, checks expiry/sequence/platform/compatibility and uses
the existing streaming downloader/controller to prepare a candidate. A manifest
cannot introduce a trusted key. Each check has an eight-second total budget
plus at most the bounded in-flight read timeout, with a three-second per-request
timeout; preparation is throttled for fifteen minutes, failures for five.
Network errors, corrupt manifests and policy rejection keep the installed CLI.
A daemon checks the throttle once per minute while an invocation remains alive;
it only prepares, never interrupts or replaces a running worker.

Cross-process discovery and update locks serialize writers. Each new launcher
and its verified child hold independent invocation locks for their whole run,
so a parent crash does not make a still-running new child look idle. Activation
runs only under the launch gate when no invocation remains and durable uploads
are clear. A prepared release can therefore wait indefinitely during continuous
work; there is no promise to replace code between every pair of tasks. A new
invocation can hand off once to the verified candidate; an already-running
invocation exits normally and uses the committed candidate on its next launch.
No claim or model request is replayed by handoff. Do not describe this as remote
hot-patching or seamless upgrade of all existing sessions.

The candidate version self-test has a separate internal `--version` dispatch
path so it cannot recursively acquire the parent's activation lock. Normal
verified children register their own liveness and bypass only repeated
discovery, not the parent's artifact verification. An internal environment flag
is not a trust source and does not authorize an arbitrary executable path.
The existing current/LKG and monotonic sequence protections remain in effect.
Windows keeps the existing replacement-denying candidate handle through child
exit. Native Windows validation is required before release; POSIX tests do not
substitute for it.

OTA history before authentication remains local. Core flight batches exclude
all `update_*` events. When a server advertises `ota_update_v1`, an independent,
bounded optional batch sends `update_observed`: a snapshot scoped to the current
authenticated batch/session, including the update state/sequence (when known),
launch method and whether this invocation has discovery enabled. It does not
rebind historical unscoped events to a later account. Heartbeat client_version
is the reported running version; downloaded/staged state is not installation.
Fast stages may occur between observations; offline/unmapped devices remain
unknown. Optional observation failure cannot fail worker registration.

This candidate does not enable managed-auth cohorts, change OAuth, change
server admission, or reserve a release sequence. 0.5.202 artifacts stay immutable.
