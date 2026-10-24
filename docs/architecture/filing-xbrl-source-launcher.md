# Filing launcher through the shared Python runtime

Status: implementation candidate. Production activation still requires native
qualification and independent exact GPT-6.1 Sol sign-off.

The owner approved moving the filing launcher into existing Python paths if the
security boundary is preserved. Windows Smart App Control rejected the unsigned
custom executable before any AppContainer child was created. This change uses
normal isolated Python execution. It does not load the blocked assembly or alter
Windows protection.

## Owned boundaries

- `execution/filing_xbrl_appcontainer_launcher.py` owns the closed command and
  bounded input protocol. It runs in its own process through the exact sealed
  bundle interpreter. It loads one adjacent support module by explicit path.
- `src/runtime/windows_appcontainer.py` owns native AppContainer, process, mutex,
  permission and private UI controls. It uses only the standard library.
- `src/filings/inline_xbrl_processor.py` owns the explicit execution mode, exact
  source/runtime closure, before/after byte checks and outer process limits.
- `execution/build_filing_xbrl_processor_bundle.py` owns bundle construction and
  binding the copied launcher/support bytes to reviewed source.
- The report facade retains its separate temporary-profile and export policy.
  It is a source of reusable native patterns, not an interchangeable filing policy.

## Required preserved controls

The parser receives a zero-capability AppContainer token. A protected global
mutex serializes the stable V2 profile across sessions. V1 remains unchanged.
Runtime and input grants permit read and execute access only. Unexpected grants
and loopback exemptions refuse execution. Each call owns a new private low-
integrity window station and desktop with exact owner/package rights. The
creator UI context must be restored.

The child starts suspended. Job assignment and token verification happen before
resume. Only the three intended standard handles are inherited. Execution and
stdin delivery are bounded. All owned descendants must stop before private UI
objects close or permission grants are removed. Failure to prove cleanup remains
a visible recovery state.

Both source files, the interpreter and the complete runtime remain byte-pinned
and protected during execution. A signed interpreter does not authenticate our
source; the sealed closure and protected artifact supply that separate control.
No generic import path, user site package, environment override or native
executable fallback may replace the selected source launcher.

## Cancellation and recovery authority

The installation descriptor names one protected `sandbox_state_root` outside
immutable runtime and input roots. The controller is the sole writer of its
exclusive pending invocation record. All approved V2 installations use the same
canonical authority; deployment cannot change that path to evade a hold.

The outer controller owns a Job before the trusted launcher can run. It bounds
stdin delivery and output capture, requests cooperative cancellation, and gives
cleanup a fixed reserve. The trusted launcher has a separate acknowledgement
pipe. The AppContainer child inherits only stdin, stdout and stderr; it cannot
send a cleanup acknowledgement or access the cancellation/control handles.

Clearing a pending record requires trusted acknowledgement, launcher exit, zero
active outer Job processes, complete affected ACL parity and unchanged runtime
and input bytes. Forced termination leaves a hold even after process count reaches
zero. Unknown stop status keeps write fences and owner resources retained. An
explicit reviewed recovery must reconcile those resources before another run.
The source launcher refuses pre-existing explicit V2 grants instead of silently
consuming them. It preserves unrelated grants.

## Evidence required before activation

Native structure/argument checks and resource-failure tests; suspended-child
failure cleanup; mutex contention and abandonment; exact permission restoration;
private UI rights/label and creator restoration; network/file/process/UI negative
controls; timeout and malformed/oversized-input cleanup; actual SYSTEM/session0
execution with host policy unchanged; retained ONON replay through the public
configured path; complete runtime/input byte and permission parity; current
repository gates; independent exact GPT-6.1 Sol review of the final release.

## Primary-source applicability

Microsoft's UpdateProcThreadAttribute documentation requires inheritable handles
and `bInheritHandles=TRUE` for an explicit handle list. The AppContainer capability
attribute remains the native process-creation boundary:
https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute

Window station rights and effective security descriptors remain native OS
controls, independent of the calling language:
https://learn.microsoft.com/en-us/windows/win32/winstation/window-station-security-and-access-rights

Job assignment occurs while suspended; failed assignment cannot be treated as
ownership or permit resume:
https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject

These sources were checked on October 23, 2026. They support the API design; they
do not establish that this implementation passes native host qualification.
