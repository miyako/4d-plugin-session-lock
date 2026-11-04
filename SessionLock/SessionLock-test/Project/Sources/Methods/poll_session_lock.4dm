//%attributes = {"invisible":true}
#DECLARE() -> $batch : Object

/*
Call SessionLock Start once from your existing 4D caller and inspect success/state.
Invoke this method periodically from that same caller's existing timer or loop.
Consume $batch.events in order and inspect $batch.dropped to detect queue overflow.
The native queue holds 128 transitions and drops the oldest on overflow.
Use $batch.state as the current cached snapshot, including availability and error.
Keep generation/sequence/drop counters as Text; Number() can lose uint64 precision.
Call SessionLock Stop when done, then drain once more if final events are needed.
This method creates no process, installs no callback and never locks the screen.
*/
$batch:=SessionLock Events
